from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from youtube_bot_dashboard.live_queue import (
    IncomingLiveMessage,
    LiveAIRequestBatch,
    LiveMessageRecord,
    LiveMessageTask,
    LivePipeline,
    LiveQueueConfig,
    TaskState,
    complete_live_batch,
    ingest_live_message,
    mark_live_batch_sent,
    prepare_due_batch,
    register_live_session,
)
from youtube_bot_dashboard.persistent_moderation import (
    ModerationAuditEvent,
    ModerationIncident,
    PersistentInternalRestriction,
    SessionStrikeRecord,
    apply_internal_restriction,
    confirm_moderation_incident,
    get_active_internal_restriction,
    get_confirmed_session_strikes,
    is_user_persistently_restricted,
    record_moderation_candidate,
    record_moderation_results,
    reject_moderation_incident,
    overturn_moderation_incident,
    revoke_internal_restriction,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema, utc_now


@pytest.fixture
def db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with factory() as session:
        for index in range(1, 4):
            register_live_session(
                session,
                session_id=f"session-{index}",
                live_chat_id=f"chat-{index}",
                broadcast_id=f"broadcast-{index}",
            )
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def create_candidate(session, *, session_id="session-1", message_id="m-1",
                    user_id="channel-1", category="HARASSMENT"):
    return record_moderation_candidate(
        session,
        session_id=session_id,
        message_id=message_id,
        user_channel_id=user_id,
        display_name="Viewer label",
        category=category,
        severity_score=72,
        confidence=0.96,
        explanation="Source-supported candidate for moderator review.",
        evidence_message_ids=[message_id],
        provider="GEMINI",
        model_id="test-model",
    )


def test_moderation_worker_results_create_pending_candidate_not_strike(db):
    _, factory = db
    now = utc_now()
    config = LiveQueueConfig()
    with factory() as session:
        ingest_live_message(
            session,
            IncomingLiveMessage(
                message_id="msg-candidate", session_id="session-1",
                user_channel_id="channel-candidate", username="Viewer",
                text="A candidate message.",
            ),
            received_at=now,
        )
        prepared = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=now + timedelta(seconds=5),
        )
        assert prepared is not None
        payload = mark_live_batch_sent(session, prepared.batch_id, model_id="test-model")
        session.commit()

    result = {
        "category": "HARASSMENT",
        "severity_score": 72,
        "confidence": 0.96,
        "evidence_message_ids": ["msg-candidate"],
        "explanation": "A moderator should review this candidate.",
        "suggest_shield": False,
    }
    with factory() as session:
        complete_live_batch(
            session, prepared.batch_id,
            result_by_message_id={"msg-candidate": result},
            raw_response={"results": [{"message_id": "msg-candidate", **result}]},
        )
        ids = record_moderation_results(
            session, batch_id=prepared.batch_id, session_id="session-1", payload=payload,
            results_by_message_id={"msg-candidate": result},
            provider="GEMINI", model_id="test-model",
        )
        session.commit()
        assert len(ids) == 1
        candidate = session.get(ModerationIncident, ids[0])
        assert candidate.status == "PENDING_REVIEW"
        assert candidate.qualifies_for_strike is False
        assert get_confirmed_session_strikes(session, "channel-candidate") == 0


def test_repeated_spam_burst_is_deduplicated_and_evidence_is_accumulated(db):
    _, factory = db
    with factory() as session:
        first = record_moderation_candidate(
            session, session_id="session-1", message_id="spam-1",
            user_channel_id="channel-spammer", display_name="Spammer",
            category="SPAM_FLOODING", severity_score=65, confidence=0.93,
            explanation="Repeated spam burst.", evidence_message_ids=["spam-1"],
        )
        second = record_moderation_candidate(
            session, session_id="session-1", message_id="spam-2",
            user_channel_id="channel-spammer", display_name="Spammer",
            category="SPAM_FLOODING", severity_score=70, confidence=0.97,
            explanation="Further messages from the same burst.", evidence_message_ids=["spam-2"],
        )
        session.commit()
        assert first.id == second.id
        assert set(json.loads(second.evidence_message_ids_json)) == {"spam-1", "spam-2"}
        assert second.status == "PENDING_REVIEW"
        assert get_confirmed_session_strikes(session, "channel-spammer") == 0


def test_one_confirmed_session_strike_per_stable_user_id_per_session(db):
    _, factory = db
    with factory() as session:
        first = create_candidate(session, message_id="m-1", category="HARASSMENT")
        second = create_candidate(session, message_id="m-2", category="PERSONAL_ATTACK")
        session.flush()
        first_outcome = confirm_moderation_incident(
            session, incident_id=first.id, actor="moderator", note="Reviewed evidence."
        )
        second_outcome = confirm_moderation_incident(
            session, incident_id=second.id, actor="moderator", note="Separate message, same session."
        )
        session.commit()
        assert first_outcome["strike_created"] is True
        assert second_outcome["strike_created"] is False
        assert first_outcome["session_strike_count"] == 1
        assert second_outcome["session_strike_count"] == 1
        assert second.qualifies_for_strike is False
        strikes = list(session.scalars(select(SessionStrikeRecord).where(
            SessionStrikeRecord.user_channel_id == "channel-1"
        )))
        assert len(strikes) == 1


def test_three_distinct_confirmed_sessions_only_reach_review_threshold(db):
    _, factory = db
    outcomes = []
    with factory() as session:
        for index in range(1, 4):
            incident = create_candidate(
                session, session_id=f"session-{index}", message_id=f"m-{index}",
                user_id="repeat-channel", category="HARASSMENT",
            )
            outcomes.append(confirm_moderation_incident(
                session, incident_id=incident.id, actor="moderator", note="Confirmed."
            ))
        session.commit()
        assert outcomes[0]["first_ban_threshold_met"] is False
        assert outcomes[1]["first_ban_threshold_met"] is False
        assert outcomes[2]["first_ban_threshold_met"] is True
        assert outcomes[2]["enforcement_enabled"] is False
        assert get_confirmed_session_strikes(session, "repeat-channel") == 3
        assert session.scalar(select(PersistentInternalRestriction.id)) is None


def test_internal_restriction_blocks_both_pipelines_across_sessions_including_super_chat(db):
    _, factory = db
    now = utc_now()
    with factory() as session:
        restriction = apply_internal_restriction(
            session, user_channel_id="restricted-channel", display_name="Blocked",
            reason="Manual internal blacklist for review.", restriction_type="PERMANENT",
            actor="moderator", now=now,
        )
        session.commit()
        assert restriction.active

    with factory() as session:
        for message_id, session_id, is_super_chat in [
            ("restricted-ordinary", "session-1", False),
            ("restricted-paid", "session-2", True),
        ]:
            result = ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id=message_id, session_id=session_id,
                    user_channel_id="restricted-channel", username="Blocked",
                    text="I paid for priority." if is_super_chat else "Ordinary message.",
                    is_super_chat=is_super_chat,
                    super_chat_amount_micros=500_000_000 if is_super_chat else None,
                    super_chat_currency="INR" if is_super_chat else None,
                ),
                received_at=now,
            )
            assert result.reply_excluded_by_restriction
            tasks = list(session.scalars(select(LiveMessageTask).where(
                LiveMessageTask.message_id == message_id
            )))
            assert {task.pipeline for task in tasks} == {
                LivePipeline.MODERATION.value, LivePipeline.REPLY_FINDER.value
            }
            assert all(task.state == TaskState.EXCLUDED_RESTRICTED.value for task in tasks)
        assert prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=LiveQueueConfig(), now=now + timedelta(seconds=10),
        ) is None
        assert prepare_due_batch(
            session, session_id="session-2", pipeline=LivePipeline.REPLY_FINDER,
            config=LiveQueueConfig(), now=now + timedelta(seconds=10),
        ) is None
        session.commit()


def test_restriction_added_after_prepare_scrubs_prepared_batch(db):
    _, factory = db
    now = utc_now()
    with factory() as session:
        for message_id, user_id in [("msg-restrict", "channel-restrict"), ("msg-keep", "channel-keep")]:
            ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id=message_id, session_id="session-1",
                    user_channel_id=user_id, username=user_id, text=message_id,
                ),
                received_at=now,
            )
        prepared = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.REPLY_FINDER,
            config=LiveQueueConfig(), now=now + timedelta(seconds=5),
        )
        assert prepared is not None
        batch_id = prepared.batch_id
        session.commit()

    with factory() as session:
        apply_internal_restriction(
            session, user_channel_id="channel-restrict",
            reason="Added after batch preparation.", actor="moderator",
        )
        session.commit()

    with factory() as session:
        batch = session.get(LiveAIRequestBatch, batch_id)
        payload = json.loads(batch.payload_json)
        assert [item["message_id"] for item in payload["messages"]] == ["msg-keep"]
        restricted_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "msg-restrict",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))
        kept_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "msg-keep",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))
        assert restricted_task.state == TaskState.EXCLUDED_RESTRICTED.value
        assert restricted_task.batch_id is None
        assert kept_task.state == TaskState.BATCHED.value


def test_timed_internal_restriction_expires_and_is_audited(db):
    _, factory = db
    now = utc_now()
    with factory() as session:
        row = apply_internal_restriction(
            session, user_channel_id="timed-channel", reason="Temporary internal restriction.",
            restriction_type="TIMED", expires_at=now + timedelta(hours=1),
            actor="moderator", now=now,
        )
        restriction_id = row.id
        assert is_user_persistently_restricted(session, "timed-channel", now=now)
        session.commit()

    with factory() as session:
        assert get_active_internal_restriction(
            session, "timed-channel", now=now + timedelta(hours=2)
        ) is None
        row = session.get(PersistentInternalRestriction, restriction_id)
        assert row.active is False
        assert row.revoked_by == "system:expiry"
        events = list(session.scalars(select(ModerationAuditEvent).where(
            ModerationAuditEvent.event_type == "INTERNAL_RESTRICTION_EXPIRED"
        )))
        assert len(events) == 1


def test_overturn_reassigns_or_revokes_strike_and_records_audit(db):
    _, factory = db
    with factory() as session:
        first = create_candidate(session, message_id="strike-first", category="THREATS")
        second = create_candidate(session, message_id="strike-alternative", category="PERSONAL_ATTACK")
        confirm_moderation_incident(session, incident_id=first.id, actor="moderator", note="First confirmed.")
        confirm_moderation_incident(session, incident_id=second.id, actor="moderator", note="Second confirmed.")
        outcome = overturn_moderation_incident(
            session, incident_id=first.id, actor="moderator",
            note="Evidence review overturned the first decision.",
        )
        session.commit()
        assert outcome["strike_reassigned"] is True
        assert outcome["session_strike_count"] == 1
        strike = session.scalar(select(SessionStrikeRecord).where(
            SessionStrikeRecord.user_channel_id == "channel-1",
            SessionStrikeRecord.session_id == "session-1",
        ))
        assert strike.status == "CONFIRMED"
        assert strike.incident_id == second.id
        assert second.qualifies_for_strike is True
        assert first.status == "OVERTURNED"


def test_missing_stable_user_id_can_be_reviewed_but_never_creates_a_strike(db):
    _, factory = db
    with factory() as session:
        incident = create_candidate(session, message_id="unknown-user-message", user_id=None)
        outcome = confirm_moderation_incident(
            session, incident_id=incident.id, actor="moderator",
            note="Confirmed content issue; no stable identity available.",
        )
        session.commit()
        assert outcome["strike_created"] is False
        assert outcome["session_strike_count"] == 0
        assert incident.status == "CONFIRMED"
        assert incident.qualifies_for_strike is False


def test_rejected_candidate_is_audited_and_does_not_count_as_strike(db):
    _, factory = db
    from youtube_bot_dashboard.persistent_moderation import reject_moderation_incident
    with factory() as session:
        incident = create_candidate(session, message_id="dismiss-me")
        rejected = reject_moderation_incident(
            session, incident_id=incident.id, actor="moderator", note="False positive."
        )
        session.commit()
        assert rejected.status == "REJECTED"
        assert get_confirmed_session_strikes(session, "channel-1") == 0
        assert session.scalar(select(ModerationAuditEvent.id).where(
            ModerationAuditEvent.event_type == "INCIDENT_REJECTED"
        )) is not None


def test_manual_revoke_does_not_requeue_previously_excluded_messages(db):
    _, factory = db
    with factory() as session:
        row = apply_internal_restriction(
            session, user_channel_id="revoked-channel", reason="Manual restriction.",
        )
        restriction_id = row.id
        ingest_live_message(
            session,
            IncomingLiveMessage(
                message_id="revoked-message", session_id="session-1",
                user_channel_id="revoked-channel", text="I was restricted.",
            ),
        )
        session.commit()
    with factory() as session:
        assert revoke_internal_restriction(
            session, restriction_id=restriction_id, actor="moderator", reason="Manual clear."
        )
        session.commit()
    with factory() as session:
        task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "revoked-message",
            LiveMessageTask.pipeline == LivePipeline.MODERATION.value,
        ))
        assert task.state == TaskState.EXCLUDED_RESTRICTED.value
        assert not is_user_persistently_restricted(session, "revoked-channel")



def test_in_flight_result_cannot_restore_task_after_persistent_restriction(db):
    _, factory = db
    now = utc_now()
    config = LiveQueueConfig()
    with factory() as session:
        for message_id, user_id in [
            ("race-restricted", "race-channel"),
            ("race-allowed", "allowed-channel"),
        ]:
            ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id=message_id, session_id="session-1",
                    user_channel_id=user_id, username=user_id, text=message_id,
                ),
                received_at=now,
            )
        prepared = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=now + timedelta(seconds=5),
        )
        assert prepared is not None
        payload = mark_live_batch_sent(session, prepared.batch_id, model_id="test-model")
        session.commit()

    with factory() as session:
        apply_internal_restriction(
            session, user_channel_id="race-channel",
            reason="Restriction arrived while provider request was in flight.",
            actor="moderator",
        )
        session.commit()

    violation = {
        "category": "THREATS", "severity_score": 88, "confidence": 0.99,
        "evidence_message_ids": ["race-restricted"],
        "explanation": "Test provider result.", "suggest_shield": False,
    }
    benign = {
        "category": "NO_VIOLATION", "severity_score": 0, "confidence": 0.99,
        "evidence_message_ids": ["race-allowed"],
        "explanation": "Safe message.", "suggest_shield": False,
    }
    with factory() as session:
        complete_live_batch(
            session, prepared.batch_id,
            result_by_message_id={"race-restricted": violation, "race-allowed": benign},
            raw_response={"results": []},
        )
        created = record_moderation_results(
            session, batch_id=prepared.batch_id, session_id="session-1", payload=payload,
            results_by_message_id={"race-restricted": violation, "race-allowed": benign},
            provider="GEMINI", model_id="test-model",
        )
        restricted_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "race-restricted",
            LiveMessageTask.pipeline == LivePipeline.MODERATION.value,
        ))
        assert restricted_task.state == TaskState.EXCLUDED_RESTRICTED.value
        assert restricted_task.result_json is None
        assert created == []
        session.commit()
