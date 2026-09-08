"""wts_attach — sanctioned P1/System-A WTS artifact attachment/update helper.

Why this exists:
  P1 can create local canary reports with write_file, but its sandboxed shell is
  not allowed to source Directus credentials or traverse every privileged WTS
  helper path. During the 2026-06-07 deploy-approval canary, P1 correctly wrote
  a report but had to return `WTS file_id/relation_id: not attached from this
  runtime` because there was no first-class WTS attach tool.

This tool mirrors wts_bind/route_to_lane: it runs the sanctioned openclaw-owned
helpers from the gateway process and returns only helper proof (VERIFY=ok,
FILE_ID, RELATION_ID, SHA256). It never exposes Directus tokens to the model.

Safety posture:
  - Dark-gated by DD_WTS_ATTACH_ENABLED=1.
  - Refuses suspicious source paths and likely-secret files.
  - Allows ordinary non-secret markdown/html/text artifacts under /tmp or
    ~/.hermes/dd-artifacts (the standard P1/lane artifact locations).
  - Uses the existing dd-wts-attach/dd-wts-update helpers for privileged writes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable

from tools.registry import registry, tool_error

_SHARED_HOME = Path.home() / ".hermes"
_ATTACH = _SHARED_HOME / "bin" / "dd-wts-attach"
_UPDATE = _SHARED_HOME / "bin" / "dd-wts-update"
_ALLOWED_ROOTS = [Path("/tmp"), Path("/private/tmp"), _SHARED_HOME / "dd-artifacts"]
_MAX_BYTES = 2 * 1024 * 1024
_TASK_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DENY_NAME_PATTERNS = (
    ".env", "auth.json", "credentials", "secret", "token", "keychain", "id_rsa", "id_ed25519",
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PRIVATE )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{20,}"),
)


def _enabled() -> bool:
    return os.getenv("DD_WTS_ATTACH_ENABLED", "").strip() == "1"


def check_wts_attach_requirements() -> bool:
    return _enabled() and _ATTACH.exists() and os.access(_ATTACH, os.X_OK)


def _resolve_path(path: str) -> Path:
    if not path or not str(path).strip():
        raise ValueError("file path is required")
    p = Path(str(path).strip()).expanduser().resolve()
    if not p.exists() or not p.is_file():
        raise ValueError(f"file does not exist or is not a regular file: {p}")
    if p.stat().st_size > _MAX_BYTES:
        raise ValueError(f"file is too large for this tool ({p.stat().st_size} bytes > {_MAX_BYTES})")
    low = str(p).lower()
    name = p.name.lower()
    if any(part in name or part in low for part in _DENY_NAME_PATTERNS):
        raise ValueError("refusing to attach a path/name that looks like credentials or secrets")
    if not any(p == root or root in p.parents for root in _ALLOWED_ROOTS):
        raise ValueError(
            "refusing to attach from outside allowed artifact roots; copy the non-secret report to /tmp "
            "or ~/.hermes/dd-artifacts first"
        )
    sample = p.read_bytes()[:65536]
    if b"\x00" in sample:
        raise ValueError("refusing to attach a binary-looking file")
    text = sample.decode("utf-8", errors="ignore")
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            raise ValueError("refusing to attach content that looks like it contains secrets; redact first")
    return p


def _run(cmd: Iterable[str], timeout: int = 90) -> tuple[int, str, str]:
    proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def wts_attach(
    task: str,
    file: str,
    name: str | None = None,
    note: str | None = None,
    status: str | None = None,
    as_agent: str | None = "dd-p1",
) -> str:
    """Attach an artifact to WTS and optionally append a task note/status.

    Returns JSON with attach proof and optional update proof. Never fabricates
    file/relation ids: if helper output does not include VERIFY=ok the tool
    reports failure.
    """
    if not check_wts_attach_requirements():
        return tool_error(
            "wts_attach: unavailable because DD_WTS_ATTACH_ENABLED is not set or dd-wts-attach is missing; "
            "do not claim WTS attachment proof from this runtime."
        )
    task = (task or "").strip()
    if not _TASK_RE.match(task):
        return tool_error("wts_attach: task must be a WTS task UUID")
    try:
        path = _resolve_path(file)
    except Exception as exc:
        return tool_error(f"wts_attach: {exc}")

    display_name = (name or path.name).strip()
    cmd = [str(_ATTACH), "--task", task, "--file", str(path), "--name", display_name]
    try:
        code, out, err = _run(cmd)
    except subprocess.TimeoutExpired:
        return tool_error("wts_attach: dd-wts-attach timed out; no attachment proof confirmed")
    except Exception as exc:
        return tool_error(f"wts_attach: failed to invoke dd-wts-attach: {exc}")
    if code != 0 or "VERIFY=ok" not in out:
        return tool_error(
            f"wts_attach: helper FAILED or did not verify (exit={code}).\n"
            f"stdout: {out[:1200] or '(empty)'}\nstderr: {err[:800] or '(empty)'}"
        )

    result = {"ok": True, "task": task, "file": str(path), "attach_stdout": out}

    note = (note or "").strip()
    status = (status or "").strip()
    if note or status:
        if not _UPDATE.exists() or not os.access(_UPDATE, os.X_OK):
            result["update"] = {"ok": False, "reason": "dd-wts-update unavailable"}
        else:
            update_cmd = [str(_UPDATE), "--task", task]
            if note:
                update_cmd += ["--append-note", note]
            if status:
                update_cmd += ["--status", status]
            if as_agent and str(as_agent).strip():
                update_cmd += ["--as-agent", str(as_agent).strip()]
            try:
                ucode, uout, uerr = _run(update_cmd)
                result["update"] = {
                    "ok": ucode == 0 and "VERIFY=ok" in uout,
                    "exit_code": ucode,
                    "stdout": uout,
                    "stderr": uerr[:800],
                }
            except Exception as exc:
                result["update"] = {"ok": False, "reason": f"failed to invoke dd-wts-update: {exc}"}
    return json.dumps(result, sort_keys=True)


WTS_ATTACH_SCHEMA = {
    "name": "wts_attach",
    "description": (
        "Attach a non-secret local report/artifact to a WTS task through the sanctioned privileged "
        "WTS helper, and optionally append a note/status. Use this when P1 has written a canary "
        "or review report and must return durable WTS file/relation proof. The file must be a "
        "plain-text artifact under /tmp or ~/.hermes/dd-artifacts; secrets/credential-looking paths "
        "are refused. Returns helper stdout with VERIFY=ok, FILE_ID, RELATION_ID, and SHA256 when successful."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "WTS task UUID to attach the artifact to."},
            "file": {"type": "string", "description": "Absolute path to the non-secret artifact/report file."},
            "name": {"type": "string", "description": "Optional display filename in WTS; defaults to source basename."},
            "note": {"type": "string", "description": "Optional note to append to the WTS task after attach."},
            "status": {"type": "string", "description": "Optional safe status update, e.g. in_progress or ready."},
            "as_agent": {"type": "string", "description": "Optional agent identity for task note; defaults to dd-p1."},
        },
        "required": ["task", "file"],
    },
}


registry.register(
    name="wts_attach",
    toolset="delegation",
    schema=WTS_ATTACH_SCHEMA,
    handler=lambda args, **kw: wts_attach(
        task=args.get("task"),
        file=args.get("file"),
        name=args.get("name"),
        note=args.get("note"),
        status=args.get("status"),
        as_agent=args.get("as_agent", "dd-p1"),
    ),
    check_fn=check_wts_attach_requirements,
    emoji="📎",
)
