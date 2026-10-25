from types import SimpleNamespace

import pytest

from youtube_bot_dashboard.live_queue import LiveSessionRecord, register_live_session
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema
from youtube_bot_dashboard.youtube_chat import send_live_chat_message


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeLiveChatMessages:
    def __init__(self):
        self.insert_calls = []
        self.response = {"id": "yt-reply-123", "snippet": {"type": "textMessageEvent"}}

    def insert(self, *, part, body):
        self.insert_calls.append({"part": part, "body": body})
        return FakeRequest(self.response)


class FakeYouTubeService:
    def __init__(self):
        self.messages = FakeLiveChatMessages()

    def liveChatMessages(self):
        return self.messages


@pytest.fixture
def db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with factory() as session:
        register_live_session(
            session,
            session_id="session-1",
            live_chat_id="chat-1",
            broadcast_id="broadcast-1",
            source="YOUTUBE_API",
        )
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def test_send_uses_edited_text_and_active_chat_only(db):
    _, factory = db
    youtube = FakeYouTubeService()

    result = send_live_chat_message(
        factory,
        session_id="session-1",
        message_text="  The key is to define your risk before entering.  ",
        youtube_service=youtube,
    )

    assert result == {
        "session_id": "session-1",
        "live_chat_id": "chat-1",
        "youtube_message_id": "yt-reply-123",
        "status": "SENT",
    }
    assert youtube.messages.insert_calls == [{
        "part": "snippet",
        "body": {
            "snippet": {
                "liveChatId": "chat-1",
                "type": "textMessageEvent",
                "textMessageDetails": {
                    "messageText": "The key is to define your risk before entering."
                },
            }
        },
    }]


def test_empty_reply_is_rejected_without_sending(db):
    _, factory = db
    youtube = FakeYouTubeService()

    with pytest.raises(ValueError, match="cannot be empty"):
        send_live_chat_message(
            factory,
            session_id="session-1",
            message_text="  \n ",
            youtube_service=youtube,
        )
    assert youtube.messages.insert_calls == []


def test_ended_or_unknown_chat_is_rejected_without_sending(db):
    _, factory = db
    youtube = FakeYouTubeService()
    with factory() as session:
        row = session.get(LiveSessionRecord, "session-1")
        row.status = "ENDED"
        session.commit()

    with pytest.raises(ValueError, match="not active"):
        send_live_chat_message(
            factory,
            session_id="session-1",
            message_text="This should not be sent.",
            youtube_service=youtube,
        )
    with pytest.raises(ValueError, match="no longer exists"):
        send_live_chat_message(
            factory,
            session_id="missing",
            message_text="This should not be sent.",
            youtube_service=youtube,
        )
    assert youtube.messages.insert_calls == []


def test_unconfirmed_youtube_response_is_not_reported_as_sent(db):
    _, factory = db
    youtube = FakeYouTubeService()
    youtube.messages.response = {"snippet": {"type": "textMessageEvent"}}

    with pytest.raises(RuntimeError, match="delivery could not be confirmed"):
        send_live_chat_message(
            factory,
            session_id="session-1",
            message_text="Hello live chat.",
            youtube_service=youtube,
        )
