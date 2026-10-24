from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import select

from youtube_bot_dashboard.live_polls import (
    POLL_ACTIVE,
    POLL_CLOSED,
    POLL_DRAFT,
    LivePollRecord,
    create_poll_draft,
    poll_options,
    validate_poll_content,
)
from youtube_bot_dashboard.live_queue import LiveSessionRecord, register_live_session
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema
from youtube_bot_dashboard.youtube_polls import end_poll_on_youtube, launch_poll_on_youtube


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeLiveChatMessages:
    def __init__(self):
        self.insert_calls = []
        self.transition_calls = []
        self.insert_response = {"id": "yt-poll-123", "snippet": {"type": "pollEvent"}}
        self.transition_response = {
            "id": "yt-poll-123",
            "snippet": {
                "type": "pollEvent",
                "pollDetails": {
                    "metadata": {
                        "questionText": "What should we cover?",
                        "status": "closed",
                        "options": [
                            {"optionText": "VWAP", "tally": "8"},
                            {"optionText": "Risk management", "tally": "12"},
                        ],
                    }
                },
            },
        }

    def insert(self, *, part, body):
        self.insert_calls.append({"part": part, "body": body})
        return FakeRequest(self.insert_response)

    def transition(self, **kwargs):
        self.transition_calls.append(kwargs)
        return FakeRequest(self.transition_response)


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


def create_draft(factory, question="What should we cover?", options=None):
    with factory() as session:
        poll = create_poll_draft(
            session,
            question_text=question,
            options=options or ["VWAP", "Risk management"],
        )
        poll_id = poll.id
        session.commit()
        return poll_id


def test_poll_content_requires_two_to_four_unique_options():
    question, options = validate_poll_content(
        "  What should we cover?  ",
        ["VWAP", "Risk management", "", "  "],
    )
    assert question == "What should we cover?"
    assert options == ["VWAP", "Risk management"]

    with pytest.raises(ValueError, match="2 to 4"):
        validate_poll_content("Question", ["Only one"])
    with pytest.raises(ValueError, match="2 to 4"):
        validate_poll_content("Question", ["A", "B", "C", "D", "E"])
    with pytest.raises(ValueError, match="different"):
        validate_poll_content("Question", ["VWAP", "vwap"])


def test_draft_creation_does_not_publish_to_youtube(db):
    _, factory = db
    poll_id = create_draft(factory)
    with factory() as session:
        poll = session.get(LivePollRecord, poll_id)
        assert poll.status == POLL_DRAFT
        assert poll.live_chat_id is None
        assert poll.youtube_message_id is None
        assert poll_options(poll) == ["VWAP", "Risk management"]


def test_launch_creates_youtube_poll_and_persists_message_id(db):
    _, factory = db
    poll_id = create_draft(factory)
    youtube = FakeYouTubeService()

    result = launch_poll_on_youtube(
        factory,
        poll_id=poll_id,
        session_id="session-1",
        youtube_service=youtube,
        launched_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
    )

    assert result == {
        "poll_id": poll_id,
        "youtube_message_id": "yt-poll-123",
        "status": POLL_ACTIVE,
    }
    call = youtube.messages.insert_calls[0]
    assert call["part"] == "snippet"
    assert call["body"]["snippet"]["liveChatId"] == "chat-1"
    assert call["body"]["snippet"]["type"] == "pollEvent"
    metadata = call["body"]["snippet"]["pollDetails"]["metadata"]
    assert metadata["questionText"] == "What should we cover?"
    assert metadata["options"] == [
        {"optionText": "VWAP"},
        {"optionText": "Risk management"},
    ]
    with factory() as session:
        saved = session.get(LivePollRecord, poll_id)
        assert saved.status == POLL_ACTIVE
        assert saved.youtube_message_id == "yt-poll-123"
        assert saved.session_id == "session-1"


def test_second_poll_is_rejected_while_one_is_active(db):
    _, factory = db
    first_id = create_draft(factory)
    second_id = create_draft(factory, question="Next topic?", options=["Macro", "Entries"])
    youtube = FakeYouTubeService()
    launch_poll_on_youtube(factory, poll_id=first_id, session_id="session-1", youtube_service=youtube)

    with pytest.raises(ValueError, match="already active"):
        launch_poll_on_youtube(factory, poll_id=second_id, session_id="session-1", youtube_service=youtube)
    assert len(youtube.messages.insert_calls) == 1


def test_end_poll_requests_final_tallies_and_persists_results(db):
    _, factory = db
    poll_id = create_draft(factory)
    youtube = FakeYouTubeService()
    launch_poll_on_youtube(factory, poll_id=poll_id, session_id="session-1", youtube_service=youtube)

    result = end_poll_on_youtube(
        factory,
        poll_id=poll_id,
        youtube_service=youtube,
        closed_at=datetime(2026, 10, 25, 1, tzinfo=timezone.utc),
    )

    assert result["status"] == POLL_CLOSED
    assert youtube.messages.transition_calls == [{
        "id": "yt-poll-123",
        "status": "closed",
        "part": "snippet",
    }]
    assert result["results"]["question_text"] == "What should we cover?"
    assert result["results"]["options"] == [
        {"option_text": "VWAP", "tally": "8"},
        {"option_text": "Risk management", "tally": "12"},
    ]
    with factory() as session:
        saved = session.get(LivePollRecord, poll_id)
        assert saved.status == POLL_CLOSED
        stored_results = json.loads(saved.results_json)
        assert [option["tally"] for option in stored_results["options"]] == ["8", "12"]


def test_launch_requires_an_active_live_chat(db):
    _, factory = db
    poll_id = create_draft(factory)
    with factory() as session:
        live_session = session.get(LiveSessionRecord, "session-1")
        live_session.status = "ENDED"
        session.commit()
    youtube = FakeYouTubeService()
    with pytest.raises(ValueError, match="active live chat"):
        launch_poll_on_youtube(factory, poll_id=poll_id, session_id="session-1", youtube_service=youtube)
    assert youtube.messages.insert_calls == []
