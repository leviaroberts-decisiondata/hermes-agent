"""Dispatch authority: who dispatched a lane, and where its callback may return.

WTS 17cbc96c, Phase B/C.

The 2026-08-10 crossover happened because a lane callback's *destination* was
resolved from ``(platform, chat_id)``. Levi's Telegram chat id ``8737984752`` is
identical across all five bots, so it is not an authority — PTG's and Azul's lane
results came back through P1's shared reaper, were injected into P1's Telegram
session, and P1 then wrote reconciliation notes onto client WTS records.

This module is the record that makes a callback checkable. At dispatch time the
caller writes a small, versioned sidecar into the lane's ``run_dir``; at callback
time the receiving gateway compares the callback against it and refuses anything
that does not match, *before* the model is invoked.

Threat model — read this before "hardening" it
----------------------------------------------
**What this defends against: a confused deputy.** A callback that is misrouted,
replayed, fabricated by accident, or addressed to a different Hermes instance must
not be able to start a model turn in *this* gateway. That is the whole 2026-08-10
incident: nobody attacked anything. A shared reaper, a shared queue and a chat id
identical across five bots were enough, on their own, to deliver PTG's and Azul's
lane results into P1's session.

**What this does NOT defend against: a hostile process running as the same uid.**
All five Hermes homes, the reaper, the gateways and this file run as ``openclaw``.
Any process with that uid can rewrite this module, the sidecars, the wake queue and
the live scripts. An HMAC over the sidecar would be theatre — the key would sit in
a file the same uid can read, and the verifier is a file the same uid can edit. We
deliberately do **not** claim same-uid integrity, and no caller may describe these
checks as authentication. Real uid separation (running the sibling homes as
different users) is the only fix for the hostile case, and it is out of scope here.

What we add instead is cheap **structural containment**
(:func:`run_dir_containment`, :func:`run_identity_mismatch`): a callback must name a
run directory that really sits inside P1's lane tree at
``$HERMES_HOME/dd-lanes/<lane>/runs/<run_id>``, reached without a symlink or a
``..`` escape, carrying a real dispatch footprint (``meta.json`` naming the same
run), whose ``run_id`` agrees with the directory name, the sidecar and the event.
That defeats a *fabricated* record — before it, a reviewer's hand-written wake event
plus a matching sidecar in a scratch directory was ADMITTED — without pretending to
be a cryptographic boundary.

Design rules
------------
* **Identity is not a model argument.** ``caller_instance`` comes from
  :func:`hermes_cli.profiles.get_active_home_id`, derived from ``HERMES_HOME``.
* **Fail closed.** An unidentifiable instance is ``""``; it is never P1. A
  missing or malformed sidecar yields no authority at all, and readers refuse.
* **No new secret surface.** The sidecar carries no chat id, session key, token
  or transcript — the destination chat is recorded as a salted-free SHA-256
  *fingerprint* so a reader can prove "this authority belongs to that chat"
  without the file containing the identifier. It is still written 0600, beside
  ``wake-target``, because it is a trusted local-process channel.
* **Additive.** Writing the sidecar never changes the existing positional
  ``dd-lane-reaper --register`` contract; a run with no sidecar behaves exactly
  as before, except that its callback now has no authority and is quarantined
  rather than admitted (which is the point).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

# Versioned so a reader can refuse a shape it does not understand instead of
# guessing. Bump the integer for any incompatible field change.
SCHEMA = "dispatch-authority/1"
SIDECAR_NAME = "dispatch-authority"

# The wake-target sidecar (written by ~/.hermes/bin/dd-lane-run at spawn) gains
# `instance` + `session_id` in v2. Readers MUST accept both: v1 sidecars exist on
# disk for every historical run and are not rewritten.
WAKE_TARGET_SCHEMA_V1 = "wake-target/1"
WAKE_TARGET_SCHEMA_V2 = "wake-target/2"
WAKE_TARGET_NAME = "wake-target"

# Structural containment (see "Threat model" above). A lane run lives at exactly
# `$HERMES_HOME/dd-lanes/<lane>/runs/<run_id>` — three components under the lane
# tree, no more, no fewer — and every real dispatch drops a `meta.json` there
# (1313 of 1316 runs on disk at 2026-08-11; the three misses are aborted
# dispatches that never produced a callback either).
LANE_TREE_DIRNAME = "dd-lanes"
RUNS_DIRNAME = "runs"
DISPATCH_FOOTPRINT_NAME = "meta.json"


def active_instance() -> str:
    """This process's Hermes instance id, or "" when unidentifiable.

    "" is never P1 — every caller treats it as an unidentified instance and
    refuses. Import is local + defensive so an identity failure can never be
    read as authority, and never breaks a producer.
    """
    try:
        from hermes_cli.profiles import get_active_home_id

        return get_active_home_id() or ""
    except Exception:
        return ""


def route_fingerprint(platform: str, chat_type: str, chat_id: str) -> str:
    """Stable, non-reversible fingerprint of a callback's destination surface.

    Lets a reader prove a callback is for the same chat the dispatch targeted
    without the authority record ever containing the chat id. Not a secret and
    not a credential: it is a collision check, not an authenticator.
    """
    raw = f"{(platform or '').strip()}:{(chat_type or '').strip()}:{(chat_id or '').strip()}"
    if raw == "::":
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── structural containment ───────────────────────────────────────────────────
# The authority record answers "who dispatched this?". These answer the question
# underneath it: "is there a real dispatch here at all?" A record is only worth
# comparing when the directory it describes is genuinely one of P1's lane runs.
# Before this, a hand-written wake event plus a hand-written sidecar in a scratch
# directory was ADMITTED — the record was self-consistent and nothing checked
# that it described anything real.
#
# This is containment, not authentication. See the module threat model: it stops
# misrouting, replay and fabrication-by-accident; it does not stop a hostile
# process running as the same uid, and nothing here should be described as if it
# did.

def lane_tree_root(home: "str | Path | None" = None) -> Path:
    """P1's lane tree — ``$HERMES_HOME/dd-lanes``.

    Derived from HERMES_HOME (trusted runtime configuration), never from an
    event field, so a callback cannot nominate the root it will be measured
    against. Tests isolate by setting HERMES_HOME, exactly as the rest of the
    lane-wake machinery already does.
    """
    base = Path(str(home)) if home else Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
    return base / LANE_TREE_DIRNAME


def run_dir_containment(run_dir: "str | Path", *,
                        lane_root: "str | Path | None" = None) -> "str | None":
    """None when ``run_dir`` really is one of P1's lane runs; else a reason code.

    Three things must hold, and each one is a way a fabricated callback failed to
    be caught before:

    1. ``run_dir`` is absolute and names ``<lane>/runs/<run_id>`` under the lane
       tree — exactly three components, no ``..`` anywhere in the path.
    2. After full symlink resolution it is STILL that same directory. This is what
       rejects a planted link, whether the link is the run directory itself or any
       component above it, that would otherwise make an outside directory *look*
       contained.
    3. It carries a dispatch footprint: ``meta.json``, naming the same run id (and
       the same lane, when it records one). A bare ``mkdir -p`` fails here.

    Reason codes are stable strings — they are logged and stored in quarantine
    evidence, so treat them as an interface.
    """
    raw = str(run_dir or "").strip()
    if not raw:
        return "run_dir_absent"

    root = Path(str(lane_root)) if lane_root else lane_tree_root()

    # (1) shape + lexical containment. Done on the UNRESOLVED path so that a
    # ".." that would be normalised away is still seen.
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        return "run_dir_escape"
    try:
        rel = Path(os.path.normpath(raw)).relative_to(Path(os.path.normpath(str(root))))
    except Exception:
        return "run_dir_escape"
    parts = rel.parts
    if len(parts) != 3 or parts[1] != RUNS_DIRNAME or not parts[0] or not parts[2]:
        return "run_dir_escape"

    # (2) physical containment: resolve BOTH sides and require the same answer.
    # realpath the ROOT too, so a symlink in the root's own prefix (macOS
    # /tmp -> /private/tmp, and every pytest tmp_path) is not mistaken for an
    # escape, while a symlink at or below the root still is. A per-component
    # os.path.islink() walk was tried here and removed: it could not reject
    # anything this comparison does not already reject, and a guard no test can
    # distinguish is a guard nobody can maintain.
    try:
        real_root = Path(os.path.realpath(str(root)))
        real_run = Path(os.path.realpath(raw))
    except Exception:
        return "run_dir_escape"
    if real_run != real_root.joinpath(*parts) or not real_run.is_dir():
        return "run_dir_escape"

    # (3) a real dispatch happened here.
    try:
        meta = json.loads((real_run / DISPATCH_FOOTPRINT_NAME).read_text(encoding="utf-8"))
    except Exception:
        return "dispatch_footprint_absent"
    if not isinstance(meta, dict):
        return "dispatch_footprint_absent"
    meta_run = str(meta.get("run_id") or meta.get("lane_run_id") or "").strip()
    if not meta_run or meta_run != parts[2]:
        return "dispatch_footprint_absent"
    meta_lane = str(meta.get("lane") or "").strip()
    if meta_lane and meta_lane != parts[0]:
        return "dispatch_footprint_absent"
    return None


def run_identity_mismatch(run_dir: "str | Path", record: "dict | None",
                          claim: "dict | None" = None) -> "str | None":
    """None when the run id agrees everywhere; else ``"run_id_mismatch"``.

    The directory name is the anchor — it is the one value an attacker cannot
    choose freely once :func:`run_dir_containment` has pinned the directory to a
    real dispatch. The sidecar and the callback must both agree with it, so a
    genuine run's directory cannot be reused to carry another run's record.
    """
    basename = Path(str(run_dir or "").rstrip("/")).name
    if not basename:
        return "run_id_mismatch"
    rec_run = str((record or {}).get("run_id") or "").strip()
    if rec_run != basename:
        return "run_id_mismatch"
    claim_run = str((claim or {}).get("run_id") or "").strip()
    if claim_run and claim_run != basename:
        return "run_id_mismatch"
    return None


def build_authority(
    *,
    caller_instance: Optional[str] = None,
    destination_instance: Optional[str] = None,
    originating_session_id: str = "",
    run_id: str = "",
    run_dir: str = "",
    lane: str = "",
    wts_task: Optional[str] = None,
    mission_id: Optional[str] = None,
    chain_id: Optional[str] = None,
    platform: str = "",
    chat_type: str = "",
    chat_id: str = "",
) -> dict:
    """Compose the dispatch authority record for one lane run.

    ``caller_instance`` defaults to this process's instance and is NOT taken from
    a model argument. ``destination_instance`` defaults to the caller: a lane
    dispatched by P1 returns to P1, never to whichever gateway happens to drain
    the shared queue first.
    """
    caller = (caller_instance if caller_instance is not None else active_instance()) or ""
    dest = (destination_instance if destination_instance is not None else caller) or ""
    return {
        "schema": SCHEMA,
        "caller_instance": caller,
        "destination_instance": dest,
        "originating_session_id": (originating_session_id or "").strip(),
        "run_id": (run_id or "").strip(),
        "run_dir": (run_dir or "").strip(),
        "lane": (lane or "").strip(),
        "wts_task": ((wts_task or "").strip() or None),
        "mission_id": ((mission_id or "").strip() or None),
        "chain_id": ((chain_id or "").strip() or None),
        "route_fingerprint": route_fingerprint(platform, chat_type, chat_id),
        "created_at": _utc_now(),
    }


def sidecar_path(run_dir: "str | Path") -> Path:
    return Path(str(run_dir)) / SIDECAR_NAME


def write_sidecar(run_dir: "str | Path", authority: dict) -> "Path | None":
    """Atomically write the authority sidecar 0600. Returns the path, or None.

    Fail-soft by contract: a dispatch must not fail because its authority record
    could not be written. The consequence of a missing record is that the
    callback has no authority and is quarantined — fail closed downstream, not a
    broken dispatch here.
    """
    try:
        rd = Path(str(run_dir))
        rd.mkdir(parents=True, exist_ok=True)
        target = rd / SIDECAR_NAME
        fd, tmp = tempfile.mkstemp(dir=str(rd), prefix=".dispatch-authority-")
        try:
            os.fchmod(fd, 0o600)
        except Exception:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(authority, fh, separators=(",", ":"), sort_keys=True)
            fh.write("\n")
        os.replace(tmp, str(target))
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
        return target
    except Exception:
        return None


def read_sidecar(run_dir: "str | Path") -> "dict | None":
    """Read a run's authority record, or None when absent/unreadable/foreign.

    A sidecar whose ``schema`` is not a ``dispatch-authority/*`` string is
    treated as ABSENT, not as authority: an unrecognised shape must never be
    interpreted optimistically.
    """
    try:
        raw = json.loads(sidecar_path(run_dir).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    if not str(raw.get("schema") or "").startswith("dispatch-authority/"):
        return None
    return raw


def read_wake_target(run_dir: "str | Path") -> "dict | None":
    """Read the ``wake-target`` sidecar written by dd-lane-run at spawn.

    Tolerates BOTH schema versions: v1 (platform/chat_type/chat_id only, every
    historical run) and v2 (adds ``instance`` + ``session_id``). A v1 sidecar
    yields no authority fields — callers must treat that as "unknown", never as
    "P1".
    """
    try:
        raw = json.loads((Path(str(run_dir)) / WAKE_TARGET_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    if not str(raw.get("schema") or "").startswith("wake-target/"):
        return None
    return raw


def authority_for_run(run_dir: "str | Path") -> "dict | None":
    """Best available authority for a run: the dispatch sidecar, else wake-target v2.

    Returns a dict in the :data:`SCHEMA` shape (so one comparison path serves
    both sources) or None when neither source carries an instance + session.
    """
    rec = read_sidecar(run_dir)
    if rec:
        return rec
    wt = read_wake_target(run_dir)
    if not wt:
        return None
    instance = str(wt.get("instance") or "").strip()
    session = str(wt.get("session_id") or "").strip()
    if not instance or not session:
        # wake-target/1 (or a partially populated v2) — no authority to derive.
        return None
    return {
        "schema": SCHEMA,
        "caller_instance": instance,
        "destination_instance": instance,
        "originating_session_id": session,
        "run_id": str(wt.get("run_id") or "").strip(),
        "run_dir": str(run_dir),
        "lane": str(wt.get("lane") or "").strip(),
        "wts_task": (str(wt.get("wts_task") or "").strip() or None),
        "mission_id": None,
        "chain_id": None,
        "route_fingerprint": route_fingerprint(
            str(wt.get("platform") or ""),
            str(wt.get("chat_type") or ""),
            str(wt.get("chat_id") or ""),
        ),
        "created_at": str(wt.get("created_at") or ""),
        "authority_source": "wake-target/2",
    }


# ── comparison ───────────────────────────────────────────────────────────────
# One place decides whether a callback matches its dispatch record, so the
# gateway drain, the reaper and the tests all agree on what "matches" means.

# Fields where the CALLBACK may legitimately be silent about something the
# record knows. Silence is not a contradiction, and treating it as one quarantined
# real work.
#
# `wts_task`: the reaper reaps a run whose meta was scrubbed (the TOCTOU recovery
# path) and re-derives its routing from the mirror, but that path has no bound WTS
# task to pass, so it emits `wts_task: None`. The dispatch record — written by
# route_to_lane, which DID know the task — has one. That asymmetry is a normal P1
# callback, and quarantining it would drop a real result on the floor while
# reporting a security refusal.
#
# The tolerance is ONLY for absence. A callback that names a DIFFERENT WTS task
# than the record is still quarantined: that is the crossover shape (P1 writing on
# a client's task), and it is exactly what must never pass. Nothing about the
# DESTINATION is tolerated here — instance, session, destination and run_id are
# checked unconditionally above.
_ABSENCE_TOLERATED = ("wts_task",)


# Ordered so the most fundamental failure is reported first; the caller uses the
# first reason as the quarantine reason code.
def authority_mismatch(
    record: "dict | None",
    claim: "dict | None",
    *,
    receiving_instance: Optional[str] = None,
) -> "str | None":
    """None when ``claim`` is authorised by ``record``; else a reason code.

    ``record`` is the dispatch-time authority (the sidecar). ``claim`` is what
    the callback asserts. ``receiving_instance`` is the gateway asking — it must
    be the destination, otherwise a shared queue lets any gateway consume any
    result (exactly the crossover).

    Reason codes are stable strings; they are logged and stored in quarantine
    evidence, so treat them as an interface.
    """
    receiver = receiving_instance if receiving_instance is not None else active_instance()

    # A gateway that cannot name itself cannot claim to be anyone's destination.
    if not receiver:
        return "unidentified_receiver"

    if not isinstance(claim, dict) or not claim:
        return "authority_absent"

    claim_instance = str(claim.get("caller_instance") or "").strip()
    claim_session = str(claim.get("originating_session_id") or "").strip()
    claim_dest = str(claim.get("destination_instance") or "").strip() or claim_instance

    # Legacy callback: no instance at all. It must NOT default to P1 — that
    # default is what let PTG/Azul results land in P1's session.
    if not claim_instance:
        return "missing_instance"
    if not claim_session:
        return "missing_session"

    if claim_dest != receiver:
        return "destination_mismatch"

    if record is None:
        # Nothing to cross-check against. The claim is self-asserted, so it can
        # only be trusted as far as "it names this gateway" — which is not far
        # enough for a callback that will drive a model turn and WTS writes.
        return "no_dispatch_record"

    if not str(record.get("schema") or "").startswith("dispatch-authority/"):
        return "schema_unsupported"

    rec_instance = str(record.get("caller_instance") or "").strip()
    rec_dest = str(record.get("destination_instance") or "").strip() or rec_instance
    rec_session = str(record.get("originating_session_id") or "").strip()

    if not rec_instance or not rec_session:
        return "record_incomplete"
    if rec_instance != claim_instance:
        return "instance_mismatch"
    if rec_dest != receiver:
        return "destination_mismatch"
    if rec_session != claim_session:
        return "session_mismatch"

    # Run / work identity. A CONFLICTING value is always a mismatch. An ABSENT
    # value on the claim side is a mismatch only for fields the emit path always
    # carries — see _ABSENCE_TOLERATED.
    for field, code in (
        ("run_id", "run_mismatch"),
        ("wts_task", "wts_mismatch"),
        ("mission_id", "mission_mismatch"),
        ("chain_id", "chain_mismatch"),
        ("route_fingerprint", "route_mismatch"),
    ):
        rec_v = (record.get(field) or "") if record.get(field) is not None else ""
        claim_v = (claim.get(field) or "") if claim.get(field) is not None else ""
        rec_v, claim_v = str(rec_v).strip(), str(claim_v).strip()
        if rec_v and claim_v and rec_v != claim_v:
            return code
        if rec_v and not claim_v and field not in _ABSENCE_TOLERATED:
            return code

    return None


def claim_from_event(event: dict) -> dict:
    """Project a wake/callback event onto the authority shape for comparison."""
    ev = event if isinstance(event, dict) else {}
    return {
        "caller_instance": str(ev.get("instance") or "").strip(),
        "destination_instance": (
            str(ev.get("destination_instance") or "").strip()
            or str(ev.get("instance") or "").strip()
        ),
        "originating_session_id": str(ev.get("originating_session_id") or "").strip(),
        "run_id": str(ev.get("run_id") or "").strip(),
        "wts_task": str(ev.get("wts_task") or "").strip(),
        "mission_id": str(ev.get("mission_id") or "").strip(),
        "chain_id": str(ev.get("chain_id") or "").strip(),
        "route_fingerprint": route_fingerprint(
            str(ev.get("platform") or ""),
            str(ev.get("chat_type") or ""),
            str(ev.get("chat_id") or ""),
        ),
    }


def stamp_event_authority(event: dict, *, run_dir: "str | Path" = "") -> dict:
    """Copy a run's authority onto an outbound callback event (in place).

    Used by the emit side so the reaper's existing CLI call gains authority with
    no argument change: the fields come from the run's own dispatch record.
    Never invents identity — a run with no record produces an event with no
    authority, which the receiving gateway then quarantines.
    """
    rec = authority_for_run(run_dir or event.get("run_dir") or "")
    if not rec:
        return event
    event.setdefault("instance", rec.get("caller_instance") or "")
    event.setdefault("destination_instance", rec.get("destination_instance") or "")
    event.setdefault("originating_session_id", rec.get("originating_session_id") or "")
    if rec.get("mission_id"):
        event.setdefault("mission_id", rec.get("mission_id"))
    if rec.get("chain_id"):
        event.setdefault("chain_id", rec.get("chain_id"))
    event.setdefault("authority_source", rec.get("authority_source") or SCHEMA)
    return event


def reinject_authorisation(run_dir: "str | Path",
                           *, receiving_instance: Optional[str] = None) -> str:
    """May THIS instance append this run's closeout to its OWN gateway transcript?

    Returns ``"ok"`` or ``"skip:<reason>"`` — never raises, and the reason is a
    short, non-alarming, non-secret token suitable for a log line and a reap
    marker.

    This is the gate for ``dd-lane-reaper``'s ``reinject_caller_session()``, which
    resolves its destination with ``gateway.mirror._find_session_id(platform,
    chat_id)``. Levi's Telegram chat id ``8737984752`` is identical across all
    five bots, so that lookup names a transport, not an owner: on 2026-08-10 it
    put PTG's and Azul's lane closeouts onto P1's transcript, and P1 then wrote
    notes onto CLIENT WTS records. The run's dispatch record knows where its
    callback may return; that, and not the chat id, decides.

    Skipping is not a failure and must not be reported as one. The result is
    already durable on the WTS task and the lane thread; only the passive
    transcript append is withheld — which is the entire point.

    FAIL CLOSED: an unidentifiable instance, an absent record, a record with no
    destination, or any exception is NOT authorisation.
    """
    try:
        me = (receiving_instance if receiving_instance is not None else active_instance()) or ""
        if not me:
            return "skip:reaper-instance-unidentified"
        rd = str(run_dir or "").strip()
        if not rd:
            return "skip:no-run-dir"
        record = authority_for_run(rd)
        if not record:
            return "skip:no-dispatch-record"
        dest = str(record.get("destination_instance")
                   or record.get("caller_instance") or "").strip()
        if not dest:
            return "skip:record-incomplete"
        if dest != me:
            return f"skip:destination-{dest}-not-{me}"
        return "ok"
    except Exception as exc:  # never break the reaper's sweep
        return f"skip:authority-check-error-{type(exc).__name__}"


def summarize_for_evidence(event: "dict | None", record: "dict | None") -> dict:
    """Non-secret metadata for quarantine/audit evidence.

    Deliberately excludes the prompt, the closeout, the transcript, the session
    key and the raw chat id. The destination surface is recorded as its
    fingerprint, which is enough to correlate two events without writing the
    identifier into an audit file.
    """
    ev = event if isinstance(event, dict) else {}
    rec = record if isinstance(record, dict) else {}
    return {
        "event_schema": str(ev.get("schema") or ""),
        "idempotency_key": str(ev.get("idempotency_key") or ""),
        "kind": str(ev.get("kind") or ""),
        "lane": str(ev.get("lane") or ""),
        "gate": str(ev.get("gate") or ""),
        "run_id": str(ev.get("run_id") or ""),
        "run_dir": str(ev.get("run_dir") or ""),
        "wts_task": str(ev.get("wts_task") or ""),
        "mission_id": str(ev.get("mission_id") or ""),
        "chain_id": str(ev.get("chain_id") or ""),
        "platform": str(ev.get("platform") or ""),
        "chat_type": str(ev.get("chat_type") or ""),
        "route_fingerprint": route_fingerprint(
            str(ev.get("platform") or ""),
            str(ev.get("chat_type") or ""),
            str(ev.get("chat_id") or ""),
        ),
        "claimed_instance": str(ev.get("instance") or ""),
        "claimed_destination": str(ev.get("destination_instance") or ""),
        "claimed_session_id": str(ev.get("originating_session_id") or ""),
        "enqueued_at": str(ev.get("enqueued_at") or ""),
        "source_label": str(ev.get("source_label") or ""),
        "record_present": bool(rec),
        "record_instance": str(rec.get("caller_instance") or ""),
        "record_destination": str(rec.get("destination_instance") or ""),
        "record_session_id": str(rec.get("originating_session_id") or ""),
        "record_run_id": str(rec.get("run_id") or ""),
        "record_created_at": str(rec.get("created_at") or ""),
    }


__all__ = [
    "DISPATCH_FOOTPRINT_NAME",
    "LANE_TREE_DIRNAME",
    "RUNS_DIRNAME",
    "SCHEMA",
    "SIDECAR_NAME",
    "WAKE_TARGET_SCHEMA_V1",
    "WAKE_TARGET_SCHEMA_V2",
    "active_instance",
    "authority_for_run",
    "authority_mismatch",
    "build_authority",
    "claim_from_event",
    "lane_tree_root",
    "read_sidecar",
    "read_wake_target",
    "reinject_authorisation",
    "route_fingerprint",
    "run_dir_containment",
    "run_identity_mismatch",
    "sidecar_path",
    "stamp_event_authority",
    "summarize_for_evidence",
    "write_sidecar",
]
