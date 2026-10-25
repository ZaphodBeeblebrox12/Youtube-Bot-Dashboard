"""Private, lightweight viewer memory for meaningful livestream follow-ups.

Viewer activity is indexed only by the stable channel ID supplied by the source.
Short suggestions await owner review; only approved memories become saved viewer cues.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select, func, delete
from sqlalchemy.orm import Mapped, Session, mapped_column

from youtube_bot_dashboard.storage import Base, utc_now

MEMORY_TYPES = {"CHALLENGE", "GOAL", "PROGRESS", "PREFERENCE", "MILESTONE", "INSIGHT"}
MEMORY_STATUSES = {"SUGGESTED", "SAVED", "DISMISSED"}
MIN_MEMORY_CONFIDENCE = 0.80
MAX_SAVED_MEMORIES_PER_VIEWER = 2
COMMUNITY_RETENTION_DAYS = 3
CLEANUP_INTERVAL = timedelta(hours=1)
_RETENTION_CLEANUP_LOCK = Lock()
_LAST_RETENTION_CLEANUP_AT: datetime | None = None


class CommunityViewerProfile(Base):
    __tablename__ = "community_viewer_profiles"

    user_channel_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_member: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    first_seen_session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    last_seen_session_id: Mapped[str] = mapped_column(String(120), nullable=False)
    distinct_sessions_seen: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    total_message_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class CommunityViewerVisit(Base):
    __tablename__ = "community_viewer_visits"
    __table_args__ = (UniqueConstraint("user_channel_id", "session_id", name="uq_viewer_visit_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_channel_id: Mapped[str] = mapped_column(
        ForeignKey("community_viewer_profiles.user_channel_id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class CommunityMemory(Base):
    __tablename__ = "community_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_channel_id: Mapped[str] = mapped_column(
        ForeignKey("community_viewer_profiles.user_channel_id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    source_message_id: Mapped[str] = mapped_column(
        ForeignKey("live_messages.message_id", ondelete="CASCADE"), index=True, nullable=False,
    )
    memory_type: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[str] = mapped_column(String(240), nullable=False)
    suggested_follow_up: Mapped[str] = mapped_column(String(240), nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="SUGGESTED", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_session_id: Mapped[str | None] = mapped_column(String(120), nullable=True)



def purge_expired_community_data(
    session: Session,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete community memories and viewer history older than the 3-day window.

    Memory suggestions and saved memories expire three days after creation.
    Viewer profiles/visit rows expire after three days without activity. For
    active profiles, visit counters are recomputed only from visits in-window.
    """
    current_time = now or utc_now()
    cutoff = current_time - timedelta(days=COMMUNITY_RETENTION_DAYS)

    expired_memories = session.execute(
        delete(CommunityMemory).where(CommunityMemory.created_at < cutoff)
    ).rowcount or 0

    stale_profile_ids = list(session.scalars(
        select(CommunityViewerProfile.user_channel_id).where(
            CommunityViewerProfile.last_seen_at < cutoff
        )
    ))
    if stale_profile_ids:
        session.execute(
            delete(CommunityMemory).where(
                CommunityMemory.user_channel_id.in_(stale_profile_ids)
            )
        )
        session.execute(
            delete(CommunityViewerVisit).where(
                CommunityViewerVisit.user_channel_id.in_(stale_profile_ids)
            )
        )

    expired_visits = session.execute(
        delete(CommunityViewerVisit).where(CommunityViewerVisit.last_seen_at < cutoff)
    ).rowcount or 0
    expired_profiles = session.execute(
        delete(CommunityViewerProfile).where(CommunityViewerProfile.last_seen_at < cutoff)
    ).rowcount or 0

    profiles = list(session.scalars(
        select(CommunityViewerProfile).where(CommunityViewerProfile.last_seen_at >= cutoff)
    ))
    for profile in profiles:
        recent_visits = list(session.scalars(
            select(CommunityViewerVisit)
            .where(
                CommunityViewerVisit.user_channel_id == profile.user_channel_id,
                CommunityViewerVisit.last_seen_at >= cutoff,
            )
            .order_by(CommunityViewerVisit.first_seen_at.asc())
        ))
        if recent_visits:
            profile.first_seen_at = recent_visits[0].first_seen_at
            profile.first_seen_session_id = recent_visits[0].session_id
            profile.distinct_sessions_seen = len(recent_visits)
            profile.total_message_count = sum(visit.message_count for visit in recent_visits)
        else:
            # A recently active profile should normally have a visit row. Keep
            # the aggregate conservative if older data is inconsistent.
            profile.distinct_sessions_seen = 1
            profile.total_message_count = 1

    session.flush()
    return {
        "expired_memories": int(expired_memories),
        "expired_visits": int(expired_visits),
        "expired_profiles": int(expired_profiles),
    }


def _ensure_community_retention_cleanup(
    session: Session,
    *,
    now: datetime | None = None,
) -> None:
    """Throttle full-table cleanup to once per hour per application process."""
    global _LAST_RETENTION_CLEANUP_AT
    current_time = now or utc_now()
    with _RETENTION_CLEANUP_LOCK:
        previous = _LAST_RETENTION_CLEANUP_AT
        if previous is not None and current_time - previous < CLEANUP_INTERVAL:
            return
        purge_expired_community_data(session, now=current_time)
        _LAST_RETENTION_CLEANUP_AT = current_time


def record_viewer_activity(
    session: Session,
    *,
    user_channel_id: str | None,
    display_name: str | None,
    session_id: str,
    is_member: bool | None = None,
    observed_at: datetime | None = None,
) -> CommunityViewerProfile | None:
    """Record presence by stable channel ID, without saving ordinary chat text."""
    if not user_channel_id or not user_channel_id.strip():
        return None
    now = observed_at or utc_now()
    _ensure_community_retention_cleanup(session)
    profile = session.get(CommunityViewerProfile, user_channel_id)
    retention_cutoff = utc_now() - timedelta(days=COMMUNITY_RETENTION_DAYS)
    if profile is not None and profile.last_seen_at < retention_cutoff:
        # If an inactive viewer returns between scheduled purges, remove their
        # expired continuity data now instead of reviving out-of-window history.
        session.execute(
            delete(CommunityMemory).where(
                CommunityMemory.user_channel_id == user_channel_id
            )
        )
        session.execute(
            delete(CommunityViewerVisit).where(
                CommunityViewerVisit.user_channel_id == user_channel_id
            )
        )
        session.delete(profile)
        session.flush()
        profile = None
    if profile is None:
        profile = CommunityViewerProfile(
            user_channel_id=user_channel_id,
            display_name=display_name,
            is_member=is_member,
            first_seen_at=now,
            last_seen_at=now,
            first_seen_session_id=session_id,
            last_seen_session_id=session_id,
            distinct_sessions_seen=1,
            total_message_count=1,
        )
        session.add(profile)
        session.flush()
        session.add(CommunityViewerVisit(
            user_channel_id=user_channel_id,
            session_id=session_id,
            first_seen_at=now,
            last_seen_at=now,
            message_count=1,
        ))
        session.flush()
        return profile

    profile.last_seen_at = now
    profile.last_seen_session_id = session_id
    profile.total_message_count += 1
    if display_name:
        profile.display_name = display_name
    if is_member is not None:
        profile.is_member = is_member
    visit = session.scalar(select(CommunityViewerVisit).where(
        CommunityViewerVisit.user_channel_id == user_channel_id,
        CommunityViewerVisit.session_id == session_id,
    ))
    if visit is None:
        profile.distinct_sessions_seen += 1
        session.add(CommunityViewerVisit(
            user_channel_id=user_channel_id,
            session_id=session_id,
            first_seen_at=now,
            last_seen_at=now,
            message_count=1,
        ))
    else:
        visit.last_seen_at = now
        visit.message_count += 1
    session.flush()
    return profile


def create_memory_suggestion(
    session: Session,
    *,
    user_channel_id: str | None,
    session_id: str,
    source_message_id: str,
    memory_type: str,
    summary: str,
    suggested_follow_up: str,
    confidence: float,
) -> CommunityMemory | None:
    """Queue at most one pending suggestion per person; never auto-save it."""
    if not user_channel_id or not user_channel_id.strip():
        return None
    _ensure_community_retention_cleanup(session)
    if memory_type not in MEMORY_TYPES:
        return None
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return None
    if confidence < MIN_MEMORY_CONFIDENCE or confidence > 1:
        return None
    summary = " ".join(summary.split()).strip()
    follow_up = " ".join(suggested_follow_up.split()).strip()
    if not summary or not follow_up:
        return None
    summary = summary[:240]
    follow_up = follow_up[:240]

    # Cross-check the candidate against the real stored message, rather than
    # trusting model-supplied identity or source references.
    from youtube_bot_dashboard.live_queue import LiveMessageRecord

    source_message = session.get(LiveMessageRecord, source_message_id)
    if (
        source_message is None
        or source_message.user_channel_id != user_channel_id
        or source_message.session_id != session_id
    ):
        return None

    profile = session.get(CommunityViewerProfile, user_channel_id)
    if profile is None:
        return None
    # Expire this viewer's stale memories even when the hourly global cleanup
    # was recently run in another request/session.
    session.execute(
        delete(CommunityMemory).where(
            CommunityMemory.user_channel_id == user_channel_id,
            CommunityMemory.created_at < utc_now() - timedelta(days=COMMUNITY_RETENTION_DAYS),
        )
    )
    session.flush()
    existing_suggestion = session.scalar(select(CommunityMemory.id).where(
        CommunityMemory.user_channel_id == user_channel_id,
        CommunityMemory.status == "SUGGESTED",
    ).limit(1))
    if existing_suggestion is not None:
        return None
    saved = list(session.scalars(select(CommunityMemory).where(
        CommunityMemory.user_channel_id == user_channel_id,
        CommunityMemory.status == "SAVED",
    )))
    if len(saved) >= MAX_SAVED_MEMORIES_PER_VIEWER:
        return None
    normalized_summary = summary.casefold()
    if any(memory.summary.casefold() == normalized_summary for memory in saved):
        return None

    record = CommunityMemory(
        user_channel_id=user_channel_id,
        session_id=session_id,
        source_message_id=source_message_id,
        memory_type=memory_type,
        summary=summary,
        suggested_follow_up=follow_up,
        confidence=float(confidence),
        status="SUGGESTED",
    )
    session.add(record)
    session.flush()
    return record


def review_memory_suggestion(
    session: Session,
    *,
    memory_id: int,
    decision: str,
    reviewed_at: datetime | None = None,
) -> str:
    """Save or dismiss a suggestion; saving is capped at two per viewer."""
    normalized = decision.strip().upper()
    if normalized not in {"SAVE", "DISMISS"}:
        raise ValueError("decision must be SAVE or DISMISS")
    _ensure_community_retention_cleanup(session)
    memory = session.get(CommunityMemory, memory_id)
    if memory is None:
        raise ValueError(f"unknown community memory {memory_id}")
    if memory.status != "SUGGESTED":
        return memory.status
    if normalized == "SAVE":
        saved_count = session.scalar(select(func.count()).select_from(CommunityMemory).where(
            CommunityMemory.user_channel_id == memory.user_channel_id,
            CommunityMemory.status == "SAVED",
        )) or 0
        if saved_count >= MAX_SAVED_MEMORIES_PER_VIEWER:
            return "LIMIT_REACHED"
        memory.status = "SAVED"
    else:
        memory.status = "DISMISSED"
    memory.reviewed_at = reviewed_at or utc_now()
    session.flush()
    return memory.status


def forget_saved_memory(session: Session, *, memory_id: int) -> bool:
    _ensure_community_retention_cleanup(session)
    memory = session.get(CommunityMemory, memory_id)
    if memory is None or memory.status != "SAVED":
        return False
    session.delete(memory)
    session.flush()
    return True


def forget_viewer(session: Session, *, user_channel_id: str) -> bool:
    """Delete the profile, visit history and associated memories for this channel ID."""
    profile = session.get(CommunityViewerProfile, user_channel_id)
    if profile is None:
        return False
    session.execute(
        CommunityMemory.__table__.delete().where(CommunityMemory.user_channel_id == user_channel_id)
    )
    session.execute(
        CommunityViewerVisit.__table__.delete().where(CommunityViewerVisit.user_channel_id == user_channel_id)
    )
    session.delete(profile)
    session.flush()
    return True
