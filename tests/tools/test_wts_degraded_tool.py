"""Tests for WTS degraded-mode fallback behavior."""
import json
from pathlib import Path
from unittest.mock import patch as mock_patch

import tools.wts_degraded_tool as wdt
from tools.wts_degraded_tool import (
    classify_wts_blocking_level,
    wts_degraded_log,
    wts_ensure,
)

TASK = "21e72d87-eaed-4dd1-8382-e973e31cbd33"


def test_classifies_safe_reporting_as_non_blocking():
    result = classify_wts_blocking_level(operation="attach_artifact", next_action="status_report")
    assert result["blocking_level"] == "non_blocking"
    assert result["next_safe_action"] == "continue"


def test_classifies_deploy_approval_without_proof_as_hard_block():
    result = classify_wts_blocking_level(operation="attach_artifact", next_action="deploy_approval")
    assert result["blocking_level"] == "hard_block"
    assert result["next_safe_action"] == "stop"
    assert "deploy" in result["reason"].lower()


def test_wts_degraded_log_writes_structured_jsonl_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    out = json.loads(wts_degraded_log(
        surface="p1",
        operation="attach_artifact",
        reason="Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456 failed",
        task_id=TASK,
        artifact_path="/tmp/report.md",
        fallback_used="local_artifact",
        blocking_level="soft_block",
        next_safe_action="continue",
    ))
    assert out["ok"] is True
    event_path = Path(out["event_path"])
    assert event_path.exists()
    event = json.loads(event_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert event["event"] == "wts_degraded"
    assert event["task_id"] == TASK
    assert event["operation"] == "attach_artifact"
    assert event["fallback_used"] == "local_artifact"
    assert event["blocking_level"] == "soft_block"
    assert "sk-" not in event["reason"]
    assert "[REDACTED]" in event["reason"]


def test_wts_degraded_log_can_stage_safe_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = Path("/tmp/wts-degraded-stage-source.md")
    source.write_text("# Safe staged report\nNo secrets.\n", encoding="utf-8")
    out = json.loads(wts_degraded_log(
        surface="p1",
        operation="attach_artifact",
        reason="attach helper timeout",
        task_id=TASK,
        artifact_path=str(source),
        fallback_used="local_artifact",
        stage_artifact=True,
    ))
    assert out["ok"] is True
    staged = Path(out["event"]["artifact_staged_path"])
    assert staged.exists()
    assert staged.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_wts_degraded_log_refuses_secret_artifact_staging(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = Path("/tmp/wts-degraded-secret-source.md")
    source.write_text("password=supersecretvalue01234567890\n", encoding="utf-8")
    out = json.loads(wts_degraded_log(
        surface="p1",
        operation="attach_artifact",
        reason="attach helper failed",
        task_id=TASK,
        artifact_path=str(source),
        stage_artifact=True,
    ))
    assert "error" in out
    assert "secret" in out["error"].lower()


def test_wts_ensure_invokes_binder_with_thread_scope(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return 0, f"WTS_TASK_ID={TASK}\nBOUND=created\nVERIFY=ok\nTRACKER=https://tracker.decisiondata.io/tasks/{TASK}", ""

    with mock_patch.object(wdt, "check_wts_ensure_requirements", return_value=True):
        with mock_patch.object(wdt, "_BINDER", Path("/usr/bin/true")):
            with mock_patch.object(wdt, "_run", side_effect=fake_run):
                out = json.loads(wts_ensure(goal="Implement fallback", chat="8737984752", thread="wts-fallback-v1"))
    assert out["ok"] is True
    assert out["task_id"] == TASK
    assert "--thread" in calls[0]
    assert "wts-fallback-v1" in calls[0]
