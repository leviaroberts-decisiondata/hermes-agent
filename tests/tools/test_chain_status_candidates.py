"""chain_status — candidates fallback + no-fabrication behavior (resolver/honesty fix).

Regression lock for the 2026-06-04 live-test FAIL: on a Telegram DM turn, the old
chain_status returned a BARE error ("could not resolve a chain …") when nothing
auto-resolved, and the model then answered from memory describing the WRONG chain.
The fix: when no chain resolves (or an explicit selector misses), the tool returns
a READ-ONLY candidates listing instead of a dead-end — so the model SELECTS the
right chain and never falls back to recollection.

These tests stub the HTTP layer (_api_get) so they run hermetically — no live
Directus, no token needed. The live-flow acceptance (real spine, real reader
identity) is exercised separately by the session probe in the fix report.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import chain_status_tool as cs  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────
def _row(rid, route_key, surface, channel, status, stage, updated, created=None,
         title="some ask"):
    return {
        "id": rid, "route_key": route_key, "title": title, "ask_summary": title,
        "source_surface": surface, "source_channel_id": channel,
        "status": status, "current_stage": stage,
        "updated_at": updated, "created_at": created or updated,
    }


# Mirrors the live shape the morning FAIL hit: a moving Slack cleanup chain plus a
# never-ticked telegram probe row (null updated_at) that must NOT outrank it.
_HELD = _row("77b185c3", "no-task:cd605a8b", "slack", "C0B7C4BC6KD",
             "escalated", "engineering", "2026-06-05T03:12:24.420Z",
             title="Please clean up our slack-demo-manager demo instance")
_PROBE = _row("5f05f7e5", "agent:probe:p2:xyz", "telegram", None,
              "active", None, None, created="2026-06-05T01:00:00.000Z",
              title="p2 probe")
_OTHER = _row("c37701ae", "no-task:d1d1a618", "slack", "C0B7C4BC6KD",
              "blocked", "deploy", "2026-06-05T03:12:24.038Z")


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    # Make the gate pass without a real token file.
    monkeypatch.setattr(cs, "_reader_token", lambda: "test-token")


def _stub_get(monkeypatch, candidates_rows, *, channel_hit=None, wts_hit=None,
              chainid_hit=None):
    """Stub _api_get: candidate sort query returns candidates_rows; explicit
    filters return their *_hit (or empty)."""
    def fake(path):
        if "sort=-updated_at" in path:           # the _candidates() query
            return 200, {"data": list(candidates_rows)}
        if "filter[source_channel_id]" in path:
            return 200, {"data": [channel_hit] if channel_hit else []}
        if "filter[wts_task_id]" in path:
            return 200, {"data": [wts_hit] if wts_hit else []}
        if "filter[route_key]" in path:
            return 200, {"data": [chainid_hit] if chainid_hit else []}
        if "request_chain_events" in path:
            return 200, {"data": []}
        if "/items/request_chains/" in path:     # by-uuid lookup
            return 200, {"data": chainid_hit} if chainid_hit else (404, {"data": None})
        return 200, {"data": []}
    monkeypatch.setattr(cs, "_api_get", fake)


class _TelegramTurn:
    """A Telegram DM turn whose bound WTS task has NO chain row — the FAIL setup."""
    _dd_route_key = "agent:main:telegram:dm:8737984752"
    _dd_session_key = "agent:main:telegram:dm:8737984752"
    _dd_wts_task_id = "a6a469c4-7a40-4b8d-a47d-d7b621e8ddbb"


# ── tests ────────────────────────────────────────────────────────────────────
def test_telegram_no_resolve_returns_candidates_not_error(monkeypatch):
    """The headline FAIL: telegram turn, no args, bound task has no chain.
    Must return candidates incl the held cleanup chain — NOT a bare error."""
    _stub_get(monkeypatch, [_PROBE, _HELD, _OTHER])  # channel/wts filters miss
    out = cs.chain_status(parent_agent=_TelegramTurn())
    assert "most recently active chains" in out
    assert "cd605a8b" in out                      # the chain Levi asked about
    assert "could not resolve a chain" not in out  # the old dead-end is gone
    assert not out.lstrip().startswith('{"error"')


def test_candidates_rank_real_movers_above_null_timestamp_probes(monkeypatch):
    """A never-ticked probe (null updated_at) must not outrank a moving chain —
    Directus floats nulls first on a desc sort, so we re-rank in Python."""
    _stub_get(monkeypatch, [_PROBE, _HELD, _OTHER])
    out = cs.chain_status(parent_agent=_TelegramTurn())
    assert out.index("cd605a8b") < out.index("agent:probe:p2:xyz")


def test_explicit_channel_miss_lists_candidates_with_reason(monkeypatch):
    """An explicit selector that matches nothing offers candidates + names the miss."""
    _stub_get(monkeypatch, [_HELD], channel_hit=None)
    out = cs.chain_status(channel="NONEXISTENT", parent_agent=_TelegramTurn())
    assert "matched no chain" in out
    assert "cd605a8b" in out


def test_channel_hit_returns_structured_status_not_candidates(monkeypatch):
    """When the channel resolves, return the five-field status — not candidates."""
    _stub_get(monkeypatch, [_HELD], channel_hit=_HELD)
    out = cs.chain_status(channel="C0B7C4BC6KD", parent_agent=_TelegramTurn())
    assert "WHERE ARE WE" in out
    assert "77b185c3" in out
    assert "escalated" in out
    assert "most recently active chains" not in out


def test_candidates_carry_no_credential_fields(monkeypatch):
    """The listing must never surface token/credential-ish fields."""
    _stub_get(monkeypatch, [_HELD, _OTHER])
    out = cs.chain_status(parent_agent=_TelegramTurn()).lower()
    for bad in ("token", "bearer", "authorization", "password", "secret"):
        assert bad not in out


def test_read_failure_still_offers_candidates_or_honest_guidance(monkeypatch):
    """If the primary read errors, degrade to candidates/guidance — never fabricate."""
    calls = {"n": 0}

    def flaky(path):
        if "sort=-updated_at" in path:
            return 200, {"data": [_HELD]}
        raise RuntimeError("boom")

    monkeypatch.setattr(cs, "_api_get", flaky)
    out = cs.chain_status(channel="x", parent_agent=_TelegramTurn())
    # channel filter raises -> outer except -> candidates fallback
    assert "cd605a8b" in out or "do NOT fabricate" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
