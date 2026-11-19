"""Official request pricing invariants through Hermes' existing cost seam."""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry, normalize_usage


def cost(usage, model="gpt-6.2-astra", **context):
    return estimate_usage_cost(model, usage, provider="openai", **context)


@pytest.mark.parametrize("model,expected", [("gpt-6.2-astra", ".1456"), ("gpt-6.2-sol", ".0364")])
def test_mixed_cache_lifetimes_replace_regular_input_and_reasoning_is_in_output(model, expected):
    raw = NS(input_tokens=10000, output_tokens=2000,
             input_tokens_details=NS(cached_tokens=4000, cache_write_tokens=3000,
                                    cache_write_30m_tokens=2000, cache_write_12h_tokens=1000),
             output_tokens_details=NS(reasoning_tokens=1500))
    usage = normalize_usage(raw, provider="openai", api_mode="codex_responses", model_name=model)
    assert (usage.input_tokens, usage.prompt_tokens, usage.total_tokens) == (3000, 10000, 12000)
    assert cost(usage, model).amount_usd == Decimal(expected)


def test_chat_usage_reports_cache_write_ttl_and_reasoning_without_changing_old_models():
    raw = NS(prompt_tokens=1000, completion_tokens=100,
             prompt_tokens_details=NS(cached_tokens=100, cache_write_tokens=200, cache_write_ttl="12h"),
             **{"completion_tokens_details": NS(reasoning_tokens=60)})
    usage = normalize_usage(raw, provider="openai", model_name="gpt-6.2-sol")
    assert usage.cache_write_12h_tokens == 200
    assert usage.reasoning_tokens == 60
    assert cost(usage, "gpt-6.2-sol").amount_usd == Decimal(".00341")
    assert normalize_usage(raw, provider="openai").reported_input_tokens is None


@pytest.mark.parametrize("at,expected", [("2027-03-01T07:59:59Z", ".036"), ("2027-03-01T08:00:00Z", ".072")])
def test_ultrafast_cutover_is_pacific_midnight(at, expected):
    assert cost(CanonicalUsage(input_tokens=1000, output_tokens=100), requested_at=at,
                service_tier="ultrafast").amount_usd == Decimal(expected)


@pytest.mark.parametrize("context", [dict(service_tier="flex"), dict(service_tier="ultrafast"),
    dict(requested_at="2026-11-16T23:59:59Z"), dict(requested_at="2027-03-01T00:00:00"),
    dict(service_tier="priority", background_completion_window="2h")])
def test_unverified_tier_or_date_is_unknown(context):
    result = cost(CanonicalUsage(input_tokens=1000), **context)
    assert result.amount_usd is None and result.status == "unknown"


def test_context_threshold_uses_inclusive_input_and_prices_whole_request():
    short = CanonicalUsage(input_tokens=1000, cache_read_tokens=271000, output_tokens=100)
    assert cost(short).amount_usd == Decimal(".1204")
    assert cost(replace(short, cache_read_tokens=271001)).amount_usd == Decimal(".2388008")


def test_batch_background_and_regional_factors():
    usage = CanonicalUsage(input_tokens=1000, output_tokens=100)
    assert cost(usage, service_tier="batch").amount_usd == Decimal(".006")
    assert cost(usage, service_tier="priority").amount_usd == Decimal(".024")
    assert cost(usage, background_completion_window="2h").amount_usd == Decimal(".0084")
    assert cost(usage, background_completion_window="8h", regional_processing=True).amount_usd == Decimal(".00792")


def test_nonzero_unknown_or_inconsistent_writes_are_not_free():
    usage = CanonicalUsage(input_tokens=100, cache_write_tokens=200)
    assert cost(usage).amount_usd is None
    assert cost(replace(usage, cache_write_30m_tokens=199)).amount_usd is None
    raw = NS(input_tokens=100, output_tokens=10, input_tokens_details=NS(cached_tokens=101))
    normalized = normalize_usage(raw, provider="openai", model_name="gpt-6.2-astra")
    assert cost(normalized).amount_usd is None
    assert cost(CanonicalUsage(output_tokens=10, reasoning_tokens=11)).amount_usd is None


def test_snapshot_exact_models_subscription_and_existing_models_keep_their_routes():
    assert get_pricing_entry("gpt-6.2-sol-2026-11-17", provider="openai").input_cost_per_million == Decimal(2)
    assert get_pricing_entry("gpt-6.2-unverified", provider="openai") is None
    result = estimate_usage_cost("gpt-6.2-astra", CanonicalUsage(input_tokens=1000), provider="openai-codex")
    assert result.status == "included" and result.amount_usd == 0
    old = estimate_usage_cost("gpt-4o", CanonicalUsage(input_tokens=1000, output_tokens=100), provider="openai")
    assert old.amount_usd == Decimal(".0035")
    assert cost(CanonicalUsage(input_tokens=1000)).notes[-1].startswith("Standard API-equivalent estimate")


@pytest.mark.parametrize("sync", [False, True])
def test_telemetry_preserves_unknown_new_cost_and_known_zero(monkeypatch, sync):
    import dd_obs
    payloads = []
    def post(path, payload):
        payloads.append(payload)
        return True
    monkeypatch.setattr(dd_obs, "_post_sync" if sync else "_post_async", post)
    emit = dd_obs.log_generation_sync if sync else dd_obs.log_generation
    for model, amount in [("gpt-6.2-sol", None), ("gpt-6.2-astra", 0), ("gpt-4o", None)]:
        emit(generation_id="synthetic", session_id="synthetic", model=model,
             requested_at="2026-11-18T00:00:00Z", cost_total_usd=amount)
    assert [p["cost_total_usd"] for p in payloads] == [None, 0.0, 0.0]
