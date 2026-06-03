"""
test_dd_agent_service.py — WS1 §2 Service-Layer registration helper.

Asserts the two invariants the contract requires of this additive wiring:
  1. DEFAULT-OFF: with the flag unset, every entry point is inert (no HTTP).
  2. NON-FATAL: a down/erroring :8510 never raises — calls return False.
And the happy path: with the flag on and a 2xx, calls return True with the
right payload.
"""
import importlib

import pytest


def _reload_with_env(monkeypatch, **env):
    """Reload the module under a given env so the module-level ENABLED re-reads."""
    for k in ("ENABLE_AGENT_SERVICE_REGISTRATION", "AGENT_SERVICE_URL", "AGENT_SERVICE_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import gateway.dd_agent_service as mod

    return importlib.reload(mod)


def test_default_off_is_inert(monkeypatch):
    mod = _reload_with_env(monkeypatch)  # flag unset
    assert mod.is_enabled() is False
    # No HTTP should happen; even without a server, these must return False (not raise).
    assert mod.register_agent("default") is False
    assert mod.register_session_created("sess-x", model="m") is False


def test_explicit_false_is_inert(monkeypatch):
    mod = _reload_with_env(monkeypatch, ENABLE_AGENT_SERVICE_REGISTRATION="false")
    assert mod.is_enabled() is False
    assert mod.register_agent("default") is False


def test_enabled_but_service_down_is_non_fatal(monkeypatch):
    # Point at a closed port; the call must swallow the connection error → False.
    mod = _reload_with_env(
        monkeypatch,
        ENABLE_AGENT_SERVICE_REGISTRATION="1",
        AGENT_SERVICE_URL="http://127.0.0.1:1",  # nothing listens
        AGENT_SERVICE_TIMEOUT="0.5",
    )
    assert mod.is_enabled() is True
    assert mod.register_agent("default") is False  # no raise
    assert mod.register_session_created("sess-x") is False  # no raise


def test_enabled_happy_path(monkeypatch):
    mod = _reload_with_env(
        monkeypatch, ENABLE_AGENT_SERVICE_REGISTRATION="1", AGENT_SERVICE_URL="http://svc:8510"
    )

    calls = {}

    class _Resp:
        status_code = 201

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            calls["url"] = url
            calls["json"] = json
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)
    assert mod.register_agent("dd-design", "http://wh") is True
    assert calls["url"] == "http://svc:8510/agents/register"
    assert calls["json"] == {"name": "dd-design", "webhook_url": "http://wh"}

    assert mod.register_session_created("sess-1", model="gpt") is True
    assert calls["url"] == "http://svc:8510/gateway/session-created"
    assert calls["json"]["session_id"] == "sess-1"
    assert calls["json"]["model"] == "gpt"


def test_empty_name_or_session_rejected(monkeypatch):
    mod = _reload_with_env(monkeypatch, ENABLE_AGENT_SERVICE_REGISTRATION="1")
    assert mod.register_agent("") is False
    assert mod.register_session_created("") is False
