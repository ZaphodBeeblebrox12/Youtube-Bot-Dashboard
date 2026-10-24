"""Persistence and validation for owner-created YouTube live polls.

Drafts can be created offline. Only explicit launch/end operations call YouTube.
"""
from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from youtube_bot_dashboard.storage import Base, utc_now

POLL_DRAFT = "DRAFT"
POLL_ACTIVE = "ACTIVE"
POLL_CLOSED = "CLOSED"
POLL_STATUSES = {POLL_DRAFT, POLL_ACTIVE, POLL_CLOSED}
MAX_QUESTION_LENGTH = 160
MAX_OPTION_LENGTH = 80


class LivePollRecord(Base):
    __tablename__ = "live_polls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("live_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    live_chat_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    broadcast_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    question_text: Mapped[str] = mapped_column(String(MAX_QUESTION_LENGTH), nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=POLL_DRAFT, nullable=False, index=True)
    youtube_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    launch_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


def validate_poll_content(question_text: str, options: list[str] | tuple[str, ...]) -> tuple[str, list[str]]:
    question = " ".join((question_text or "").split()).strip()
    if not question:
        raise ValueError("Enter a poll question.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ValueError(f"Keep the question to {MAX_QUESTION_LENGTH} characters or fewer.")
    cleaned = [" ".join((option or "").split()).strip() for option in options]
    cleaned = [option for option in cleaned if option]
    if not 2 <= len(cleaned) <= 4:
        raise ValueError("A YouTube live poll needs 2 to 4 non-empty options.")
    if any(len(option) > MAX_OPTION_LENGTH for option in cleaned):
        raise ValueError(f"Keep each option to {MAX_OPTION_LENGTH} characters or fewer.")
    folded = [option.casefold() for option in cleaned]
    if len(set(folded)) != len(folded):
        raise ValueError("Poll options must be different.")
    return question, cleaned


def create_poll_draft(
    session: Session,
    *,
    question_text: str,
    options: list[str] | tuple[str, ...],
) -> LivePollRecord:
    """Create an offline-safe draft; it does not call YouTube."""
    question, cleaned_options = validate_poll_content(question_text, options)
    record = LivePollRecord(
        question_text=question,
        options_json=json.dumps(cleaned_options, ensure_ascii=False),
        status=POLL_DRAFT,
    )
    session.add(record)
    session.flush()
    return record


def poll_options(record: LivePollRecord) -> list[str]:
    try:
        options = json.loads(record.options_json)
    except (TypeError, json.JSONDecodeError):
        return []
    return options if isinstance(options, list) and all(isinstance(x, str) for x in options) else []


def active_poll_for_chat(session: Session, live_chat_id: str) -> LivePollRecord | None:
    return session.scalar(select(LivePollRecord).where(
        LivePollRecord.live_chat_id == live_chat_id,
        LivePollRecord.status == POLL_ACTIVE,
    ).order_by(LivePollRecord.launched_at.desc()).limit(1))


def mark_poll_launched(
    session: Session,
    *,
    poll_id: int,
    session_id: str,
    live_chat_id: str,
    broadcast_id: str | None,
    youtube_message_id: str,
    response: dict,
    launched_at: datetime | None = None,
) -> LivePollRecord:
    record = session.get(LivePollRecord, poll_id)
    if record is None:
        raise ValueError(f"Unknown poll draft {poll_id}.")
    if record.status != POLL_DRAFT:
        raise ValueError(f"Only a draft can be launched; this poll is {record.status.lower()}.")
    if not youtube_message_id:
        raise ValueError("YouTube did not return a poll message ID.")
    existing = active_poll_for_chat(session, live_chat_id)
    if existing is not None:
        raise ValueError("Another poll is already active in this live chat.")
    record.session_id = session_id
    record.live_chat_id = live_chat_id
    record.broadcast_id = broadcast_id
    record.youtube_message_id = youtube_message_id
    record.launch_response_json = json.dumps(response, ensure_ascii=False)
    record.status = POLL_ACTIVE
    record.launched_at = launched_at or utc_now()
    record.last_error = None
    session.flush()
    return record


def extract_poll_results(response: dict) -> dict:
    """Extract the owner-visible result tallies from a YouTube transition response."""
    snippet = response.get("snippet") if isinstance(response, dict) else None
    poll_details = snippet.get("pollDetails", {}) if isinstance(snippet, dict) else {}
    metadata = poll_details.get("metadata", {}) if isinstance(poll_details, dict) else {}
    options = metadata.get("options", []) if isinstance(metadata, dict) else []
    result_options = []
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        tally = option.get("tally")
        result_options.append({
            "option_text": str(option.get("optionText", "")),
            "tally": str(tally) if tally is not None else None,
        })
    return {
        "question_text": metadata.get("questionText") if isinstance(metadata, dict) else None,
        "status": metadata.get("status") if isinstance(metadata, dict) else None,
        "options": result_options,
        "raw_response": response,
        "collected_at": utc_now().isoformat(),
    }


def mark_poll_closed(
    session: Session,
    *,
    poll_id: int,
    response: dict,
    closed_at: datetime | None = None,
) -> LivePollRecord:
    record = session.get(LivePollRecord, poll_id)
    if record is None:
        raise ValueError(f"Unknown poll {poll_id}.")
    if record.status != POLL_ACTIVE:
        raise ValueError(f"Only an active poll can be ended; this poll is {record.status.lower()}.")
    record.results_json = json.dumps(extract_poll_results(response), ensure_ascii=False)
    record.status = POLL_CLOSED
    record.closed_at = closed_at or utc_now()
    record.last_error = None
    session.flush()
    return record
