"""Slice D (WTS 51e12a09) — route_to_lane routing parity + mission plumb-through.

Covers:
  * schema parity: every registered lane on BOTH transports is advertised in the
    tool schema/description (F6: the Slack-centric schema made P1 hand-shell the
    Telegram wrapper, which failed on --goal and returned empty/exit-1),
  * mission env plumb-through: mission_id/purpose/remediation_depth reach the
    wrapper env as DD_MISSION_* (consumed by dd-lane-run's transition validator),
  * typed MISSION_REJECTED: wrapper exit 76 surfaces as MISSION_REJECTED with
    do-not-retry guidance, never a generic HANDOFF FAILED.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import tools.route_to_lane_tool as rtl


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_schema_advertises_both_transports_and_all_lanes():
    desc = rtl.ROUTE_TO_LANE_SCHEMA["description"]
    lane_desc = rtl.ROUTE_TO_LANE_SCHEMA["parameters"]["properties"]["lane"]["description"]
    for lane in ("design", "engineering", "qa", "pmo", "architecture", "product",
                 "knowledge", "devops", "deploy-ops", "security"):
        assert lane in lane_desc, f"lane {lane} missing from schema lane description"
    assert "Telegram" in desc and "Slack" in desc
    # The anti-hand-shelling contract is explicit.
    assert "NEVER shell" in desc
    assert "dd-telegram-visible-lane-run" in desc
    # Typed statuses are enumerated so P1 knows the full response contract.
    for token in ("ACCEPTED", "HANDOFF OK", "HANDOFF FAILED", "MISSION_REJECTED"):
        assert token in desc, f"typed status {token} missing from description"


def test_mission_params_in_schema():
    props = rtl.ROUTE_TO_LANE_SCHEMA["parameters"]["properties"]
    assert "mission_id" in props and "purpose" in props and "remediation_depth" in props
    assert set(props["purpose"]["enum"]) == {
        "implement", "verify", "deploy", "recover", "remediate"}


def test_mission_env_plumb_through(monkeypatch, tmp_path):
    fake_wrapper = _executable(tmp_path / "dd-visible-lane-run")
    packet = tmp_path / "packet.md"
    packet.write_text("# packet\n", encoding="utf-8")
    monkeypatch.setattr(rtl, "_WRAPPER", fake_wrapper)
    monkeypatch.setattr(rtl, "_known_lanes", lambda: ["engineering"])
    monkeypatch.setattr(rtl, "_all_lanes", lambda: ["engineering"])

    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env=None):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="[engineering] PASS | ok", stderr="")

    monkeypatch.setattr(rtl.subprocess, "run", fake_run)
    result = rtl.route_to_lane(
        lane="engineering", goal="g", packet=str(packet),
        mission_id="m-abc123", purpose="remediate", remediation_depth=1,
        parent_agent=SimpleNamespace(),
    )
    assert result.startswith("HANDOFF OK")
    assert captured["env"]["DD_MISSION_ID"] == "m-abc123"
    assert captured["env"]["DD_MISSION_PURPOSE"] == "remediate"
    assert captured["env"]["DD_MISSION_DEPTH"] == "1"


def test_no_mission_env_without_mission(monkeypatch, tmp_path):
    fake_wrapper = _executable(tmp_path / "dd-visible-lane-run")
    packet = tmp_path / "packet.md"
    packet.write_text("# packet\n", encoding="utf-8")
    monkeypatch.setattr(rtl, "_WRAPPER", fake_wrapper)
    monkeypatch.setattr(rtl, "_known_lanes", lambda: ["engineering"])
    monkeypatch.setattr(rtl, "_all_lanes", lambda: ["engineering"])

    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env=None):
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0, stdout="[engineering] PASS | ok", stderr="")

    monkeypatch.setattr(rtl.subprocess, "run", fake_run)
    rtl.route_to_lane(lane="engineering", goal="g", packet=str(packet),
                      parent_agent=SimpleNamespace())
    assert "DD_MISSION_ID" not in captured["env"]


def test_mission_rejected_is_typed(monkeypatch, tmp_path):
    fake_wrapper = _executable(tmp_path / "dd-visible-lane-run")
    packet = tmp_path / "packet.md"
    packet.write_text("# packet\n", encoding="utf-8")
    monkeypatch.setattr(rtl, "_WRAPPER", fake_wrapper)
    monkeypatch.setattr(rtl, "_known_lanes", lambda: ["engineering"])
    monkeypatch.setattr(rtl, "_all_lanes", lambda: ["engineering"])

    def fake_run(cmd, capture_output, text, timeout, env=None):
        return subprocess.CompletedProcess(
            cmd, 76,
            stdout="MISSION_REJECTED lane=engineering mission=m-x purpose=remediate\n"
                   "REASON_CODE=over-depth\nREASON=depth 2 exceeds max 1",
            stderr="")

    monkeypatch.setattr(rtl.subprocess, "run", fake_run)
    result = rtl.route_to_lane(
        lane="engineering", goal="g", packet=str(packet),
        mission_id="m-x", purpose="remediate", remediation_depth=2,
        parent_agent=SimpleNamespace(),
    )
    assert "MISSION_REJECTED" in result
    assert "do NOT retry" in result
    assert "REASON_CODE=over-depth" in result
    assert "HANDOFF FAILED" not in result
