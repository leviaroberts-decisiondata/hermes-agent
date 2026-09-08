"""Tests for the P1 WTS attachment tool."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

import tools.wts_attach_tool as wat
from tools.wts_attach_tool import wts_attach, check_wts_attach_requirements


TASK = "5f3077e5-24c9-496e-b946-7f7e9a244333"


def test_dark_gated(monkeypatch):
    monkeypatch.delenv("DD_WTS_ATTACH_ENABLED", raising=False)
    assert check_wts_attach_requirements() is False


def test_refuses_outside_allowed_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("DD_WTS_ATTACH_ENABLED", "1")
    p = tmp_path / "report.md"
    p.write_text("safe report", encoding="utf-8")
    with mock_patch.object(wat, "check_wts_attach_requirements", return_value=True):
        out = json.loads(wts_attach(TASK, str(p)))
    assert "error" in out
    assert "outside allowed artifact roots" in out["error"]


def test_refuses_secret_like_content(monkeypatch):
    monkeypatch.setenv("DD_WTS_ATTACH_ENABLED", "1")
    p = Path("/tmp/wts-attach-secret-test.md")
    p.write_text("API_KEY=sk_test_abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
    with mock_patch.object(wat, "check_wts_attach_requirements", return_value=True):
        out = json.loads(wts_attach(TASK, str(p)))
    assert "error" in out
    assert "secrets" in out["error"] or "credentials" in out["error"]


def test_runs_attach_helper_and_reports_verify(monkeypatch):
    monkeypatch.setenv("DD_WTS_ATTACH_ENABLED", "1")
    p = Path("/tmp/wts-attach-safe-report.md")
    p.write_text("# Safe report\n\nNo secrets here.\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, timeout=90):
        calls.append(cmd)
        return 0, "FILE_ID=file-1\nRELATION_ID=42\nSHA256=abc\nVERIFY=ok", ""

    with mock_patch.object(wat, "check_wts_attach_requirements", return_value=True):
        with mock_patch.object(wat, "_ATTACH", Path("/usr/bin/true")):
            with mock_patch.object(wat, "_run", side_effect=fake_run):
                out = json.loads(wts_attach(TASK, str(p), name="safe.md"))
    assert out["ok"] is True
    assert "FILE_ID=file-1" in out["attach_stdout"]
    assert calls[0][:2] == ["/usr/bin/true", "--task"]
    assert "safe.md" in calls[0]


def test_optional_note_uses_update_helper(monkeypatch):
    monkeypatch.setenv("DD_WTS_ATTACH_ENABLED", "1")
    p = Path("/tmp/wts-attach-safe-report-note.md")
    p.write_text("# Safe report\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, timeout=90):
        calls.append(cmd)
        if "--append-note" in cmd:
            return 0, "VERIFY=ok", ""
        return 0, "FILE_ID=file-1\nRELATION_ID=42\nSHA256=abc\nVERIFY=ok", ""

    with mock_patch.object(wat, "check_wts_attach_requirements", return_value=True):
        with mock_patch.object(wat, "_ATTACH", Path("/usr/bin/true")):
            with mock_patch.object(wat, "_UPDATE", Path("/usr/bin/true")):
                with mock_patch.object(wat, "_run", side_effect=fake_run):
                    out = json.loads(wts_attach(TASK, str(p), note="attached canary report"))
    assert out["ok"] is True
    assert out["update"]["ok"] is True
    assert any("--append-note" in c for c in calls)
