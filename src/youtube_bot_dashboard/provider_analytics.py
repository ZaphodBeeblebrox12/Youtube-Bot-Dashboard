"""Provider-specific usage, latency, cache, and configurable cost estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from youtube_bot_dashboard.live_queue import AppSettingRecord, LiveAIRequestBatch, ProviderName


PRICE_FIELDS = (
    "input_usd_per_million",
    "cached_input_usd_per_million",
    "cache_write_usd_per_million",
    "output_usd_per_million",
)
PROVIDER_ORDER = (
    ProviderName.GEMINI.value,
    ProviderName.QWEN.value,
    ProviderName.DEEPSEEK.value,
    ProviderName.KIMI.value,
)


@dataclass(frozen=True, slots=True)
class ProviderRateCard:
    """Model-specific USD prices per million tokens."""

    input_usd_per_million: float
    cached_input_usd_per_million: float
    cache_write_usd_per_million: float
    output_usd_per_million: float

    def __post_init__(self) -> None:
        for name in PRICE_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


def _setting_key(provider: str | ProviderName, model_id: str, field: str) -> str:
    provider_value = provider.value if isinstance(provider, ProviderName) else str(provider).upper()
    model_hash = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:12]
    return f"provider_price:{provider_value}:{model_hash}:{field}"


def load_provider_rate_card(
    session: Session, provider: str | ProviderName, model_id: str | None
) -> ProviderRateCard | None:
    """Load rates only for the exact configured model; missing rates mean unknown cost."""
    if not model_id:
        return None
    values: dict[str, float] = {}
    for field in PRICE_FIELDS:
        row = session.get(AppSettingRecord, _setting_key(provider, model_id, field))
        if row is None:
            return None
        try:
            value = float(row.value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        values[field] = value
    return ProviderRateCard(**values)


def save_provider_rate_card(
    session: Session,
    provider: str | ProviderName,
    model_id: str,
    rate_card: ProviderRateCard | None,
) -> None:
    """Save or clear the rate card for a specific provider/model pair."""
    if not model_id or not model_id.strip():
        raise ValueError("A configured model ID is required before pricing can be saved.")
    for field in PRICE_FIELDS:
        key = _setting_key(provider, model_id, field)
        row = session.get(AppSettingRecord, key)
        if rate_card is None:
            if row is not None:
                session.delete(row)
            continue
        value = str(float(getattr(rate_card, field)))
        if row is None:
            session.add(AppSettingRecord(key=key, value=value))
        else:
            row.value = value
    session.flush()


def estimate_batch_cost_usd(batch: LiveAIRequestBatch, rate_card: ProviderRateCard | None) -> float | None:
    """Estimate token cost without inventing usage or cache-write values.

    Providers that do not report cache details are conservatively priced as if
    the whole input were uncached. A cost estimate is unavailable if rate cards
    or total input/output token usage are missing.
    """
    if rate_card is None or batch.input_tokens is None or batch.output_tokens is None:
        return None
    input_tokens = max(0, int(batch.input_tokens))
    output_tokens = max(0, int(batch.output_tokens))
    cached_tokens = max(0, int(batch.cached_input_tokens or 0))
    cache_write_tokens = max(0, int(batch.cache_write_tokens or 0))
    cached_tokens = min(input_tokens, cached_tokens)
    cache_write_tokens = min(input_tokens - cached_tokens, cache_write_tokens)
    uncached_tokens = max(0, input_tokens - cached_tokens - cache_write_tokens)
    return (
        uncached_tokens * rate_card.input_usd_per_million
        + cached_tokens * rate_card.cached_input_usd_per_million
        + cache_write_tokens * rate_card.cache_write_usd_per_million
        + output_tokens * rate_card.output_usd_per_million
    ) / 1_000_000


def _new_stats(provider: str, pipeline: str | None = None, model_id: str | None = None) -> dict:
    return {
        "provider": provider,
        "pipeline": pipeline,
        "model": model_id,
        "requests": 0,
        "successful_requests": 0,
        "failed_requests": 0,
        "in_flight_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "cache_observed_requests": 0,
        "cache_observed_input_tokens": 0,
        "latencies": [],
        "cost_estimate_usd_sum": 0.0,
        "cost_covered_requests": 0,
        "cost_missing_usage_requests": 0,
    }


def _record_batch(stats: dict, batch: LiveAIRequestBatch, rate_card: ProviderRateCard | None) -> None:
    if batch.sent_at is None:
        return
    stats["requests"] += 1
    if batch.status in {"COMPLETED", "PARTIAL"}:
        stats["successful_requests"] += 1
    elif batch.status == "FAILED":
        stats["failed_requests"] += 1
    elif batch.status == "SENT":
        stats["in_flight_requests"] += 1

    if batch.input_tokens is not None:
        stats["input_tokens"] += max(0, int(batch.input_tokens))
    if batch.output_tokens is not None:
        stats["output_tokens"] += max(0, int(batch.output_tokens))
    if batch.cached_input_tokens is not None:
        stats["cached_input_tokens"] += max(0, int(batch.cached_input_tokens))
    if batch.cache_write_tokens is not None:
        stats["cache_write_tokens"] += max(0, int(batch.cache_write_tokens))
    if batch.cached_input_tokens is not None or batch.cache_write_tokens is not None:
        stats["cache_observed_requests"] += 1
        if batch.input_tokens is not None:
            stats["cache_observed_input_tokens"] += max(0, int(batch.input_tokens))

    if batch.sent_at is not None and batch.completed_at is not None:
        latency = (batch.completed_at - batch.sent_at).total_seconds()
        if math.isfinite(latency) and latency >= 0:
            stats["latencies"].append(latency)

    cost = estimate_batch_cost_usd(batch, rate_card)
    if cost is not None:
        stats["cost_estimate_usd_sum"] += cost
        stats["cost_covered_requests"] += 1
    elif rate_card is not None:
        stats["cost_missing_usage_requests"] += 1


def _finalize_stats(stats: dict) -> dict:
    latencies = sorted(stats.pop("latencies"))
    latency_count = len(latencies)
    stats["average_latency_seconds"] = (
        sum(latencies) / latency_count if latency_count else None
    )
    stats["p95_latency_seconds"] = (
        latencies[max(0, math.ceil(latency_count * 0.95) - 1)] if latency_count else None
    )
    stats["success_rate_percent"] = (
        100.0 * stats["successful_requests"] / stats["requests"] if stats["requests"] else None
    )
    stats["cache_hit_rate_percent"] = (
        100.0 * stats["cached_input_tokens"] / stats["cache_observed_input_tokens"]
        if stats["cache_observed_input_tokens"] else None
    )
    stats["cost_estimate_usd"] = (
        stats["cost_estimate_usd_sum"] if stats["cost_covered_requests"] else None
    )
    stats.pop("cost_estimate_usd_sum", None)
    return stats


def get_provider_analytics(session: Session, *, since: datetime | None = None) -> dict[str, list[dict]]:
    """Aggregate persisted provider batches all-time or from a supplied timestamp."""
    statement = select(LiveAIRequestBatch).order_by(LiveAIRequestBatch.created_at.asc())
    if since is not None:
        statement = statement.where(LiveAIRequestBatch.created_at >= since)
    batches = list(session.scalars(statement))

    detail: dict[tuple[str, str, str], dict] = {}
    summary: dict[str, dict] = {
        provider: _new_stats(provider) for provider in PROVIDER_ORDER
    }
    rate_cards: dict[tuple[str, str], ProviderRateCard | None] = {}

    for batch in batches:
        provider = batch.provider or "UNKNOWN"
        pipeline = batch.pipeline or "UNKNOWN"
        model_id = batch.model_id or ""
        key = (provider, pipeline, model_id)
        if key not in detail:
            detail[key] = _new_stats(provider, pipeline, model_id or None)
        summary.setdefault(provider, _new_stats(provider))

        rate_key = (provider, model_id)
        if rate_key not in rate_cards:
            try:
                rate_cards[rate_key] = load_provider_rate_card(session, provider, model_id)
            except ValueError:
                rate_cards[rate_key] = None
        rate_card = rate_cards[rate_key]
        _record_batch(detail[key], batch, rate_card)
        _record_batch(summary[provider], batch, rate_card)

    by_pipeline = [
        _finalize_stats(row)
        for row in sorted(detail.values(), key=lambda item: (item["provider"], item["pipeline"] or "", item["model"] or ""))
        if row["requests"]
    ]
    provider_summary = [
        _finalize_stats(summary[provider])
        for provider in (*PROVIDER_ORDER, *sorted(set(summary) - set(PROVIDER_ORDER)))
    ]
    return {"providers": provider_summary, "by_pipeline": by_pipeline}
