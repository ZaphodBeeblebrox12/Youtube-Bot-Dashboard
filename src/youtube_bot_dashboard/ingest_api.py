"""Authenticated JSON ingress for a normalized external live-chat source.

This API does not poll YouTube directly. It accepts messages from a trusted
YouTube listener or other source and persists them into the live queue. Set
INGEST_API_KEY to a long random secret and put the service behind HTTPS.
"""

from __future__ import annotations

from functools import lru_cache
import hmac
import os
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv

from youtube_bot_dashboard.live_queue import (
    IncomingLiveMessage,
    LiveSessionRecord,
    close_session_shield,
    ingest_live_message,
    register_live_session,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema

load_dotenv()

app = FastAPI(
    title="YouTube Bot Dashboard Ingest API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)


class IncomingMessageBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: str = Field(min_length=1, max_length=160)
    user_id: str | None = Field(default=None, max_length=120)
    username: str | None = Field(default=None, max_length=300)
    is_member: bool | None = None
    text: str = Field(max_length=10000)
    published_at: datetime | None = None


class LiveIngestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1, max_length=120)
    live_chat_id: str | None = Field(default=None, max_length=160)
    broadcast_id: str | None = Field(default=None, max_length=160)
    session_started_at: datetime | None = None
    messages: list[IncomingMessageBody] = Field(min_length=1, max_length=250)


def _require_ingest_key(x_ingest_key: Annotated[str | None, Header(alias="X-Ingest-Key")] = None) -> None:
    expected = os.getenv("INGEST_API_KEY")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingest API is disabled until INGEST_API_KEY is configured.",
        )
    if not x_ingest_key or not hmac.compare_digest(x_ingest_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ingest key.")


@lru_cache(maxsize=1)
def _database():
    database_url = os.getenv("DATABASE_URL", "sqlite:///./youtube_bot_dashboard.db")
    engine, factory = create_engine_and_sessions(database_url)
    create_schema(engine)
    return engine, factory


@app.get("/health")
def health() -> dict:
    """Liveness only; does not expose database content or secret configuration."""
    return {"status": "ok", "service": "youtube-bot-dashboard-ingest"}


def ingest_payload(factory, body: LiveIngestBody) -> dict:
    """Persist one normalized message batch idempotently. Exposed for tests too."""
    created = 0
    duplicates = 0
    reply_excluded = 0
    try:
        with factory() as session:
            live_session = session.get(LiveSessionRecord, body.session_id)
            if live_session is None:
                register_live_session(
                    session,
                    session_id=body.session_id,
                    live_chat_id=body.live_chat_id,
                    broadcast_id=body.broadcast_id,
                    started_at=body.session_started_at,
                )
            else:
                if live_session.status != "ACTIVE":
                    raise ValueError("Session is already ended; use a new session_id for a new live session.")
                if body.live_chat_id and live_session.live_chat_id not in (None, body.live_chat_id):
                    raise ValueError("session_id is already linked to another live_chat_id.")

            for item in body.messages:
                result = ingest_live_message(
                    session,
                    IncomingLiveMessage(
                        message_id=item.message_id,
                        session_id=body.session_id,
                        text=item.text,
                        user_channel_id=item.user_id,
                        username=item.username,
                        is_member=item.is_member,
                        published_at=item.published_at,
                    ),
                )
                if result.created:
                    created += 1
                else:
                    duplicates += 1
                if result.reply_excluded_by_shield:
                    reply_excluded += 1
            session.commit()
    except ValueError:
        raise
    return {
        "session_id": body.session_id,
        "received": len(body.messages),
        "created": created,
        "duplicates": duplicates,
        "reply_excluded_by_session_shield": reply_excluded,
        "status": "accepted",
    }


@app.post("/api/v1/live/ingest", dependencies=[Depends(_require_ingest_key)])
def ingest_live_batch(body: LiveIngestBody) -> dict:
    """Accept a bounded batch of normalized chat messages from a trusted source."""
    _, factory = _database()
    try:
        return ingest_payload(factory, body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


class EndSessionBody(BaseModel):
    ended_at: datetime | None = None


@app.post("/api/v1/live/sessions/{session_id}/end", dependencies=[Depends(_require_ingest_key)])
def end_live_session_endpoint(session_id: str, body: EndSessionBody | None = None) -> dict:
    """Mark the live session ended and expire its internal Session Shield records."""
    _, factory = _database()
    from youtube_bot_dashboard.live_queue import close_session_shield

    with factory() as session:
        if session.get(LiveSessionRecord, session_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown session_id.")
        close_session_shield(
            session,
            session_id,
            ended_at=body.ended_at if body else None,
        )
        session.commit()
    return {"session_id": session_id, "status": "ended", "session_shield_expired": True}
