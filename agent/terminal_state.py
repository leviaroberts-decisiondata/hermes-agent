"""Canonical typed terminal-state contract — schema ``terminal-state/1``.

WTS ac4bcb05: parent continuation, the lane reaper, wake injection, Slack
settlement, and the WTS attachment each spoke a different outcome
vocabulary, so a killed specialist surfaced as a reaper-synthesized exit
124 and a delegate timeout surfaced as ``summary=None``.  This module owns
the ONE machine-readable settlement record every surface consumes.

The same schema is written by two producers:

* this module (delegate_tool / any Python supervisor), and
* the dd-lane-run daemon (stdlib-only embedded Python — it cannot import
  the hermes venv).  Schema parity between the two writers is enforced by
  fixture tests on both sides (tests/test_terminal_state.py and
  tests/dd-lane-run-lifecycle.test.sh) — change the schema in ONE place
  only ever together with those fixtures.

Deliberately NOT a WTS ``tasks.status`` value: execution-attempt state is
attached to the task (file attachment / note projection), never written
into the task's own status enum.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

SCHEMA = "terminal-state/1"
FILENAME = "terminal-state.json"

# The seven approved terminal states.
STATE_COMPLETED = "completed"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
STATE_TIMED_OUT = "timed_out"
STATE_CANCELLED = "cancelled"
STATE_ORPHANED = "orphaned"
STATE_RECOVERY_REQUIRED = "recovery_required"

TERMINAL_STATES = (
    STATE_COMPLETED,
    STATE_PARTIAL,
    STATE_FAILED,
    STATE_TIMED_OUT,
    STATE_CANCELLED,
    STATE_ORPHANED,
    STATE_RECOVERY_REQUIRED,
)

# How the next attempt must differ.  "identical" retries after
# timed_out/orphaned are forbidden (AC6) — producers must emit one of the
# differentiated dispositions for those states.
RETRY_NONE = "none"                # done / do not retry
RETRY_NARROW_SCOPE = "narrow_scope"  # retry allowed with a smaller packet
RETRY_CHANGE_RAIL = "change_rail"    # retry allowed on the durable rail
RETRY_ESCALATE = "escalate"          # needs an operator decision

RETRY_DISPOSITIONS = (RETRY_NONE, RETRY_NARROW_SCOPE, RETRY_CHANGE_RAIL, RETRY_ESCALATE)

REQUIRED_KEYS = (
    "schema",
    "state",
    "cause",
    "exit_code",
    "started_at",
    "ended_at",
    "budget_secs",
    "elapsed_secs",
    "closeout",
    "evidence",
    "retry_disposition",
    "run_id",
    "wts_task",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_terminal_state(
    state: str,
    *,
    cause: str,
    exit_code: Optional[int] = None,
    killed_by: Optional[str] = None,
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    budget_secs: Optional[float] = None,
    elapsed_secs: Optional[float] = None,
    closeout_entered: bool = False,
    closeout_at: Optional[str] = None,
    evidence: Optional[dict] = None,
    retry_disposition: Optional[str] = None,
    run_id: Optional[str] = None,
    wts_task: Optional[str] = None,
    partial: Optional[dict] = None,
) -> dict:
    if state not in TERMINAL_STATES:
        raise ValueError(f"unknown terminal state {state!r}; must be one of {TERMINAL_STATES}")
    if retry_disposition is None:
        retry_disposition = _default_retry_disposition(state)
    if retry_disposition not in RETRY_DISPOSITIONS:
        raise ValueError(
            f"unknown retry_disposition {retry_disposition!r}; must be one of {RETRY_DISPOSITIONS}"
        )
    # AC6: an identical blind retry after timeout/orphan is structurally
    # unrepresentable — those states may not carry disposition "none"
    # unless an operator explicitly escalated.
    if state in (STATE_TIMED_OUT, STATE_ORPHANED) and retry_disposition == RETRY_NONE:
        retry_disposition = _default_retry_disposition(state)

    payload = {
        "schema": SCHEMA,
        "state": state,
        "cause": str(cause or ""),
        "exit_code": exit_code if exit_code is None else int(exit_code),
        "killed_by": killed_by,
        "started_at": started_at,
        "ended_at": ended_at or utc_now_iso(),
        "budget_secs": budget_secs if budget_secs is None else float(budget_secs),
        "elapsed_secs": elapsed_secs if elapsed_secs is None else float(elapsed_secs),
        "closeout": {"entered": bool(closeout_entered), "at": closeout_at},
        "evidence": dict(evidence or {}),
        "retry_disposition": retry_disposition,
        "run_id": run_id,
        "wts_task": wts_task,
    }
    if partial:
        payload["partial"] = partial
    return payload


def _default_retry_disposition(state: str) -> str:
    if state in (STATE_COMPLETED,):
        return RETRY_NONE
    if state in (STATE_PARTIAL,):
        return RETRY_NARROW_SCOPE
    if state in (STATE_TIMED_OUT, STATE_ORPHANED):
        # The whole point of AC6: after a timeout/orphan the next attempt
        # must change shape — smaller scope or the durable rail.
        return RETRY_CHANGE_RAIL
    if state == STATE_RECOVERY_REQUIRED:
        return RETRY_ESCALATE
    return RETRY_NARROW_SCOPE  # failed / cancelled: retry with changes


def validate_terminal_state(payload: dict) -> list:
    """Return a list of schema violations (empty ⇒ valid).

    Shared by the Python tests AND the bash-side parity fixtures so both
    writers stay on one schema.
    """
    errors = []
    if not isinstance(payload, dict):
        return ["payload is not a dict"]
    if payload.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA!r}, got {payload.get('schema')!r}")
    for key in REQUIRED_KEYS:
        if key not in payload:
            errors.append(f"missing required key {key!r}")
    state = payload.get("state")
    if state not in TERMINAL_STATES:
        errors.append(f"state {state!r} not in {TERMINAL_STATES}")
    rd = payload.get("retry_disposition")
    if rd not in RETRY_DISPOSITIONS:
        errors.append(f"retry_disposition {rd!r} not in {RETRY_DISPOSITIONS}")
    if state in (STATE_TIMED_OUT, STATE_ORPHANED) and rd == RETRY_NONE:
        errors.append(f"state {state!r} may not carry retry_disposition 'none' (AC6)")
    closeout = payload.get("closeout")
    if not isinstance(closeout, dict) or "entered" not in closeout:
        errors.append("closeout must be a dict with an 'entered' key")
    if not isinstance(payload.get("evidence"), dict):
        errors.append("evidence must be a dict")
    return errors


def write_terminal_state(run_dir: str, payload: dict) -> str:
    """Atomically write ``run_dir/terminal-state.json``; returns the path.

    tmp+rename in the same directory so consumers (reaper, --poll,
    parents) can never observe a torn file.
    """
    errors = validate_terminal_state(payload)
    if errors:
        raise ValueError("invalid terminal-state payload: " + "; ".join(errors))
    path = os.path.join(run_dir, FILENAME)
    fd, tmp = tempfile.mkstemp(prefix=".terminal-state.", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_terminal_state(run_dir: str) -> Optional[dict]:
    """Read and validate; returns None when absent or invalid (fail open —
    consumers fall back to their legacy inference path)."""
    path = os.path.join(run_dir, FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    return payload if not validate_terminal_state(payload) else None
