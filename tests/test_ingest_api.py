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
                avatar_url="https://yt3.ggpht.com/example-avatar",
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


def test_json_ingest_persists_avatar_on_message_and_viewer_profile():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        result = ingest_payload(factory, make_body())
        assert result["created"] == 1
        with factory() as session:
            from youtube_bot_dashboard.community_memory import CommunityViewerProfile
            from youtube_bot_dashboard.live_queue import LiveMessageRecord

            message = session.get(LiveMessageRecord, "message-1")
            profile = session.get(CommunityViewerProfile, "channel-1")
            assert message is not None
            assert profile is not None
            assert message.avatar_url == "https://yt3.ggpht.com/example-avatar"
            assert profile.avatar_url == "https://yt3.ggpht.com/example-avatar"
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
            assert by_pipeline["MODERATION"].state == TaskState.EXCLUDED_SHIELDED.value
            assert by_pipeline["REPLY_FINDER"].state == TaskState.EXCLUDED_SHIELDED.value
    finally:
        engine.dispose()

def test_json_ingest_preserves_source_confirmed_super_chat_metadata():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        body = LiveIngestBody(
            session_id="paid-stream",
            live_chat_id="youtube-chat-paid",
            broadcast_id="broadcast-paid",
            messages=[IncomingMessageBody(
                message_id="paid-message",
                user_id="paid-channel",
                username="Paid Viewer",
                text="Thanks for the stream!",
                is_super_chat=True,
                super_chat_amount_micros=250_000_000,
                super_chat_currency="INR",
                super_chat_amount_display="₹250.00",
                super_chat_tier=1,
            )],
        )
        result = ingest_payload(factory, body)
        assert result["created"] == 1
        with factory() as session:
            from youtube_bot_dashboard.live_queue import LiveMessageRecord
            paid = session.get(LiveMessageRecord, "paid-message")
            assert paid is not None
            assert paid.is_super_chat is True
            assert paid.super_chat_amount_micros == 250_000_000
            assert paid.super_chat_currency == "INR"
            assert paid.super_chat_amount_display == "₹250.00"
            assert paid.super_chat_tier == 1
    finally:
        engine.dispose()
