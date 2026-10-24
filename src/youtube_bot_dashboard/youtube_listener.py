"""Direct YouTube live chat ingestion using the streaming Live Chat API.

The listener only collects messages into the durable JSON/live queue. It does
not mute, ban, time out, delete, or publish messages.
"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from youtube_bot_dashboard.live_queue import (
    AppSettingRecord,
    IncomingLiveMessage,
    LiveSessionRecord,
    close_session_shield,
    ingest_live_message,
    register_live_session,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema, utc_now

logger = logging.getLogger("youtube_bot_dashboard.youtube_listener")
_PROTO_TEMP = None
_PROTO_MODULES = None
YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


def load_stream_list_stub():
    """Compile the small, wire-compatible streamList schema into a temp folder."""
    global _PROTO_TEMP, _PROTO_MODULES
    if _PROTO_MODULES is not None:
        return _PROTO_MODULES
    try:
        import grpc
        from grpc_tools import protoc
    except ImportError as exc:
        raise RuntimeError('Install the YouTube extras with: pip install -e ".[youtube]"') from exc

    proto_path = Path(__file__).parent / "protos" / "stream_list.proto"
    if not proto_path.is_file():
        raise RuntimeError(f"Missing streamList protobuf file: {proto_path}")
    _PROTO_TEMP = tempfile.TemporaryDirectory(prefix="youtube-streamlist-proto-")
    output_dir = _PROTO_TEMP.name
    result = protoc.main([
        "grpc_tools.protoc",
        f"-I{proto_path.parent}",
        f"--python_out={output_dir}",
        f"--grpc_python_out={output_dir}",
        str(proto_path),
    ])
    if result != 0:
        raise RuntimeError(f"Could not compile streamList protobuf schema (exit code {result}).")
    sys.path.insert(0, output_dir)
    importlib.invalidate_caches()
    pb2 = importlib.import_module("stream_list_pb2")
    pb2_grpc = importlib.import_module("stream_list_pb2_grpc")
    _PROTO_MODULES = (grpc, pb2, pb2_grpc)
    return _PROTO_MODULES


def load_youtube_credentials():
    """Load/refresh a token previously created by youtube_auth."""
    load_dotenv()
    token_file = Path(os.getenv("YOUTUBE_TOKEN_FILE", ".youtube-oauth-token.json"))
    if not token_file.is_file():
        raise RuntimeError(
            f"YouTube OAuth token not found at {token_file}. Run "
            "'python -m youtube_bot_dashboard.youtube_auth' first."
        )
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError('Install the YouTube extras with: pip install -e ".[youtube]"') from exc

    credentials = Credentials.from_authorized_user_file(str(token_file), scopes=[YOUTUBE_READ_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass
    if not credentials.valid or not credentials.token:
        raise RuntimeError("YouTube credentials are not valid. Re-run the local OAuth setup.")
    return credentials


def build_youtube_service(credentials):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError('Install the YouTube extras with: pip install -e ".[youtube]"') from exc
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def discover_active_broadcast(youtube) -> tuple[str, str] | None:
    """Return (broadcast ID, liveChatId) for the first active owned broadcast."""
    manual_chat_id = os.getenv("YOUTUBE_LIVE_CHAT_ID")
    if manual_chat_id:
        return os.getenv("YOUTUBE_BROADCAST_ID", "manual-broadcast"), manual_chat_id

    response = youtube.liveBroadcasts().list(
        part="id,snippet,status",
        broadcastStatus="active",
        mine=True,
        maxResults=10,
    ).execute()
    items = response.get("items", [])
    for item in items:
        broadcast_id = item.get("id")
        snippet = item.get("snippet", {})
        live_chat_id = snippet.get("liveChatId")
        if broadcast_id and live_chat_id:
            return broadcast_id, live_chat_id
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_viewer_text(message) -> tuple[str, str | None, str | None, datetime | None, bool | None] | None:
    """Extract only human-authored text/comment events, not system chat events."""
    snippet = message.snippet
    text = ""
    try:
        if snippet.HasField("text_message_details"):
            text = snippet.text_message_details.message_text
        elif snippet.HasField("super_chat_details"):
            text = snippet.super_chat_details.user_comment
        elif snippet.HasField("member_milestone_chat_details"):
            text = snippet.member_milestone_chat_details.user_comment
    except ValueError:
        # Older generated schemas may not expose every optional message field.
        return None

    text = (text or "").strip()
    if not text:
        return None
    author = getattr(message, "author_details", None)
    user_id = None
    username = None
    is_member = None
    if author is not None:
        user_id = getattr(author, "channel_id", None) or None
        username = getattr(author, "display_name", None) or None
        try:
            if author.HasField("is_chat_sponsor"):
                is_member = bool(getattr(author, "is_chat_sponsor", False))
        except (AttributeError, ValueError):
            is_member = None
    if not user_id:
        user_id = getattr(snippet, "author_channel_id", None) or None
    published_at = _parse_timestamp(getattr(snippet, "published_at", None))
    message_id = getattr(message, "id", None)
    if not message_id:
        return None
    return text, user_id, username, published_at, is_member


def _cursor_key(live_chat_id: str) -> str:
    return f"youtube_stream_page_token:{live_chat_id}"


def _read_page_token(session_factory, live_chat_id: str) -> str | None:
    with session_factory() as session:
        row = session.get(AppSettingRecord, _cursor_key(live_chat_id))
        return row.value if row and row.value else None


def persist_stream_response(
    session_factory,
    *,
    live_chat_id: str,
    broadcast_id: str,
    response,
) -> dict:
    """Atomically store messages and the next page token before reconnecting."""
    created = 0
    duplicates = 0
    ended = bool(getattr(response, "offline_at", None))
    page_token = getattr(response, "next_page_token", None)
    with session_factory() as session:
        live_session = session.get(LiveSessionRecord, live_chat_id)
        if live_session is None:
            register_live_session(
                session,
                session_id=live_chat_id,
                live_chat_id=live_chat_id,
                broadcast_id=broadcast_id,
                started_at=utc_now(),
                source="YOUTUBE_API",
            )
        elif live_session.status != "ACTIVE":
            raise RuntimeError("Received stream messages for a session already marked ended.")

        for item in getattr(response, "items", []):
            extracted = extract_viewer_text(item)
            if not extracted:
                continue
            text, user_id, username, published_at, is_member = extracted
            result = ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id=item.id,
                    session_id=live_chat_id,
                    text=text,
                    user_channel_id=user_id,
                    username=username,
                    is_member=is_member,
                    published_at=published_at,
                ),
            )
            if result.created:
                created += 1
            else:
                duplicates += 1

        if page_token:
            cursor = session.get(AppSettingRecord, _cursor_key(live_chat_id))
            if cursor is None:
                session.add(AppSettingRecord(
                    key=_cursor_key(live_chat_id),
                    value=page_token,
                    updated_at=utc_now(),
                ))
            else:
                cursor.value = page_token
                cursor.updated_at = utc_now()
        if ended:
            close_session_shield(session, live_chat_id, ended_at=utc_now())
        session.commit()
    return {
        "created": created,
        "duplicates": duplicates,
        "ended": ended,
        "page_token_updated": bool(page_token),
    }


def _close_stale_sessions(session_factory, active_chat_ids: set[str]) -> None:
    with session_factory() as session:
        rows = list(session.query(LiveSessionRecord).filter(
            LiveSessionRecord.status == "ACTIVE",
            LiveSessionRecord.source == "YOUTUBE_API",
        ).all())
        for row in rows:
            if row.live_chat_id and row.live_chat_id not in active_chat_ids:
                close_session_shield(session, row.session_id, ended_at=utc_now())
        session.commit()


def stream_one_live_chat(session_factory, *, credentials, broadcast_id: str, live_chat_id: str) -> bool:
    """Stream until YouTube reports the chat has ended. Returns true if ended."""
    grpc, pb2, pb2_grpc = load_stream_list_stub()
    stub = pb2_grpc.V3DataLiveChatMessageServiceStub(
        grpc.secure_channel("youtube.googleapis.com:443", grpc.ssl_channel_credentials())
    )
    page_token = _read_page_token(session_factory, live_chat_id)

    while True:
        # Refresh credentials on reconnect so long-running streams can resume.
        credentials = load_youtube_credentials()
        request = pb2.LiveChatMessageListRequest(
            live_chat_id=live_chat_id,
            part=["id", "snippet", "authorDetails"],
        )
        if page_token:
            request.page_token = page_token
        metadata = (("authorization", f"Bearer {credentials.token}"),)
        reconnect = False
        try:
            for response in stub.StreamList(request, metadata=metadata):
                result = persist_stream_response(
                    session_factory,
                    live_chat_id=live_chat_id,
                    broadcast_id=broadcast_id,
                    response=response,
                )
                if getattr(response, "next_page_token", None):
                    page_token = response.next_page_token
                if result["ended"]:
                    return True
                # An empty next token means this connection has caught up; open
                # a new stream from the last persisted non-empty token.
                if not getattr(response, "next_page_token", None):
                    reconnect = True
                    break
        except grpc.RpcError as exc:
            logger.warning("YouTube live chat stream disconnected: %s", exc)
            reconnect = True

        if reconnect:
            time.sleep(0.5)
            continue


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    database_url = os.getenv("DATABASE_URL", "sqlite:///./youtube_bot_dashboard.db")
    reconnect_seconds = max(1, float(os.getenv("YOUTUBE_RECONNECT_SECONDS", "5")))
    engine, factory = create_engine_and_sessions(database_url)
    create_schema(engine)
    try:
        credentials = load_youtube_credentials()
        youtube = build_youtube_service(credentials)
        logger.info("YouTube live chat listener started (read-only; no moderation actions).")
        while True:
            credentials = load_youtube_credentials()
            youtube = build_youtube_service(credentials)
            active = discover_active_broadcast(youtube)
            if not active:
                _close_stale_sessions(factory, set())
                time.sleep(reconnect_seconds)
                continue
            broadcast_id, live_chat_id = active
            _close_stale_sessions(factory, {live_chat_id})
            register_needed = False
            with factory() as session:
                existing = session.get(LiveSessionRecord, live_chat_id)
                register_needed = existing is None
                if existing is not None and existing.status != "ACTIVE":
                    logger.error("Found an ended record for active live chat %s; start with a fresh database/session ID.", live_chat_id)
                    time.sleep(reconnect_seconds)
                    continue
                if register_needed:
                    register_live_session(
                        session,
                        session_id=live_chat_id,
                        live_chat_id=live_chat_id,
                        broadcast_id=broadcast_id,
                        started_at=utc_now(),
                        source="YOUTUBE_API",
                    )
                    session.commit()
            try:
                ended = stream_one_live_chat(
                    factory,
                    credentials=credentials,
                    broadcast_id=broadcast_id,
                    live_chat_id=live_chat_id,
                )
                if ended:
                    logger.info("YouTube reported live chat %s has ended.", live_chat_id)
                    with factory() as session:
                        close_session_shield(session, live_chat_id, ended_at=utc_now())
                        session.commit()
            except Exception:
                logger.exception("Live chat listener failed for %s; retrying.", live_chat_id)
                time.sleep(reconnect_seconds)
    except KeyboardInterrupt:
        logger.info("YouTube live chat listener stopping.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    import logging
    main()
