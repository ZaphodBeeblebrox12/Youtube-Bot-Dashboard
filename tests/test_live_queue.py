from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from youtube_bot_dashboard.live_queue import (
    AppSettingRecord,
    BatchState,
    LiveAIRequestBatch,
    LiveMessageRecord,
    LiveMessageTask,
    LivePipeline,
    LiveQueueConfig,
    ProviderName,
    ProviderRouteState,
    ProviderStrategy,
    SessionShieldRecord,
    TaskState,
    complete_live_batch,
    end_live_session,
    fail_live_batch,
    get_live_queue_config,
    ingest_live_message,
    mark_live_batch_sent,
    prepare_due_batch,
    register_live_session,
    shield_user_for_session,
    should_flush_batch,
    unshield_user_for_session,
    update_live_queue_config,
    IncomingLiveMessage,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


@pytest.fixture
def db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with factory() as session:
        register_live_session(session, session_id="session-1", live_chat_id="chat-1")
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def msg(message_id, user_id="channel-1", text=None):
    return IncomingLiveMessage(
        message_id=message_id,
        session_id="session-1",
        user_channel_id=user_id,
        username=user_id,
        text=text or f"Hello {message_id}",
    )


def test_default_live_config_is_two_seconds_and_round_robin(db):
    _, factory = db
    with factory() as session:
        config = get_live_queue_config(session)
        assert config.batch_delay_seconds == 2.0
        assert config.max_messages_per_batch == 100
        assert config.provider_strategy is ProviderStrategy.ROUND_ROBIN
        assert config.enabled_providers == (ProviderName.GEMINI, ProviderName.QWEN)


def test_dashboard_editable_live_settings_persist(db):
    _, factory = db
    with factory() as session:
        config = LiveQueueConfig(batch_delay_seconds=3.5, max_messages_per_batch=50)
        update_live_queue_config(session, config)
        session.commit()
    with factory() as session:
        saved = get_live_queue_config(session)
        assert saved.batch_delay_seconds == 3.5
        assert saved.max_messages_per_batch == 50


def test_live_settings_validate_delay_and_message_limit():
    with pytest.raises(ValueError):
        LiveQueueConfig(batch_delay_seconds=0)
    with pytest.raises(ValueError):
        LiveQueueConfig(max_messages_per_batch=0)


def test_ingest_stores_one_message_and_queues_two_independent_pipelines(db):
    _, factory = db
    with factory() as session:
        result = ingest_live_message(session, msg("m-1"))
        session.commit()
        assert result.created
        assert result.moderation_task_id is not None
        assert result.reply_task_id is not None
        tasks = list(session.scalars(select(LiveMessageTask).where(LiveMessageTask.message_id == "m-1")))
        assert {t.pipeline for t in tasks} == {"MODERATION", "REPLY_FINDER"}
        assert all(t.state == TaskState.PENDING.value for t in tasks)
        assert session.get(LiveMessageRecord, "m-1") is not None


def test_duplicate_ingest_does_not_create_duplicate_tasks(db):
    _, factory = db
    with factory() as session:
        first = ingest_live_message(session, msg("m-1"))
        second = ingest_live_message(session, msg("m-1"))
        session.commit()
        assert first.created
        assert not second.created
        assert len(list(session.scalars(select(LiveMessageTask).where(LiveMessageTask.message_id == "m-1")))) == 2


def test_session_shield_filters_reply_finder_but_keeps_moderation(db):
    _, factory = db
    with factory() as session:
        shield_user_for_session(
            session, session_id="session-1", user_channel_id="channel-evil",
            reason="High-confidence repeated antagonism", confidence=0.98
        )
        result = ingest_live_message(session, msg("m-evil", user_id="channel-evil"))
        session.commit()
        tasks = list(session.scalars(select(LiveMessageTask).where(LiveMessageTask.message_id == "m-evil")))
        by_pipeline = {t.pipeline: t for t in tasks}
        assert result.reply_excluded_by_shield
        assert by_pipeline["MODERATION"].state == TaskState.PENDING.value
        assert by_pipeline["REPLY_FINDER"].state == TaskState.EXCLUDED_SHIELDED.value


def test_shielding_after_ingest_removes_reply_task_from_prepared_payload(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    with factory() as session:
        ingest_live_message(session, msg("m-1", "channel-evil"))
        ingest_live_message(session, msg("m-2", "channel-good"))
        session.commit()

        config = LiveQueueConfig(batch_delay_seconds=2.0, max_messages_per_batch=100)
        batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.REPLY_FINDER,
            config=config, now=now + timedelta(seconds=3)
        )
        assert batch is not None
        assert set(batch.message_ids) == {"m-1", "m-2"}
        session.commit()

        shield_user_for_session(
            session, session_id="session-1", user_channel_id="channel-evil",
            reason="Flagged in current session"
        )
        session.commit()

        stored_batch = session.get(LiveAIRequestBatch, batch.batch_id)
        payload = json.loads(stored_batch.payload_json)
        assert [m["message_id"] for m in payload["messages"]] == ["m-2"]
        evil_task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "m-1",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))
        assert evil_task.state == TaskState.EXCLUDED_SHIELDED.value


def test_batch_waits_for_two_second_window_unless_full(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(batch_delay_seconds=2.0, max_messages_per_batch=100)
    with factory() as session:
        ingest_live_message(session, msg("m-1"), received_at=now)
        session.commit()
        assert not should_flush_batch(
            pending_count=1, oldest_queued_at=now, now=now + timedelta(seconds=1.99), config=config
        )
        assert should_flush_batch(
            pending_count=1, oldest_queued_at=now, now=now + timedelta(seconds=2), config=config
        )
        assert should_flush_batch(
            pending_count=100, oldest_queued_at=now, now=now + timedelta(milliseconds=10), config=config
        )


def test_due_batches_alternate_gemini_and_qwen_per_pipeline(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(batch_delay_seconds=2.0)
    with factory() as session:
        ingest_live_message(session, msg("m-1"), received_at=now)
        session.commit()
        first = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.REPLY_FINDER,
            config=config, now=now + timedelta(seconds=2)
        )
        session.commit()
        assert first.provider is ProviderName.GEMINI
        mark_live_batch_sent(session, first.batch_id, model_id="gemini-model")
        complete_live_batch(
            session, first.batch_id, result_by_message_id={"m-1": {"selected": True}},
            raw_response={"results": [{"message_id": "m-1", "selected": True}]},
            input_tokens=100, output_tokens=20
        )
        session.commit()

        ingest_live_message(session, msg("m-2"), received_at=now + timedelta(seconds=4))
        session.commit()
        second = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.REPLY_FINDER,
            config=config, now=now + timedelta(seconds=7)
        )
        session.commit()
        assert second.provider is ProviderName.QWEN
        assert second.message_ids == ("m-2",)


def test_provider_strategy_can_be_pinned_to_one_provider(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(
        batch_delay_seconds=2,
        provider_strategy=ProviderStrategy.QWEN_ONLY,
        enabled_providers=(ProviderName.GEMINI, ProviderName.QWEN),
    )
    with factory() as session:
        ingest_live_message(session, msg("m-1"), received_at=now)
        session.commit()
        batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=now + timedelta(seconds=3)
        )
        assert batch.provider is ProviderName.QWEN


def test_moderation_response_missing_message_ids_are_retryable_and_extra_ids_ignored(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(batch_delay_seconds=2, max_messages_per_batch=10)
    with factory() as session:
        ingest_live_message(session, msg("m-1"), received_at=now)
        ingest_live_message(session, msg("m-2"), received_at=now)
        session.commit()
        batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=now + timedelta(seconds=3)
        )
        session.commit()
        mark_live_batch_sent(session, batch.batch_id, model_id="gemini-model")
        completed, missing = complete_live_batch(
            session, batch.batch_id,
            result_by_message_id={"m-1": {"category": "NO_VIOLATION"}, "not-in-batch": {"category": "SPAM"}},
            raw_response={"results": [{"message_id": "m-1"}]},
        )
        session.commit()
        assert (completed, missing) == (1, 1)
        tasks = {t.message_id: t for t in session.scalars(select(LiveMessageTask).where(
            LiveMessageTask.pipeline == LivePipeline.MODERATION.value
        ))}
        assert tasks["m-1"].state == TaskState.COMPLETED.value
        assert tasks["m-2"].state == TaskState.FAILED_RETRYABLE.value
        assert tasks["m-2"].batch_id is None


def test_reply_finder_non_selected_messages_are_completed_not_retried(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(batch_delay_seconds=2, max_messages_per_batch=10)
    with factory() as session:
        ingest_live_message(session, msg("m-1"), received_at=now)
        ingest_live_message(session, msg("m-2"), received_at=now)
        session.commit()
        batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.REPLY_FINDER,
            config=config, now=now + timedelta(seconds=3)
        )
        session.commit()
        mark_live_batch_sent(session, batch.batch_id, model_id="gemini-model")
        completed, missing = complete_live_batch(
            session, batch.batch_id,
            result_by_message_id={"m-1": {"priority": "HIGH", "selected": True}},
            raw_response={"selected_messages": [{"message_id": "m-1"}]},
        )
        session.commit()
        assert (completed, missing) == (2, 0)
        tasks = {t.message_id: t for t in session.scalars(select(LiveMessageTask).where(
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value
        ))}
        assert tasks["m-1"].state == TaskState.COMPLETED.value
        assert tasks["m-2"].state == TaskState.COMPLETED.value
        assert json.loads(tasks["m-2"].result_json)["selected"] is False


def test_provider_failure_retries_then_marks_final_after_limit(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(batch_delay_seconds=2, max_attempts=1)
    with factory() as session:
        ingest_live_message(session, msg("m-1"), received_at=now)
        session.commit()
        batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=now + timedelta(seconds=3)
        )
        session.commit()
        mark_live_batch_sent(session, batch.batch_id, model_id="gemini-model")
        session.commit()
        retryable, final = fail_live_batch(
            session, batch.batch_id, error="provider timed out", config=config,
            failed_at=now + timedelta(seconds=4)
        )
        session.commit()
        task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "m-1",
            LiveMessageTask.pipeline == LivePipeline.MODERATION.value,
        ))
        assert (retryable, final) == (0, 1)
        assert task.state == TaskState.FAILED_FINAL.value


def test_unshield_only_affects_current_session_record(db):
    _, factory = db
    with factory() as session:
        shield_user_for_session(session, session_id="session-1", user_channel_id="channel-1", reason="manual")
        session.commit()
        assert unshield_user_for_session(session, session_id="session-1", user_channel_id="channel-1")
        assert not unshield_user_for_session(session, session_id="session-1", user_channel_id="channel-1")
        session.commit()


def test_cannot_ingest_after_session_ends(db):
    _, factory = db
    with factory() as session:
        end_live_session(session, "session-1")
        session.commit()
        with pytest.raises(ValueError, match="ended session"):
            ingest_live_message(session, msg("m-late"))


def test_provider_cursor_is_persisted_for_restart(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(batch_delay_seconds=2)
    with factory() as session:
        ingest_live_message(session, msg("m-1"), received_at=now)
        session.commit()
        batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=now + timedelta(seconds=3)
        )
        session.commit()
        assert batch.provider is ProviderName.GEMINI
        state = session.get(ProviderRouteState, "live:MODERATION")
        assert state.next_index == 1



def test_token_budget_does_not_send_single_oversized_message(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(
        batch_delay_seconds=2,
        max_input_tokens=2000,
        system_prompt_reserve_tokens=1800,
    )
    with factory() as session:
        ingest_live_message(session, msg("m-big", text="x" * 2500), received_at=now)
        session.commit()
        batch = prepare_due_batch(
            session,
            session_id="session-1",
            pipeline=LivePipeline.REPLY_FINDER,
            config=config,
            now=now + timedelta(seconds=3),
        )
        session.commit()
        assert batch is None
        task = session.scalar(select(LiveMessageTask).where(
            LiveMessageTask.message_id == "m-big",
            LiveMessageTask.pipeline == LivePipeline.REPLY_FINDER.value,
        ))
        assert task.state == TaskState.FAILED_FINAL.value
        assert "exceeds estimated payload budget" in task.last_error


def test_provider_cursor_does_not_advance_when_no_message_fits(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(
        batch_delay_seconds=2,
        max_input_tokens=2000,
        system_prompt_reserve_tokens=1800,
    )
    with factory() as session:
        ingest_live_message(session, msg("m-big", text="x" * 2500), received_at=now)
        session.commit()
        batch = prepare_due_batch(
            session,
            session_id="session-1",
            pipeline=LivePipeline.REPLY_FINDER,
            config=config,
            now=now + timedelta(seconds=3),
        )
        session.commit()
        assert batch is None
        assert session.get(ProviderRouteState, "live:REPLY_FINDER") is None


def test_shielded_message_is_not_in_reply_ai_payload_but_remains_moderation_payload(db):
    _, factory = db
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    config = LiveQueueConfig(batch_delay_seconds=2)
    with factory() as session:
        shield_user_for_session(
            session, session_id="session-1", user_channel_id="channel-evil",
            reason="Repeated antagonism", source="AI", confidence=0.99,
        )
        ingest_live_message(session, msg("m-shielded", "channel-evil"), received_at=now)
        session.commit()

        reply_batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.REPLY_FINDER,
            config=config, now=now + timedelta(seconds=3)
        )
        assert reply_batch is None

        moderation_batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=now + timedelta(seconds=3)
        )
        assert moderation_batch is not None
        assert moderation_batch.message_ids == ("m-shielded",)
