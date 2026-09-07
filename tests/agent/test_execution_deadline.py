"""Deterministic tests for the execution-deadline contract (WTS ac4bcb05).

Network-free. Covers: env contract parsing, closeout fuse math, per-call
capping (the mechanism that stops a provider retry from outliving the
deadline), scrub-survival of the env names, terminal-state schema +
AC6 retry coercion, and the wake-event typed-state threading.
"""

import json
import re
import time

import pytest

from agent.deadline import (
    CLOSEOUT_INSTRUCTION,
    DEFAULT_CLOSEOUT_FRACTION,
    ENV_BUDGET_SECS,
    ENV_CLOSEOUT_TS,
    ENV_DEADLINE_TS,
    MIN_CALL_CAP_SECS,
    ExecutionDeadline,
)
from agent import terminal_state as ts


# ── ExecutionDeadline ───────────────────────────────────────────────────


def test_from_env_absent_returns_none():
    assert ExecutionDeadline.from_env(env={}) is None


def test_from_env_malformed_returns_none():
    assert ExecutionDeadline.from_env(env={ENV_DEADLINE_TS: "not-a-number"}) is None
    assert ExecutionDeadline.from_env(env={ENV_BUDGET_SECS: "NaN-ish"}) is None


def test_from_env_absolute_pair():
    now = time.time()
    dl = ExecutionDeadline.from_env(env={
        ENV_DEADLINE_TS: repr(now + 100),
        ENV_CLOSEOUT_TS: repr(now + 80),
    })
    assert dl is not None
    assert 99 < dl.remaining(now) <= 100
    assert not dl.in_closeout(now)
    assert dl.in_closeout(now + 80.1)
    assert dl.expired(now + 100.1)


def test_from_env_budget_fallback_uses_fraction():
    dl = ExecutionDeadline.from_env(env={ENV_BUDGET_SECS: "600"})
    assert dl is not None
    budget = dl.deadline_ts - dl.created_ts
    closeout = dl.closeout_ts - dl.created_ts
    assert budget == pytest.approx(600, abs=1)
    assert closeout == pytest.approx(600 * DEFAULT_CLOSEOUT_FRACTION, abs=1)


def test_closeout_never_past_deadline():
    now = time.time()
    dl = ExecutionDeadline(deadline_ts=now + 10, closeout_ts=now + 999, created_ts=now)
    assert dl.closeout_ts == dl.deadline_ts


def test_approved_policy_shapes():
    """10-min delegate ceiling → 8-min fuse; 20-min lane ceiling → 16-min fuse."""
    now = time.time()
    delegate = ExecutionDeadline.from_budget(600, 0.8, now=now)
    assert delegate.closeout_ts - now == pytest.approx(480)
    lane = ExecutionDeadline.from_budget(1200, 0.8, now=now)
    assert lane.closeout_ts - now == pytest.approx(960)


def test_cap_bounds_calls_to_remaining_budget():
    """AC3: no individual call (or provider retry attempt) can exceed the
    remaining deadline — each attempt's clock is capped, so a retry loop
    cannot restart a full-length wait."""
    now = time.time()
    dl = ExecutionDeadline(deadline_ts=now + 50, created_ts=now)
    assert dl.cap(1800, now=now) == pytest.approx(50)
    assert dl.cap(10, now=now) == 10
    # Near/через expiry: never below the floor (fail via the fuse, not
    # via a storm of 0-second call timeouts).
    assert dl.cap(1800, now=now + 49.5) == MIN_CALL_CAP_SECS
    assert dl.cap(1800, now=now + 200) == MIN_CALL_CAP_SECS


def test_env_roundtrip():
    now = time.time()
    dl = ExecutionDeadline.from_budget(1200, 0.8, now=now)
    env = dl.to_env()
    dl2 = ExecutionDeadline.from_env(env=env)
    assert dl2.deadline_ts == pytest.approx(dl.deadline_ts)
    assert dl2.closeout_ts == pytest.approx(dl.closeout_ts)


def test_env_names_survive_lane_run_routing_scrub():
    """dd-lane-run scrubs ^(HERMES|DD)_.*(ROUTE|SESSION|WAKE|RESUME|REPLY|
    CHAT_ID|THREAD_ID|CHANNEL_ID|SOURCE_CHANNEL_ID|SOURCE_THREAD_ID) from the
    child env. The deadline contract MUST NOT match, or children would never
    see their deadline. This pins the invariant against future renames."""
    scrub = re.compile(
        r"^(HERMES|DD)_.*(ROUTE|SESSION|WAKE|RESUME|REPLY|CHAT_ID|THREAD_ID|"
        r"CHANNEL_ID|SOURCE_CHANNEL_ID|SOURCE_THREAD_ID).*$"
    )
    for name in (ENV_DEADLINE_TS, ENV_CLOSEOUT_TS, ENV_BUDGET_SECS, "DD_RUN_DIR"):
        assert not scrub.match(name), f"{name} would be scrubbed from lane children"


def test_closeout_instruction_demands_partial():
    assert "PARTIAL" in CLOSEOUT_INSTRUCTION
    assert "tool calls" in CLOSEOUT_INSTRUCTION.lower()


# ── terminal-state/1 schema ─────────────────────────────────────────────


def _build(state, **kw):
    kw.setdefault("cause", "test")
    return ts.build_terminal_state(state, **kw)


def test_all_seven_states_build_and_validate():
    for state in ts.TERMINAL_STATES:
        payload = _build(state)
        assert ts.validate_terminal_state(payload) == []


def test_unknown_state_rejected():
    with pytest.raises(ValueError):
        _build("exploded")
    bad = _build("failed")
    bad["state"] = "exploded"
    assert ts.validate_terminal_state(bad)


def test_ac6_no_identical_retry_after_timeout_or_orphan():
    """AC6: timed_out/orphaned may never carry retry_disposition 'none' —
    the builder coerces, the validator rejects."""
    for state in (ts.STATE_TIMED_OUT, ts.STATE_ORPHANED):
        payload = _build(state, retry_disposition=ts.RETRY_NONE)
        assert payload["retry_disposition"] == ts.RETRY_CHANGE_RAIL
        forged = dict(payload, retry_disposition="none")
        assert any("AC6" in e for e in ts.validate_terminal_state(forged))


def test_default_dispositions():
    assert _build(ts.STATE_COMPLETED)["retry_disposition"] == ts.RETRY_NONE
    assert _build(ts.STATE_PARTIAL)["retry_disposition"] == ts.RETRY_NARROW_SCOPE
    assert _build(ts.STATE_TIMED_OUT)["retry_disposition"] == ts.RETRY_CHANGE_RAIL
    assert _build(ts.STATE_ORPHANED)["retry_disposition"] == ts.RETRY_CHANGE_RAIL
    assert _build(ts.STATE_RECOVERY_REQUIRED)["retry_disposition"] == ts.RETRY_ESCALATE


def test_write_is_atomic_and_readable(tmp_path):
    payload = _build(ts.STATE_TIMED_OUT, run_id="r1", budget_secs=1200,
                     evidence={"stdout": "stdout.log"})
    path = ts.write_terminal_state(str(tmp_path), payload)
    assert path.endswith(ts.FILENAME)
    back = ts.read_terminal_state(str(tmp_path))
    assert back["state"] == "timed_out"
    assert back["retry_disposition"] == "change_rail"
    # No temp litter (atomicity).
    litter = [p.name for p in tmp_path.iterdir() if p.name.startswith(".terminal-state.")]
    assert litter == []


def test_read_invalid_returns_none(tmp_path):
    (tmp_path / ts.FILENAME).write_text("{not json", encoding="utf-8")
    assert ts.read_terminal_state(str(tmp_path)) is None
    (tmp_path / ts.FILENAME).write_text(json.dumps({"schema": "terminal-state/1"}))
    assert ts.read_terminal_state(str(tmp_path)) is None


def test_write_rejects_invalid_payload(tmp_path):
    with pytest.raises(ValueError):
        ts.write_terminal_state(str(tmp_path), {"schema": "nope"})


# ── wake-event typed-state threading (lane_wake) ────────────────────────


def test_continuation_prompt_carries_state_and_retry_policy():
    from gateway.lane_wake import build_continuation_prompt

    prompt = build_continuation_prompt(
        wts_task="6a47a61a-f111-470f-a3e6-70f65b3c5904",
        lane="knowledge",
        gate="TIMED_OUT",
        run_dir="/tmp/x/runs/20260709-175735-30978",
        terminal_state="timed_out",
        retry_disposition="change_rail",
    )
    assert "Terminal state: timed_out" in prompt
    assert "Retry disposition: change_rail" in prompt
    assert "RETRY POLICY" in prompt
    assert "IDENTICAL packet" in prompt


def test_continuation_prompt_no_retry_block_for_completed():
    from gateway.lane_wake import build_continuation_prompt

    prompt = build_continuation_prompt(
        wts_task=None, lane="qa", gate="PASS", run_dir="/tmp/r",
        terminal_state="completed", retry_disposition="none",
    )
    assert "Terminal state: completed" in prompt
    assert "RETRY POLICY" not in prompt


def test_emit_wake_event_includes_typed_state(tmp_path, monkeypatch):
    import gateway.lane_wake as lw

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DD_LANE_WAKE_ENABLED", "1")
    run_dir = tmp_path / "dd-lanes" / "knowledge" / "runs" / "20260709-000000-1"
    run_dir.mkdir(parents=True)
    token = lw.emit_wake_event(
        run_dir=str(run_dir), lane="knowledge", gate="TIMED_OUT",
        session_key="agent:main:telegram:dm:123", platform="telegram",
        chat_id="123", chat_type="dm", wts_task="t-1",
        terminal_state="timed_out", retry_disposition="change_rail",
    )
    assert token.startswith("emitted(")
    events = list((tmp_path / "dd-lanes" / "wake-queue").glob("*.json"))
    assert len(events) == 1
    ev = json.loads(events[0].read_text())
    assert ev["terminal_state"] == "timed_out"
    assert ev["retry_disposition"] == "change_rail"
    assert "RETRY POLICY" in ev["prompt"]


# ── delegate config readers ─────────────────────────────────────────────


def test_closeout_fraction_clamped(monkeypatch):
    from tools import delegate_tool as dt

    monkeypatch.setattr(dt, "_load_config", lambda: {"closeout_fraction": "0.2"})
    assert dt._get_closeout_fraction() == 0.5
    monkeypatch.setattr(dt, "_load_config", lambda: {"closeout_fraction": "0.99"})
    assert dt._get_closeout_fraction() == 0.95
    monkeypatch.setattr(dt, "_load_config", lambda: {"closeout_fraction": "garbage"})
    assert dt._get_closeout_fraction() == 0.8
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.delenv("DELEGATION_CLOSEOUT_FRACTION", raising=False)
    assert dt._get_closeout_fraction() == 0.8


def test_collect_grace_clamped(monkeypatch):
    from tools import delegate_tool as dt

    monkeypatch.setattr(dt, "_load_config", lambda: {"collect_grace_seconds": 999})
    assert dt._get_collect_grace_secs() == 120.0
    monkeypatch.setattr(dt, "_load_config", lambda: {"collect_grace_seconds": 1})
    assert dt._get_collect_grace_secs() == 5.0
    monkeypatch.setattr(dt, "_load_config", lambda: {})
    monkeypatch.delenv("DELEGATION_COLLECT_GRACE_SECONDS", raising=False)
    assert dt._get_collect_grace_secs() == 30.0
