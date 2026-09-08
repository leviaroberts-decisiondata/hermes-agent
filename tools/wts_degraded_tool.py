"""WTS degraded-mode fallback tools for P1/System-A.

WTS is the durability/provenance layer, but safe coordination work should not
spin forever when WTS bind/attach/update plumbing is degraded. This module gives
P1 two pragmatic primitives:

* ``wts_ensure``: resolve/create a WTS task through the existing sanctioned
  binder, with optional thread scoping so new work does not accidentally bind to
  an unrelated historical Telegram task.
* ``wts_degraded_log``: write a normalized local degradation event and optionally
  stage a non-secret artifact for later replay/repair.

It deliberately does NOT bypass WTS proof for risky actions. Use the classifier
below: deploy approvals, production mutation, source landing, external comms,
security/auth/env/DB migration, and broad Slack routing changes remain hard
blocks when durable WTS proof is missing.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error

_SHARED_HOME = get_hermes_home()
_BINDER = _SHARED_HOME / "bin" / "dd-wts-bind"
_MAX_STAGE_BYTES = 2 * 1024 * 1024
_TASK_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SAFE_TEXT_EXTS = {".md", ".txt", ".json", ".jsonl", ".html", ".csv", ".log"}
_SECRET_NAME_PATTERNS = (
    ".env", "auth.json", "credentials", "secret", "token", "keychain", "id_rsa", "id_ed25519",
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PRIVATE )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{12,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_./+\-=]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
)
_HARD_ACTION_PATTERNS = (
    "deploy", "deploy_approval", "approve_deploy", "production_restart", "restart",
    "source_landing", "source_land", "merge", "external_comm", "customer_comm",
    "security", "auth", "credential", "secret", "env", "database_migration", "db_migration",
    "broad_slack_routing", "slack_routing", "channel_routing",
)
_SOFT_ACTION_PATTERNS = ("handoff", "qa", "lane", "task_update")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _home() -> Path:
    # Re-read for tests/profile correctness; module-level _SHARED_HOME is kept for
    # binder compatibility but degraded logs should honor the current env.
    return get_hermes_home()


def _redact(text: str | None) -> str:
    value = str(text or "")[:4000]
    for pat in _SECRET_PATTERNS:
        value = pat.sub("[REDACTED]", value)
    return value


def _run(cmd: Iterable[str], timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def check_wts_ensure_requirements() -> bool:
    return _BINDER.exists() and os.access(_BINDER, os.X_OK)


def check_wts_degraded_log_requirements() -> bool:
    # Fallback logging is intentionally always available if the filesystem works.
    return True


def classify_wts_blocking_level(operation: str, next_action: str | None = None, risk_flags: list[str] | None = None) -> dict:
    """Classify whether a WTS failure should block the caller.

    The classifier is conservative for production/source/security/external
    actions and permissive for safe reporting/planning/canary-readiness work.
    """
    operation_s = str(operation or "unknown").strip().lower()
    action_s = str(next_action or "").strip().lower()
    flags = [str(x).strip().lower() for x in (risk_flags or []) if str(x).strip()]
    haystack = " ".join([operation_s, action_s, *flags])
    if any(pat in haystack for pat in _HARD_ACTION_PATTERNS):
        return {
            "blocking_level": "hard_block",
            "next_safe_action": "stop",
            "reason": "Durable WTS proof is required before deploy/source/production/external/security-sensitive actions.",
        }
    if any(pat in haystack for pat in _SOFT_ACTION_PATTERNS):
        return {
            "blocking_level": "soft_block",
            "next_safe_action": "continue",
            "reason": "WTS proof is degraded, but this coordination/reporting action can continue with fallback evidence.",
        }
    return {
        "blocking_level": "non_blocking",
        "next_safe_action": "continue",
        "reason": "Safe reporting/planning work may continue with structured WTS degradation logging.",
    }


def _validate_task_id(task_id: str | None) -> str | None:
    if task_id is None or str(task_id).strip() in {"", "null", "None"}:
        return None
    val = str(task_id).strip()
    if not _TASK_RE.match(val):
        raise ValueError("task_id must be a WTS task UUID when provided")
    return val


def _stage_artifact(path: str | None) -> str | None:
    if not path or not str(path).strip():
        return None
    src = Path(str(path).strip()).expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise ValueError(f"artifact_path does not exist or is not a file: {src}")
    low = str(src).lower()
    if any(pat in low for pat in _SECRET_NAME_PATTERNS):
        raise ValueError("refusing to stage artifact path that looks like credentials/secrets")
    if src.suffix.lower() not in _SAFE_TEXT_EXTS:
        raise ValueError("refusing to stage artifact with unsupported extension; use markdown/text/json/html/csv/log")
    if src.stat().st_size > _MAX_STAGE_BYTES:
        raise ValueError(f"refusing to stage artifact larger than {_MAX_STAGE_BYTES} bytes")
    sample = src.read_bytes()[:65536]
    if b"\x00" in sample:
        raise ValueError("refusing to stage binary-looking artifact")
    text = sample.decode("utf-8", errors="ignore")
    if _redact(text) != text:
        raise ValueError("refusing to stage artifact that appears to contain secrets; redact first")
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = _home() / "dd-wts-degraded" / "artifacts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stamp}-{os.getpid()}-{src.name}"
    shutil.copy2(src, dest)
    try:
        dest.chmod(0o644)
    except Exception:
        pass
    return str(dest)


def wts_degraded_log(
    surface: str,
    operation: str,
    reason: str,
    task_id: str | None = None,
    artifact_path: str | None = None,
    fallback_used: str | None = "local_artifact",
    blocking_level: str | None = None,
    next_safe_action: str | None = None,
    repair_bucket: str | None = "wts-fallbacks",
    stage_artifact: bool | None = False,
    risk_flags: list[str] | None = None,
) -> str:
    """Write a structured WTS degradation event and optionally stage an artifact."""
    try:
        clean_task = _validate_task_id(task_id)
        classification = classify_wts_blocking_level(operation, next_safe_action, risk_flags)
        level = (blocking_level or classification["blocking_level"]).strip()
        action = (next_safe_action or classification["next_safe_action"]).strip()
        if level not in {"non_blocking", "soft_block", "hard_block"}:
            raise ValueError("blocking_level must be non_blocking, soft_block, or hard_block")
        if action not in {"continue", "stop", "escalate"}:
            raise ValueError("next_safe_action must be continue, stop, or escalate")
        staged = _stage_artifact(artifact_path) if stage_artifact else None
        event = {
            "event": "wts_degraded",
            "timestamp": _now(),
            "surface": (surface or "unknown").strip() or "unknown",
            "operation": (operation or "unknown").strip() or "unknown",
            "task_id": clean_task,
            "artifact_path": str(artifact_path).strip() if artifact_path else None,
            "artifact_staged_path": staged,
            "fallback_used": (fallback_used or "none").strip() or "none",
            "blocking_level": level,
            "reason": _redact(reason),
            "repair_bucket": (repair_bucket or "wts-fallbacks").strip() or "wts-fallbacks",
            "next_safe_action": action,
        }
        event_dir = _home() / "dd-wts-degraded" / "events"
        event_dir.mkdir(parents=True, exist_ok=True)
        event_path = event_dir / f"{_dt.datetime.now(_dt.timezone.utc).date().isoformat()}.jsonl"
        with event_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        return json.dumps({"ok": True, "event_path": str(event_path), "event": event}, sort_keys=True)
    except Exception as exc:
        return tool_error(f"wts_degraded_log: {exc}")


def _parse_key_values(stdout: str) -> dict[str, str]:
    vals = {}
    for line in (stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def wts_ensure(
    goal: str,
    chat: str | None = None,
    thread: str | None = None,
    notes: str | None = None,
    owner: str | None = "dd-p1",
    task_type: str | None = "chore",
    milestone: str | None = None,
    resolve_only: bool | None = False,
    parent_agent=None,
) -> str:
    """Resolve/create a WTS task using the sanctioned binder.

    Pass ``thread`` for a fresh logical unit of work; otherwise Telegram chat
    anchors may correctly reuse an existing task for the same chat.
    """
    if not check_wts_ensure_requirements():
        return tool_error("wts_ensure: dd-wts-bind is unavailable; use wts_degraded_log and do not claim WTS task proof")
    chat_id = str(chat or "").strip()
    if not chat_id and parent_agent is not None:
        for attr in ("_dd_route_key", "_dd_session_key"):
            key = str(getattr(parent_agent, attr, "") or "")
            parts = key.split(":")
            if len(parts) >= 5 and parts[:3] == ["agent", "main", "telegram"]:
                chat_id = parts[4]
                break
    if not chat_id:
        return tool_error("wts_ensure: chat is required unless this turn has a parseable Telegram route key")
    if not resolve_only and not str(goal or "").strip():
        return tool_error("wts_ensure: goal is required unless resolve_only=true")
    cmd = [str(_BINDER), "--chat", chat_id]
    if thread and str(thread).strip():
        cmd += ["--thread", str(thread).strip()]
    if resolve_only:
        cmd += ["--resolve-only"]
    else:
        cmd += ["--goal", str(goal).strip()]
        if notes and str(notes).strip():
            cmd += ["--notes", str(notes).strip()]
        if owner and str(owner).strip():
            cmd += ["--owner", str(owner).strip()]
        if task_type and str(task_type).strip():
            cmd += ["--task-type", str(task_type).strip()]
        if milestone and str(milestone).strip():
            cmd += ["--milestone", str(milestone).strip()]
    try:
        code, out, err = _run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        return tool_error("wts_ensure: dd-wts-bind timed out; task was not confirmed")
    except Exception as exc:
        return tool_error(f"wts_ensure: failed to invoke dd-wts-bind: {exc}")
    if code != 0:
        return tool_error(f"wts_ensure: binder failed exit={code}\nstdout: {out[:1000]}\nstderr: {err[:800]}")
    vals = _parse_key_values(out)
    ok = vals.get("VERIFY") == "ok" and bool(vals.get("WTS_TASK_ID"))
    return json.dumps({
        "ok": ok,
        "stdout": out,
        "task_id": vals.get("WTS_TASK_ID"),
        "bound": vals.get("BOUND"),
        "verify": vals.get("VERIFY"),
        "tracker": vals.get("TRACKER"),
        "anchor_key": vals.get("ANCHOR_KEY"),
    }, sort_keys=True)


WTS_DEGRADED_LOG_SCHEMA = {
    "name": "wts_degraded_log",
    "description": (
        "Log a structured WTS degraded-mode event when WTS bind/attach/update is unavailable or fails. "
        "Use this so safe P1/reporting work can continue with a repair trail instead of spinning. "
        "Does not create durable WTS proof; risky deploy/source/external/security actions must still hard-block."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "surface": {"type": "string", "description": "Surface/runtime, e.g. p1, telegram, slack, lane."},
            "operation": {"type": "string", "description": "WTS operation that degraded: ensure_task, attach_artifact, append_note, update_status, relation."},
            "reason": {"type": "string", "description": "Redacted reason/error. Secret-looking values are redacted again."},
            "task_id": {"type": "string", "description": "Optional WTS task UUID if known."},
            "artifact_path": {"type": "string", "description": "Optional local artifact path related to the failure."},
            "fallback_used": {"type": "string", "description": "local_artifact, outbox, pmo_update, none."},
            "blocking_level": {"type": "string", "description": "non_blocking, soft_block, or hard_block. Auto-classified if omitted."},
            "next_safe_action": {"type": "string", "description": "continue, stop, or escalate. Auto-classified if omitted."},
            "repair_bucket": {"type": "string", "description": "Repair bucket label; defaults to wts-fallbacks."},
            "stage_artifact": {"type": "boolean", "description": "If true, copy a safe non-secret artifact into the degraded-mode artifact store."},
            "risk_flags": {"type": "array", "items": {"type": "string"}, "description": "Optional risk flags for classification."},
        },
        "required": ["surface", "operation", "reason"],
    },
}

WTS_ENSURE_SCHEMA = {
    "name": "wts_ensure",
    "description": (
        "Resolve or create a WTS task through the sanctioned Telegram/System-A binder. "
        "Use when execution-scoped work needs a task and none is bound. Pass a thread key for a new logical unit "
        "so work is not accidentally attached to an unrelated historical Telegram task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "One-line work goal/title. Required unless resolve_only=true."},
            "chat": {"type": "string", "description": "Telegram chat id; usually omitted when route context exists."},
            "thread": {"type": "string", "description": "Optional logical thread/message id to scope the task anchor."},
            "notes": {"type": "string", "description": "Optional longer notes for created task."},
            "owner": {"type": "string", "description": "Task owner, default dd-p1."},
            "task_type": {"type": "string", "description": "Task type, default chore."},
            "milestone": {"type": "string", "description": "Optional milestone UUID."},
            "resolve_only": {"type": "boolean", "description": "If true, do not create if absent."},
        },
        "required": [],
    },
}

registry.register(
    name="wts_degraded_log",
    toolset="delegation",
    schema=WTS_DEGRADED_LOG_SCHEMA,
    handler=lambda args, **kw: wts_degraded_log(
        surface=args.get("surface"),
        operation=args.get("operation"),
        reason=args.get("reason"),
        task_id=args.get("task_id"),
        artifact_path=args.get("artifact_path"),
        fallback_used=args.get("fallback_used", "local_artifact"),
        blocking_level=args.get("blocking_level"),
        next_safe_action=args.get("next_safe_action"),
        repair_bucket=args.get("repair_bucket", "wts-fallbacks"),
        stage_artifact=bool(args.get("stage_artifact", False)),
        risk_flags=args.get("risk_flags"),
    ),
    check_fn=check_wts_degraded_log_requirements,
    emoji="🧾",
)

registry.register(
    name="wts_ensure",
    toolset="delegation",
    schema=WTS_ENSURE_SCHEMA,
    handler=lambda args, **kw: wts_ensure(
        goal=args.get("goal"),
        chat=args.get("chat"),
        thread=args.get("thread"),
        notes=args.get("notes"),
        owner=args.get("owner", "dd-p1"),
        task_type=args.get("task_type", "chore"),
        milestone=args.get("milestone"),
        resolve_only=bool(args.get("resolve_only", False)),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_wts_ensure_requirements,
    emoji="🧭",
)
