"""deploy_status / pr_status — read-only self-service for agents (deploy-friction batch #4).

Why this exists:
  A Slack project agent that submits a deploy (or opens a PR) currently cannot
  answer "did my deploy land / is my PR merged?" without a human relay. Both
  answers already exist behind surfaces the agent's SANDBOXED shell cannot reach:
  the agent runs as the unprivileged `dd-delivery` uid, which cannot read tokens
  and cannot traverse ~/.hermes/bin (0700 openclaw). So — exactly like
  chain_status / wts_bind / route_to_lane solve the same boundary — these run
  IN-PROCESS in the gateway (openclaw), which CAN reach mc-api and run an authed
  `gh`. No mc-api change: both are reads of surfaces that already exist.

  * deploy_status → GET mc-api /api/deploy-queue (list, filterable) or
    /api/deploy-queue/{id} (one row). Localhost, unauthenticated. Returns SAFE
    metadata only (status / decision / commit / timestamps) — never the
    execution-bearing build/restart/test commands.
  * pr_status → `gh pr view <n> --json …` (read-only), authed as the openclaw
    GitHub identity. Returns state / mergeability / a checks rollup.

Both are strictly READ-ONLY: no mutation, no approval, no deploy. They never
surface tokens (deploy-queue GET needs none; gh uses the openclaw keyring, never
argv/stdout/model).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from tools.registry import registry, tool_error

_MC_API_BASE = os.getenv("MC_API_BASE_URL", "http://127.0.0.1:8502").rstrip("/")
_DEFAULT_PR_REPO = os.getenv("DD_DEFAULT_PR_REPO", "leviaroberts-decisiondata/dd-platform")

# Master kill-switch for both read tools (default ON). Read-only + low-risk, so
# they ship enabled; flip to "0"/"off" to withdraw them from every surface.
_READ_TOOLS_ENABLED = os.getenv("DD_AGENT_READ_TOOLS", "1").strip().lower() not in (
    "0", "off", "false", "no", "")

# Row fields safe to echo back to an agent. Deliberately EXCLUDES build_command /
# restart_command / test_command / pre_deploy_test — those are execution-bearing
# and must never round-trip through the model.
_SAFE_ROW_FIELDS = (
    "id", "service_name", "service_port", "status", "wts_task_id", "target_commit",
    "submitted_at", "decided_at", "decided_by", "deployed_at", "conflict_flag",
    "agent_session_id",
)


def _safe_row(row: dict) -> dict:
    out = {k: row.get(k) for k in _SAFE_ROW_FIELDS if row.get(k) is not None}
    fc = row.get("files_changed")
    if isinstance(fc, list):
        out["files_changed_count"] = len(fc)
        out["files_changed"] = fc[:12] + (["…"] if len(fc) > 12 else [])
    return out


def _mc_get(path: str) -> "tuple[int, object]":
    req = urllib.request.Request(f"{_MC_API_BASE}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        return e.code, body[:300]
    except Exception as e:  # connection refused / timeout — honest, non-fatal
        raise RuntimeError(f"{type(e).__name__}: {e}")


# ─────────────────────────────── deploy_status ────────────────────────────────

def deploy_status(entry_id=None, service=None, wts_task=None, status=None,
                  limit=10, parent_agent=None) -> str:
    """Read the deploy queue. With no filter, defaults to THIS turn's bound WTS
    task so "did my deploy land?" works with zero args."""
    if not _READ_TOOLS_ENABLED:
        return tool_error("deploy_status is disabled (DD_AGENT_READ_TOOLS=off)")

    entry_id = (str(entry_id).strip() if entry_id else "")
    service = (str(service).strip() if service else "")
    status = (str(status).strip() if status else "")
    wts_task = (str(wts_task).strip() if wts_task else "")
    # Zero-arg default: scope to the turn's bound task (same anchor deploy_submit
    # inherits) so the common "did my deploy go through?" needs no arguments.
    if not (entry_id or service or wts_task or status):
        bound = getattr(parent_agent, "_dd_wts_task_id", None)
        if bound and str(bound).strip():
            wts_task = str(bound).strip()

    try:
        if entry_id:
            code, data = _mc_get(f"/api/deploy-queue/{urllib.parse.quote(entry_id)}")
            if code >= 400 or not isinstance(data, dict):
                return json.dumps({"ok": False, "status_code": code,
                                   "message": f"no deploy-queue row {entry_id!r}",
                                   "detail": data if isinstance(data, str) else None})
            return json.dumps({"ok": True, "row": _safe_row(data)})

        qs = []
        if service:
            qs.append("service=" + urllib.parse.quote(service))
        if status:
            qs.append("status=" + urllib.parse.quote(status))
        path = "/api/deploy-queue" + ("?" + "&".join(qs) if qs else "")
        code, data = _mc_get(path)
        if code >= 400:
            return json.dumps({"ok": False, "status_code": code,
                               "detail": data if isinstance(data, str) else None})
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        if wts_task:  # list endpoint has no wts filter — apply client-side
            items = [r for r in items if str(r.get("wts_task_id") or "") == wts_task]
        try:
            lim = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            lim = 10
        rows = [_safe_row(r) for r in items[:lim]]
        return json.dumps({
            "ok": True,
            "count": len(rows),
            "total_matched": len(items),
            "scoped_to_wts_task": wts_task or None,
            "rows": rows,
        })
    except RuntimeError as e:
        return json.dumps({"ok": False, "message": f"deploy-queue read failed: {e}"})


# ──────────────────────────────── pr_status ───────────────────────────────────

def _summarize_checks(rollup) -> dict:
    if not isinstance(rollup, list):
        return {"total": 0}
    passed = failed = pending = 0
    failing = []
    for c in rollup:
        # gh emits CheckRun (status/conclusion) and StatusContext (state) shapes.
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        st = (c.get("status") or "").upper()
        name = c.get("name") or c.get("context") or "check"
        if concl in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            passed += 1
        elif concl in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            failed += 1
            failing.append(name)
        elif st in ("IN_PROGRESS", "QUEUED", "PENDING") or concl in ("PENDING", ""):
            pending += 1
    return {"total": len(rollup), "passed": passed, "failed": failed,
            "pending": pending, "failing": failing[:10]}


def pr_status(pr=None, repo=None, parent_agent=None) -> str:
    """Read-only GitHub PR state via `gh pr view`. `pr` is a number or URL;
    `repo` defaults to the dd-platform repo (ignored when `pr` is a full URL)."""
    if not _READ_TOOLS_ENABLED:
        return tool_error("pr_status is disabled (DD_AGENT_READ_TOOLS=off)")
    if not shutil.which("gh"):
        return tool_error("`gh` CLI is not available in the gateway environment")
    pr = (str(pr).strip() if pr else "")
    if not pr:
        return tool_error("pr is required (a PR number or URL)")
    repo = (str(repo).strip() if repo else _DEFAULT_PR_REPO)

    cmd = ["gh", "pr", "view", pr, "--json",
           "number,title,state,mergeable,mergeStateStatus,mergedAt,url,"
           "headRefName,baseRefName,isDraft,statusCheckRollup"]
    # A bare number needs an explicit repo; a full URL carries its own.
    if not pr.lower().startswith("http"):
        cmd += ["--repo", repo]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except Exception as e:
        return json.dumps({"ok": False, "message": f"gh invocation failed: {type(e).__name__}: {e}"})
    if cp.returncode != 0:
        return json.dumps({"ok": False, "message": "gh pr view failed",
                           "detail": (cp.stderr or "").strip()[:300]})
    try:
        d = json.loads(cp.stdout)
    except Exception:
        return json.dumps({"ok": False, "message": "could not parse gh output"})
    return json.dumps({
        "ok": True,
        "number": d.get("number"),
        "title": d.get("title"),
        "state": d.get("state"),               # OPEN | MERGED | CLOSED
        "merged": d.get("state") == "MERGED",
        "merged_at": d.get("mergedAt"),
        "is_draft": d.get("isDraft"),
        "mergeable": d.get("mergeable"),        # MERGEABLE | CONFLICTING | UNKNOWN
        "merge_state": d.get("mergeStateStatus"),
        "head": d.get("headRefName"),
        "base": d.get("baseRefName"),
        "url": d.get("url"),
        "checks": _summarize_checks(d.get("statusCheckRollup")),
    })


# ─────────────────────────────── registration ─────────────────────────────────
# Co-located with chain_status under the "delegation" toolset — the surface P1 /
# Slack project agents already carry for "where are we?" reads. Registered with NO
# check_fn so we never clobber the toolset-level availability check (the master
# kill-switch + gh presence are enforced inside each handler instead).

DEPLOY_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "entry_id": {"type": "string",
                     "description": "A specific deploy_queue row id to read (UUID). Overrides the filters."},
        "service": {"type": "string", "description": "Filter by service_name (e.g. 'digital-iq')."},
        "wts_task": {"type": "string",
                     "description": "Filter by linked WTS task id. Defaults to THIS turn's bound task when no filter is given."},
        "status": {"type": "string",
                   "description": "Filter by status (pending|deploying|deployed|proven|failed|rejected)."},
        "limit": {"type": "integer", "description": "Max rows to return (1-50, default 10)."},
    },
    "required": [],
    "additionalProperties": False,
}

PR_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "pr": {"type": "string", "description": "PR number (e.g. '561') or full GitHub PR URL."},
        "repo": {"type": "string",
                 "description": "owner/repo (default the dd-platform repo). Ignored when 'pr' is a full URL."},
    },
    "required": ["pr"],
    "additionalProperties": False,
}

registry.register(
    name="deploy_status",
    toolset="delegation",
    schema=DEPLOY_STATUS_SCHEMA,
    handler=lambda args, **kw: deploy_status(
        entry_id=args.get("entry_id"),
        service=args.get("service"),
        wts_task=args.get("wts_task"),
        status=args.get("status"),
        limit=args.get("limit", 10),
        parent_agent=kw.get("parent_agent"),
    ),
    emoji="📦",
)

registry.register(
    name="pr_status",
    toolset="delegation",
    schema=PR_STATUS_SCHEMA,
    handler=lambda args, **kw: pr_status(
        pr=args.get("pr"),
        repo=args.get("repo"),
        parent_agent=kw.get("parent_agent"),
    ),
    emoji="🔀",
)
