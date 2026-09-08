from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.route_to_lane_tool as rtl


@pytest.fixture(autouse=True)
def _as_authorized_p1(monkeypatch):
    """Exercise transport behavior with an explicitly authorized P1 caller.

    HERMES_HOME is independently varied to test wrapper env pinning, and the
    global test fixture otherwise supplies an unidentified temporary home.
    Caller refusal is covered by tests/tools/test_p1_caller_boundary.py.
    """
    monkeypatch.setattr("tools.p1_caller_boundary.active_caller_id", lambda: "default")
    monkeypatch.setattr("tools.p1_caller_boundary._is_p1_internal", lambda: True)
    monkeypatch.setattr("tools.dispatch_authority.active_instance", lambda: "default")


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_reaper_registration_forces_shared_hermes_home(monkeypatch, tmp_path):
    """Regression: callers from .hermes-classic must not poison reaper home."""
    fake_reaper = _executable(tmp_path / "dd-lane-reaper")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(rtl, "_REAPER", fake_reaper)
    monkeypatch.setenv("HERMES_HOME", "/Users/openclaw/.hermes-classic")

    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="registered", stderr="")

    monkeypatch.setattr(rtl.subprocess, "run", fake_run)
    parent = SimpleNamespace(_dd_route_key="agent:main:telegram:dm:8737984752")

    note = rtl._register_pending_with_reaper(
        f"[engineering-2] PENDING | run_dir={run_dir}",
        parent,
        "93b11f2b-a8ac-4524-bfc7-8b1be14f6bac",
    )

    assert "reaper-registration: OK" in note
    assert captured["env"]["HERMES_HOME"] == str(rtl._SHARED_HOME)
    assert captured["env"]["DD_HERMES_AGENT_DIR"] == str(rtl._SHARED_HOME / "hermes-agent")
    assert captured["env"]["HERMES_HOME"] != "/Users/openclaw/.hermes-classic"


def test_lane_wrapper_invocation_forces_shared_hermes_home(monkeypatch, tmp_path):
    """Regression: Telegram-only lane wrappers inherit shared P1 home, not caller home."""
    fake_wrapper = _executable(tmp_path / "dd-telegram-visible-lane-run")
    packet = tmp_path / "packet.md"
    packet.write_text("# packet\n", encoding="utf-8")
    monkeypatch.setattr(rtl, "_WRAPPER", fake_wrapper)
    monkeypatch.setattr(rtl, "_TELEGRAM_RUNNER", fake_wrapper)
    monkeypatch.setattr(rtl, "_known_lanes", lambda: ["engineering"])
    monkeypatch.setattr(rtl, "_all_lanes", lambda: ["engineering", "engineering-2"])
    monkeypatch.setenv("HERMES_HOME", "/Users/openclaw/.hermes-classic")

    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="[engineering-2] PASS | ok", stderr="")

    monkeypatch.setattr(rtl.subprocess, "run", fake_run)
    parent = SimpleNamespace(_dd_route_key="agent:main:telegram:dm:8737984752")

    result = rtl.route_to_lane(
        lane="engineering-2",
        goal="test shared home routing",
        packet=str(packet),
        parent_agent=parent,
    )

    assert result.startswith("HANDOFF OK")
    assert captured["env"]["HERMES_HOME"] == str(rtl._SHARED_HOME)
    assert captured["env"]["DD_HERMES_AGENT_DIR"] == str(rtl._SHARED_HOME / "hermes-agent")
    assert captured["env"]["HERMES_HOME"] != "/Users/openclaw/.hermes-classic"
