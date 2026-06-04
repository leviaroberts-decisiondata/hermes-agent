"""Tests for the _save_auth_store anti-clobber guard.

Regression guard for the 2026-06-04 wipe: a non-clear save must never replace
a non-empty ``providers`` map with an empty one. See _save_auth_store in
hermes_cli/auth.py.
"""

import json
from pathlib import Path

from hermes_cli.auth import _save_auth_store, _load_auth_store, clear_provider_auth


def _populated_store():
    return {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": {"access_token": "a", "refresh_token": "r"},
                "last_refresh": "2026-06-04T00:00:00Z",
                "auth_mode": "chatgpt",
            }
        },
    }


def _write_store(hermes_home: Path, store: dict):
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(store, indent=2))


def test_save_refuses_to_empty_populated_providers(tmp_path, monkeypatch):
    """A non-clear save with empty providers must NOT overwrite a populated store."""
    hermes_home = tmp_path / "hermes_test"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_store(hermes_home, _populated_store())

    # Simulate the wipe: a credential-pool seed saves a store with empty
    # providers but a new credential_pool entry (size grows, providers lost).
    wiping_store = {
        "version": 1,
        "providers": {},
        "credential_pool": {"copilot": [{"access_token": "x", "source": "gh_cli"}]},
    }
    _save_auth_store(wiping_store)  # no allow_provider_clear

    # The on-disk store must STILL have the codex provider — the wipe was refused.
    after = _load_auth_store()
    assert "openai-codex" in after.get("providers", {}), (
        "anti-clobber guard failed: populated providers were wiped by a "
        "non-clear save"
    )


def test_save_allows_empty_providers_when_clearing(tmp_path, monkeypatch):
    """An explicit clear/logout (allow_provider_clear=True) may empty providers."""
    hermes_home = tmp_path / "hermes_test"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_store(hermes_home, _populated_store())

    cleared = {"version": 1, "providers": {}}
    _save_auth_store(cleared, allow_provider_clear=True)

    after = _load_auth_store()
    assert after.get("providers", {}) == {}, (
        "explicit clear should be allowed to empty the providers map"
    )


def test_clear_provider_auth_empties_via_guarded_path(tmp_path, monkeypatch):
    """clear_provider_auth (logout) must succeed even though it empties providers."""
    hermes_home = tmp_path / "hermes_test"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_store(hermes_home, _populated_store())

    assert clear_provider_auth("openai-codex") is True
    after = _load_auth_store()
    assert "openai-codex" not in after.get("providers", {})


def test_save_allows_empty_providers_when_no_existing_store(tmp_path, monkeypatch):
    """First-ever save with empty providers (no on-disk store) is fine."""
    hermes_home = tmp_path / "hermes_test"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    hermes_home.mkdir(parents=True, exist_ok=True)
    # No auth.json yet.
    _save_auth_store({"version": 1, "providers": {}})
    assert (hermes_home / "auth.json").exists()


def test_save_allows_provider_to_provider_update(tmp_path, monkeypatch):
    """A normal save that keeps providers non-empty is never blocked."""
    hermes_home = tmp_path / "hermes_test"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_store(hermes_home, _populated_store())

    updated = _populated_store()
    updated["providers"]["copilot"] = {"tokens": {"access_token": "c"}}
    _save_auth_store(updated)

    after = _load_auth_store()
    assert set(after.get("providers", {}).keys()) == {"openai-codex", "copilot"}
