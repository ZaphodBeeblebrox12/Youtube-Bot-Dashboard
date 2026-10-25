"""Persistent moderation review records and internal restrictions.

This module deliberately does not call YouTube enforcement APIs. AI output creates
review candidates only. A human-confirmed qualifying case can create at most one
session strike for a stable channel ID in a given live session. The saved project
policy makes three distinct confirmed session strikes a future enforcement-review
threshold; actual enforcement and later escalation remain disabled/unfinalized.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from youtube_bot_dashboard.storage import Base, utc_now
from youtube_bot_dashboard.live_queue import (
    BatchState,
    LiveAIRequestBatch,
    LiveMessageRecord,
    LiveMessageTask,
    LivePipeline,
    LiveSessionRecord,
    TaskState,
)

FIRST_BAN_ELIGIBILITY_SESSION_STRIKES = 3
VIOLATION_CATEGORIES = {
    "PERSONAL_ATTACK", "HARASSMENT", "SPAM_FLOODING", "HATE_SPEECH",
    "THREATS", "DOXXING_PRIVACY", "SCAM_PHISHING", "UNSOLICITED_PROMOTION",
    "IMPERSONATION", "DISRUPTION",
}


class ModerationIncident(Base):
    __tablename__ = "moderation_incidents"
    __table_args__ = (
        UniqueConstraint("incident_key", name="uq_moderation_incident_key"),
        UniqueConstraint("session_id", "id", name="uq_moderation_incident_session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("live_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_channel_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    severity_score: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_message_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_message_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING_REVIEW", index=True)
    qualifies_for_strike: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionStrikeRecord(Base):
    __tablename__ = "moderation_session_strikes"
    __table_args__ = (
        UniqueConstraint("user_channel_id", "session_id", name="uq_moderation_strike_user_session"),
        UniqueConstraint("incident_id", name="uq_moderation_strike_incident"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("live_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True
    )
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("moderation_incidents.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CONFIRMED", index=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PersistentInternalRestriction(Base):
    __tablename__ = "persistent_internal_restrictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    restriction_type: Mapped[str] = mapped_column(String(20), nullable=False, default="PERMANENT")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_incident_id: Mapped[int | None] = mapped_column(
        ForeignKey("moderation_incidents.id"), nullable=True, index=True
    )


class ModerationAuditEvent(Base):
    __tablename__ = "moderation_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    user_channel_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


def _audit(
    session: Session, *, event_type: str, target_type: str, target_id: str | int,
    user_channel_id: str | None = None, actor: str | None = None, details: dict | None = None,
) -> ModerationAuditEvent:
    event = ModerationAuditEvent(
        event_type=event_type,
        target_type=target_type,
        target_id=str(target_id),
        user_channel_id=user_channel_id,
        actor=(actor or "system")[:120],
        details_json=json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str),
    )
    session.add(event)
    session.flush()
    return event


def expire_due_restrictions(session: Session, *, now: datetime | None = None) -> int:
    timestamp = now or utc_now()
    rows = list(session.scalars(select(PersistentInternalRestriction).where(
        PersistentInternalRestriction.active.is_(True),
        PersistentInternalRestriction.expires_at.is_not(None),
        PersistentInternalRestriction.expires_at <= timestamp,
    )))
    for row in rows:
        row.active = False
        row.revoked_at = timestamp
        row.revoked_by = "system:expiry"
        row.revocation_reason = "Timed internal restriction expired."
        _audit(
            session, event_type="INTERNAL_RESTRICTION_EXPIRED", target_type="restriction",
            target_id=row.id, user_channel_id=row.user_channel_id, actor="system:expiry",
            details={"expires_at": row.expires_at, "restriction_type": row.restriction_type},
        )
    if rows:
        session.flush()
    return len(rows)


def get_active_internal_restriction(
    session: Session, user_channel_id: str | None, *, now: datetime | None = None
) -> PersistentInternalRestriction | None:
    if not user_channel_id:
        return None
    timestamp = now or utc_now()
    expire_due_restrictions(session, now=timestamp)
    return session.scalar(select(PersistentInternalRestriction).where(
        PersistentInternalRestriction.user_channel_id == user_channel_id,
        PersistentInternalRestriction.active.is_(True),
        PersistentInternalRestriction.starts_at <= timestamp,
        or_(
            PersistentInternalRestriction.expires_at.is_(None),
            PersistentInternalRestriction.expires_at > timestamp,
        ),
    ).order_by(PersistentInternalRestriction.created_at.desc()).limit(1))


def is_user_persistently_restricted(
    session: Session, user_channel_id: str | None, *, now: datetime | None = None
) -> bool:
    """Return true only for a matching stable channel ID with an active internal restriction."""
    return get_active_internal_restriction(session, user_channel_id, now=now) is not None


def _exclude_user_from_ai(session: Session, user_channel_id: str, *, reason: str) -> int:
    message_ids = select(LiveMessageRecord.message_id).where(
        LiveMessageRecord.user_channel_id == user_channel_id
    )
    tasks = list(session.scalars(select(LiveMessageTask).where(
        LiveMessageTask.message_id.in_(message_ids),
        LiveMessageTask.state.in_([
            TaskState.PENDING.value, TaskState.BATCHED.value, TaskState.SENT.value,
            TaskState.FAILED_RETRYABLE.value, TaskState.COMPLETED.value,
        ]),
    )))
    excluded = 0
    for task in tasks:
        # Preserve completed moderation classifications as evidence/audit history.
        if task.pipeline == LivePipeline.MODERATION.value and task.state == TaskState.COMPLETED.value:
            continue
        message = session.get(LiveMessageRecord, task.message_id)
        batch = session.get(LiveAIRequestBatch, task.batch_id) if task.batch_id else None
        if batch is not None and batch.status == BatchState.PREPARED.value:
            try:
                payload = json.loads(batch.payload_json)
                payload["messages"] = [
                    item for item in payload.get("messages", [])
                    if item.get("message_id") != task.message_id
                ]
                batch.payload_json = json.dumps(payload, ensure_ascii=False)
                if not payload["messages"]:
                    batch.status = BatchState.CANCELLED.value
            except (TypeError, json.JSONDecodeError):
                batch.status = BatchState.CANCELLED.value
        task.state = TaskState.EXCLUDED_RESTRICTED.value
        task.batch_id = None
        task.provider = None
        task.result_json = None
        task.last_error = reason[:1000]
        excluded += 1
    session.flush()
    return excluded


def apply_internal_restriction(
    session: Session, *, user_channel_id: str, reason: str,
    restriction_type: str = "PERMANENT", expires_at: datetime | None = None,
    display_name: str | None = None, actor: str = "moderator",
    related_incident_id: int | None = None, now: datetime | None = None,
) -> PersistentInternalRestriction:
    """Persist an internal AI blacklist. This never contacts YouTube."""
    if not user_channel_id or not user_channel_id.strip():
        raise ValueError("A stable user/channel ID is required.")
    if not reason or not reason.strip():
        raise ValueError("A reason is required.")
    if restriction_type not in {"TIMED", "PERMANENT"}:
        raise ValueError("restriction_type must be TIMED or PERMANENT.")
    timestamp = now or utc_now()
    expire_due_restrictions(session, now=timestamp)
    if restriction_type == "TIMED":
        if expires_at is None or expires_at <= timestamp:
            raise ValueError("A timed restriction requires a future expiry.")
    else:
        expires_at = None
    row = session.scalar(select(PersistentInternalRestriction).where(
        PersistentInternalRestriction.user_channel_id == user_channel_id,
        PersistentInternalRestriction.active.is_(True),
    ).order_by(PersistentInternalRestriction.created_at.desc()).limit(1))
    created = row is None
    if row is None:
        row = PersistentInternalRestriction(
            user_channel_id=user_channel_id,
            display_name=display_name,
            restriction_type=restriction_type,
            reason=reason.strip(),
            starts_at=timestamp,
            expires_at=expires_at,
            active=True,
            created_by=actor,
            related_incident_id=related_incident_id,
        )
        session.add(row)
        session.flush()
    else:
        row.display_name = display_name or row.display_name
        row.restriction_type = restriction_type
        row.reason = reason.strip()
        row.starts_at = timestamp
        row.expires_at = expires_at
        row.created_by = actor
        row.related_incident_id = related_incident_id
        row.revoked_at = None
        row.revoked_by = None
        row.revocation_reason = None
    excluded_count = _exclude_user_from_ai(
        session, user_channel_id,
        reason="Excluded by persistent internal restriction before AI dispatch.",
    )
    _audit(
        session,
        event_type="INTERNAL_RESTRICTION_CREATED" if created else "INTERNAL_RESTRICTION_UPDATED",
        target_type="restriction", target_id=row.id, user_channel_id=user_channel_id,
        actor=actor, details={
            "restriction_type": restriction_type, "expires_at": expires_at,
            "reason": reason.strip(), "related_incident_id": related_incident_id,
            "excluded_task_count": excluded_count, "youtube_enforcement": "NOT_CONNECTED",
        },
    )
    session.flush()
    return row


def revoke_internal_restriction(
    session: Session, *, restriction_id: int, actor: str = "moderator",
    reason: str = "Moderator cleared internal restriction.", now: datetime | None = None,
) -> bool:
    row = session.get(PersistentInternalRestriction, restriction_id)
    if row is None or not row.active:
        return False
    timestamp = now or utc_now()
    row.active = False
    row.revoked_at = timestamp
    row.revoked_by = actor
    row.revocation_reason = reason
    _audit(
        session, event_type="INTERNAL_RESTRICTION_REVOKED", target_type="restriction",
        target_id=row.id, user_channel_id=row.user_channel_id, actor=actor,
        details={"reason": reason, "restriction_type": row.restriction_type},
    )
    # Deliberately do not requeue previously excluded messages. Future messages
    # may re-enter AI processing; old exclusions remain an auditable decision.
    session.flush()
    return True


def record_moderation_candidate(
    session: Session, *, session_id: str, message_id: str,
    user_channel_id: str | None, display_name: str | None, category: str,
    severity_score: int, confidence: float, explanation: str,
    evidence_message_ids: list[str] | None = None, provider: str | None = None,
    model_id: str | None = None, now: datetime | None = None,
) -> ModerationIncident | None:
    """Record a review candidate, never a strike, from validated classifier output."""
    if category not in VIOLATION_CATEGORIES:
        return None
    if session.get(LiveSessionRecord, session_id) is None:
        raise ValueError("Unknown live session for moderation incident.")
    if not 0 <= severity_score <= 100 or not 0 <= confidence <= 1:
        raise ValueError("Invalid severity or confidence.")
    # A spam flood is consolidated into one candidate per stable account/category/session.
    # Other categories remain independently reviewable by source message ID.
    if user_channel_id and category == "SPAM_FLOODING":
        identity = "|".join((session_id, user_channel_id, category, "session-spam-burst"))
    else:
        identity = "|".join((session_id, user_channel_id or "unknown-user", category, message_id))
    incident_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    row = session.scalar(select(ModerationIncident).where(
        ModerationIncident.incident_key == incident_key
    ))
    evidence = list(dict.fromkeys(
        item for item in ([message_id] + (evidence_message_ids or []))
        if isinstance(item, str) and item
    ))[:200]
    timestamp = now or utc_now()
    if row is None:
        row = ModerationIncident(
            incident_key=incident_key,
            session_id=session_id,
            user_channel_id=user_channel_id,
            display_name=display_name,
            category=category,
            severity_score=severity_score,
            confidence=float(confidence),
            explanation=explanation[:4000],
            evidence_message_ids_json=json.dumps(evidence, ensure_ascii=False),
            source_message_id=message_id,
            provider=provider,
            model_id=model_id,
            status="PENDING_REVIEW",
            qualifies_for_strike=False,
            created_at=timestamp,
        )
        session.add(row)
        session.flush()
        _audit(
            session, event_type="INCIDENT_CANDIDATE_RECORDED", target_type="incident",
            target_id=row.id, user_channel_id=user_channel_id, actor="ai",
            details={"session_id": session_id, "message_id": message_id, "category": category,
                     "severity_score": severity_score, "confidence": confidence,
                     "provider": provider, "model_id": model_id},
        )
    else:
        previous = json.loads(row.evidence_message_ids_json or "[]")
        row.evidence_message_ids_json = json.dumps(list(dict.fromkeys(previous + evidence))[:200], ensure_ascii=False)
        row.severity_score = max(row.severity_score, severity_score)
        row.confidence = max(row.confidence, float(confidence))
        if row.status == "PENDING_REVIEW" and explanation:
            row.explanation = explanation[:4000]
        session.flush()
    return row


def record_moderation_results(
    session: Session, *, batch_id: str, session_id: str, payload: dict,
    results_by_message_id: dict[str, dict], provider: str | None, model_id: str | None,
    now: datetime | None = None,
) -> list[int]:
    """Persist only results for tasks that remain completed after any concurrent exclusion."""
    sources = {
        item.get("message_id"): item
        for item in payload.get("messages", [])
        if isinstance(item, dict) and isinstance(item.get("message_id"), str)
    }
    ids: list[int] = []
    for message_id, result in results_by_message_id.items():
        source = sources.get(message_id)
        if source is None:
            continue
        task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == message_id,
            LiveMessageTask.pipeline == LivePipeline.MODERATION.value,
            LiveMessageTask.batch_id == batch_id,
            LiveMessageTask.state == TaskState.COMPLETED.value,
        ))
        if task is None:
            continue
        category = result.get("category")
        if category not in VIOLATION_CATEGORIES:
            continue
        incident = record_moderation_candidate(
            session, session_id=session_id, message_id=message_id,
            user_channel_id=source.get("user_id"),
            display_name=source.get("username"),
            category=category, severity_score=result["severity_score"],
            confidence=float(result["confidence"]), explanation=result.get("explanation", ""),
            evidence_message_ids=result.get("evidence_message_ids", []),
            provider=provider, model_id=model_id, now=now,
        )
        if incident is not None:
            ids.append(incident.id)
    session.flush()
    return list(dict.fromkeys(ids))


def _confirmed_strike_count(session: Session, user_channel_id: str) -> int:
    return int(session.scalar(select(func.count(SessionStrikeRecord.id)).where(
        SessionStrikeRecord.user_channel_id == user_channel_id,
        SessionStrikeRecord.status == "CONFIRMED",
    )) or 0)


def confirm_moderation_incident(
    session: Session, *, incident_id: int, actor: str,
    note: str = "", now: datetime | None = None,
) -> dict:
    incident = session.get(ModerationIncident, incident_id)
    if incident is None:
        raise ValueError("Unknown moderation incident.")
    if incident.status != "PENDING_REVIEW":
        raise ValueError("Only pending incidents can be confirmed.")
    timestamp = now or utc_now()
    incident.status = "CONFIRMED"
    incident.reviewed_at = timestamp
    incident.reviewed_by = actor
    incident.review_note = note[:2000] if note else None
    strike_created = False
    if incident.user_channel_id:
        existing = session.scalar(select(SessionStrikeRecord).where(
            SessionStrikeRecord.user_channel_id == incident.user_channel_id,
            SessionStrikeRecord.session_id == incident.session_id,
        ))
        if existing is None:
            strike = SessionStrikeRecord(
                user_channel_id=incident.user_channel_id,
                session_id=incident.session_id,
                incident_id=incident.id,
                status="CONFIRMED",
                confirmed_at=timestamp,
            )
            session.add(strike)
            incident.qualifies_for_strike = True
            strike_created = True
        elif existing.status != "CONFIRMED":
            existing.incident_id = incident.id
            existing.status = "CONFIRMED"
            existing.confirmed_at = timestamp
            existing.revoked_at = None
            existing.revocation_note = None
            incident.qualifies_for_strike = True
            strike_created = True
        else:
            # One qualifying session strike per stable user ID and live session.
            incident.qualifies_for_strike = existing.incident_id == incident.id
    session.flush()
    strike_count = _confirmed_strike_count(session, incident.user_channel_id) if incident.user_channel_id else 0
    _audit(
        session, event_type="INCIDENT_CONFIRMED", target_type="incident",
        target_id=incident.id, user_channel_id=incident.user_channel_id, actor=actor,
        details={"note": note, "strike_created": strike_created,
                 "session_strike_count": strike_count,
                 "first_ban_threshold_met": strike_count >= FIRST_BAN_ELIGIBILITY_SESSION_STRIKES,
                 "enforcement": "DISABLED; review eligibility only"},
    )
    if strike_created:
        _audit(
            session, event_type="SESSION_STRIKE_CONFIRMED", target_type="session_strike",
            target_id=incident.id, user_channel_id=incident.user_channel_id, actor=actor,
            details={"session_id": incident.session_id, "incident_id": incident.id,
                     "distinct_session_strikes": strike_count},
        )
    session.flush()
    return {
        "incident_id": incident.id, "strike_created": strike_created,
        "session_strike_count": strike_count,
        "first_ban_threshold_met": strike_count >= FIRST_BAN_ELIGIBILITY_SESSION_STRIKES,
        "enforcement_enabled": False,
    }


def reject_moderation_incident(
    session: Session, *, incident_id: int, actor: str, note: str = "",
    now: datetime | None = None,
) -> ModerationIncident:
    incident = session.get(ModerationIncident, incident_id)
    if incident is None:
        raise ValueError("Unknown moderation incident.")
    if incident.status != "PENDING_REVIEW":
        raise ValueError("Only pending incidents can be rejected.")
    incident.status = "REJECTED"
    incident.reviewed_at = now or utc_now()
    incident.reviewed_by = actor
    incident.review_note = note[:2000] if note else "Rejected during moderator review."
    _audit(
        session, event_type="INCIDENT_REJECTED", target_type="incident", target_id=incident.id,
        user_channel_id=incident.user_channel_id, actor=actor,
        details={"note": incident.review_note},
    )
    session.flush()
    return incident


def overturn_moderation_incident(
    session: Session, *, incident_id: int, actor: str, note: str,
    now: datetime | None = None,
) -> dict:
    incident = session.get(ModerationIncident, incident_id)
    if incident is None:
        raise ValueError("Unknown moderation incident.")
    if incident.status != "CONFIRMED":
        raise ValueError("Only confirmed incidents can be overturned.")
    if not note.strip():
        raise ValueError("An overturn reason is required.")
    timestamp = now or utc_now()
    incident.status = "OVERTURNED"
    incident.reviewed_at = timestamp
    incident.reviewed_by = actor
    incident.review_note = note[:2000]
    incident.qualifies_for_strike = False
    reassigned = False
    strike = session.scalar(select(SessionStrikeRecord).where(
        SessionStrikeRecord.incident_id == incident.id,
        SessionStrikeRecord.status == "CONFIRMED",
    ))
    if strike is not None:
        alternative = None
        if incident.user_channel_id:
            alternative = session.scalar(select(ModerationIncident).where(
                ModerationIncident.user_channel_id == incident.user_channel_id,
                ModerationIncident.session_id == incident.session_id,
                ModerationIncident.status == "CONFIRMED",
                ModerationIncident.id != incident.id,
            ).order_by(ModerationIncident.reviewed_at.asc(), ModerationIncident.id.asc()).limit(1))
        if alternative is not None:
            strike.incident_id = alternative.id
            alternative.qualifies_for_strike = True
            reassigned = True
            _audit(
                session, event_type="SESSION_STRIKE_REASSIGNED", target_type="session_strike",
                target_id=strike.id, user_channel_id=incident.user_channel_id, actor=actor,
                details={"from_incident_id": incident.id, "to_incident_id": alternative.id},
            )
        else:
            strike.status = "REVOKED"
            strike.revoked_at = timestamp
            strike.revocation_note = note[:2000]
            _audit(
                session, event_type="SESSION_STRIKE_REVOKED", target_type="session_strike",
                target_id=strike.id, user_channel_id=incident.user_channel_id, actor=actor,
                details={"incident_id": incident.id, "note": note},
            )
    _audit(
        session, event_type="INCIDENT_OVERTURNED", target_type="incident", target_id=incident.id,
        user_channel_id=incident.user_channel_id, actor=actor,
        details={"note": note, "strike_reassigned": reassigned},
    )
    session.flush()
    count = _confirmed_strike_count(session, incident.user_channel_id) if incident.user_channel_id else 0
    return {"incident_id": incident.id, "strike_reassigned": reassigned,
            "session_strike_count": count, "enforcement_enabled": False}


def list_active_internal_restrictions(session: Session, *, now: datetime | None = None) -> list[PersistentInternalRestriction]:
    timestamp = now or utc_now()
    expire_due_restrictions(session, now=timestamp)
    return list(session.scalars(select(PersistentInternalRestriction).where(
        PersistentInternalRestriction.active.is_(True),
        PersistentInternalRestriction.starts_at <= timestamp,
        or_(PersistentInternalRestriction.expires_at.is_(None),
            PersistentInternalRestriction.expires_at > timestamp),
    ).order_by(PersistentInternalRestriction.created_at.desc())))


def get_confirmed_session_strikes(session: Session, user_channel_id: str) -> int:
    """Count qualifying strikes, with the schema enforcing one per distinct session."""
    return _confirmed_strike_count(session, user_channel_id)


def list_pending_incidents(session: Session, *, limit: int = 100) -> list[ModerationIncident]:
    return list(session.scalars(select(ModerationIncident).where(
        ModerationIncident.status == "PENDING_REVIEW"
    ).order_by(ModerationIncident.created_at.desc()).limit(max(1, min(limit, 500)))))


def list_recent_incidents(session: Session, *, limit: int = 200) -> list[ModerationIncident]:
    return list(session.scalars(select(ModerationIncident).order_by(
        ModerationIncident.created_at.desc()
    ).limit(max(1, min(limit, 500)))))


def list_recent_audit_events(session: Session, *, limit: int = 200) -> list[ModerationAuditEvent]:
    return list(session.scalars(select(ModerationAuditEvent).order_by(
        ModerationAuditEvent.created_at.desc()
    ).limit(max(1, min(limit, 500)))))
