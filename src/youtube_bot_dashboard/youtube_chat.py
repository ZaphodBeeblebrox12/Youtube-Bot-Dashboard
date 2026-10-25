"""Explicit, owner-triggered sends of edited replies to YouTube live chat."""

from __future__ import annotations


def _get_youtube_service():
    """Use the existing YouTube OAuth token and live-chat write scope."""
    from youtube_bot_dashboard.youtube_listener import (
        build_youtube_service,
        load_youtube_credentials,
    )

    return build_youtube_service(load_youtube_credentials())


def send_live_chat_message(
    session_factory,
    *,
    session_id: str,
    message_text: str,
    youtube_service=None,
) -> dict:
    """Send edited text only after an explicit UI action and only to an active chat."""
    text = str(message_text or "").strip()
    if not text:
        raise ValueError("Reply text cannot be empty.")

    # Resolve the destination from the database; never trust a browser-supplied chat ID.
    from youtube_bot_dashboard.live_queue import LiveSessionRecord

    with session_factory() as session:
        live_session = session.get(LiveSessionRecord, session_id)
        if live_session is None:
            raise ValueError("The selected live session no longer exists.")
        if live_session.status != "ACTIVE" or not live_session.live_chat_id:
            raise ValueError("The selected YouTube live chat is not active or connected.")
        live_chat_id = live_session.live_chat_id

    if youtube_service is None:
        youtube_service = _get_youtube_service()

    body = {
        "snippet": {
            "liveChatId": live_chat_id,
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": text},
        }
    }
    response = youtube_service.liveChatMessages().insert(
        part="snippet",
        body=body,
    ).execute()
    if not isinstance(response, dict) or not response.get("id"):
        raise RuntimeError("YouTube returned an unreadable response; delivery could not be confirmed.")
    return {
        "session_id": session_id,
        "live_chat_id": live_chat_id,
        "youtube_message_id": response["id"],
        "status": "SENT",
    }
