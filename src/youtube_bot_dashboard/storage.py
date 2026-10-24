"""Durable storage primitives for incremental YouTube comment processing.

The schema is compatible with SQLite for local tests and PostgreSQL for a
managed deployment. Production schema changes should be shipped as migrations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from youtube_bot_dashboard.policies.comment_cleanup import (
    CleanupResult,
    CommentAssessment,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class MonitoredVideo(Base):
    __tablename__ = "monitored_videos"

    video_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    monitoring_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    monitoring_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_swept_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_sweep_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sweep_status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class CommentRecord(Base):
    __tablename__ = "comments"

    comment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("monitored_videos.video_id", ondelete="CASCADE"), index=True, nullable=False)
    parent_comment_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    author_channel_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    author_display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    text_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="DISCOVERED", nullable=False, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_target: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_sentiment: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ai_conduct_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(nullable=True)
    ai_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleanup_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cleanup_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleanup_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommentTextVersion(Base):
    __tablename__ = "comment_text_versions"
    __table_args__ = (
        UniqueConstraint("comment_id", "version_number", name="uq_comment_text_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comment_id: Mapped[str] = mapped_column(ForeignKey("comments.comment_id", ondelete="CASCADE"), index=True, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class UpsertOutcome(StrEnum):
    NEW = "NEW"
    UNCHANGED = "UNCHANGED"
    EDITED = "EDITED"


@dataclass(frozen=True, slots=True)
class DiscoveredComment:
    comment_id: str
    video_id: str
    text: str
    parent_comment_id: str | None = None
    author_channel_id: str | None = None
    author_display_name: str | None = None
    published_at: datetime | None = None
    updated_at_source: datetime | None = None

    def __post_init__(self) -> None:
        if not self.comment_id.strip():
            raise ValueError("comment_id must not be empty")
        if not self.video_id.strip():
            raise ValueError("video_id must not be empty")


@dataclass(frozen=True, slots=True)
class UpsertResult:
    outcome: UpsertOutcome
    comment_id: str
    current_version: int
    should_classify: bool
    is_edited_reassessment: bool


def create_engine_and_sessions(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    """Create an engine/session factory; pass a PostgreSQL URL in production."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    """Create registered tables for local development/tests; use migrations in production.

    Import the live queue models here so callers who use only this helper still
    register all current model tables before create_all. Production deployments
    should use versioned migrations rather than create_all.
    """
    from youtube_bot_dashboard import live_queue as _live_queue  # noqa: F401
    from youtube_bot_dashboard import models_comment_cleanup as _comment_cleanup  # noqa: F401

    Base.metadata.create_all(engine)


def register_video(
    session: Session,
    *,
    video_id: str,
    title: str | None = None,
    published_at: datetime | None = None,
    monitoring_started_at: datetime | None = None,
    monitoring_ends_at: datetime | None = None,
) -> MonitoredVideo:
    """Register/update a video before its comments are ingested."""
    if not video_id.strip():
        raise ValueError("video_id must not be empty")
    video = session.get(MonitoredVideo, video_id)
    if video is None:
        video = MonitoredVideo(
            video_id=video_id,
            title=title,
            published_at=published_at,
            monitoring_started_at=monitoring_started_at,
            monitoring_ends_at=monitoring_ends_at,
        )
        session.add(video)
    else:
        if title is not None:
            video.title = title
        if published_at is not None:
            video.published_at = published_at
        if monitoring_started_at is not None:
            video.monitoring_started_at = monitoring_started_at
        if monitoring_ends_at is not None:
            video.monitoring_ends_at = monitoring_ends_at
        video.updated_at = utc_now()
    session.flush()
    return video


def upsert_discovered_comment(
    session: Session,
    incoming: DiscoveredComment,
    *,
    seen_at: datetime | None = None,
) -> UpsertResult:
    """Persist all discovered comments and detect new or edited text idempotently.

    The caller owns the transaction and commits or rolls it back. A monitored
    video must be registered before ingesting its comments.
    """
    if session.get(MonitoredVideo, incoming.video_id) is None:
        raise ValueError(f"video {incoming.video_id!r} is not registered for monitoring")

    now = seen_at or utc_now()
    digest = text_hash(incoming.text)
    record = session.get(CommentRecord, incoming.comment_id)

    if record is None:
        record = CommentRecord(
            comment_id=incoming.comment_id,
            video_id=incoming.video_id,
            parent_comment_id=incoming.parent_comment_id,
            author_channel_id=incoming.author_channel_id,
            author_display_name=incoming.author_display_name,
            text=incoming.text,
            content_hash=digest,
            text_version=1,
            published_at=incoming.published_at,
            updated_at_source=incoming.updated_at_source,
            first_seen_at=now,
            last_seen_at=now,
            state="DISCOVERED",
        )
        session.add(record)
        session.flush()
        session.add(CommentTextVersion(
            comment_id=incoming.comment_id,
            version_number=1,
            text=incoming.text,
            content_hash=digest,
            observed_at=now,
        ))
        session.flush()
        return UpsertResult(UpsertOutcome.NEW, incoming.comment_id, 1, True, False)

    if record.video_id != incoming.video_id:
        raise ValueError("comment_id already exists under a different video_id")

    record.last_seen_at = now
    record.parent_comment_id = incoming.parent_comment_id or record.parent_comment_id
    record.author_channel_id = incoming.author_channel_id or record.author_channel_id
    record.author_display_name = incoming.author_display_name or record.author_display_name
    record.published_at = incoming.published_at or record.published_at
    record.updated_at_source = incoming.updated_at_source or record.updated_at_source

    if digest == record.content_hash:
        session.flush()
        # DISCOVERED/FAILED_RETRYABLE rows remain discoverable by the queue; this
        # result says this sweep did not discover a new or edited comment.
        return UpsertResult(UpsertOutcome.UNCHANGED, record.comment_id, record.text_version, False, False)

    new_version = record.text_version + 1
    session.add(CommentTextVersion(
        comment_id=record.comment_id,
        version_number=new_version,
        text=incoming.text,
        content_hash=digest,
        observed_at=now,
    ))
    record.text = incoming.text
    record.content_hash = digest
    record.text_version = new_version
    record.state = "EDITED_REASSESSMENT"
    record.retry_count = 0
    record.last_error = None
    # Preserve old versions in CommentTextVersion, but clear current assessment
    # fields so the edited version cannot inherit a stale decision.
    record.ai_target = None
    record.ai_sentiment = None
    record.ai_conduct_type = None
    record.ai_confidence = None
    record.ai_rationale = None
    record.cleanup_decision = None
    record.cleanup_reason_code = None
    record.cleanup_explanation = None
    record.classification_model = None
    record.classified_at = None
    session.flush()
    return UpsertResult(UpsertOutcome.EDITED, record.comment_id, new_version, True, True)


def pending_classification_ids(
    session: Session,
    *,
    limit: int = 100,
    include_edited: bool = False,
) -> list[str]:
    """Return IDs eligible for an AI run; edited versions are separate by default."""
    if limit < 1:
        raise ValueError("limit must be positive")
    states = ["DISCOVERED", "FAILED_RETRYABLE"]
    if include_edited:
        states.append("EDITED_REASSESSMENT")
    statement = (
        select(CommentRecord.comment_id)
        .where(CommentRecord.state.in_(states))
        .order_by(CommentRecord.first_seen_at, CommentRecord.comment_id)
        .limit(limit)
    )
    return list(session.scalars(statement))


def mark_comment_classified(
    session: Session,
    assessment: CommentAssessment,
    result: CleanupResult,
    *,
    model_id: str,
) -> None:
    """Persist the classifier output; does not delete or publish anything."""
    record = session.get(CommentRecord, assessment.comment_id)
    if record is None:
        raise ValueError(f"unknown comment_id {assessment.comment_id!r}")
    if record.video_id != assessment.video_id:
        raise ValueError("assessment video_id does not match stored comment")

    record.ai_target = assessment.target.value
    record.ai_sentiment = assessment.sentiment.value
    record.ai_conduct_type = assessment.conduct_type.value
    record.ai_confidence = assessment.confidence
    record.ai_rationale = assessment.rationale
    record.cleanup_decision = result.decision.value
    record.cleanup_reason_code = result.reason_code
    record.cleanup_explanation = result.explanation
    record.classification_model = model_id
    record.classified_at = utc_now()
    record.last_error = None
    record.state = "REVIEW_REQUIRED" if result.requires_owner_approval else "AI_CLASSIFIED"
    session.flush()
