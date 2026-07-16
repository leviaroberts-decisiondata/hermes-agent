"""Lane-result ACTIVE WAKE bridge (WTS 331b65f8).

Why this exists
---------------
A detached specialist-lane run is reaped by ``dd-lane-reaper`` and its closeout
is *mirrored* into the caller's gateway session via
``gateway.mirror.mirror_to_session`` — a PASSIVE transcript append. That mirror
lets P1 *see* the result in history, but it does NOT trigger a new P1 reasoning
turn: P1 only continues after an inbound event (a user message). So a lane that
finished while P1 was idle sat un-continued until Levi nudged.

This module is the ACTIVE half — kept strictly SEPARATE from the passive mirror
(do not change ``mirror_to_session`` semantics). A lane-result producer (the
reaper or the chain driver) calls :func:`emit_wake_event` to drop a small JSON
event onto a durable file queue. The gateway runs a standing background drain
(``GatewayRunner._drain_lane_wake_queue``) that consumes the queue and injects a
REAL internal ``MessageEvent(internal=True)`` — the same proven wake path the
in-process background-process watcher uses (``_inject_watch_notification``) — so
P1 wakes and reconciles the result autonomously.

Design invariants
-----------------
* PASSIVE MIRROR and ACTIVE WAKE are independent. The mirror still fires exactly
  as before; the wake is additive and FLAG-GATED (``DD_LANE_WAKE_ENABLED``, the
  emit side; the drain side reads the same flag). Either can be turned off.
* IDEMPOTENT. Each event carries a stable ``idempotency_key`` (lane-run id +
  wts_task + kind). The writer refuses to enqueue a key it has already enqueued;
  the drain refuses to inject a key it has already processed. A reaper re-run or
  a duplicate reap therefore never double-wakes P1 (→ never double-dispatches QA
  or double-closes).
* DURABLE + CROSS-PROCESS. The queue is a directory of one-file-per-event JSON
  under ``$HERMES_HOME/dd-lanes/wake-queue/``; the producer (a separate process)
  and the gateway never share memory. Writes are atomic (tmp + os.replace).
* FAIL-SOFT. A wake failure NEVER breaks the producer or the mirror — the result
  still reached the human and the transcript. The wake is best-effort recovery
  of the *autonomous-continuation* property, not a delivery guarantee.

The event is data only — it carries no secret. The structured P1 continuation
prompt is built here (:func:`build_continuation_prompt`) so the contract lives in
one tested place, and it explicitly forbids unauthorized deploy/restart/push/
merge/Slack-canary actions (req 5).
"""

from __future__ import annotations

import json
import os
import re
import time
import hashlib
import tempfile
from pathlib import Path
from typing import Optional


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))


def wake_queue_dir() -> Path:
    return hermes_home() / "dd-lanes" / "wake-queue"


def _processed_dir() -> Path:
    return wake_queue_dir() / ".processed"


def _enqueued_dir() -> Path:
    return wake_queue_dir() / ".enqueued"


# The producer (emit) side feature flag. Default ON so the canary works out of
# the box, but it remains a real off switch: DD_LANE_WAKE_ENABLED in {0,false,off,no}.
def wake_enabled() -> bool:
    raw = (os.getenv("DD_LANE_WAKE_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _safe_key_filename(key: str) -> str:
    """A filesystem-safe, collision-resistant filename for an idempotency key."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", key)[:80]
    return f"{slug}.{digest}"


def make_idempotency_key(*, run_id: str, wts_task: Optional[str], kind: str) -> str:
    """Stable dedupe key. Mirrors the packet's recommended shape
    ``lane-result:<run>:<wts>:<kind>`` — same (run, task, kind) → same key, so a
    reaper re-run cannot produce a second wake for the same completed run."""
    return f"lane-result:{run_id}:{(wts_task or 'none')}:{kind}"


def build_continuation_prompt(
    *,
    wts_task: Optional[str],
    lane: str,
    gate: str,
    run_dir: str,
    result_relation: Optional[str] = None,
    result_file: Optional[str] = None,
    result_sha: Optional[str] = None,
    terminal_state: Optional[str] = None,
    retry_disposition: Optional[str] = None,
    closeout: Optional[str] = None,
) -> str:
    """The STRUCTURED INTERNAL continuation prompt P1 receives on wake (req 5).

    P1 is the workflow GOVERNOR here, not a chat responder. The prompt tells it to
    reconcile the just-returned lane result, update WTS, then decide the next move
    (route the next lane / hold for authorization / final synthesis) — and it
    HARD-FORBIDS running the Slack canary, a deploy, a restart, a push, or a merge
    without durable authorization."""
    run_id = Path(run_dir.rstrip("/")).name if run_dir else "(unknown run)"
    art = result_relation or result_file or "(see the lane run dir / WTS task)"
    return (
        "[LANE RESULT RETURNED — CONTINUE WORKFLOW]\n"
        f"WTS: {wts_task or '(none — see safety note)'}\n"
        f"Lane: {lane}\n"
        f"Gate: {gate}\n"
        + (f"Terminal state: {terminal_state}\n" if terminal_state else "")
        + (f"Retry disposition: {retry_disposition}\n" if retry_disposition else "")
        + f"Run dir: {run_dir}\n"
        f"Run id: {run_id}\n"
        f"Result artifact: {art}"
        + (f"  (sha {result_sha[:12]})" if result_sha else "")
        + "\n\n"
        "Instruction (you are the workflow governor — act now, do NOT wait for a "
        "user message):\n"
        "1. Reconcile this lane result against the request and your earlier HANDOFF "
        "PENDING for this lane.\n"
        "2. Update WTS: confirm the final (non-pending) lane result artifact is "
        "attached to the bound task; if it is missing, attach it.\n"
        "3. Decide the next action under policy:\n"
        "   - route the NEXT lane (e.g. engineering PASS → QA) via route_to_lane; or\n"
        "   - HOLD for authorization and say exactly what you are waiting on; or\n"
        "   - emit the FINAL SYNTHESIS (owner + next action) and close out.\n"
        "4. If the lane says no downstream handoff is needed, emit a final "
        "hold/owner/action — do NOT go idle.\n\n"
        "SAFETY (hard): Do NOT run the Slack canary, submit/execute a deploy, "
        "restart any service, push, or merge as part of this continuation unless "
        "you hold DURABLE, explicit authorization for that specific action. "
        "Surfacing a deploy-ready state and HOLDING is correct; taking the last "
        "mile is not. If WTS is unknown/missing, HOLD and surface that — do not "
        "best-effort continue."
        + (
            "\n\nRETRY POLICY (hard, WTS ac4bcb05): this execution ended "
            f"{terminal_state}. Do NOT re-dispatch an IDENTICAL packet on the same "
            "rail — the runner will refuse it (RETRY_BLOCKED). Either NARROW the "
            "scope (smaller packet), promote to the durable/background rail "
            "(dd-lane-run --background, collect with --poll), or HOLD and "
            "escalate to the operator with the partial evidence."
            if terminal_state in ("timed_out", "orphaned") else ""
        ) + (
        (
            "\n\n--- authoritative closeout (single source of truth; composed once by "
            "the reaper — receipts + integrity flags included) ---\n" + closeout
        ) if closeout else ""
    )
    )


def emit_wake_event(
    *,
    run_dir: str,
    lane: str,
    gate: str,
    session_key: str,
    platform: str,
    chat_id: str,
    chat_type: str = "",
    thread_id: Optional[str] = None,
    wts_task: Optional[str] = None,
    kind: str = "lane-result",
    result_relation: Optional[str] = None,
    result_file: Optional[str] = None,
    result_sha: Optional[str] = None,
    source_label: str = "dd-lane-reaper",
    terminal_state: Optional[str] = None,
    retry_disposition: Optional[str] = None,
    closeout: Optional[str] = None,
) -> str:
    """Enqueue a single ACTIVE-WAKE event onto the durable file queue.

    Returns a short status token (no secrets):
      ``emitted(<key>)``          — a new event was written
      ``skipped(disabled)``       — DD_LANE_WAKE_ENABLED is off
      ``skipped(no-routing)``     — no session/platform/chat_id to wake
      ``skipped(dup:<key>)``      — this idempotency key was already enqueued
      ``skipped(processed:<key>)``— this key was already drained+injected
      ``error(<reason>)``         — best-effort failure (never raised)

    Idempotency is enforced HERE (writer side) AND in the drain (consumer side):
    a key seen in either ``.enqueued`` or ``.processed`` is refused. The
    ``.enqueued`` marker is removed by the drain after it processes the event, so
    a genuinely new run (new run_id) is never blocked.
    """
    try:
        if not wake_enabled():
            return "skipped(disabled)"
        # A wake needs a real session to target. Without platform+chat_id (or a
        # parseable session_key) there is nothing to inject into — the mirror still
        # delivered the substance, so this is a clean skip, not an error.
        if not (platform and chat_id) and not session_key:
            return "skipped(no-routing)"

        run_id = Path(run_dir.rstrip("/")).name if run_dir else ""
        key = make_idempotency_key(run_id=run_id, wts_task=wts_task, kind=kind)
        fname = _safe_key_filename(key)

        qdir = wake_queue_dir()
        qdir.mkdir(parents=True, exist_ok=True)
        _enqueued_dir().mkdir(parents=True, exist_ok=True)
        _processed_dir().mkdir(parents=True, exist_ok=True)

        # Idempotency gate (writer side).
        if (_processed_dir() / fname).exists():
            return f"skipped(processed:{key})"
        if (_enqueued_dir() / fname).exists() or (qdir / f"{fname}.json").exists():
            return f"skipped(dup:{key})"

        event = {
            "schema": "lane-wake/1",
            "idempotency_key": key,
            "kind": kind,
            "run_dir": run_dir,
            "run_id": run_id,
            "lane": lane,
            "gate": gate,
            "session_key": session_key,
            "platform": platform,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "thread_id": thread_id or None,
            "wts_task": (wts_task or "").strip() or None,
            "result_relation": result_relation or None,
            "result_file": result_file or None,
            "result_sha": result_sha or None,
            "source_label": source_label,
            "terminal_state": (terminal_state or "").strip() or None,
            "retry_disposition": (retry_disposition or "").strip() or None,
            "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # Exactly-once delivery (board 2026-07-15): the wake embeds the reaper's
        # closeout so the injected turn IS the authoritative receipt — no second
        # composition, no reinject double-delivery. Keep the tail (receipts +
        # integrity flags live at the end) when truncating.
        _co = (closeout or "").strip()
        if _co and len(_co) > 8000:
            _co = "…[head truncated]\n" + _co[-8000:]
        event["closeout_embedded"] = bool(_co)
        event["prompt"] = build_continuation_prompt(
            wts_task=event["wts_task"],
            lane=lane,
            gate=gate,
            run_dir=run_dir,
            result_relation=result_relation,
            result_file=result_file,
            result_sha=result_sha,
            terminal_state=event["terminal_state"],
            retry_disposition=event["retry_disposition"],
            closeout=_co or None,
        )

        # Atomic write: tmp in the same dir, then os.replace.
        fd, tmp = tempfile.mkstemp(dir=str(qdir), prefix=".wake-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(event, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(qdir / f"{fname}.json"))
        # Mark enqueued so a same-tick duplicate emit is refused even before the
        # drain runs. (Removed by the drain once consumed.)
        try:
            (_enqueued_dir() / fname).write_text(event["enqueued_at"], encoding="utf-8")
        except Exception:
            pass
        return f"emitted({key})"
    except Exception as exc:  # never raise into the producer
        return f"error({type(exc).__name__})"


def list_pending_events() -> list[dict]:
    """Return queued (un-processed) wake events, oldest first. Drain side."""
    qdir = wake_queue_dir()
    if not qdir.is_dir():
        return []
    events = []
    for p in sorted(qdir.glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            ev = json.loads(p.read_text(encoding="utf-8"))
            ev["_path"] = str(p)
            events.append(ev)
        except Exception:
            # A malformed event file is moved aside so it can't wedge the drain.
            try:
                p.rename(p.with_suffix(".json.bad"))
            except Exception:
                pass
    return events


def already_processed(idempotency_key: str) -> bool:
    """True iff this key was already drained+injected (consumer-side dedupe)."""
    return (_processed_dir() / _safe_key_filename(idempotency_key)).exists()


def _processed_marker_payload(event: dict, *, outcome: str) -> str:
    key = event.get("idempotency_key") or ""
    return json.dumps(
        {
            "idempotency_key": key,
            "outcome": outcome,
            "run_id": event.get("run_id"),
            "wts_task": event.get("wts_task"),
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


def remove_event_file(event: dict) -> None:
    """Remove a queue file fail-soft; used by losing consumers too."""
    path = event.get("_path")
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


def claim_processed(event: dict, *, outcome: str = "claimed") -> bool:
    """Atomically claim an event's idempotency key for at-most-once injection.

    Returns True only for the process that created the processed marker with
    O_CREAT|O_EXCL. Losing consumers must skip injection.
    """
    key = event.get("idempotency_key") or ""
    if not key:
        return True
    fname = _safe_key_filename(key)
    marker = _processed_dir() / fname
    try:
        _processed_dir().mkdir(parents=True, exist_ok=True)
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_processed_marker_payload(event, outcome=outcome))
        (_enqueued_dir() / fname).unlink(missing_ok=True)
        return True
    except FileExistsError:
        remove_event_file(event)
        return False
    except Exception:
        return False


def mark_processed(event: dict, *, outcome: str = "injected") -> None:
    """Record that an event was consumed: write/update a durable processed marker,
    drop the .enqueued marker, and remove the queue file. Idempotent and fail-soft."""
    key = event.get("idempotency_key") or ""
    fname = _safe_key_filename(key) if key else None
    try:
        _processed_dir().mkdir(parents=True, exist_ok=True)
        if fname:
            marker = _processed_dir() / fname
            if not marker.exists():
                claim_processed(event, outcome=outcome)
            else:
                marker.write_text(_processed_marker_payload(event, outcome=outcome), encoding="utf-8")
                (_enqueued_dir() / fname).unlink(missing_ok=True)
    except Exception:
        pass
    # Remove the queue file last (the processed marker is the source of truth).
    remove_event_file(event)


# ── tiny CLI so the bash producers (dd-lane-reaper / dd-chain-driver) can emit ──
# without re-implementing the schema/idempotency. Prints the status token.
def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="lane_wake", description="emit a lane-result wake event")
    ap.add_argument("--emit", action="store_true", help="enqueue a wake event")
    ap.add_argument("--closeout-file", default="")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--lane", default="")
    ap.add_argument("--gate", default="")
    ap.add_argument("--session-key", default="")
    ap.add_argument("--platform", default="")
    ap.add_argument("--chat-id", default="")
    ap.add_argument("--chat-type", default="")
    ap.add_argument("--thread-id", default="")
    ap.add_argument("--wts-task", default="")
    ap.add_argument("--kind", default="lane-result")
    ap.add_argument("--result-relation", default="")
    ap.add_argument("--result-file", default="")
    ap.add_argument("--result-sha", default="")
    ap.add_argument("--source-label", default="dd-lane-reaper")
    ap.add_argument("--terminal-state", default="", help="typed terminal state (terminal-state/1)")
    ap.add_argument("--retry-disposition", default="", help="typed retry disposition (terminal-state/1)")
    ap.add_argument("--list", action="store_true", help="print pending events (no secrets)")
    args = ap.parse_args(argv)

    if args.list:
        for ev in list_pending_events():
            print(json.dumps({k: v for k, v in ev.items() if k != "prompt"}, ensure_ascii=False))
        return 0

    if args.emit:
        _closeout = None
        if args.closeout_file:
            try:
                _closeout = Path(args.closeout_file).read_text(encoding="utf-8", errors="replace")
            except OSError:
                _closeout = None
        token = emit_wake_event(
            closeout=_closeout,
            run_dir=args.run_dir,
            lane=args.lane,
            gate=args.gate,
            session_key=args.session_key,
            platform=args.platform,
            chat_id=args.chat_id,
            chat_type=args.chat_type,
            thread_id=args.thread_id or None,
            wts_task=args.wts_task or None,
            kind=args.kind,
            result_relation=args.result_relation or None,
            result_file=args.result_file or None,
            result_sha=args.result_sha or None,
            source_label=args.source_label,
            terminal_state=args.terminal_state or None,
            retry_disposition=args.retry_disposition or None,
        )
        print(token)
        # Exit 0 on a clean emit/skip; 9 only on a real error (so bash can log it).
        return 0 if not token.startswith("error(") else 9

    ap.print_help()
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
