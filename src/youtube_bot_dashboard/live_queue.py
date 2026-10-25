"""Persistent live-chat batching, per-message tracking, and provider routing.

This module does not make network calls. A worker prepares a due batch, marks it
sent, invokes the selected provider adapter, then stores the validated response.
Session Shield excludes a blacklisted viewer from both AI pipelines for the rest of the live session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import math
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from youtube_bot_dashboard.storage import Base, utc_now, text_hash
from youtube_bot_dashboard.community_memory import record_viewer_activity


LIVE_MESSAGE_TEXT_EXPIRED = "[expired by retention policy]"


class LivePipeline(StrEnum):
    MODERATION = "MODERATION"
    REPLY_FINDER = "REPLY_FINDER"


class ProviderName(StrEnum):
    GEMINI = "GEMINI"
    QWEN = "QWEN"
    DEEPSEEK = "DEEPSEEK"
    KIMI = "KIMI"


class ProviderStrategy(StrEnum):
    ROUND_ROBIN = "ROUND_ROBIN"
    GEMINI_ONLY = "GEMINI_ONLY"
    QWEN_ONLY = "QWEN_ONLY"
    DEEPSEEK_ONLY = "DEEPSEEK_ONLY"
    KIMI_ONLY = "KIMI_ONLY"


class TaskState(StrEnum):
    PENDING = "PENDING"
    BATCHED = "BATCHED"
    SENT = "SENT"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    EXCLUDED_SHIELDED = "EXCLUDED_SHIELDED"
    EXCLUDED_RETENTION = "EXCLUDED_RETENTION"
    ANSWERED = "ANSWERED"
    DISMISSED = "DISMISSED"
    SAVED_FOR_LATER = "SAVED_FOR_LATER"


class BatchState(StrEnum):
    PREPARED = "PREPARED"
    SENT = "SENT"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class LiveQueueConfig:
    batch_delay_seconds: float = 2.0
    minimum_request_interval_seconds: float = 1.0
    max_messages_per_batch: int = 100
    max_input_tokens: int = 12_000
    system_prompt_reserve_tokens: int = 3_000
    session_shield_confidence_threshold: float = 0.90
    provider_strategy: ProviderStrategy = ProviderStrategy.ROUND_ROBIN
    enabled_providers: tuple[ProviderName, ...] = (ProviderName.GEMINI, ProviderName.QWEN)
    max_attempts: int = 3
    retry_base_seconds: int = 2

    def __post_init__(self) -> None:
        if not math.isfinite(self.batch_delay_seconds) or not 0.25 <= self.batch_delay_seconds <= 30:
            raise ValueError("batch_delay_seconds must be between 0.25 and 30")
        if not math.isfinite(self.minimum_request_interval_seconds) or not 0.1 <= self.minimum_request_interval_seconds <= 30:
            raise ValueError("minimum_request_interval_seconds must be between 0.1 and 30")
        if not 1 <= self.max_messages_per_batch <= 500:
            raise ValueError("max_messages_per_batch must be between 1 and 500")
        if not 1_000 <= self.max_input_tokens <= 100_000:
            raise ValueError("max_input_tokens must be between 1000 and 100000")
        if not 0 <= self.system_prompt_reserve_tokens < self.max_input_tokens - 100:
            raise ValueError("system_prompt_reserve_tokens must leave at least 100 input tokens for messages")
        if not math.isfinite(self.session_shield_confidence_threshold) or not 0 <= self.session_shield_confidence_threshold <= 1:
            raise ValueError("session_shield_confidence_threshold must be between 0 and 1")
        if self.max_attempts < 1 or self.retry_base_seconds < 1:
            raise ValueError("retry settings must be positive")
        if not self.enabled_providers:
            raise ValueError("at least one AI provider must be enabled")
        if len(set(self.enabled_providers)) != len(self.enabled_providers):
            raise ValueError("enabled_providers cannot contain duplicates")
        fixed_provider = {
            ProviderStrategy.GEMINI_ONLY: ProviderName.GEMINI,
            ProviderStrategy.QWEN_ONLY: ProviderName.QWEN,
            ProviderStrategy.DEEPSEEK_ONLY: ProviderName.DEEPSEEK,
            ProviderStrategy.KIMI_ONLY: ProviderName.KIMI,
        }.get(self.provider_strategy)
        if fixed_provider is not None and fixed_provider not in self.enabled_providers:
            raise ValueError(f"{self.provider_strategy.value} requires {fixed_provider.value} to be enabled")


class LiveSessionRecord(Base):
    __tablename__ = "live_sessions"

    session_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    live_chat_id: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    broadcast_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(24), default="JSON_INGEST", nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE", nullable=False, index=True)


class SessionShieldRecord(Base):
    __tablename__ = "session_shield_users"
    __table_args__ = (
        UniqueConstraint("session_id", "user_channel_id", name="uq_shield_session_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.session_id", ondelete="CASCADE"), index=True, nullable=False)
    user_channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="AI", nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LiveMessageRecord(Base):
    __tablename__ = "live_messages"

    message_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.session_id", ondelete="CASCADE"), index=True, nullable=False)
    user_channel_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(300), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_super_chat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    super_chat_amount_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    super_chat_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    super_chat_amount_display: Mapped[str | None] = mapped_column(String(64), nullable=True)
    super_chat_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class LiveAIRequestBatch(Base):
    __tablename__ = "live_ai_request_batches"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.session_id", ondelete="CASCADE"), index=True, nullable=False)
    pipeline: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=BatchState.PREPARED.value, nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LiveMessageTask(Base):
    __tablename__ = "live_message_tasks"
    __table_args__ = (
        UniqueConstraint("message_id", "pipeline", "content_hash", name="uq_live_message_pipeline_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("live_messages.message_id", ondelete="CASCADE"), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("live_sessions.session_id", ondelete="CASCADE"), index=True, nullable=False)
    pipeline: Mapped[str] = mapped_column(String(24), index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default=TaskState.PENDING.value, nullable=False, index=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("live_ai_request_batches.batch_id", ondelete="SET NULL"), nullable=True, index=True)
    provider: Mapped[str | None] = mapped_column(String(24), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSettingRecord(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ProviderRouteState(Base):
    __tablename__ = "provider_route_state"

    route_key: Mapped[str] = mapped_column(String(60), primary_key=True)
    next_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


@dataclass(frozen=True, slots=True)
class IncomingLiveMessage:
    message_id: str
    session_id: str
    text: str
    user_channel_id: str | None = None
    username: str | None = None
    is_member: bool | None = None
    published_at: datetime | None = None
    is_super_chat: bool = False
    super_chat_amount_micros: int | None = None
    super_chat_currency: str | None = None
    super_chat_amount_display: str | None = None
    super_chat_tier: int | None = None

    def __post_init__(self) -> None:
        if not self.message_id.strip():
            raise ValueError("message_id must not be empty")
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")
        if self.super_chat_amount_micros is not None and (
            isinstance(self.super_chat_amount_micros, bool) or self.super_chat_amount_micros < 0
        ):
            raise ValueError("super_chat_amount_micros must be a non-negative integer")
        if self.super_chat_currency is not None and len(self.super_chat_currency) > 3:
            raise ValueError("super_chat_currency must be at most three characters")
        if self.super_chat_amount_display is not None and len(self.super_chat_amount_display) > 64:
            raise ValueError("super_chat_amount_display must be at most 64 characters")
        if self.super_chat_tier is not None and (
            isinstance(self.super_chat_tier, bool) or self.super_chat_tier < 0
        ):
            raise ValueError("super_chat_tier must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class IngestResult:
    created: bool
    message_id: str
    moderation_task_id: int | None
    reply_task_id: int | None
    reply_excluded_by_shield: bool


@dataclass(frozen=True, slots=True)
class PreparedLiveBatch:
    batch_id: str
    session_id: str
    pipeline: LivePipeline
    provider: ProviderName
    message_ids: tuple[str, ...]
    payload: dict


def get_live_queue_config(session: Session) -> LiveQueueConfig:
    """Load dashboard-editable settings, defaulting to a 2-second live window."""
    def get(key: str, default: str) -> str:
        row = session.get(AppSettingRecord, key)
        return row.value if row else default

    providers_raw = json.loads(get("live_enabled_providers", '["GEMINI","QWEN"]'))
    strategy_raw = get("live_provider_strategy", ProviderStrategy.ROUND_ROBIN.value)
    return LiveQueueConfig(
        batch_delay_seconds=float(get("live_batch_delay_seconds", "2.0")),
        minimum_request_interval_seconds=float(get("live_minimum_request_interval_seconds", "1.0")),
        max_messages_per_batch=int(get("live_max_messages_per_batch", "100")),
        max_input_tokens=int(get("live_max_input_tokens", "12000")),
        system_prompt_reserve_tokens=int(get("live_system_prompt_reserve_tokens", "3000")),
        session_shield_confidence_threshold=float(get("live_session_shield_confidence_threshold", "0.90")),
        provider_strategy=ProviderStrategy(strategy_raw),
        enabled_providers=tuple(ProviderName(p) for p in providers_raw),
        max_attempts=int(get("live_max_attempts", "3")),
        retry_base_seconds=int(get("live_retry_base_seconds", "2")),
    )


def update_live_queue_config(session: Session, config: LiveQueueConfig) -> None:
    """Persist settings for the future dashboard settings form."""
    values = {
        "live_batch_delay_seconds": str(config.batch_delay_seconds),
        "live_minimum_request_interval_seconds": str(config.minimum_request_interval_seconds),
        "live_max_messages_per_batch": str(config.max_messages_per_batch),
        "live_max_input_tokens": str(config.max_input_tokens),
        "live_system_prompt_reserve_tokens": str(config.system_prompt_reserve_tokens),
        "live_session_shield_confidence_threshold": str(config.session_shield_confidence_threshold),
        "live_provider_strategy": config.provider_strategy.value,
        "live_enabled_providers": json.dumps([p.value for p in config.enabled_providers]),
        "live_max_attempts": str(config.max_attempts),
        "live_retry_base_seconds": str(config.retry_base_seconds),
    }
    for key, value in values.items():
        row = session.get(AppSettingRecord, key)
        if row is None:
            session.add(AppSettingRecord(key=key, value=value))
        else:
            row.value = value
            row.updated_at = utc_now()
    session.flush()


def register_live_session(
    session: Session,
    *,
    session_id: str,
    live_chat_id: str | None = None,
    broadcast_id: str | None = None,
    source: str | None = None,
    started_at: datetime | None = None,
) -> LiveSessionRecord:
    if not session_id.strip():
        raise ValueError("session_id must not be empty")
    record = session.get(LiveSessionRecord, session_id)
    if record is None:
        record = LiveSessionRecord(
            session_id=session_id,
            live_chat_id=live_chat_id,
            broadcast_id=broadcast_id,
            source=source or "JSON_INGEST",
            started_at=started_at or utc_now(),
            status="ACTIVE",
        )
        session.add(record)
    else:
        if live_chat_id is not None:
            record.live_chat_id = live_chat_id
        if broadcast_id is not None:
            record.broadcast_id = broadcast_id
        if source is not None:
            record.source = source
        if record.status != "ACTIVE":
            raise ValueError("cannot receive new messages for a closed session")
    session.flush()
    return record


def end_live_session(session: Session, session_id: str, *, ended_at: datetime | None = None) -> None:
    record = session.get(LiveSessionRecord, session_id)
    if record is None:
        raise ValueError(f"unknown session_id {session_id!r}")
    record.status = "ENDED"
    record.ended_at = ended_at or utc_now()
    session.flush()


def is_user_shielded(session: Session, *, session_id: str, user_channel_id: str | None) -> bool:
    if not user_channel_id:
        return False
    statement = select(SessionShieldRecord.id).where(
        SessionShieldRecord.session_id == session_id,
        SessionShieldRecord.user_channel_id == user_channel_id,
        SessionShieldRecord.active.is_(True),
    )
    return session.scalar(statement) is not None


def shield_user_for_session(
    session: Session,
    *,
    session_id: str,
    user_channel_id: str,
    reason: str,
    source: str = "AI",
    confidence: float | None = None,
    display_name: str | None = None,
    created_by: str | None = None,
) -> SessionShieldRecord:
    """Exclude a shielded user from both AI pipelines; never call YouTube enforcement APIs."""
    if not user_channel_id.strip():
        raise ValueError("user_channel_id must not be empty")
    if not reason.strip():
        raise ValueError("reason must not be empty")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if session.get(LiveSessionRecord, session_id) is None:
        raise ValueError(f"unknown session_id {session_id!r}")

    existing = session.scalar(select(SessionShieldRecord).where(
        SessionShieldRecord.session_id == session_id,
        SessionShieldRecord.user_channel_id == user_channel_id,
    ))
    if existing is None:
        existing = SessionShieldRecord(
            session_id=session_id,
            user_channel_id=user_channel_id,
            display_name=display_name,
            reason=reason,
            source=source,
            confidence=confidence,
            created_by=created_by,
            active=True,
        )
        session.add(existing)
    else:
        existing.active = True
        existing.reason = reason
        existing.source = source
        existing.confidence = confidence
        existing.display_name = display_name or existing.display_name
        existing.created_by = created_by or existing.created_by
    session.flush()

    # Exclude unprocessed messages from both AI pipelines. Keep completed moderation
    # results as audit records for the message that triggered the shield, but hide
    # completed Reply Finder cards. Scrub prepared-but-unsent requests too.
    tasks = list(session.scalars(select(LiveMessageTask).where(
        LiveMessageTask.session_id == session_id,
        LiveMessageTask.pipeline.in_([
            LivePipeline.MODERATION.value,
            LivePipeline.REPLY_FINDER.value,
        ]),
        LiveMessageTask.state.in_([
            TaskState.PENDING.value,
            TaskState.BATCHED.value,
            TaskState.SENT.value,
            TaskState.FAILED_RETRYABLE.value,
            TaskState.COMPLETED.value,
        ]),
    )))
    for task in tasks:
        message = session.get(LiveMessageRecord, task.message_id)
        if not message or message.user_channel_id != user_channel_id:
            continue
        if task.pipeline == LivePipeline.MODERATION.value and task.state == TaskState.COMPLETED.value:
            continue

        batch = session.get(LiveAIRequestBatch, task.batch_id) if task.batch_id else None
        if batch and batch.status == BatchState.PREPARED.value:
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

        # Already-dispatched provider calls cannot be recalled; ignore their
        # result and ensure no later request is dispatched for this user.
        task.state = TaskState.EXCLUDED_SHIELDED.value
        task.batch_id = None
        task.provider = None
        task.result_json = None
        task.last_error = "Excluded by Session Shield from all AI pipelines until this session ends."
    session.flush()
    return existing


def unshield_user_for_session(
    session: Session,
    *,
    session_id: str,
    user_channel_id: str,
) -> bool:
    record = session.scalar(select(SessionShieldRecord).where(
        SessionShieldRecord.session_id == session_id,
        SessionShieldRecord.user_channel_id == user_channel_id,
        SessionShieldRecord.active.is_(True),
    ))
    if record is None:
        return False
    record.active = False
    session.flush()
    return True


def ingest_live_message(
    session: Session,
    incoming: IncomingLiveMessage,
    *,
    received_at: datetime | None = None,
) -> IngestResult:
    """Persist once and enqueue distinct moderation/reply tasks."""
    live_session = session.get(LiveSessionRecord, incoming.session_id)
    if live_session is None:
        raise ValueError(f"unknown session_id {incoming.session_id!r}")
    if live_session.status != "ACTIVE":
        raise ValueError("cannot ingest messages for an ended session")

    now = received_at or utc_now()
    digest = text_hash(incoming.text)
    existing = session.get(LiveMessageRecord, incoming.message_id)
    if existing is not None:
        if existing.session_id != incoming.session_id:
            raise ValueError("message_id already exists under a different session")
        if existing.text != LIVE_MESSAGE_TEXT_EXPIRED and existing.content_hash != digest:
            # Live messages should be immutable. Update the stored text to
            # prevent stale content being submitted on a still-pending task.
            # An expired body must never be restored by listener replay/edit.
            existing.text = incoming.text
            existing.content_hash = digest
        if incoming.is_super_chat:
            existing.is_super_chat = True
        if incoming.super_chat_amount_micros is not None:
            existing.super_chat_amount_micros = incoming.super_chat_amount_micros
        if incoming.super_chat_currency is not None:
            existing.super_chat_currency = incoming.super_chat_currency
        if incoming.super_chat_amount_display is not None:
            existing.super_chat_amount_display = incoming.super_chat_amount_display
        if incoming.super_chat_tier is not None:
            existing.super_chat_tier = incoming.super_chat_tier
        existing.last_seen_at = now
        session.flush()
        tasks = list(session.scalars(select(LiveMessageTask).where(
            LiveMessageTask.message_id == incoming.message_id,
        )))
        mod_task = next((t for t in tasks if t.pipeline == LivePipeline.MODERATION.value), None)
        reply_task = next((t for t in tasks if t.pipeline == LivePipeline.REPLY_FINDER.value), None)
        return IngestResult(False, incoming.message_id, mod_task.id if mod_task else None,
                            reply_task.id if reply_task else None,
                            reply_task is not None and reply_task.state == TaskState.EXCLUDED_SHIELDED.value)

    message = LiveMessageRecord(
        message_id=incoming.message_id,
        session_id=incoming.session_id,
        user_channel_id=incoming.user_channel_id,
        username=incoming.username,
        text=incoming.text,
        is_super_chat=incoming.is_super_chat,
        super_chat_amount_micros=incoming.super_chat_amount_micros,
        super_chat_currency=incoming.super_chat_currency,
        super_chat_amount_display=incoming.super_chat_amount_display,
        super_chat_tier=incoming.super_chat_tier,
        content_hash=digest,
        published_at=incoming.published_at,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(message)
    session.flush()
    record_viewer_activity(
        session,
        user_channel_id=incoming.user_channel_id,
        display_name=incoming.username,
        session_id=incoming.session_id,
        is_member=incoming.is_member,
        observed_at=now,
    )

    excluded = is_user_shielded(
        session, session_id=incoming.session_id, user_channel_id=incoming.user_channel_id
    )
    excluded_reason = (
        "Excluded by Session Shield before any AI request; no message text sent to a provider."
        if excluded else None
    )
    mod_task = LiveMessageTask(
        message_id=incoming.message_id,
        session_id=incoming.session_id,
        pipeline=LivePipeline.MODERATION.value,
        content_hash=digest,
        state=TaskState.EXCLUDED_SHIELDED.value if excluded else TaskState.PENDING.value,
        queued_at=now,
        last_error=excluded_reason,
    )
    session.add(mod_task)

    reply_task = LiveMessageTask(
        message_id=incoming.message_id,
        session_id=incoming.session_id,
        pipeline=LivePipeline.REPLY_FINDER.value,
        content_hash=digest,
        state=TaskState.EXCLUDED_SHIELDED.value if excluded else TaskState.PENDING.value,
        queued_at=now,
        last_error=excluded_reason,
    )
    session.add(reply_task)
    session.flush()
    return IngestResult(True, incoming.message_id, mod_task.id, reply_task.id, excluded)


def _as_utc_aware(value: datetime) -> datetime:
    """Normalize SQLite's naive datetime round-trip and aware PostgreSQL values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def should_flush_batch(
    *,
    pending_count: int,
    oldest_queued_at: datetime | None,
    now: datetime,
    config: LiveQueueConfig,
) -> bool:
    """Flush when full or when the oldest message reaches the configured delay."""
    if pending_count <= 0:
        return False
    if pending_count >= config.max_messages_per_batch:
        return True
    if oldest_queued_at is None:
        return False
    elapsed = (_as_utc_aware(now) - _as_utc_aware(oldest_queued_at)).total_seconds()
    return elapsed >= config.batch_delay_seconds


def _select_next_provider(
    session: Session,
    *,
    pipeline: LivePipeline,
    config: LiveQueueConfig,
) -> ProviderName:
    fixed_provider = {
        ProviderStrategy.GEMINI_ONLY: ProviderName.GEMINI,
        ProviderStrategy.QWEN_ONLY: ProviderName.QWEN,
        ProviderStrategy.DEEPSEEK_ONLY: ProviderName.DEEPSEEK,
        ProviderStrategy.KIMI_ONLY: ProviderName.KIMI,
    }.get(config.provider_strategy)
    if fixed_provider is not None:
        return fixed_provider
    route_key = f"live:{pipeline.value}"
    state = session.get(ProviderRouteState, route_key, with_for_update=True)
    if state is None:
        state = ProviderRouteState(route_key=route_key, next_index=0)
        session.add(state)
        session.flush()
    index = state.next_index % len(config.enabled_providers)
    provider = config.enabled_providers[index]
    state.next_index = (index + 1) % len(config.enabled_providers)
    state.updated_at = utc_now()
    session.flush()
    return provider


def prepare_due_batch(
    session: Session,
    *,
    session_id: str,
    pipeline: LivePipeline,
    config: LiveQueueConfig,
    now: datetime | None = None,
) -> PreparedLiveBatch | None:
    """Claim a due batch and persist the exact provider/payload before network IO.

    A worker should commit this transaction, mark the batch SENT, then call the
    selected provider. For production multi-worker use, add a PostgreSQL
    SKIP LOCKED claim transaction/migration before running multiple consumers.
    """
    timestamp = now or utc_now()

    tasks = list(session.scalars(select(LiveMessageTask)
        .join(LiveMessageRecord, LiveMessageRecord.message_id == LiveMessageTask.message_id)
        .where(
            LiveMessageTask.session_id == session_id,
            LiveMessageTask.pipeline == pipeline.value,
            LiveMessageTask.state.in_([TaskState.PENDING.value, TaskState.FAILED_RETRYABLE.value]),
            (LiveMessageTask.next_attempt_at.is_(None)) | (LiveMessageTask.next_attempt_at <= timestamp),
        )
        .order_by(
            LiveMessageRecord.is_super_chat.desc(),
            LiveMessageTask.queued_at,
            LiveMessageTask.id,
        )
        .limit(config.max_messages_per_batch)))

    # Defensively apply Session Shield to both pipelines at request preparation.
    eligible = []
    for task in tasks:
        message = session.get(LiveMessageRecord, task.message_id)
        if message and is_user_shielded(session, session_id=session_id, user_channel_id=message.user_channel_id):
            task.state = TaskState.EXCLUDED_SHIELDED.value
            task.batch_id = None
            task.provider = None
            task.last_error = "Excluded by Session Shield before any AI request."
        else:
            eligible.append(task)
    tasks = eligible
    session.flush()

    if not tasks:
        return None

    # A pending Super Chat skips the normal collection delay but still needs
    # the same AI safety check before appearing in the visible feed.
    first_message = session.get(LiveMessageRecord, tasks[0].message_id)
    has_super_chat = bool(first_message and first_message.is_super_chat)
    if not has_super_chat and not should_flush_batch(
        pending_count=len(tasks),
        oldest_queued_at=min(task.queued_at for task in tasks),
        now=timestamp,
        config=config,
    ):
        return None

    messages = []
    claimed_tasks = []
    pending_payload = {
        "session_id": session_id,
        "pipeline": pipeline.value,
        "messages": [],
    }
    payload_budget = config.max_input_tokens - config.system_prompt_reserve_tokens
    for task in tasks:
        message = session.get(LiveMessageRecord, task.message_id)
        if message is None:
            task.state = TaskState.FAILED_FINAL.value
            task.last_error = "Live message record is missing."
            continue
        item = {
            "message_id": message.message_id,
            "user_id": message.user_channel_id,
            "username": message.username,
            "published_at": message.published_at.isoformat() if message.published_at else None,
            "text": message.text,
        }
        if pipeline is LivePipeline.REPLY_FINDER:
            item.update({
                "is_super_chat": bool(message.is_super_chat),
                "super_chat_amount_display": message.super_chat_amount_display,
                "super_chat_currency": message.super_chat_currency,
                "super_chat_tier": message.super_chat_tier,
            })
        candidate_payload = {**pending_payload, "messages": [*messages, item]}
        estimated_payload_tokens = estimate_message_tokens(candidate_payload)
        if estimated_payload_tokens > payload_budget:
            if not claimed_tasks:
                # Do not silently drop/ truncate a single message that cannot fit.
                # Mark it as final failure so an operator can see the error.
                task.state = TaskState.FAILED_FINAL.value
                task.last_error = (
                    f"Single message exceeds estimated payload budget of {payload_budget} tokens; "
                    "review/truncate manually or adjust the model/budget."
                )
            # Keep this task and all later tasks pending for the next batch.
            break
        messages.append(item)
        claimed_tasks.append(task)

    if not claimed_tasks:
        session.flush()
        return None

    provider = _select_next_provider(session, pipeline=pipeline, config=config)
    batch_id = str(uuid.uuid4())
    payload = {
        "session_id": session_id,
        "pipeline": pipeline.value,
        "messages": messages,
    }
    batch = LiveAIRequestBatch(
        batch_id=batch_id,
        session_id=session_id,
        pipeline=pipeline.value,
        provider=provider.value,
        status=BatchState.PREPARED.value,
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        created_at=timestamp,
    )
    session.add(batch)
    session.flush()
    for task in claimed_tasks:
        task.state = TaskState.BATCHED.value
        task.batch_id = batch_id
        task.provider = provider.value
        task.last_error = None
    session.flush()
    return PreparedLiveBatch(
        batch_id=batch_id,
        session_id=session_id,
        pipeline=pipeline,
        provider=provider,
        message_ids=tuple(m["message_id"] for m in messages),
        payload=payload,
    )


def mark_live_batch_sent(
    session: Session,
    batch_id: str,
    *,
    model_id: str,
    sent_at: datetime | None = None,
) -> dict:
    """Transition PREPARED -> SENT and return the current persisted payload.

    Always send this returned payload, not the possibly stale snapshot originally
    returned by prepare_due_batch: Session Shield may have removed a user while
    the batch was waiting to be sent.
    """
    batch = session.get(LiveAIRequestBatch, batch_id)
    if batch is None:
        raise ValueError(f"unknown batch_id {batch_id!r}")
    if batch.status != BatchState.PREPARED.value:
        raise ValueError(f"cannot send batch in state {batch.status}")
    payload = json.loads(batch.payload_json)
    if not payload.get("messages"):
        batch.status = BatchState.CANCELLED.value
        session.flush()
        raise ValueError("cannot send an empty batch; it was cancelled or shielded")
    now = sent_at or utc_now()
    batch.status = BatchState.SENT.value
    batch.model_id = model_id
    batch.sent_at = now
    tasks = list(session.scalars(select(LiveMessageTask).where(
        LiveMessageTask.batch_id == batch_id,
        LiveMessageTask.state == TaskState.BATCHED.value,
    )))
    payload_ids = {item.get("message_id") for item in payload["messages"]}
    for task in tasks:
        if task.message_id not in payload_ids:
            task.state = TaskState.EXCLUDED_SHIELDED.value
            task.batch_id = None
            task.provider = None
            task.last_error = "Removed from batch before send."
            continue
        task.state = TaskState.SENT.value
        task.attempt_count += 1
        task.sent_at = now
    session.flush()
    return payload


def complete_live_batch(
    session: Session,
    batch_id: str,
    *,
    result_by_message_id: dict[str, dict],
    raw_response: dict,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    completed_at: datetime | None = None,
) -> tuple[int, int]:
    """Persist per-message results; missing results become retryable, not lost."""
    batch = session.get(LiveAIRequestBatch, batch_id)
    if batch is None:
        raise ValueError(f"unknown batch_id {batch_id!r}")
    if batch.status != BatchState.SENT.value:
        raise ValueError(f"cannot complete batch in state {batch.status}")
    now = completed_at or utc_now()
    batch.response_json = json.dumps(raw_response, ensure_ascii=False)
    batch.input_tokens = input_tokens
    batch.output_tokens = output_tokens
    batch.cached_input_tokens = cached_input_tokens
    batch.cache_write_tokens = cache_write_tokens
    batch.completed_at = now

    tasks = list(session.scalars(select(LiveMessageTask).where(
        LiveMessageTask.batch_id == batch_id,
        LiveMessageTask.state == TaskState.SENT.value,
    )))
    completed = 0
    missing = 0
    is_reply_finder = batch.pipeline == LivePipeline.REPLY_FINDER.value
    for task in tasks:
        item = result_by_message_id.get(task.message_id)
        if isinstance(item, dict):
            task.result_json = json.dumps(item, ensure_ascii=False)
            task.state = TaskState.COMPLETED.value
            task.completed_at = now
            task.last_error = None
            completed += 1
        elif is_reply_finder:
            # Reply Finder intentionally returns only selected messages.
            # A valid response omitting a supplied ID means "not selected",
            # not a failed classification. The caller must validate the shape
            # and the selected IDs before calling this function.
            task.result_json = json.dumps({
                "message_id": task.message_id,
                "selected": False,
                "reason": "Not selected by Reply Finder.",
            }, ensure_ascii=False)
            task.state = TaskState.COMPLETED.value
            task.completed_at = now
            task.last_error = None
            completed += 1
        else:
            task.state = TaskState.FAILED_RETRYABLE.value
            task.next_attempt_at = now + timedelta(seconds=2 ** max(task.attempt_count - 1, 0))
            task.batch_id = None
            task.last_error = "AI response omitted this message ID; retry only this message."
            missing += 1
    # Extra/unknown IDs are ignored; only original batched IDs can be completed.
    batch.status = BatchState.PARTIAL.value if missing else BatchState.COMPLETED.value
    session.flush()
    return completed, missing


def fail_live_batch(
    session: Session,
    batch_id: str,
    *,
    error: str,
    config: LiveQueueConfig,
    failed_at: datetime | None = None,
) -> tuple[int, int]:
    """Fail a provider request and schedule bounded retries for its items."""
    batch = session.get(LiveAIRequestBatch, batch_id)
    if batch is None:
        raise ValueError(f"unknown batch_id {batch_id!r}")
    if batch.status not in {BatchState.PREPARED.value, BatchState.SENT.value}:
        raise ValueError(f"cannot fail batch in state {batch.status}")
    now = failed_at or utc_now()
    batch.status = BatchState.FAILED.value
    batch.completed_at = now
    batch.last_error = error[:4000]
    tasks = list(session.scalars(select(LiveMessageTask).where(
        LiveMessageTask.batch_id == batch_id,
        LiveMessageTask.state.in_([TaskState.BATCHED.value, TaskState.SENT.value]),
    )))
    retryable = 0
    final = 0
    for task in tasks:
        task.batch_id = None
        task.last_error = error[:4000]
        if task.attempt_count >= config.max_attempts:
            task.state = TaskState.FAILED_FINAL.value
            final += 1
        else:
            task.state = TaskState.FAILED_RETRYABLE.value
            wait_seconds = min(60, config.retry_base_seconds * (2 ** max(task.attempt_count - 1, 0)))
            task.next_attempt_at = now + timedelta(seconds=wait_seconds)
            retryable += 1
    session.flush()
    return retryable, final


def close_session_shield(session: Session, session_id: str, *, ended_at: datetime | None = None) -> None:
    """End the session and close all session-specific Shield exclusions."""
    end_live_session(session, session_id, ended_at=ended_at)
    shields = list(session.scalars(select(SessionShieldRecord).where(
        SessionShieldRecord.session_id == session_id,
        SessionShieldRecord.active.is_(True),
    )))
    for shield in shields:
        shield.active = False
    session.flush()


def estimate_message_tokens(payload: dict) -> int:
    """Conservative rough estimate; use provider tokenizer/count endpoint in production."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # A deliberately conservative heuristic, not a substitute for model token counting.
    return max(1, math.ceil(len(text) / 3.5))
