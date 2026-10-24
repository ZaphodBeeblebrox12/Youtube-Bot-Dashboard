"""Moderator-tracked appeals for confirmed moderation incidents.

Appeals are internal records entered/reviewed by the channel owner or moderator.
They never call YouTube enforcement APIs.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from youtube_bot_dashboard.storage import Base, utc_now
from youtube_bot_dashboard.persistent_moderation import (
    ModerationAuditEvent,
    ModerationIncident,
    _audit,
    overturn_moderation_incident,
)


class ModerationAppealRecord(Base):
    __tablename__ = "moderation_appeals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("moderation_incidents.id"), nullable=False, index=True
    )
    appellant_label: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN", index=True)
    submitted_by: Mapped[str] = mapped_column(String(120), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


def create_moderation_appeal(
    session: Session, *, incident_id: int, appellant_label: str,
    reason: str, actor: str, now: datetime | None = None,
) -> ModerationAppealRecord:
    incident = session.get(ModerationIncident, incident_id)
    if incident is None:
        raise ValueError("Unknown moderation incident.")
    if incident.status != "CONFIRMED":
        raise ValueError("Appeals can be opened only for confirmed incidents.")
    if not appellant_label.strip() or not reason.strip():
        raise ValueError("An appellant label and appeal reason are required.")
    row = ModerationAppealRecord(
        incident_id=incident_id,
        appellant_label=appellant_label.strip()[:200],
        reason=reason.strip()[:4000],
        status="OPEN",
        submitted_by=actor[:120],
        submitted_at=now or utc_now(),
    )
    session.add(row)
    session.flush()
    _audit(
        session, event_type="APPEAL_OPENED", target_type="appeal", target_id=row.id,
        user_channel_id=incident.user_channel_id, actor=actor,
        details={"incident_id": incident_id, "appellant_label": row.appellant_label,
                 "reason": row.reason},
    )
    return row


def resolve_moderation_appeal(
    session: Session, *, appeal_id: int, decision: str, actor: str,
    note: str, now: datetime | None = None,
) -> ModerationAppealRecord:
    appeal = session.get(ModerationAppealRecord, appeal_id)
    if appeal is None:
        raise ValueError("Unknown moderation appeal.")
    if appeal.status != "OPEN":
        raise ValueError("Only open appeals can be resolved.")
    if decision not in {"UPHOLD", "OVERTURN"}:
        raise ValueError("decision must be UPHOLD or OVERTURN.")
    if not note.strip():
        raise ValueError("A resolution note is required.")
    incident = session.get(ModerationIncident, appeal.incident_id)
    if incident is None:
        raise ValueError("The appeal's incident record is missing.")
    timestamp = now or utc_now()
    if decision == "OVERTURN":
        overturn_moderation_incident(
            session, incident_id=appeal.incident_id, actor=actor, note=note, now=timestamp
        )
        appeal.status = "OVERTURNED"
    else:
        appeal.status = "UPHELD"
    appeal.decided_by = actor[:120]
    appeal.decided_at = timestamp
    appeal.resolution_note = note.strip()[:4000]
    _audit(
        session, event_type="APPEAL_" + appeal.status, target_type="appeal",
        target_id=appeal.id, user_channel_id=incident.user_channel_id, actor=actor,
        details={"incident_id": incident.id, "decision": decision, "note": appeal.resolution_note},
    )
    session.flush()
    return appeal


def list_open_moderation_appeals(session: Session, *, limit: int = 100) -> list[ModerationAppealRecord]:
    return list(session.scalars(select(ModerationAppealRecord).where(
        ModerationAppealRecord.status == "OPEN"
    ).order_by(ModerationAppealRecord.submitted_at.desc()).limit(max(1, min(limit, 500)))))
