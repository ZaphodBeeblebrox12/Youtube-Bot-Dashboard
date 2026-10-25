"""Tests for live-message body retention; processing history and memories remain intact."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from youtube_bot_dashboard.community_memory import CommunityMemory, CommunityViewerProfile
from youtube_bot_dashboard.live_queue import (
    AppSettingRecord,
    BatchState,
    LiveAIRequestBatch,
    LiveMessageRecord,
    LiveMessageTask,
    LivePipeline,
    TaskState,
    register_live_session,
    ingest_live_message,
    IncomingLiveMessage,
    LIVE_MESSAGE_TEXT_EXPIRED,
)
from youtube_bot_dashboard.message_retention import (
    cleanup_expired_live_message_content,
    get_live_message_retention_days,
    save_live_message_retention_days,
    run_live_message_retention_cleanup,
    validate_live_message_retention_days,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


@pytest.fixture
def db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with factory() as session:
        register_live_session(session, session_id="retention-session", live_chat_id="retention-chat")
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _ingest(session, *, message_id: str, user_id: str, text: str, received_at: datetime):
    return ingest_live_message(
        session,
        IncomingLiveMessage(
            message_id=message_id,
            session_id="retention-session",
            user_channel_id=user_id,
            username=user_id,
            text=text,
        ),
        received_at=received_at,
    )


def test_message_retention_defaults_to_indefinite_and_validates_window(db):
    _, factory = db
    with factory() as session:
        assert get_live_message_retention_days(session) == 0
        assert validate_live_message_retention_days(0) == 0
        assert validate_live_message_retention_days(365) == 365
        with pytest.raises(ValueError):
            validate_live_message_retention_days(-1)
        with pytest.raises(ValueError):
            validate_live_message_retention_days(366)
        with pytest.raises(ValueError):
            validate_live_message_retention_days(True)


def test_zero_days_keeps_message_text(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    with factory() as session:
        _ingest(
            session,
            message_id="keep-me",
            user_id="viewer-keep",
            text="Keep this raw message",
            received_at=now - timedelta(days=500),
        )
        result = cleanup_expired_live_message_content(session, retention_days=0, now=now)
        session.commit()
        assert result["expired_messages"] == 0
        assert session.get(LiveMessageRecord, "keep-me").text == "Keep this raw message"


def test_retention_redacts_old_bodies_scrubs_batches_and_preserves_memories_and_recent_messages(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    with factory() as session:
        _ingest(
            session,
            message_id="old-message",
            user_id="viewer-old",
            text="This old raw message must disappear from stored payloads",
            received_at=now - timedelta(days=10),
        )
        _ingest(
            session,
            message_id="recent-message",
            user_id="viewer-recent",
            text="This recent message must remain available",
            received_at=now - timedelta(days=2),
        )
        old_reply_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "old-message",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))
        recent_reply_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "recent-message",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))
        old_reply_task.state = TaskState.BATCHED.value
        old_reply_task.batch_id = "batch-retention"
        recent_reply_task.state = TaskState.BATCHED.value
        recent_reply_task.batch_id = "batch-retention"
        session.add(LiveAIRequestBatch(
            batch_id="batch-retention",
            session_id="retention-session",
            pipeline=LivePipeline.REPLY_FINDER.value,
            provider="GEMINI",
            status=BatchState.PREPARED.value,
            payload_json=json.dumps({
                "session_id": "retention-session",
                "messages": [
                    {"message_id": "old-message", "user_id": "viewer-old", "text": "This old raw message must disappear from stored payloads"},
                    {"message_id": "recent-message", "user_id": "viewer-recent", "text": "This recent message must remain available"},
                ],
            }),
        ))
        session.add(CommunityMemory(
            user_channel_id="viewer-old",
            session_id="retention-session",
            source_message_id="old-message",
            memory_type="CHALLENGE",
            summary="Working on patience before entering trades",
            suggested_follow_up="Has waiting for confirmation helped?",
            confidence=0.92,
            status="SAVED",
        ))
        session.commit()

        result = cleanup_expired_live_message_content(session, retention_days=7, now=now)
        session.commit()

        old_message = session.get(LiveMessageRecord, "old-message")
        recent_message = session.get(LiveMessageRecord, "recent-message")
        stored_batch = session.get(LiveAIRequestBatch, "batch-retention")
        payload = json.loads(stored_batch.payload_json)
        old_reply_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "old-message",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))
        recent_reply_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "recent-message",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))

        assert result["expired_messages"] == 1
        assert result["scrubbed_batch_payloads"] == 1
        assert old_message is not None and old_message.text == LIVE_MESSAGE_TEXT_EXPIRED
        assert recent_message.text == "This recent message must remain available"
        assert payload["messages"] == [{
            "message_id": "recent-message",
            "user_id": "viewer-recent",
            "text": "This recent message must remain available",
        }]
        assert old_reply_task.state == TaskState.EXCLUDED_RETENTION.value
        assert old_reply_task.batch_id is None
        assert recent_reply_task.state == TaskState.BATCHED.value
        assert recent_reply_task.batch_id == "batch-retention"
        assert session.get(CommunityViewerProfile, "viewer-old") is not None
        saved_memory = session.scalar(select(CommunityMemory).where(CommunityMemory.source_message_id == "old-message"))
        assert saved_memory is not None and saved_memory.summary == "Working on patience before entering trades"


def test_saved_retention_setting_is_applied_by_worker_cleanup(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    with factory() as session:
        _ingest(
            session,
            message_id="policy-old",
            user_id="viewer-policy",
            text="Clear this according to persisted retention",
            received_at=now - timedelta(days=4),
        )
        save_live_message_retention_days(session, 3)
        session.commit()

    result = run_live_message_retention_cleanup(factory, now=now)
    assert result["retention_days"] == 3
    assert result["expired_messages"] == 1
    with factory() as session:
        assert session.get(LiveMessageRecord, "policy-old").text == LIVE_MESSAGE_TEXT_EXPIRED
        assert session.get(AppSettingRecord, "live_message_retention_days").value == "3"


def test_replaying_an_expired_message_does_not_restore_its_body(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    with factory() as session:
        _ingest(session, message_id="replayed", user_id="viewer-replay", text="Original body", received_at=now - timedelta(days=8))
        cleanup_expired_live_message_content(session, retention_days=7, now=now)
        session.commit()
        _ingest(session, message_id="replayed", user_id="viewer-replay", text="Changed replay body", received_at=now)
        session.commit()
        assert session.get(LiveMessageRecord, "replayed").text == LIVE_MESSAGE_TEXT_EXPIRED
