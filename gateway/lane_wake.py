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
import sys
import time
import hashlib
import tempfile
from pathlib import Path
from typing import Optional


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))


# ── GOVERNOR ADMISSION (WTS 319dc317, §4). Best-effort import of the ONE admission
# authority from ~/.hermes/bin, mirroring the driver's discipline: a missing helper
# NEVER breaks the wake. lane_wake is one of the two loops §4 names ("you wire
# lane_wake here"). SHADOW-ONLY here: a wake for a failed lane still emits exactly
# as today; the admission verdict is journaled + surfaced in the continuation prompt
# so P1 sees the governor's would-stop, but the wake is NOT suppressed (enforce is a
# separate Levi-gated decision on the P1 continuation path, not this emit).
def _admission_bin_dir() -> str:
    return str((hermes_home() / "bin"))


try:
    if _admission_bin_dir() not in sys.path:
        sys.path.insert(0, _admission_bin_dir())
    from dd_admission import admit_attempt as _admit_attempt  # noqa: E402
    from dd_admission import normalize_failure_class as _admit_normalize  # noqa: E402
    from dd_admission import convergence_fingerprint as _admit_fingerprint  # noqa: E402
    _ADMISSION_AVAILABLE = True
except Exception:
    _ADMISSION_AVAILABLE = False

    def _admit_attempt(*_a, **_k):  # type: ignore
        return None

    def _admit_normalize(_x):  # type: ignore
        return "unknown"

    def _admit_fingerprint(**_k):  # type: ignore
        return ""


# A wake carries a WORK-RETRY signal only for these terminal states / gates; a
# clean PASS wake is a normal continuation, NOT a retry, and is not admitted.
_RETRY_TERMINAL_STATES = frozenset({"timed_out", "orphaned", "recovery_required"})
_RETRY_GATES = frozenset({"FAIL", "FAILED", "BLOCK", "BLOCKED", "ERROR", "STALLED",
                          "NEEDS-YOU", "NEEDSYOU"})


def _is_work_retry_wake(gate: str, terminal_state: Optional[str]) -> bool:
    g = (gate or "").strip().upper()
    ts = (terminal_state or "").strip().lower()
    return ts in _RETRY_TERMINAL_STATES or g in _RETRY_GATES


def shadow_admission_for_wake(*, wts_task: Optional[str], lane: str, gate: str,
                              run_id: str, terminal_state: Optional[str],
                              elapsed: Optional[int] = None) -> Optional[dict]:
    """Consult the ONE admission authority for a wake that WOULD drive a work retry.
    Returns the decision dict (also journaled) or None (not a retry wake / module
    unavailable). SHADOW: the caller still emits the wake; it only records the
    verdict + surfaces it. obligation_id == wts_task (the parent identity)."""
    if not _ADMISSION_AVAILABLE:
        return None
    if not _is_work_retry_wake(gate, terminal_state):
        return None
    obligation = (wts_task or "").strip() or f"lane:{lane}"
    fclass = _admit_normalize(terminal_state or gate or lane)
    # stable root key so a relabel (timed_out↔orphaned on the same lane) shares
    # one retry budget line.
    rck = f"{obligation}:{lane}:{fclass}"
    attempt_id = f"{obligation}:{lane}:{run_id or 'run'}"
    fp = _admit_fingerprint(failure_class=fclass, target_surface=lane,
                            acceptance_test=(gate or ""), root_cause_key=rck)
    try:
        dec = _admit_attempt(obligation, attempt_id, fclass, rck,
                             delta=None, elapsed=elapsed,
                             convergence=fp, mode="shadow", persist=True)
        return dec.__dict__ if dec is not None else None
    except Exception:
        return None


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
    gov_admission: Optional[dict] = None,
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
        ) + (
            "\n\nGOVERNOR ADMISSION (§4, SHADOW — advisory this run): the mission "
            f"governor's verdict for a retry here is **{gov_admission.get('verdict')}** "
            f"(reason: {gov_admission.get('reason')}; root-cause retries used "
            f"{gov_admission.get('class_retries_used')}, total {gov_admission.get('total_retries_used')}). "
            + ("It WOULD STOP an automatic retry — prefer consolidating to a single "
               "operator decision over re-dispatching. "
               if gov_admission.get("would_stop") else
               "It would admit a retry that carries a MEANINGFUL delta. ")
            + "This is shadow guidance; enforcement is operator-gated."
            if gov_admission else ""
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
    instance: Optional[str] = None,
    destination_instance: Optional[str] = None,
    originating_session_id: Optional[str] = None,
    mission_id: Optional[str] = None,
    chain_id: Optional[str] = None,
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

        # GOVERNOR ADMISSION (§4, SHADOW) — for a wake that would drive a work
        # retry, record the would-stop verdict + surface it in the prompt. Does
        # NOT suppress the wake (shadow); persist-before-dispatch is honored (the
        # decision is journaled before the event is written).
        gov = shadow_admission_for_wake(wts_task=wts_task, lane=lane, gate=gate,
                                        run_id=run_id, terminal_state=terminal_state)

        event = {
            # lane-wake/2 adds the callback AUTHORITY fields (instance,
            # destination_instance, originating_session_id, mission/chain) —
            # WTS 17cbc96c. The shape is otherwise unchanged and readers accept
            # v1 too, but a v1 event carries no instance and is therefore
            # quarantined by the receiving gateway rather than admitted.
            "schema": "lane-wake/2",
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
            # §4 shadow: the governor admission verdict for this (retry) wake, or
            # None if this wake is not a work-retry. A projection/signal only.
            "gov_admission": ({"verdict": gov.get("decision"), "reason": gov.get("reason"),
                               "would_stop": gov.get("would_stop"),
                               "class_retries_used": gov.get("class_retries_used"),
                               "total_retries_used": gov.get("total_retries_used"),
                               "mode": gov.get("mode")} if gov else None),
            "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # ── CALLBACK AUTHORITY (WTS 17cbc96c) ──
        # Explicit args win (the producer knows); otherwise the fields are read
        # from the run's OWN dispatch record, so the reaper's existing CLI call
        # gains authority without an argument change. Nothing is invented: a run
        # with no record produces an event with no authority, and the receiving
        # gateway quarantines it. That is the designed failure mode.
        for _field, _value in (
            ("instance", instance),
            ("destination_instance", destination_instance),
            ("originating_session_id", originating_session_id),
            ("mission_id", mission_id),
            ("chain_id", chain_id),
        ):
            _clean = (_value or "").strip() if isinstance(_value, str) else _value
            if _clean:
                event[_field] = _clean
        try:
            from tools import dispatch_authority as _da

            _da.stamp_event_authority(event, run_dir=run_dir)
        except Exception:
            pass
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
            gov_admission=event["gov_admission"],
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
