from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from youtube_bot_dashboard.policies.comment_cleanup import (
    CommentAssessment,
    CommentTarget,
    ConductType,
    Sentiment,
    decide_cleanup,
)
from youtube_bot_dashboard.storage import (
    CommentRecord,
    CommentTextVersion,
    DiscoveredComment,
    MonitoredVideo,
    UpsertOutcome,
    create_engine_and_sessions,
    create_schema,
    mark_comment_classified,
    pending_classification_ids,
    register_video,
    upsert_discovered_comment,
)


@pytest.fixture
def db():
    engine, sessions = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with sessions() as session:
        register_video(session, video_id="video-1", title="Lesson 1")
        session.commit()
    try:
        yield engine, sessions
    finally:
        engine.dispose()


def discovered(text="This lesson is terrible and your explanation makes no sense."):
    return DiscoveredComment(
        comment_id="comment-1",
        video_id="video-1",
        text=text,
        author_channel_id="channel-123",
        author_display_name="viewer",
    )


def make_assessment(text):
    return CommentAssessment(
        comment_id="comment-1",
        video_id="video-1",
        target=CommentTarget.CONTENT,
        sentiment=Sentiment.NEGATIVE,
        conduct_type=ConductType.CONTENT_CRITICISM,
        confidence=0.99,
        rationale="Genuine criticism of content",
        text=text,
    )


def test_first_seen_comment_is_stored_and_queued_once(db):
    _, sessions = db
    with sessions() as session:
        result = upsert_discovered_comment(session, discovered())
        session.commit()
        assert result.outcome is UpsertOutcome.NEW
        assert result.should_classify
        assert pending_classification_ids(session) == ["comment-1"]
        assert session.scalar(select(CommentRecord).where(CommentRecord.comment_id == "comment-1")).state == "DISCOVERED"
        assert len(list(session.scalars(select(CommentTextVersion)))) == 1


def test_repeat_sweep_does_not_requeue_unchanged_classified_comment(db):
    _, sessions = db
    with sessions() as session:
        original_text = discovered().text
        upsert_discovered_comment(session, discovered())
        session.commit()
        assessment = make_assessment(original_text)
        mark_comment_classified(session, assessment, decide_cleanup(assessment), model_id="test-model")
        session.commit()
        assert pending_classification_ids(session) == []
        result = upsert_discovered_comment(
            session, discovered(),
            seen_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        session.commit()
        assert result.outcome is UpsertOutcome.UNCHANGED
        assert not result.should_classify
        assert pending_classification_ids(session) == []
        assert session.get(CommentRecord, "comment-1").state == "AI_CLASSIFIED"


def test_edited_comment_is_queued_separately_and_history_is_preserved(db):
    _, sessions = db
    with sessions() as session:
        original_text = discovered().text
        upsert_discovered_comment(session, discovered())
        session.commit()
        assessment = make_assessment(original_text)
        mark_comment_classified(session, assessment, decide_cleanup(assessment), model_id="test-model")
        session.commit()

        changed = discovered("Your explanation is wrong because the chart shows the opposite.")
        result = upsert_discovered_comment(session, changed)
        session.commit()
        assert result.outcome is UpsertOutcome.EDITED
        assert result.current_version == 2
        assert result.is_edited_reassessment
        assert pending_classification_ids(session) == []
        assert pending_classification_ids(session, include_edited=True) == ["comment-1"]
        versions = list(session.scalars(select(CommentTextVersion).order_by(CommentTextVersion.version_number)))
        assert [v.version_number for v in versions] == [1, 2]
        record = session.get(CommentRecord, "comment-1")
        assert record.state == "EDITED_REASSESSMENT"
        assert record.cleanup_decision is None


def test_every_discovered_comment_is_stored_including_positive_comments(db):
    _, sessions = db
    with sessions() as session:
        one = upsert_discovered_comment(session, discovered("This helped me understand the topic."))
        two = upsert_discovered_comment(session, DiscoveredComment(
            comment_id="comment-2", video_id="video-1", text="Thanks for the lesson!",
        ))
        session.commit()
        assert one.outcome is UpsertOutcome.NEW
        assert two.outcome is UpsertOutcome.NEW
        assert session.get(CommentRecord, "comment-1") is not None
        assert session.get(CommentRecord, "comment-2") is not None


def test_comment_must_belong_to_a_registered_video(db):
    _, sessions = db
    with sessions() as session:
        with pytest.raises(ValueError, match="not registered"):
            upsert_discovered_comment(session, DiscoveredComment(
                comment_id="comment-x", video_id="unknown-video", text="hello",
            ))


def test_duplicate_comment_id_cannot_be_reused_for_another_video(db):
    _, sessions = db
    with sessions() as session:
        register_video(session, video_id="video-2", title="Lesson 2")
        session.commit()
        upsert_discovered_comment(session, discovered())
        session.commit()
        with pytest.raises(ValueError, match="different video_id"):
            upsert_discovered_comment(session, DiscoveredComment(
                comment_id="comment-1", video_id="video-2", text=discovered().text,
            ))


def test_records_persist_after_engine_restart(tmp_path):
    db_path = tmp_path / "comments.sqlite3"
    url = f"sqlite:///{db_path}"
    engine1, sessions1 = create_engine_and_sessions(url)
    create_schema(engine1)
    with sessions1() as session:
        register_video(session, video_id="video-persist", title="Persistent")
        upsert_discovered_comment(session, DiscoveredComment(
            comment_id="comment-persist", video_id="video-persist", text="Still here",
        ))
        session.commit()
    engine1.dispose()

    engine2, sessions2 = create_engine_and_sessions(url)
    create_schema(engine2)
    with sessions2() as session:
        record = session.get(CommentRecord, "comment-persist")
        assert record is not None
        assert record.text == "Still here"
    engine2.dispose()
