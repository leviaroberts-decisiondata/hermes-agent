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

    # Run / work identity. Compared only when BOTH sides carry the field, so a
    # partially populated older record cannot be used to smuggle a mismatch: an
    # absent field on the *claim* side for a field the record HAS is a mismatch.
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
        if rec_v and not claim_v:
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
    "SCHEMA",
    "SIDECAR_NAME",
    "WAKE_TARGET_SCHEMA_V1",
    "WAKE_TARGET_SCHEMA_V2",
    "active_instance",
    "authority_for_run",
    "authority_mismatch",
    "build_authority",
    "claim_from_event",
    "read_sidecar",
    "read_wake_target",
    "route_fingerprint",
    "sidecar_path",
    "stamp_event_authority",
    "summarize_for_evidence",
    "write_sidecar",
]
