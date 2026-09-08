"""Deploy-friction batch #4 — read-only deploy_status / pr_status agent tools.

These wrap surfaces the agent's sandboxed shell cannot reach (mc-api deploy-queue
GET; an authed `gh`) and must return SAFE metadata only — never the
execution-bearing build/restart/test commands, never a token.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import tools.deploy_pr_read_tools as m


# ─────────────────────────────── deploy_status ────────────────────────────────

_ROW = {
    "id": "row-1", "service_name": "digital-iq", "service_port": 8617,
    "status": "failed", "wts_task_id": "task-A",
    "target_commit": "abc123", "submitted_at": "t0", "decided_at": "t1",
    "decided_by": "Levi", "deployed_at": None, "conflict_flag": False,
    "agent_session_id": "s1", "files_changed": ["a.ts", "b.ts"],
    # execution-bearing — MUST NOT round-trip:
    "build_command": "pnpm build && rm -rf /", "restart_command": "launchctl x",
    "pre_deploy_test": "curl evil", "notes": "secret-ish",
}


def _list(items):
    return (200, {"items": items, "total": len(items)})


def test_deploy_status_excludes_execution_fields():
    with patch.object(m, "_mc_get", return_value=_list([_ROW])):
        out = json.loads(m.deploy_status(service="digital-iq"))
    assert out["ok"] is True
    row = out["rows"][0]
    for banned in ("build_command", "restart_command", "pre_deploy_test", "notes"):
        assert banned not in row, f"{banned} leaked to the agent"
    assert row["status"] == "failed"
    assert row["files_changed_count"] == 2


def test_deploy_status_zero_arg_infers_turn_task():
    agent = SimpleNamespace(_dd_wts_task_id="task-A")
    rows = [_ROW, {**_ROW, "id": "row-2", "wts_task_id": "task-OTHER"}]
    with patch.object(m, "_mc_get", return_value=_list(rows)) as mg:
        out = json.loads(m.deploy_status(parent_agent=agent))
    assert out["scoped_to_wts_task"] == "task-A"
    assert out["count"] == 1 and out["rows"][0]["id"] == "row-1"
    # zero-arg must NOT push a wts filter into the query string (client-side filter)
    assert mg.call_args[0][0] == "/api/deploy-queue"


def test_deploy_status_by_entry_id():
    with patch.object(m, "_mc_get", return_value=(200, _ROW)) as mg:
        out = json.loads(m.deploy_status(entry_id="row-1"))
    assert out["ok"] is True and out["row"]["id"] == "row-1"
    assert mg.call_args[0][0] == "/api/deploy-queue/row-1"


def test_deploy_status_missing_entry():
    with patch.object(m, "_mc_get", return_value=(404, "not found")):
        out = json.loads(m.deploy_status(entry_id="nope"))
    assert out["ok"] is False


def test_deploy_status_connection_error_is_honest():
    with patch.object(m, "_mc_get", side_effect=RuntimeError("ConnectionRefused")):
        out = json.loads(m.deploy_status(service="x"))
    assert out["ok"] is False and "read failed" in out["message"]


def test_deploy_status_disabled_switch():
    with patch.object(m, "_READ_TOOLS_ENABLED", False):
        out = m.deploy_status(service="x")
    assert "disabled" in out


def test_deploy_status_limit_bounds():
    rows = [{**_ROW, "id": f"r{i}"} for i in range(100)]
    with patch.object(m, "_mc_get", return_value=_list(rows)):
        out = json.loads(m.deploy_status(service="digital-iq", limit=999))
    assert out["count"] == 50  # capped
    assert out["total_matched"] == 100


# ──────────────────────────────── pr_status ───────────────────────────────────

_GH_MERGED = {
    "number": 561, "title": "fix things", "state": "MERGED",
    "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN",
    "mergedAt": "2026-07-17T14:28:35Z", "url": "https://x/pull/561",
    "headRefName": "feat/x", "baseRefName": "main", "isDraft": False,
    "statusCheckRollup": [
        {"name": "build", "conclusion": "SUCCESS"},
        {"name": "lint", "conclusion": "FAILURE"},
        {"name": "e2e", "status": "IN_PROGRESS", "conclusion": ""},
    ],
}


def _gh_ok(payload):
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def test_pr_status_merged_and_checks():
    with patch.object(m.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(m.subprocess, "run", return_value=_gh_ok(_GH_MERGED)):
        out = json.loads(m.pr_status(pr="561"))
    assert out["ok"] is True
    assert out["merged"] is True and out["state"] == "MERGED"
    assert out["checks"] == {"total": 3, "passed": 1, "failed": 1,
                             "pending": 1, "failing": ["lint"]}


def test_pr_status_bare_number_passes_repo():
    with patch.object(m.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(m.subprocess, "run", return_value=_gh_ok(_GH_MERGED)) as run:
        m.pr_status(pr="561")
    assert "--repo" in run.call_args[0][0]


def test_pr_status_url_omits_repo():
    with patch.object(m.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(m.subprocess, "run", return_value=_gh_ok(_GH_MERGED)) as run:
        m.pr_status(pr="https://github.com/o/r/pull/561")
    assert "--repo" not in run.call_args[0][0]


def test_pr_status_gh_missing():
    with patch.object(m.shutil, "which", return_value=None):
        out = m.pr_status(pr="561")
    assert "gh" in out and "not available" in out


def test_pr_status_gh_failure_is_honest():
    fail = SimpleNamespace(returncode=1, stdout="", stderr="no such PR")
    with patch.object(m.shutil, "which", return_value="/usr/bin/gh"), \
         patch.object(m.subprocess, "run", return_value=fail):
        out = json.loads(m.pr_status(pr="99999999"))
    assert out["ok"] is False and "no such PR" in out["detail"]


def test_pr_status_requires_pr():
    with patch.object(m.shutil, "which", return_value="/usr/bin/gh"):
        out = m.pr_status(pr="")
    assert "required" in out


def test_pr_status_disabled_switch():
    with patch.object(m, "_READ_TOOLS_ENABLED", False):
        out = m.pr_status(pr="561")
    assert "disabled" in out
