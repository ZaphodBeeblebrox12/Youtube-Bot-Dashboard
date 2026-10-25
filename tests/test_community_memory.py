from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func

from youtube_bot_dashboard.community_memory import (
    CommunityMemory,
    CommunityViewerProfile,
    CommunityViewerVisit,
    create_memory_suggestion,
    forget_saved_memory,
    forget_viewer,
    record_viewer_activity,
    review_memory_suggestion,
    purge_expired_community_data,
)
from youtube_bot_dashboard.live_queue import IncomingLiveMessage, ingest_live_message, register_live_session
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


def ingest_source(session, message_id, session_id="s1", user_id="channel-1"):
    return ingest_live_message(session, IncomingLiveMessage(
        message_id=message_id,
        session_id=session_id,
        user_channel_id=user_id,
        username="Asha" if user_id else None,
        text=f"Source message {message_id}",
    ))


def setup_db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with factory() as session:
        register_live_session(session, session_id="s1", live_chat_id="chat-1")
        register_live_session(session, session_id="s2", live_chat_id="chat-2")
        session.commit()
    return engine, factory


def test_profile_tracks_first_seen_last_seen_and_distinct_sessions():
    engine, factory = setup_db()
    try:
        first = datetime(2026, 10, 1, tzinfo=timezone.utc)
        second = first + timedelta(days=30)
        with factory() as session:
            record_viewer_activity(session, user_channel_id="channel-1", display_name="Asha", session_id="s1", observed_at=first)
            record_viewer_activity(session, user_channel_id="channel-1", display_name="Asha", session_id="s1", observed_at=first + timedelta(minutes=1))
            record_viewer_activity(session, user_channel_id="channel-1", display_name="Asha", session_id="s2", observed_at=second)
            session.commit()
        with factory() as session:
            profile = session.get(CommunityViewerProfile, "channel-1")
            visits = list(session.scalars(select(CommunityViewerVisit).where(CommunityViewerVisit.user_channel_id == "channel-1")))
            # Viewer history is retained as a rolling three-day window. A
            # return after thirty days is treated as a fresh retained profile.
            assert profile.first_seen_at.replace(tzinfo=timezone.utc) == second
            assert profile.last_seen_at.replace(tzinfo=timezone.utc) == second
            assert profile.distinct_sessions_seen == 1
            assert profile.total_message_count == 1
            assert len(visits) == 1
            assert visits[0].session_id == "s2"
    finally:
        engine.dispose()


def test_messages_without_stable_channel_id_are_not_profiled():
    engine, factory = setup_db()
    try:
        with factory() as session:
            assert record_viewer_activity(session, user_channel_id=None, display_name="Anonymous", session_id="s1") is None
            session.commit()
            assert session.get(CommunityViewerProfile, "Anonymous") is None
    finally:
        engine.dispose()


def test_memory_suggestion_requires_existing_profile_and_high_confidence():
    engine, factory = setup_db()
    try:
        with factory() as session:
            assert create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="missing-message",
                memory_type="CHALLENGE", summary="Often enters too early",
                suggested_follow_up="Has that improved?", confidence=0.99,
            ) is None
            ingest_source(session, "m-1")
            assert create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-1",
                memory_type="CHALLENGE", summary="Often enters too early",
                suggested_follow_up="Has that improved?", confidence=0.60,
            ) is None
            created = create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-1",
                memory_type="CHALLENGE", summary="Often enters too early",
                suggested_follow_up="Has that improved?", confidence=0.95,
            )
            session.commit()
            assert created is not None
            assert created.status == "SUGGESTED"
    finally:
        engine.dispose()


def test_one_pending_suggestion_and_two_saved_memories_maximum():
    engine, factory = setup_db()
    try:
        with factory() as session:
            ingest_source(session, "m-1", "s1")
            first = create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-1",
                memory_type="CHALLENGE", summary="Struggles with early entries",
                suggested_follow_up="Have entries improved?", confidence=0.95,
            )
            ingest_source(session, "m-2", "s1")
            assert create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-2",
                memory_type="PROGRESS", summary="Got better at risk management",
                suggested_follow_up="How is that going?", confidence=0.94,
            ) is None
            assert review_memory_suggestion(session, memory_id=first.id, decision="SAVE") == "SAVED"

            ingest_source(session, "m-3", "s2")
            second = create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s2", source_message_id="m-3",
                memory_type="PROGRESS", summary="Practicing defined-risk trades",
                suggested_follow_up="How is that practice going?", confidence=0.93,
            )
            assert review_memory_suggestion(session, memory_id=second.id, decision="SAVE") == "SAVED"
            ingest_source(session, "m-4", "s1")
            third = create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-4",
                memory_type="GOAL", summary="Wants to build patience",
                suggested_follow_up="How is that goal going?", confidence=0.92,
            )
            assert third is None
            saved = list(session.scalars(select(CommunityMemory).where(
                CommunityMemory.user_channel_id == "channel-1", CommunityMemory.status == "SAVED"
            )))
            assert len(saved) == 2
            assert review_memory_suggestion(session, memory_id=first.id, decision="SAVE") == "SAVED"
            session.commit()
    finally:
        engine.dispose()


def test_forget_controls_remove_memory_and_entire_profile():
    engine, factory = setup_db()
    try:
        with factory() as session:
            record_viewer_activity(session, user_channel_id="channel-1", display_name="Asha", session_id="s1")
            # Use a real message to satisfy the source message foreign key.
            ingest_live_message(session, IncomingLiveMessage(
                message_id="m-1", session_id="s1", user_channel_id="channel-1",
                username="Asha", text="I keep entering too early.",
            ))
            memory = create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-1",
                memory_type="CHALLENGE", summary="Struggles with early entries",
                suggested_follow_up="Has that improved?", confidence=0.95,
            )
            review_memory_suggestion(session, memory_id=memory.id, decision="SAVE")
            session.commit()
            assert forget_saved_memory(session, memory_id=memory.id)
            session.commit()
            assert session.get(CommunityMemory, memory.id) is None
            assert forget_viewer(session, user_channel_id="channel-1")
            session.commit()
            assert session.get(CommunityViewerProfile, "channel-1") is None
    finally:
        engine.dispose()


def test_membership_is_only_saved_when_source_explicitly_provides_it():
    engine, factory = setup_db()
    try:
        with factory() as session:
            record_viewer_activity(
                session, user_channel_id="channel-member", display_name="Member",
                session_id="s1", is_member=True,
            )
            profile = session.get(CommunityViewerProfile, "channel-member")
            assert profile.is_member is True

            record_viewer_activity(
                session, user_channel_id="channel-member", display_name="Member",
                session_id="s1", is_member=None,
            )
            assert profile.is_member is True

            record_viewer_activity(
                session, user_channel_id="channel-member", display_name="Member",
                session_id="s1", is_member=False,
            )
            assert profile.is_member is False
            session.commit()
    finally:
        engine.dispose()


def test_community_memories_expire_three_days_after_creation():
    engine, factory = setup_db()
    try:
        now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        with factory() as session:
            record_viewer_activity(
                session, user_channel_id="channel-1", display_name="Asha",
                session_id="s1", observed_at=now,
            )
            ingest_source(session, "m-old", "s1")
            old_memory = create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-old",
                memory_type="CHALLENGE", summary="Struggles with early entries",
                suggested_follow_up="Has that improved?", confidence=0.95,
            )
            assert review_memory_suggestion(session, memory_id=old_memory.id, decision="SAVE") == "SAVED"

            ingest_source(session, "m-new", "s1")
            new_memory = create_memory_suggestion(
                session, user_channel_id="channel-1", session_id="s1", source_message_id="m-new",
                memory_type="PROGRESS", summary="Practicing patient entries",
                suggested_follow_up="How is that going?", confidence=0.95,
            )
            old_memory.created_at = now - timedelta(days=3, seconds=1)
            new_memory.created_at = now - timedelta(days=2)
            session.flush()

            outcome = purge_expired_community_data(session, now=now)
            session.commit()

            assert outcome["expired_memories"] == 1
            assert session.get(CommunityMemory, old_memory.id) is None
            assert session.get(CommunityMemory, new_memory.id) is not None
            assert session.get(CommunityViewerProfile, "channel-1") is not None
    finally:
        engine.dispose()


def test_viewer_profile_and_visit_history_expire_after_three_days_inactive():
    engine, factory = setup_db()
    try:
        now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        with factory() as session:
            record_viewer_activity(
                session, user_channel_id="old-channel", display_name="Former viewer",
                session_id="s1", observed_at=now - timedelta(days=4),
            )
            record_viewer_activity(
                session, user_channel_id="recent-channel", display_name="Returning viewer",
                session_id="s1", observed_at=now - timedelta(days=2),
            )
            outcome = purge_expired_community_data(session, now=now)
            session.commit()
            assert outcome["expired_profiles"] == 1
            assert session.get(CommunityViewerProfile, "old-channel") is None
            assert session.get(CommunityViewerProfile, "recent-channel") is not None
            assert session.scalar(select(func.count()).select_from(CommunityViewerVisit).where(
                CommunityViewerVisit.user_channel_id == "old-channel"
            )) == 0
    finally:
        engine.dispose()
