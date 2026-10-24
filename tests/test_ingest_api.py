from sqlalchemy import select

from youtube_bot_dashboard.ingest_api import LiveIngestBody, IncomingMessageBody, ingest_payload
from youtube_bot_dashboard.live_queue import (
    LiveMessageTask,
    LivePipeline,
    LiveSessionRecord,
    SessionShieldRecord,
    TaskState,
    shield_user_for_session,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


def make_body(session_id="stream-1", msg_id="message-1"):
    return LiveIngestBody(
        session_id=session_id,
        live_chat_id="youtube-chat-1",
        broadcast_id="broadcast-1",
        messages=[
            IncomingMessageBody(
                message_id=msg_id,
                user_id="channel-1",
                username="Viewer",
                text="A normal live chat question",
            )
        ],
    )


def test_json_ingest_creates_session_and_both_pipeline_tasks():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        result = ingest_payload(factory, make_body())
        assert result["created"] == 1
        assert result["duplicates"] == 0
        with factory() as session:
            assert session.get(LiveSessionRecord, "stream-1") is not None
            tasks = list(session.scalars(select(LiveMessageTask).where(LiveMessageTask.message_id == "message-1")))
            assert {task.pipeline for task in tasks} == {
                LivePipeline.MODERATION.value,
                LivePipeline.REPLY_FINDER.value,
            }
    finally:
        engine.dispose()


def test_repeated_json_ingest_is_idempotent():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        first = ingest_payload(factory, make_body())
        second = ingest_payload(factory, make_body())
        assert first["created"] == 1
        assert second["created"] == 0
        assert second["duplicates"] == 1
        with factory() as session:
            tasks = list(session.scalars(select(LiveMessageTask).where(LiveMessageTask.message_id == "message-1")))
            assert len(tasks) == 2
    finally:
        engine.dispose()


def test_ingest_reports_messages_filtered_by_session_shield():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        ingest_payload(factory, make_body())
        with factory() as session:
            shield_user_for_session(
                session,
                session_id="stream-1",
                user_channel_id="channel-1",
                reason="Flagged in this session",
                source="MANUAL",
            )
            session.commit()
        result = ingest_payload(factory, LiveIngestBody(
            session_id="stream-1",
            live_chat_id="youtube-chat-1",
            broadcast_id="broadcast-1",
            messages=[IncomingMessageBody(
                message_id="message-2",
                user_id="channel-1",
                username="Viewer",
                text="Another message from shielded viewer",
            )],
        ))
        assert result["reply_excluded_by_session_shield"] == 1
        with factory() as session:
            tasks = list(session.scalars(select(LiveMessageTask).where(LiveMessageTask.message_id == "message-2")))
            by_pipeline = {task.pipeline: task for task in tasks}
            assert by_pipeline["MODERATION"].state == TaskState.PENDING.value
            assert by_pipeline["REPLY_FINDER"].state == TaskState.EXCLUDED_SHIELDED.value
    finally:
        engine.dispose()
