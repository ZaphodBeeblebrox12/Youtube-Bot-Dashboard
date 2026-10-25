"""Retention controls for raw live-chat message content.

This redacts message bodies instead of deleting message rows. Stable IDs, AI
decisions, approved community memories, and moderation history remain available;
persisted request payload copies of expired messages are scrubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select, update

from youtube_bot_dashboard.live_queue import (
    AppSettingRecord,
    BatchState,
    LiveAIRequestBatch,
    LiveMessageRecord,
    LiveMessageTask,
    LIVE_MESSAGE_TEXT_EXPIRED,
    TaskState,
)
from youtube_bot_dashboard.storage import utc_now

LIVE_MESSAGE_RETENTION_SETTING = "live_message_retention_days"
MIN_LIVE_MESSAGE_RETENTION_DAYS = 0
MAX_LIVE_MESSAGE_RETENTION_DAYS = 365
ACTIVE_TASK_STATES_FOR_RETENTION = (
    TaskState.PENDING.value,
    TaskState.BATCHED.value,
    TaskState.SENT.value,
    TaskState.FAILED_RETRYABLE.value,
)


def validate_live_message_retention_days(days: int) -> int:
    """Validate a retention window; zero means keep message text indefinitely."""
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError("retention days must be an integer")
    if not MIN_LIVE_MESSAGE_RETENTION_DAYS <= days <= MAX_LIVE_MESSAGE_RETENTION_DAYS:
        raise ValueError(
            f"retention days must be between {MIN_LIVE_MESSAGE_RETENTION_DAYS} and "
            f"{MAX_LIVE_MESSAGE_RETENTION_DAYS}; use 0 to keep message text indefinitely"
        )
    return days


def get_live_message_retention_days(session) -> int:
    """Return the saved retention window, safely defaulting to indefinite retention."""
    setting = session.get(AppSettingRecord, LIVE_MESSAGE_RETENTION_SETTING)
    if setting is None:
        return 0
    try:
        days = int(setting.value)
    except (TypeError, ValueError):
        return 0
    if not MIN_LIVE_MESSAGE_RETENTION_DAYS <= days <= MAX_LIVE_MESSAGE_RETENTION_DAYS:
        # Bad configuration must never trigger unexpected data destruction.
        return 0
    return days


def save_live_message_retention_days(session, days: int) -> int:
    """Persist the chosen window in app settings."""
    days = validate_live_message_retention_days(days)
    now = utc_now()
    setting = session.get(AppSettingRecord, LIVE_MESSAGE_RETENTION_SETTING)
    if setting is None:
        session.add(AppSettingRecord(
            key=LIVE_MESSAGE_RETENTION_SETTING,
            value=str(days),
            updated_at=now,
        ))
    else:
        setting.value = str(days)
        setting.updated_at = now
    session.flush()
    return days


def cleanup_expired_live_message_content(
    session,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Redact old live-message bodies and their persisted request copies.

    Rows and completed task results are preserved so foreign keys, moderation
    history, and approved memory source links continue to work. Unsent/in-flight
    tasks are excluded from retries and late result persistence.
    """
    days = validate_live_message_retention_days(retention_days)
    result = {
        "retention_days": days,
        "expired_messages": 0,
        "scrubbed_batch_payloads": 0,
        "excluded_tasks": 0,
    }
    if days == 0:
        return result

    timestamp = now or utc_now()
    cutoff = timestamp - timedelta(days=days)
    expired_filter = (
        LiveMessageRecord.first_seen_at < cutoff,
        LiveMessageRecord.text != LIVE_MESSAGE_TEXT_EXPIRED,
    )
    affected_sessions = list(session.scalars(
        select(LiveMessageRecord.session_id)
        .where(*expired_filter)
        .distinct()
    ))
    if not affected_sessions:
        return result

    # Scrub the persisted batch copies before redacting canonical message rows.
    # We inspect message IDs against first_seen_at, preserving newer messages
    # that may share a batch with an expired message.
    batches = list(session.scalars(
        select(LiveAIRequestBatch).where(
            LiveAIRequestBatch.session_id.in_(affected_sessions)
        )
    ))
    for batch in batches:
        try:
            payload = json.loads(batch.payload_json or "{}")
        except (TypeError, ValueError) as exc:
            # Abort transaction rather than falsely claim retention succeeded
            # while a malformed payload might still contain raw chat text.
            raise RuntimeError(
                f"Retention cleanup cannot parse stored payload for batch {batch.batch_id!r}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("messages", []), list):
            raise RuntimeError(
                f"Retention cleanup found an invalid payload shape for batch {batch.batch_id!r}"
            )

        message_items = payload.get("messages", [])
        message_ids = []
        for item in message_items:
            if not isinstance(item, dict) or not isinstance(item.get("message_id"), str):
                raise RuntimeError(
                    f"Retention cleanup found an invalid message item in batch {batch.batch_id!r}"
                )
            message_ids.append(item["message_id"])
        if not message_ids:
            continue
        expired_ids = set(session.scalars(
            select(LiveMessageRecord.message_id).where(
                LiveMessageRecord.message_id.in_(message_ids),
                LiveMessageRecord.first_seen_at < cutoff,
            )
        ))
        if not expired_ids:
            continue

        payload["messages"] = [
            item for item in message_items
            if item["message_id"] not in expired_ids
        ]
        batch.payload_json = json.dumps(payload, ensure_ascii=False)
        result["scrubbed_batch_payloads"] += 1
        if not payload["messages"] and batch.status == BatchState.PREPARED.value:
            batch.status = BatchState.CANCELLED.value
            batch.completed_at = timestamp
            batch.last_error = (
                "Cancelled because all messages in the batch expired under the retention policy."
            )

    # Redact bodies rather than delete rows, keeping stable IDs and FK relations.
    updated = session.execute(
        update(LiveMessageRecord)
        .where(*expired_filter)
        .values(text=LIVE_MESSAGE_TEXT_EXPIRED)
        .execution_options(synchronize_session=False)
    )
    result["expired_messages"] = max(0, updated.rowcount or 0)

    # Pending, batched, sent/in-flight and retryable tasks must not send, retry,
    # or persist late AI output after their source content has expired.
    expired_ids_subquery = select(LiveMessageRecord.message_id).where(
        LiveMessageRecord.first_seen_at < cutoff
    )
    tasks_updated = session.execute(
        update(LiveMessageTask)
        .where(
            LiveMessageTask.message_id.in_(expired_ids_subquery),
            LiveMessageTask.state.in_(ACTIVE_TASK_STATES_FOR_RETENTION),
        )
        .values(
            state=TaskState.EXCLUDED_RETENTION.value,
            batch_id=None,
            next_attempt_at=None,
            last_error="Message content expired under the configured live-message retention policy.",
        )
        .execution_options(synchronize_session=False)
    )
    result["excluded_tasks"] = max(0, tasks_updated.rowcount or 0)
    session.flush()
    return result


def run_live_message_retention_cleanup(session_factory, *, now: datetime | None = None) -> dict[str, int]:
    """Read the persisted policy and execute it in one committed transaction."""
    with session_factory() as session:
        days = get_live_message_retention_days(session)
        result = cleanup_expired_live_message_content(session, retention_days=days, now=now)
        session.commit()
        return result
