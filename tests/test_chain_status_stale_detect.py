#!/usr/bin/env python3
"""WS-5 — chain_status stale/disagreement detection (read side).

Proves the snapshot parser + disagreement detector that turn the terminal-mirror gap
from a SILENT wrong answer into a LOUD warning:

  1. parses the most-recent [WORK OWNERSHIP …] snapshot from _session_messages;
  2. FIRES when the snapshot/ledger reads escalated/needs-you but the PG row is active
     (the exact §2.2b freeze) — and when the stage disagrees;
  3. STAYS QUIET when the row and snapshot agree (no false positive);
  4. STAYS QUIET when no snapshot is in context (cannot over-claim).

Pure offline unit test (no network) — exercises the detection functions directly with a
fake parent_agent. Run under any python:
  ~/.hermes/hermes-agent/venv/bin/python ~/.hermes/hermes-agent/tests/test_chain_status_stale_detect.py
"""
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
sys.path.insert(0, str(HERMES / "hermes-agent"))

# The registry import pulls config; isolate by importing the module's functions directly.
from tools import chain_status_tool as cs  # noqa: E402

FAILS = []


def _check(cond, label):
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}")
    if not cond:
        FAILS.append(label)


class _FakeAgent:
    def __init__(self, messages):
        self._session_messages = messages


# A real escalated snapshot, verbatim in the driver's wording (fields_line + owner).
_ESC_SNAPSHOT = (
    "[WORK OWNERSHIP — I own this request through engineering → qa → deploy → report "
    "(task n/a); you don't need to ask.]\n"
    "stage: qa · owner: DD P1 (me) — escalated to you; awaiting your unblock/redirect · "
    "next: deploy · blocker: could not dispatch the qa stage · eta: blocked — needs you\n"
    "still holding this — resting escalated, awaiting your direction."
)
_ACTIVE_SNAPSHOT = (
    "[WORK OWNERSHIP — I own this request through engineering → qa (task n/a); "
    "you don't need to ask.]\n"
    "stage: engineering · owner: engineering lane (run abc123) · next: qa · "
    "blocker: none · eta: ~5 min\n"
    "engineering lane is running."
)


def main():
    print("== WS-5 chain_status stale/disagreement detection ==")

    # ── Parser: most-recent snapshot wins, escalation flagged ──
    agent = _FakeAgent([
        {"role": "user", "content": "where are we?"},
        {"role": "assistant", "content": _ACTIVE_SNAPSHOT},
        {"role": "assistant", "content": _ESC_SNAPSHOT},  # newer — should win
    ])
    snap = cs._latest_work_ownership_snapshot(agent)
    _check(snap is not None, "parser finds a snapshot")
    _check(snap and snap.get("stage") == "qa", f"parser takes the LATEST snapshot (stage={snap and snap.get('stage')})")
    _check(snap and snap.get("escalated") is True, "parser flags escalation wording")

    # ── (2a) FIRES: snapshot escalated, PG row frozen at active ──
    pg_frozen_active = {"id": "x", "status": "active", "current_stage": "engineering"}
    w = cs._detect_disagreement(pg_frozen_active, snap)
    _check(w is not None and "STALE MIRROR" in w,
           "FIRES on escalated-snapshot vs active-PG (the §2.2b freeze)")
    _check(w is not None and "escalat" in w.lower(),
           "warning names the escalation disagreement")

    # ── (2b) FIRES: stage disagreement even if both 'active' ──
    snap_active = cs._latest_work_ownership_snapshot(_FakeAgent(
        [{"role": "assistant", "content": _ACTIVE_SNAPSHOT}]))
    pg_other_stage = {"id": "x", "status": "active", "current_stage": "qa"}
    w2 = cs._detect_disagreement(pg_other_stage, snap_active)
    _check(w2 is not None and "stage" in w2.lower(),
           "FIRES on stage mismatch (snapshot=engineering vs PG=qa)")

    # ── (3) QUIET: row agrees with snapshot ──
    pg_agree = {"id": "x", "status": "escalated", "current_stage": "qa"}
    w3 = cs._detect_disagreement(pg_agree, snap)
    _check(w3 is None, "QUIET when PG row matches the escalated snapshot (no false positive)")

    pg_agree_active = {"id": "x", "status": "active", "current_stage": "engineering"}
    w3b = cs._detect_disagreement(pg_agree_active, snap_active)
    _check(w3b is None, "QUIET when PG row matches the active snapshot")

    # ── (4) QUIET: no snapshot in context (cannot over-claim staleness) ──
    w4 = cs._detect_disagreement(pg_frozen_active, None)
    _check(w4 is None, "QUIET when there is NO snapshot in context")
    empty_snap = cs._latest_work_ownership_snapshot(_FakeAgent(
        [{"role": "user", "content": "hi"}]))
    _check(empty_snap is None, "parser returns None when no snapshot present")

    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} CHECK(S) FAILED:")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("RESULT: ALL CHECKS PASSED — stale-detection fires on disagreement, quiet on agreement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
