"""Regression: telemetry uses the active compressor budget, not a global bar."""
from types import SimpleNamespace
import pytest
from run_agent import _build_hermes_context_usage_payload


def payload(**kw):
    return _build_hermes_context_usage_payload(
        canonical_usage=SimpleNamespace(input_tokens=3999, cache_read_tokens=129792,
                                        cache_write_tokens=0, output_tokens=282),
        model="test-model", **kw)


def test_runtime_budget_overrides_stale_display_environment(monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_LIMIT", "400000")
    monkeypatch.setenv("HERMES_CONTEXT_FLOOR", "320000")
    out = payload(context_length=272000, compression_threshold=136000)
    assert out["tokens"] == 133791
    assert out["limit"] == 272000
    assert out["floor"] == 136000
    assert out["pct"] == 49.19
    assert out["remaining"] == 2209
    assert out["compression_threshold"] == 136000
    assert out["budget_source"] == "active_context_engine"
    assert out["status"] == "yellow"


@pytest.mark.parametrize("limit,threshold", [(372000,186000),(128000,64000)])
def test_budget_follows_model_switch(limit, threshold):
    out = payload(context_length=limit, compression_threshold=threshold)
    assert out["limit"] == limit
    assert out["floor"] == threshold


def test_disabled_compression_reports_no_compression_threshold():
    out = payload(context_length=272000, compression_threshold=136000, compression_enabled=False)
    assert out["compression_threshold"] is None
    assert out["floor"] == 217600


def test_unknown_budget_is_not_reported_as_known_capacity(monkeypatch):
    monkeypatch.delenv("HERMES_CONTEXT_LIMIT", raising=False)
    monkeypatch.delenv("HERMES_CONTEXT_FLOOR", raising=False)
    out = payload()
    assert out["budget_source"] == "legacy_display_fallback"


def test_runtime_budget_ignores_malformed_display_environment(monkeypatch):
    monkeypatch.setenv("HERMES_CONTEXT_LIMIT", "invalid")
    monkeypatch.setenv("HERMES_CONTEXT_FLOOR", "invalid")
    assert payload(context_length=272000, compression_threshold=136000)["limit"] == 272000
