"""Tests for DecisionData obs-ingest discovery/config alignment."""

from __future__ import annotations

import importlib


def _fresh_dd_obs(monkeypatch):
    monkeypatch.setenv("DD_REGISTRY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DD_OBS_HTTP_TIMEOUT", "0.1")
    import dd_obs

    dd_obs = importlib.reload(dd_obs)
    dd_obs._OBS_INGEST_BASE = None
    return dd_obs


def test_resolve_obs_ingest_honors_gateway_env_var_first(monkeypatch):
    """dd_obs per-call writes must target the same env override as gateway/run.py."""

    monkeypatch.setenv("HERMES_DECISIONDATA_OBSERVABILITY_URL", "http://obs-from-hermes-env:9999/")
    monkeypatch.setenv("DECISIONDATA_OBSERVABILITY_URL", "http://obs-from-decisiondata-env:9998")
    monkeypatch.setenv("DD_OBS_INGEST_URL", "http://obs-from-legacy-env:9997")

    dd_obs = _fresh_dd_obs(monkeypatch)

    assert dd_obs._resolve_obs_ingest_base() == "http://obs-from-hermes-env:9999"


def test_resolve_obs_ingest_honors_decisiondata_env_var(monkeypatch):
    monkeypatch.delenv("HERMES_DECISIONDATA_OBSERVABILITY_URL", raising=False)
    monkeypatch.setenv("DECISIONDATA_OBSERVABILITY_URL", "http://obs-from-decisiondata-env:9998/")
    monkeypatch.setenv("DD_OBS_INGEST_URL", "http://obs-from-legacy-env:9997")

    dd_obs = _fresh_dd_obs(monkeypatch)

    assert dd_obs._resolve_obs_ingest_base() == "http://obs-from-decisiondata-env:9998"


def test_resolve_obs_ingest_keeps_legacy_dd_obs_env_var(monkeypatch):
    monkeypatch.delenv("HERMES_DECISIONDATA_OBSERVABILITY_URL", raising=False)
    monkeypatch.delenv("DECISIONDATA_OBSERVABILITY_URL", raising=False)
    monkeypatch.setenv("DD_OBS_INGEST_URL", "http://obs-from-legacy-env:9997/")

    dd_obs = _fresh_dd_obs(monkeypatch)

    assert dd_obs._resolve_obs_ingest_base() == "http://obs-from-legacy-env:9997"


def test_resolve_obs_ingest_default_matches_gateway_default(monkeypatch):
    monkeypatch.delenv("HERMES_DECISIONDATA_OBSERVABILITY_URL", raising=False)
    monkeypatch.delenv("DECISIONDATA_OBSERVABILITY_URL", raising=False)
    monkeypatch.delenv("DD_OBS_INGEST_URL", raising=False)

    dd_obs = _fresh_dd_obs(monkeypatch)

    assert dd_obs._resolve_obs_ingest_base() == "http://127.0.0.1:8511"


def test_generation_payloads_carry_provider(monkeypatch):
    """provider must reach the /log_generation payload (WTS 298bdde6).

    A stale comment claimed provider was not a GenerationRequest field (it is,
    since mc-api's phase-2 migration) and both variants dropped it: the async
    path explicitly (`if provider: pass`), the sync path by accepting the
    kwarg and never adding it. 7,217 hermes-gateway generations in one 30-day
    window carried provider NULL as a result. This fails if either variant
    drops it again.
    """
    dd_obs = _fresh_dd_obs(monkeypatch)
    sent = []

    monkeypatch.setattr(dd_obs, "_post_async", lambda path, payload: sent.append(("async", path, payload)))
    monkeypatch.setattr(dd_obs, "_post_sync", lambda path, payload: sent.append(("sync", path, payload)) or True)

    common = dict(
        generation_id="gen-p", session_id="sess-p", model="claude-sonnet-4-6",
        requested_at="2026-09-01T10:00:00Z", provider="anthropic", run_id="run-p",
    )
    dd_obs.log_generation(**common)
    dd_obs.log_generation_sync(**common)

    assert len(sent) == 2
    for variant, path, payload in sent:
        assert path == "/log_generation"
        assert payload.get("provider") == "anthropic", f"{variant} variant dropped provider"
        assert payload.get("run_id") == "run-p"

    # Absent provider stays absent — never fabricated.
    sent.clear()
    no_provider = {k: v for k, v in common.items() if k != "provider"}
    dd_obs.log_generation(**{**no_provider, "generation_id": "gen-np"})
    assert "provider" not in sent[0][2]
