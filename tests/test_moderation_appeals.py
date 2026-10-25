import pytest
from sqlalchemy import select

from youtube_bot_dashboard.live_queue import register_live_session
from youtube_bot_dashboard.moderation_appeals import (
    ModerationAppealRecord,
    create_moderation_appeal,
    resolve_moderation_appeal,
)
from youtube_bot_dashboard.persistent_moderation import (
    ModerationAuditEvent,
    SessionStrikeRecord,
    confirm_moderation_incident,
    get_confirmed_session_strikes,
    record_moderation_candidate,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


@pytest.fixture
def db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with factory() as session:
        register_live_session(session, session_id="session-1", live_chat_id="chat-1")
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def confirmed_incident(session):
    incident = record_moderation_candidate(
        session, session_id="session-1", message_id="appeal-message",
        user_channel_id="channel-appeal", display_name="Viewer",
        category="HARASSMENT", severity_score=70, confidence=0.94,
        explanation="Candidate reviewed before confirmation.",
        evidence_message_ids=["appeal-message"],
    )
    confirm_moderation_incident(
        session, incident_id=incident.id, actor="moderator", note="Confirmed during review."
    )
    return incident


def test_appeal_can_be_upheld_and_is_audited(db):
    _, factory = db
    with factory() as session:
        incident = confirmed_incident(session)
        appeal = create_moderation_appeal(
            session, incident_id=incident.id, appellant_label="viewer@example",
            reason="I believe this was taken out of context.", actor="moderator",
        )
        resolved = resolve_moderation_appeal(
            session, appeal_id=appeal.id, decision="UPHOLD", actor="moderator",
            note="Evidence and context support the original decision.",
        )
        session.commit()
        assert resolved.status == "UPHELD"
        assert get_confirmed_session_strikes(session, "channel-appeal") == 1
        assert session.scalar(select(ModerationAuditEvent.id).where(
            ModerationAuditEvent.event_type == "APPEAL_UPHELD"
        )) is not None


def test_successful_appeal_overturns_incident_and_revokes_strike(db):
    _, factory = db
    with factory() as session:
        incident = confirmed_incident(session)
        appeal = create_moderation_appeal(
            session, incident_id=incident.id, appellant_label="viewer",
            reason="Additional context shows no qualifying violation.", actor="moderator",
        )
        resolved = resolve_moderation_appeal(
            session, appeal_id=appeal.id, decision="OVERTURN", actor="moderator",
            note="Appeal accepted after reviewing additional context.",
        )
        session.commit()
        assert resolved.status == "OVERTURNED"
        assert incident.status == "OVERTURNED"
        assert get_confirmed_session_strikes(session, "channel-appeal") == 0
        strike = session.scalar(select(SessionStrikeRecord).where(
            SessionStrikeRecord.user_channel_id == "channel-appeal"
        ))
        assert strike.status == "REVOKED"


def test_appeal_requires_confirmed_incident_and_resolution_note(db):
    _, factory = db
    with factory() as session:
        incident = record_moderation_candidate(
            session, session_id="session-1", message_id="pending-message",
            user_channel_id="channel-pending", display_name="Viewer",
            category="THREATS", severity_score=80, confidence=0.98,
            explanation="Pending review.", evidence_message_ids=["pending-message"],
        )
        with pytest.raises(ValueError, match="confirmed incidents"):
            create_moderation_appeal(
                session, incident_id=incident.id, appellant_label="viewer",
                reason="Please review.", actor="moderator",
            )
        confirmed = confirmed_incident(session)
        appeal = create_moderation_appeal(
            session, incident_id=confirmed.id, appellant_label="viewer",
            reason="Please review.", actor="moderator",
        )
        with pytest.raises(ValueError, match="resolution note"):
            resolve_moderation_appeal(
                session, appeal_id=appeal.id, decision="OVERTURN",
                actor="moderator", note="",
            )
