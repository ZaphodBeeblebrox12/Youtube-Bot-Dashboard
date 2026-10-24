import json
from datetime import datetime, timedelta, timezone

import pytest

from youtube_bot_dashboard.live_queue import (
    LivePipeline,
    LiveQueueConfig,
    ProviderName,
    ProviderStrategy,
    register_live_session,
    ingest_live_message,
    prepare_due_batch,
)
from youtube_bot_dashboard.live_worker import (
    _sentence_count,
    process_prepared_batch,
    validate_moderation_response,
    validate_reply_finder_response,
)
from youtube_bot_dashboard.providers.base import ProviderResponse, ProviderError
from youtube_bot_dashboard.providers.registry import ProviderRegistry
from youtube_bot_dashboard.live_queue import IncomingLiveMessage
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


def simple_payload():
    return {
        "session_id": "session-1",
        "pipeline": "REPLY_FINDER",
        "messages": [
            {"message_id": "m-1", "user_id": "source-user-1", "username": "Viewer 1", "text": "Why can stocks fall when inflation declines?"},
            {"message_id": "m-2", "user_id": "source-user-2", "username": "Viewer 2", "text": "Hi"},
        ],
    }


def test_moderation_response_validates_exact_message_ids():
    payload = simple_payload()
    response = {
        "results": [
            {
                "message_id": "m-1", "category": "NO_VIOLATION", "severity_score": 0,
                "confidence": 0.98, "evidence_message_ids": ["m-1"],
                "explanation": "Ordinary question.", "suggest_shield": False,
            }
        ]
    }
    mapped = validate_moderation_response(response, payload)
    assert list(mapped) == ["m-1"]


def test_moderation_response_rejects_unknown_ids_and_invalid_scores():
    payload = simple_payload()
    base = {
        "category": "NO_VIOLATION", "severity_score": 0, "confidence": 0.8,
        "evidence_message_ids": [], "explanation": "No violation.", "suggest_shield": False,
    }
    with pytest.raises(ProviderError, match="unknown message_id"):
        validate_moderation_response({"results": [{"message_id": "invented", **base}]}, payload)
    with pytest.raises(ProviderError, match="severity_score"):
        validate_moderation_response({"results": [{"message_id": "m-1", **{**base, "severity_score": 999}}]}, payload)


def test_reply_finder_uses_source_identity_and_checks_two_sentences():
    payload = simple_payload()
    response = {
        "selected_messages": [
            {
                "message_id": "m-1",
                "user_id": "hallucinated-user",
                "username": "Fake Name",
                "question_type": "MACROECONOMICS",
                "ease_to_answer": "MODERATE",
                "trading_stance": "NOT_APPLICABLE",
                "priority": "HIGH",
                "selection_reason": "Useful general question.",
                "suggested_answer_angle": None,
                "suggested_live_response": "Falling inflation does not guarantee stocks will rise because markets also price earnings and rates. If growth weakens or rates stay restrictive, equities can still fall.",
                "needs_research": False,
                "confidence": 0.9,
                "cluster_message_ids": ["m-1"],
            }
        ]
    }
    mapped = validate_reply_finder_response(response, payload)
    assert mapped["m-1"]["user_id"] == "source-user-1"
    assert mapped["m-1"]["username"] == "Viewer 1"
    assert mapped["m-1"]["answer_validation"] == "PASS"


def test_reply_finder_invalid_selected_message_is_rejected():
    with pytest.raises(ProviderError, match="unknown message_id"):
        validate_reply_finder_response(
            {"selected_messages": [{
                "message_id": "not-in-payload",
                "question_type": "COMMUNITY",
                "ease_to_answer": "EASY",
                "trading_stance": "NOT_APPLICABLE",
                "priority": "NORMAL",
                "confidence": 0.8,
                "cluster_message_ids": ["not-in-payload"],
            }]},
            simple_payload(),
        )


def test_sentence_counter_detects_two_simple_sentences():
    assert _sentence_count("First sentence. Second sentence.") == 2
    assert _sentence_count("Only one sentence.") == 1


class FakeGemini:
    provider_name = "GEMINI"
    model_id = "gemini-test-model"

    def generate_json(self, *, system_prompt, payload):
        return ProviderResponse(
            text=json.dumps({
                "results": [
                    {
                        "message_id": message["message_id"],
                        "category": "NO_VIOLATION",
                        "severity_score": 0,
                        "confidence": 0.99,
                        "evidence_message_ids": [message["message_id"]],
                        "explanation": "Normal message.",
                        "suggest_shield": False,
                    }
                    for message in payload["messages"]
                ]
            }),
            model_id=self.model_id,
            input_tokens=300,
            output_tokens=80,
            cached_input_tokens=100,
        )


def test_worker_dispatches_provider_and_persists_full_response_lifecycle():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        config = LiveQueueConfig(
            batch_delay_seconds=2,
            provider_strategy=ProviderStrategy.GEMINI_ONLY,
            enabled_providers=(ProviderName.GEMINI,),
        )
        with factory() as session:
            register_live_session(session, session_id="session-1", live_chat_id="chat-1")
            ingest_live_message(
                session,
                IncomingLiveMessage(message_id="m-1", session_id="session-1", text="Hello", user_channel_id="u-1"),
                received_at=now,
            )
            batch = prepare_due_batch(
                session, session_id="session-1", pipeline=LivePipeline.MODERATION,
                config=config, now=now + timedelta(seconds=3)
            )
            session.commit()
        registry = ProviderRegistry({ProviderName.GEMINI: FakeGemini()})
        outcome = process_prepared_batch(factory, batch.batch_id, registry, config)
        assert outcome["status"] == "COMPLETED"
        with factory() as session:
            from youtube_bot_dashboard.live_queue import LiveAIRequestBatch, LiveMessageTask
            from sqlalchemy import select
            saved_batch = session.get(LiveAIRequestBatch, batch.batch_id)
            task = session.scalar(select(LiveMessageTask).where(LiveMessageTask.message_id == "m-1", LiveMessageTask.pipeline == "MODERATION"))
            assert saved_batch.status == "COMPLETED"
            assert saved_batch.input_tokens == 300
            assert saved_batch.cached_input_tokens == 100
            assert task.state == "COMPLETED"
            assert task.provider == "GEMINI"
    finally:
        engine.dispose()


def test_worker_keeps_batch_pending_if_provider_is_not_available():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        config = LiveQueueConfig(
            batch_delay_seconds=2,
            provider_strategy=ProviderStrategy.GEMINI_ONLY,
            enabled_providers=(ProviderName.GEMINI,),
        )
        with factory() as session:
            register_live_session(session, session_id="session-1")
            ingest_live_message(
                session,
                IncomingLiveMessage(message_id="m-1", session_id="session-1", text="Hello", user_channel_id="u-1"),
                received_at=now,
            )
            batch = prepare_due_batch(
                session, session_id="session-1", pipeline=LivePipeline.MODERATION,
                config=config, now=now + timedelta(seconds=3)
            )
            session.commit()
        outcome = process_prepared_batch(factory, batch.batch_id, ProviderRegistry({}), config)
        assert outcome["status"] == "WAITING_FOR_PROVIDER"
        with factory() as session:
            from youtube_bot_dashboard.live_queue import LiveAIRequestBatch, LiveMessageTask
            from sqlalchemy import select
            assert session.get(LiveAIRequestBatch, batch.batch_id).status == "CANCELLED"
            task = session.scalar(select(LiveMessageTask).where(LiveMessageTask.message_id == "m-1", LiveMessageTask.pipeline == "MODERATION"))
            assert task.state == "PENDING"
            assert task.batch_id is None
    finally:
        engine.dispose()



class FakeDualPurposeProvider:
    provider_name = "GEMINI"
    model_id = "gemini-test-model"

    def __init__(self):
        self.reply_payloads = []

    def generate_json(self, *, system_prompt, payload):
        if "You are the Reply Finder" in system_prompt:
            self.reply_payloads.append(payload)
            selected = []
            for message in payload["messages"]:
                selected.append({
                    "message_id": message["message_id"],
                    "question_type": "COMMUNITY",
                    "ease_to_answer": "EASY",
                    "trading_stance": "NOT_APPLICABLE",
                    "priority": "NORMAL",
                    "selection_reason": "A relevant, constructive message.",
                    "suggested_answer_angle": "Answer briefly.",
                    "suggested_live_response": None,
                    "needs_research": False,
                    "confidence": 0.94,
                    "cluster_message_ids": [message["message_id"]],
                })
            return ProviderResponse(
                text=json.dumps({"selected_messages": selected}),
                model_id=self.model_id,
                input_tokens=180,
                output_tokens=50,
            )

        results = []
        for message in payload["messages"]:
            is_antagonist = message.get("user_id") == "channel-evil"
            results.append({
                "message_id": message["message_id"],
                "category": "PERSONAL_ATTACK" if is_antagonist else "NO_VIOLATION",
                "severity_score": 92 if is_antagonist else 0,
                "confidence": 0.99 if is_antagonist else 0.98,
                "evidence_message_ids": [message["message_id"]],
                "explanation": "Direct personal abuse." if is_antagonist else "Ordinary chat.",
                "suggest_shield": is_antagonist,
            })
        return ProviderResponse(
            text=json.dumps({"results": results}),
            model_id=self.model_id,
            input_tokens=250,
            output_tokens=60,
        )


def test_moderation_shields_user_before_prepared_reply_batch_is_sent():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    provider = FakeDualPurposeProvider()
    config = LiveQueueConfig(
        batch_delay_seconds=2,
        provider_strategy=ProviderStrategy.GEMINI_ONLY,
        enabled_providers=(ProviderName.GEMINI,),
        session_shield_confidence_threshold=0.90,
    )
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    try:
        with factory() as session:
            register_live_session(session, session_id="session-1", live_chat_id="chat-1")
            ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id="m-evil", session_id="session-1",
                    user_channel_id="channel-evil", username="Bad Actor",
                    text="You are an idiot.",
                ),
                received_at=now,
            )
            ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id="m-good", session_id="session-1",
                    user_channel_id="channel-good", username="Good Viewer",
                    text="Can you explain support and resistance?",
                ),
                received_at=now,
            )
            mod_batch = prepare_due_batch(
                session, session_id="session-1", pipeline=LivePipeline.MODERATION,
                config=config, now=now + timedelta(seconds=3)
            )
            reply_batch = prepare_due_batch(
                session, session_id="session-1", pipeline=LivePipeline.REPLY_FINDER,
                config=config, now=now + timedelta(seconds=3)
            )
            session.commit()

        registry = ProviderRegistry({ProviderName.GEMINI: provider})
        mod_outcome = process_prepared_batch(factory, mod_batch.batch_id, registry, config)
        assert mod_outcome["status"] == "COMPLETED"
        assert mod_outcome["session_shield_users_added"] == ["channel-evil"]

        reply_outcome = process_prepared_batch(factory, reply_batch.batch_id, registry, config)
        assert reply_outcome["status"] == "COMPLETED"
        assert len(provider.reply_payloads) == 1
        sent_ids = [m["message_id"] for m in provider.reply_payloads[0]["messages"]]
        assert sent_ids == ["m-good"]

        with factory() as session:
            from sqlalchemy import select
            from youtube_bot_dashboard.live_queue import SessionShieldRecord, LiveMessageTask
            shield = session.scalar(select(SessionShieldRecord).where(
                SessionShieldRecord.session_id == "session-1",
                SessionShieldRecord.user_channel_id == "channel-evil",
            ))
            assert shield is not None and shield.active
            evil_reply_task = session.scalar(select(LiveMessageTask).where(
                LiveMessageTask.message_id == "m-evil",
                LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
            ))
            evil_moderation_task = session.scalar(select(LiveMessageTask).where(
                LiveMessageTask.message_id == "m-evil",
                LiveMessageTask.pipeline == LivePipeline.MODERATION.value,
            ))
            assert evil_reply_task.state == "EXCLUDED_SHIELDED"
            assert evil_moderation_task.state == "COMPLETED"
    finally:
        engine.dispose()


def test_low_confidence_shield_suggestion_does_not_exclude_user():
    from youtube_bot_dashboard.live_worker import apply_session_shield_suggestions
    from youtube_bot_dashboard.live_queue import SessionShieldRecord
    from sqlalchemy import select

    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        with factory() as session:
            register_live_session(session, session_id="session-1")
            ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id="m-1", session_id="session-1",
                    user_channel_id="channel-1", text="A somewhat rude message",
                ),
            )
            session.commit()
            shielded = apply_session_shield_suggestions(
                session,
                session_id="session-1",
                payload={"messages": [{"message_id": "m-1", "user_id": "channel-1", "username": "Viewer"}]},
                results_by_message_id={
                    "m-1": {
                        "suggest_shield": True,
                        "category": "PERSONAL_ATTACK",
                        "confidence": 0.75,
                        "explanation": "Ambiguous wording.",
                    }
                },
                confidence_threshold=0.90,
            )
            session.commit()
            assert shielded == []
            assert session.scalar(select(SessionShieldRecord.id)) is None
    finally:
        engine.dispose()
