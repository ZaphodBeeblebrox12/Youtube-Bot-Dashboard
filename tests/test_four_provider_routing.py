from datetime import datetime, timedelta, timezone

import pytest

from youtube_bot_dashboard.live_queue import (
    IncomingLiveMessage,
    LivePipeline,
    LiveQueueConfig,
    ProviderName,
    ProviderStrategy,
    ingest_live_message,
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
