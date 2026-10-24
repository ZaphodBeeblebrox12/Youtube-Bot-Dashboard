"""Explicit YouTube live poll actions.

All writes are opt-in calls from the dashboard. No automatic poll is created.
"""
from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import select

from youtube_bot_dashboard.live_polls import (
    POLL_ACTIVE,
    POLL_DRAFT,
    LivePollRecord,
    active_poll_for_chat,
    mark_poll_closed,
    mark_poll_launched,
    poll_options,
)
from youtube_bot_dashboard.live_queue import LiveSessionRecord


def _get_youtube_service():
    from youtube_bot_dashboard.youtube_listener import build_youtube_service, load_youtube_credentials
    return build_youtube_service(load_youtube_credentials())


def launch_poll_on_youtube(
    session_factory,
    *,
    poll_id: int,
    session_id: str,
    youtube_service=None,
    launched_at: datetime | None = None,
) -> dict:
    """Post one saved draft to an active YouTube chat, then persist the returned ID."""
    with session_factory() as session:
        poll = session.get(LivePollRecord, poll_id)
        if poll is None:
            raise ValueError(f"Unknown poll draft {poll_id}.")
        if poll.status != POLL_DRAFT:
            raise ValueError("Only a draft can be launched.")
        live_session = session.get(LiveSessionRecord, session_id)
        if live_session is None or live_session.status != "ACTIVE" or not live_session.live_chat_id:
            raise ValueError("Choose an active live chat before launching a poll.")
        if active_poll_for_chat(session, live_session.live_chat_id) is not None:
            raise ValueError("A poll is already active in this live chat. End it before launching another.")
        question = poll.question_text
        options = poll_options(poll)
        if not 2 <= len(options) <= 4:
            raise ValueError("The saved draft does not have 2–4 valid options.")
        chat_id = live_session.live_chat_id
        broadcast_id = live_session.broadcast_id

    if youtube_service is None:
        youtube_service = _get_youtube_service()

    body = {
        "snippet": {
            "liveChatId": chat_id,
            "type": "pollEvent",
            "pollDetails": {
                "metadata": {
                    "questionText": question,
                    "options": [{"optionText": option} for option in options],
                }
            },
        }
    }
    try:
        response = youtube_service.liveChatMessages().insert(part="snippet", body=body).execute()
        if not isinstance(response, dict) or not response.get("id"):
            raise RuntimeError("YouTube returned a poll response without a message ID.")
    except Exception as exc:
        with session_factory() as session:
            current = session.get(LivePollRecord, poll_id)
            if current is not None and current.status == POLL_DRAFT:
                current.last_error = str(exc)[:2000]
                session.commit()
        raise

    with session_factory() as session:
        # Re-check local status after the network call so a stale UI action cannot
        # silently overwrite a different state.
        current = session.get(LivePollRecord, poll_id)
        if current is None or current.status != POLL_DRAFT:
            raise RuntimeError("YouTube created the poll, but the local draft changed before its ID could be saved.")
        active = active_poll_for_chat(session, chat_id)
        if active is not None:
            raise RuntimeError(
                "YouTube created the poll, but another local active poll record appeared concurrently. "
                "Check YouTube chat before retrying."
            )
        mark_poll_launched(
            session,
            poll_id=poll_id,
            session_id=session_id,
            live_chat_id=chat_id,
            broadcast_id=broadcast_id,
            youtube_message_id=response["id"],
            response=response,
            launched_at=launched_at,
        )
        session.commit()
    return {"poll_id": poll_id, "youtube_message_id": response["id"], "status": POLL_ACTIVE}


def end_poll_on_youtube(
    session_factory,
    *,
    poll_id: int,
    youtube_service=None,
    closed_at: datetime | None = None,
) -> dict:
    """Close an active poll and persist YouTube's final tallies."""
    with session_factory() as session:
        poll = session.get(LivePollRecord, poll_id)
        if poll is None:
            raise ValueError(f"Unknown poll {poll_id}.")
        if poll.status != POLL_ACTIVE or not poll.youtube_message_id:
            raise ValueError("Only an active YouTube poll can be ended.")
        youtube_message_id = poll.youtube_message_id

    if youtube_service is None:
        youtube_service = _get_youtube_service()

    response = youtube_service.liveChatMessages().transition(
        id=youtube_message_id,
        status="closed",
        part="snippet",
    ).execute()
    if not isinstance(response, dict):
        raise RuntimeError("YouTube returned an unreadable poll-results response.")

    with session_factory() as session:
        current = session.get(LivePollRecord, poll_id)
        if current is None or current.status != POLL_ACTIVE:
            raise RuntimeError("YouTube closed the poll, but its local record changed before results were saved.")
        mark_poll_closed(session, poll_id=poll_id, response=response, closed_at=closed_at)
        session.commit()
        return {
            "poll_id": poll_id,
            "status": current.status,
            "results": json.loads(current.results_json or "{}"),
        }
