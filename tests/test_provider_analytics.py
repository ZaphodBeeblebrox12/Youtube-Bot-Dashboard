from datetime import datetime, timedelta, timezone

import pytest

from youtube_bot_dashboard.live_queue import LiveAIRequestBatch, register_live_session
from youtube_bot_dashboard.provider_analytics import (
    ProviderRateCard,
    estimate_batch_cost_usd,
    get_provider_analytics,
    load_provider_rate_card,
    save_provider_rate_card,
)
from youtube_bot_dashboard.storage import create_engine_and_sessions, create_schema


@pytest.fixture
def db():
    engine, factory = create_engine_and_sessions("sqlite://")
    create_schema(engine)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def test_provider_rate_cards_are_model_specific_and_persist(db):
    _, factory = db
    rate_card = ProviderRateCard(1.0, 0.1, 0.2, 2.0)
    with factory() as session:
        save_provider_rate_card(session, "KIMI", "kimi-k2.6", rate_card)
        session.commit()

    with factory() as session:
        assert load_provider_rate_card(session, "KIMI", "kimi-k2.6") == rate_card
        assert load_provider_rate_card(session, "KIMI", "kimi-k3") is None


@pytest.mark.parametrize(
    "rates",
    [
        (-1, 0, 0, 0),
        (0, float("inf"), 0, 0),
        (0, 0, float("nan"), 0),
    ],
)
def test_rate_cards_reject_invalid_rates(rates):
    with pytest.raises(ValueError):
        ProviderRateCard(*rates)


def test_cost_estimate_accounts_for_uncached_cached_write_and_output_tokens():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    batch = LiveAIRequestBatch(
        batch_id="batch-1",
        session_id="session-1",
        pipeline="MODERATION",
        provider="KIMI",
        model_id="kimi-k2.6",
        payload_json="{}",
        status="COMPLETED",
        input_tokens=1000,
        output_tokens=500,
        cached_input_tokens=400,
        cache_write_tokens=200,
        sent_at=start,
        completed_at=start + timedelta(seconds=2.5),
    )

    assert estimate_batch_cost_usd(batch, ProviderRateCard(1.0, 0.1, 0.2, 2.0)) == pytest.approx(0.00148)
    assert estimate_batch_cost_usd(batch, None) is None


def test_analytics_summarizes_latency_cache_tokens_and_cost(db):
    _, factory = db
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with factory() as session:
        register_live_session(session, session_id="session-1")
        save_provider_rate_card(session, "KIMI", "kimi-k2.6", ProviderRateCard(1.0, 0.1, 0.2, 2.0))
        session.add(LiveAIRequestBatch(
            batch_id="batch-1",
            session_id="session-1",
            pipeline="MODERATION",
            provider="KIMI",
            model_id="kimi-k2.6",
            payload_json="{}",
            status="COMPLETED",
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=400,
            cache_write_tokens=200,
            sent_at=start,
            completed_at=start + timedelta(seconds=2.5),
            created_at=start,
        ))
        session.commit()

    with factory() as session:
        report = get_provider_analytics(session)
        kimi = next(row for row in report["providers"] if row["provider"] == "KIMI")
        detail = report["by_pipeline"][0]

    assert kimi["requests"] == 1
    assert kimi["successful_requests"] == 1
    assert kimi["average_latency_seconds"] == pytest.approx(2.5)
    assert kimi["p95_latency_seconds"] == pytest.approx(2.5)
    assert kimi["cached_input_tokens"] == 400
    assert kimi["cache_write_tokens"] == 200
    assert kimi["cache_hit_rate_percent"] == pytest.approx(40.0)
    assert kimi["cost_estimate_usd"] == pytest.approx(0.00148)
    assert detail["pipeline"] == "MODERATION"



def test_cache_hit_rate_excludes_records_missing_total_input_usage(db):
    _, factory = db
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with factory() as session:
        register_live_session(session, session_id="session-cache-coverage")
        session.add_all([
            LiveAIRequestBatch(
                batch_id="batch-cache-unknown-input",
                session_id="session-cache-coverage",
                pipeline="MODERATION",
                provider="KIMI",
                model_id="kimi-test",
                payload_json="{}",
                status="COMPLETED",
                input_tokens=None,
                output_tokens=10,
                cached_input_tokens=50,
                sent_at=start,
                completed_at=start + timedelta(seconds=1),
                created_at=start,
            ),
            LiveAIRequestBatch(
                batch_id="batch-cache-known-input",
                session_id="session-cache-coverage",
                pipeline="MODERATION",
                provider="KIMI",
                model_id="kimi-test",
                payload_json="{}",
                status="COMPLETED",
                input_tokens=100,
                output_tokens=10,
                cached_input_tokens=20,
                sent_at=start + timedelta(seconds=2),
                completed_at=start + timedelta(seconds=3),
                created_at=start + timedelta(seconds=2),
            ),
        ])
        session.commit()

    with factory() as session:
        report = get_provider_analytics(session)
        kimi = next(row for row in report["providers"] if row["provider"] == "KIMI")

    # Recorded cache-hit tokens remain visible, but only the second request
    # has a complete denominator for computing the cache-hit percentage.
    assert kimi["cached_input_tokens"] == 70
    assert kimi["cache_hit_rate_percent"] == pytest.approx(20.0)


def test_provider_rate_card_can_be_cleared(db):
    _, factory = db
    rate_card = ProviderRateCard(1.0, 0.1, 0.2, 2.0)
    with factory() as session:
        save_provider_rate_card(session, "DEEPSEEK", "deepseek-test", rate_card)
        session.commit()
    with factory() as session:
        assert load_provider_rate_card(session, "DEEPSEEK", "deepseek-test") == rate_card
        save_provider_rate_card(session, "DEEPSEEK", "deepseek-test", None)
        session.commit()
    with factory() as session:
        assert load_provider_rate_card(session, "DEEPSEEK", "deepseek-test") is None
