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
