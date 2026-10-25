from datetime import datetime, timedelta, timezone

import pytest

from youtube_bot_dashboard.live_queue import (
    IncomingLiveMessage,
    LivePipeline,
    LiveQueueConfig,
    ProviderName,
    ProviderStrategy,
    fail_live_batch,
    ingest_live_message,
    mark_live_batch_sent,
    prepare_due_batch,
    register_live_session,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


@pytest.fixture
def db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    with factory() as session:
        register_live_session(session, session_id="session-1")
        session.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


def test_round_robin_cycles_through_all_four_enabled_providers(db):
    _, factory = db
    base = datetime.now(timezone.utc)
    providers = (
        ProviderName.GEMINI,
        ProviderName.QWEN,
        ProviderName.DEEPSEEK,
        ProviderName.KIMI,
    )
    config = LiveQueueConfig(
        batch_delay_seconds=0.25,
        max_messages_per_batch=1,
        enabled_providers=providers,
        provider_strategy=ProviderStrategy.ROUND_ROBIN,
    )
    with factory() as session:
        for index in range(4):
            ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id=f"m-{index}",
                    session_id="session-1",
                    user_channel_id=f"user-{index}",
                    text=f"message {index}",
                ),
                received_at=base,
            )
        session.commit()

        actual = []
        for index in range(4):
            batch = prepare_due_batch(
                session,
                session_id="session-1",
                pipeline=LivePipeline.MODERATION,
                config=config,
                now=base + timedelta(seconds=10 + index),
            )
            assert batch is not None
            actual.append(batch.provider)
            session.commit()

    assert tuple(actual) == providers


def test_fixed_provider_strategy_requires_that_provider_to_be_enabled():
    config = LiveQueueConfig(
        provider_strategy=ProviderStrategy.KIMI_ONLY,
        enabled_providers=(ProviderName.KIMI,),
    )
    assert config.provider_strategy is ProviderStrategy.KIMI_ONLY

    with pytest.raises(ValueError):
        LiveQueueConfig(
            provider_strategy=ProviderStrategy.KIMI_ONLY,
            enabled_providers=(ProviderName.GEMINI,),
        )


def test_round_robin_retry_stays_with_original_provider_without_advancing_rotation(db):
    _, factory = db
    base = datetime.now(timezone.utc)
    providers = (
        ProviderName.GEMINI,
        ProviderName.QWEN,
        ProviderName.DEEPSEEK,
        ProviderName.KIMI,
    )
    config = LiveQueueConfig(
        batch_delay_seconds=0.25,
        max_messages_per_batch=1,
        enabled_providers=providers,
        provider_strategy=ProviderStrategy.ROUND_ROBIN,
    )
    with factory() as session:
        for message_id in ("retry-me", "new-message"):
            ingest_live_message(
                session,
                IncomingLiveMessage(
                    message_id=message_id,
                    session_id="session-1",
                    user_channel_id=message_id,
                    text=message_id,
                ),
                received_at=base,
            )
        session.commit()

        first = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=base + timedelta(seconds=10),
        )
        assert first is not None
        assert first.provider is ProviderName.GEMINI
        session.commit()

        mark_live_batch_sent(
            session, first.batch_id, model_id="gemini-test",
            sent_at=base + timedelta(seconds=11),
        )
        fail_live_batch(
            session, first.batch_id, error="simulated timeout",
            config=config, failed_at=base + timedelta(seconds=12),
        )
        session.commit()

        retry = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=base + timedelta(seconds=60),
        )
        assert retry is not None
        assert retry.message_ids == ("retry-me",)
        assert retry.provider is ProviderName.GEMINI
        session.commit()

        next_new_batch = prepare_due_batch(
            session, session_id="session-1", pipeline=LivePipeline.MODERATION,
            config=config, now=base + timedelta(seconds=61),
        )
        assert next_new_batch is not None
        assert next_new_batch.message_ids == ("new-message",)
        assert next_new_batch.provider is ProviderName.QWEN
