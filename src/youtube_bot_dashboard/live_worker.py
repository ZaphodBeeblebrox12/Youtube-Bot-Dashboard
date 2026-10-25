"""Process due live batches with provider routing, JSON validation, and retries.

Run with: python -m youtube_bot_dashboard.live_worker
YouTube ingestion must independently persist incoming messages; this worker only
processes those durable queues. Active Moderation enforcement is never called.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
import re
import time

from sqlalchemy import select
from dotenv import load_dotenv

from youtube_bot_dashboard.community_memory import MEMORY_TYPES, create_memory_suggestion
from youtube_bot_dashboard.persistent_moderation import record_moderation_results
from youtube_bot_dashboard.live_queue import (
    AppSettingRecord,
    BatchState,
    LiveAIRequestBatch,
    LiveMessageTask,
    LivePipeline,
    LiveQueueConfig,
    LiveSessionRecord,
    ProviderName,
    ProviderStrategy,
    TaskState,
    complete_live_batch,
    fail_live_batch,
    get_live_queue_config,
    mark_live_batch_sent,
    prepare_due_batch,
    shield_user_for_session,
)
from youtube_bot_dashboard.prompts.live_moderation import LIVE_MODERATION_SYSTEM_PROMPT
from youtube_bot_dashboard.prompts.reply_finder import REPLY_FINDER_SYSTEM_PROMPT
from youtube_bot_dashboard.providers.base import ProviderError
from youtube_bot_dashboard.providers.registry import ProviderRegistry
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema, utc_now

logger = logging.getLogger("youtube_bot_dashboard.live_worker")

MODERATION_CATEGORIES = {
    "PERSONAL_ATTACK", "HARASSMENT", "SPAM_FLOODING", "HATE_SPEECH", "THREATS",
    "DOXXING_PRIVACY", "SCAM_PHISHING", "UNSOLICITED_PROMOTION", "IMPERSONATION",
    "DISRUPTION", "NO_VIOLATION", "UNCERTAIN",
}
QUESTION_TYPES = {
    "CREATOR_SETUP", "GENERAL_FINANCE", "MACROECONOMICS", "CURRENT_MARKET_OR_NEWS",
    "COMMUNITY", "FEEDBACK", "HELP_REQUEST", "UNCLEAR",
}
EASE_VALUES = {"EASY", "MODERATE", "DIFFICULT"}
STANCE_VALUES = {"BULLISH", "BEARISH", "NEUTRAL", "MIXED", "NOT_APPLICABLE"}
PRIORITY_VALUES = {"HIGH", "NORMAL", "LOW"}


def parse_json_object(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"AI returned invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("AI JSON output must be an object.")
    return parsed


def validate_moderation_response(parsed: dict, payload: dict) -> dict[str, dict]:
    """Validate per-message moderation results against IDs from the submitted batch."""
    expected = {item["message_id"] for item in payload.get("messages", [])}
    results = parsed.get("results")
    if not isinstance(results, list):
        raise ProviderError("Moderation response is missing its results list.")
    mapped: dict[str, dict] = {}
    for item in results:
        if not isinstance(item, dict):
            raise ProviderError("Moderation result entry must be an object.")
        message_id = item.get("message_id")
        if not isinstance(message_id, str) or message_id not in expected:
            raise ProviderError("Moderation response included an unknown message_id.")
        if message_id in mapped:
            raise ProviderError(f"Moderation response duplicated message_id {message_id!r}.")
        if item.get("category") not in MODERATION_CATEGORIES:
            raise ProviderError(f"Invalid moderation category for {message_id!r}.")
        severity = item.get("severity_score")
        confidence = item.get("confidence")
        if isinstance(severity, bool) or not isinstance(severity, int) or not 0 <= severity <= 100:
            raise ProviderError(f"Invalid severity_score for {message_id!r}.")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ProviderError(f"Invalid confidence for {message_id!r}.")
        evidence = item.get("evidence_message_ids", [])
        if not isinstance(evidence, list) or any(evidence_id not in expected for evidence_id in evidence):
            raise ProviderError(f"Invalid evidence_message_ids for {message_id!r}.")
        if not isinstance(item.get("explanation"), str):
            raise ProviderError(f"Invalid explanation for {message_id!r}.")
        if not isinstance(item.get("suggest_shield"), bool):
            raise ProviderError(f"Invalid suggest_shield flag for {message_id!r}.")
        mapped[message_id] = item
    # Missing inputs are deliberately left out of the map. Persistence marks
    # those specific message tasks retryable instead of losing the batch.
    return mapped


def _sentence_count(text: str) -> int:
    """Lightweight check for an exactly-two-sentence live answer.

    This is a quality gate, not a linguistic parser; abbreviations can lead to a
    review flag rather than a false claim of certainty.
    """
    trimmed = text.strip()
    if not trimmed:
        return 0
    parts = re.findall(r"[^.!?]+[.!?]+(?:[\"'”’)]*)", trimmed)
    tail = re.sub(r"[^.!?]+[.!?]+(?:[\"'”’)]*)", "", trimmed).strip()
    return len(parts) + (1 if tail else 0)


def _normalize_community_memory(candidate, source: dict) -> dict:
    """Fail closed: malformed or identity-free memory proposals become no-ops."""
    empty = {
        "save_candidate": False,
        "memory_type": None,
        "summary": "",
        "suggested_follow_up": "",
        "confidence": 0.0,
    }
    if not isinstance(candidate, dict) or not source.get("user_id"):
        return empty
    if candidate.get("save_candidate") is not True:
        return empty
    memory_type = candidate.get("memory_type")
    summary = candidate.get("summary")
    follow_up = candidate.get("suggested_follow_up")
    confidence = candidate.get("confidence")
    if (
        memory_type not in MEMORY_TYPES
        or not isinstance(summary, str)
        or not isinstance(follow_up, str)
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.80 <= confidence <= 1.0
    ):
        return empty
    summary = " ".join(summary.split()).strip()
    follow_up = " ".join(follow_up.split()).strip()
    if not summary or not follow_up:
        return empty
    return {
        "save_candidate": True,
        "memory_type": memory_type,
        "summary": summary[:240],
        "suggested_follow_up": follow_up[:240],
        "confidence": float(confidence),
    }


def validate_reply_finder_response(parsed: dict, payload: dict) -> dict[str, dict]:
    """Validate only selections; unselected inputs are completed by the queue."""
    input_messages = {
        item["message_id"]: item
        for item in payload.get("messages", [])
        if isinstance(item, dict) and isinstance(item.get("message_id"), str)
    }
    selected = parsed.get("selected_messages")
    if not isinstance(selected, list):
        raise ProviderError("Reply Finder response is missing selected_messages.")
    mapped: dict[str, dict] = {}
    for item in selected:
        if not isinstance(item, dict):
            raise ProviderError("Reply Finder entry must be an object.")
        message_id = item.get("message_id")
        if not isinstance(message_id, str) or message_id not in input_messages:
            raise ProviderError("Reply Finder selected an unknown message_id.")
        if message_id in mapped:
            raise ProviderError(f"Reply Finder duplicated message_id {message_id!r}.")
        if item.get("question_type") not in QUESTION_TYPES:
            raise ProviderError(f"Invalid question_type for {message_id!r}.")
        if item.get("ease_to_answer") not in EASE_VALUES:
            raise ProviderError(f"Invalid ease_to_answer for {message_id!r}.")
        if item.get("trading_stance") not in STANCE_VALUES:
            raise ProviderError(f"Invalid trading_stance for {message_id!r}.")
        if item.get("priority") not in PRIORITY_VALUES:
            raise ProviderError(f"Invalid priority for {message_id!r}.")
        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ProviderError(f"Invalid confidence for {message_id!r}.")
        cluster_ids = item.get("cluster_message_ids", [message_id])
        if not isinstance(cluster_ids, list) or any(
            not isinstance(cluster_id, str) or cluster_id not in input_messages
            for cluster_id in cluster_ids
        ):
            raise ProviderError(f"Invalid cluster_message_ids for {message_id!r}.")
        if message_id not in cluster_ids:
            cluster_ids = [message_id, *cluster_ids]
        answer = item.get("suggested_live_response")
        if item["ease_to_answer"] in {"MODERATE", "DIFFICULT"}:
            if not isinstance(answer, str):
                item = {**item, "answer_validation": "REVIEW_REQUIRED"}
            else:
                item = {
                    **item,
                    "answer_validation": "PASS" if _sentence_count(answer) == 2 else "REVIEW_REQUIRED",
                }
        else:
            item = {**item, "answer_validation": "NOT_REQUIRED"}
        # Preserve exact source identity, not an AI-inferred identity.
        source = input_messages[message_id]
        item = {
            **item,
            "message_id": message_id,
            "user_id": source.get("user_id"),
            "username": source.get("username"),
            "selected": True,
            "cluster_message_ids": cluster_ids,
        }
        item["community_memory"] = _normalize_community_memory(
            item.get("community_memory"), source
        )
        mapped[message_id] = item
    return mapped


def apply_session_shield_suggestions(
    session,
    *,
    session_id: str,
    payload: dict,
    results_by_message_id: dict[str, dict],
    confidence_threshold: float,
) -> list[str]:
    """Apply only the internal Reply Finder filter; this makes no YouTube API call."""
    source_messages = {
        item["message_id"]: item
        for item in payload.get("messages", [])
        if isinstance(item, dict) and isinstance(item.get("message_id"), str)
    }
    shieldable_categories = MODERATION_CATEGORIES - {"NO_VIOLATION", "UNCERTAIN"}
    shielded_users: list[str] = []
    for message_id, result in results_by_message_id.items():
        source = source_messages.get(message_id)
        if not source:
            continue
        user_id = source.get("user_id")
        confidence = result.get("confidence")
        category = result.get("category")
        if (
            not user_id
            or not result.get("suggest_shield")
            or category not in shieldable_categories
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or confidence < confidence_threshold
        ):
            continue
        reason = (
            f"Session Shield suggestion: {category}; "
            f"message {message_id}; {str(result.get('explanation', 'High-confidence moderation classification'))[:600]}"
        )
        shield_user_for_session(
            session,
            session_id=session_id,
            user_channel_id=user_id,
            display_name=source.get("username"),
            reason=reason,
            source="AI",
            confidence=float(confidence),
        )
        shielded_users.append(user_id)
    return sorted(set(shielded_users))


def _claim_request_slot(session_factory, minimum_interval_seconds: float) -> None:
    """Persist the global AI request start time and enforce a minimum start gap.

    The initial worker is a single consumer. Multi-worker deployments require
    an atomic database lease/lock before enabling more than one worker.
    """
    while True:
        wait_seconds = 0.0
        with session_factory() as session:
            now = utc_now()
            row = session.get(AppSettingRecord, "live_last_ai_request_started_at", with_for_update=True)
            last_started = None
            if row is not None:
                try:
                    last_started = datetime.fromisoformat(row.value)
                    if last_started.tzinfo is None:
                        last_started = last_started.replace(tzinfo=timezone.utc)
                    else:
                        last_started = last_started.astimezone(timezone.utc)
                except (ValueError, TypeError):
                    last_started = None

            if last_started is not None:
                elapsed = (now - last_started).total_seconds()
                wait_seconds = minimum_interval_seconds - elapsed

            if wait_seconds <= 0:
                if row is None:
                    row = AppSettingRecord(
                        key="live_last_ai_request_started_at",
                        value=now.isoformat(),
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    row.value = now.isoformat()
                    row.updated_at = now
                session.commit()
                return

        # Do not hold a database connection while waiting for the next request slot.
        time.sleep(wait_seconds)


def _effective_config(config: LiveQueueConfig, registry: ProviderRegistry) -> LiveQueueConfig | None:
    available = registry.available()
    if config.provider_strategy is ProviderStrategy.ROUND_ROBIN:
        enabled = tuple(p for p in config.enabled_providers if p in available)
        if not enabled:
            return None
        return replace(config, enabled_providers=enabled)
    required = ProviderName.GEMINI if config.provider_strategy is ProviderStrategy.GEMINI_ONLY else ProviderName.QWEN
    return config if required in available and required in config.enabled_providers else None


def _release_unavailable_prepared_batch(session_factory, batch_id: str) -> None:
    with session_factory() as session:
        batch = session.get(LiveAIRequestBatch, batch_id)
        if batch is None or batch.status != BatchState.PREPARED.value:
            return
        batch.status = BatchState.CANCELLED.value
        batch.last_error = "Provider is no longer configured; batch returned to queue."
        tasks = list(session.scalars(select(LiveMessageTask).where(
            LiveMessageTask.batch_id == batch_id,
            LiveMessageTask.state == TaskState.BATCHED.value,
        )))
        for task in tasks:
            task.state = TaskState.PENDING.value
            task.batch_id = None
            task.provider = None
        session.commit()


def process_prepared_batch(session_factory, batch_id: str, registry: ProviderRegistry, config: LiveQueueConfig) -> dict:
    """Mark sent, invoke one provider, validate, and persist outcomes."""
    with session_factory() as session:
        batch = session.get(LiveAIRequestBatch, batch_id)
        if batch is None or batch.status != BatchState.PREPARED.value:
            return {"batch_id": batch_id, "status": "SKIPPED"}
        provider_name = ProviderName(batch.provider)
        pipeline = LivePipeline(batch.pipeline)
        session_id = batch.session_id

    try:
        adapter = registry.get(provider_name)
    except ProviderError:
        _release_unavailable_prepared_batch(session_factory, batch_id)
        return {"batch_id": batch_id, "status": "WAITING_FOR_PROVIDER"}

    system_prompt = (
        LIVE_MODERATION_SYSTEM_PROMPT
        if pipeline is LivePipeline.MODERATION
        else REPLY_FINDER_SYSTEM_PROMPT
    )
    try:
        _claim_request_slot(session_factory, config.minimum_request_interval_seconds)
        with session_factory() as session:
            stored = session.get(LiveAIRequestBatch, batch_id)
            if stored is None or stored.status != BatchState.PREPARED.value:
                return {"batch_id": batch_id, "status": "SKIPPED"}
            payload = mark_live_batch_sent(
                session,
                batch_id,
                model_id=adapter.model_id,
            )
            session.commit()

        # Never send the stale snapshot returned by prepare_due_batch. The
        # persisted payload above is the last chance for Session Shield filtering.
        provider_result = adapter.generate_json(system_prompt=system_prompt, payload=payload)
        parsed = parse_json_object(provider_result.text)
        results_by_id = (
            validate_moderation_response(parsed, payload)
            if pipeline is LivePipeline.MODERATION
            else validate_reply_finder_response(parsed, payload)
        )
        with session_factory() as session:
            completed, missing = complete_live_batch(
                session,
                batch_id,
                result_by_message_id=results_by_id,
                raw_response=parsed,
                input_tokens=provider_result.input_tokens,
                output_tokens=provider_result.output_tokens,
                cached_input_tokens=provider_result.cached_input_tokens,
            )
            incident_ids = []
            if pipeline is LivePipeline.MODERATION:
                incident_ids = record_moderation_results(
                    session, batch_id=batch_id, session_id=session_id, payload=payload,
                    results_by_message_id=results_by_id, provider=provider_name.value,
                    model_id=adapter.model_id,
                )
            memory_suggestions = []
            if pipeline is LivePipeline.REPLY_FINDER:
                source_by_id = {
                    message["message_id"]: message
                    for message in payload.get("messages", [])
                    if isinstance(message, dict) and isinstance(message.get("message_id"), str)
                }
                for message_id, result in results_by_id.items():
                    proposal = result.get("community_memory", {})
                    source = source_by_id.get(message_id, {})
                    if not proposal.get("save_candidate"):
                        continue
                    saved_candidate = create_memory_suggestion(
                        session,
                        user_channel_id=source.get("user_id"),
                        session_id=session_id,
                        source_message_id=message_id,
                        memory_type=proposal.get("memory_type"),
                        summary=proposal.get("summary", ""),
                        suggested_follow_up=proposal.get("suggested_follow_up", ""),
                        confidence=proposal.get("confidence", 0.0),
                    )
                    if saved_candidate is not None:
                        memory_suggestions.append(saved_candidate.id)
            shielded_users = []
            if pipeline is LivePipeline.MODERATION:
                shielded_users = apply_session_shield_suggestions(
                    session,
                    session_id=session_id,
                    payload=payload,
                    results_by_message_id=results_by_id,
                    confidence_threshold=config.session_shield_confidence_threshold,
                )
            session.commit()
        return {
            "batch_id": batch_id,
            "provider": provider_name.value,
            "pipeline": pipeline.value,
            "status": "PARTIAL" if missing else "COMPLETED",
            "completed_messages": completed,
            "retryable_messages": missing,
            "session_shield_users_added": shielded_users if pipeline is LivePipeline.MODERATION else [],
            "moderation_incident_ids": incident_ids if pipeline is LivePipeline.MODERATION else [],
            "community_memory_suggestion_ids": memory_suggestions if pipeline is LivePipeline.REPLY_FINDER else [],
            "input_tokens": provider_result.input_tokens,
            "output_tokens": provider_result.output_tokens,
            "cached_input_tokens": provider_result.cached_input_tokens,
        }
    except Exception as exc:
        logger.exception("AI batch %s failed", batch_id)
        with session_factory() as session:
            current = session.get(LiveAIRequestBatch, batch_id)
            if current and current.status in {BatchState.PREPARED.value, BatchState.SENT.value}:
                config_for_retry = config
                retryable, final = fail_live_batch(
                    session,
                    batch_id,
                    error=str(exc),
                    config=config_for_retry,
                )
                session.commit()
                return {
                    "batch_id": batch_id,
                    "provider": provider_name.value,
                    "pipeline": pipeline.value,
                    "status": "FAILED",
                    "retryable_messages": retryable,
                    "final_failures": final,
                    "error": str(exc),
                }
        return {"batch_id": batch_id, "status": "FAILED", "error": str(exc)}


def run_worker_once(session_factory, registry: ProviderRegistry, *, now: datetime | None = None) -> list[dict]:
    """Prepare due batches for all active sessions, then process them sequentially."""
    timestamp = now or utc_now()
    batch_ids: list[str] = []
    with session_factory() as session:
        active_sessions = list(session.scalars(
            select(LiveSessionRecord).where(LiveSessionRecord.status == "ACTIVE")
        ))
        for live_session in active_sessions:
            stored_config = get_live_queue_config(session)
            config = _effective_config(stored_config, registry)
            if config is None:
                continue
            for pipeline in (LivePipeline.MODERATION, LivePipeline.REPLY_FINDER):
                batch = prepare_due_batch(
                    session,
                    session_id=live_session.session_id,
                    pipeline=pipeline,
                    config=config,
                    now=timestamp,
                )
                if batch:
                    batch_ids.append(batch.batch_id)
        session.commit()

    outcomes = []
    for batch_id in batch_ids:
        # Read the stored config again for bounded retries; provider selection
        # for this batch was already persisted and is not recalculated on retry.
        with session_factory() as session:
            config = get_live_queue_config(session)
        outcomes.append(process_prepared_batch(session_factory, batch_id, registry, config))
    return outcomes


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    database_url = os.getenv("DATABASE_URL", "sqlite:///./youtube_bot_dashboard.db")
    poll_interval = max(0.1, float(os.getenv("LIVE_WORKER_POLL_SECONDS", "0.25")))
    engine, session_factory = create_engine_and_sessions(database_url)
    create_schema(engine)
    registry = ProviderRegistry.from_environment()
    if not registry.available():
        logger.warning("No AI providers configured. Set Gemini or Qwen environment variables before starting the worker.")
    logger.info("Live worker started; provider(s) configured: %s", [p.value for p in registry.available()])
    try:
        while True:
            outcomes = run_worker_once(session_factory, registry)
            for outcome in outcomes:
                logger.info("Batch outcome: %s", outcome)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Live worker stopping.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
