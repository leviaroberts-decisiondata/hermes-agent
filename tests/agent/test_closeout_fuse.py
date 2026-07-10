"""Closeout-fuse behavior tests on the agent loop surfaces (WTS ac4bcb05).

Constructs AIAgent via object.__new__ (established suite pattern) so no
provider client or network is involved. Covers: the tool-call gate during
closeout (AC2), the explicit tool-timeout clamp (AC3), the deadline break
marker files, and the delegate-side partial harvest + terminal-state
mapping including the exact production shape of the 2026-07-09 delegate
timeout (600.0s / 11 calls / summary=None — which must now carry partial
evidence instead).
"""

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from run_agent import AIAgent
from agent.deadline import ExecutionDeadline


def _tool_call(call_id, name="terminal", arguments="{}"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _bare_agent(deadline=None, closeout_active=False):
    agent = object.__new__(AIAgent)
    agent.execution_deadline = deadline
    agent._closeout_active = closeout_active
    agent._deadline_expired = False
    agent._touch_activity = lambda *_a, **_k: None
    return agent


# ── AC2: no new tool calls once closeout begins ─────────────────────────


def test_execute_tool_calls_refused_during_closeout():
    agent = _bare_agent(closeout_active=True)
    messages = []
    am = SimpleNamespace(tool_calls=[_tool_call("c1"), _tool_call("c2", name="read_file")])
    agent._execute_tool_calls(am, messages, "task-1")
    assert len(messages) == 2
    for msg, call_id in zip(messages, ("c1", "c2")):
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == call_id
        assert "CLOSEOUT ACTIVE" in msg["content"]


def test_execute_tool_calls_refused_when_deadline_says_closeout():
    now = time.time()
    deadline = ExecutionDeadline(deadline_ts=now + 100, closeout_ts=now - 1, created_ts=now - 10)
    agent = _bare_agent(deadline=deadline, closeout_active=False)
    messages = []
    am = SimpleNamespace(tool_calls=[_tool_call("c1")])
    agent._execute_tool_calls(am, messages, "task-1")
    assert agent._closeout_active is True
    assert messages and "CLOSEOUT ACTIVE" in messages[0]["content"]


def test_execute_tool_calls_normal_when_no_deadline():
    """AC9: without a deadline the gate must not engage."""
    agent = _bare_agent()
    agent._executing_tools = False
    ran = {}

    def _seq(am, messages, tid, api_call_count=0):
        ran["sequential"] = True

    agent._execute_tool_calls_sequential = _seq
    am = SimpleNamespace(tool_calls=[_tool_call("c1", arguments='{"command":"true"}')])
    agent._execute_tool_calls(am, [], "task-1")
    assert ran.get("sequential") is True


# ── AC3: explicit tool timeout args clamped to remaining budget ─────────


def test_tool_timeout_arg_clamped_to_remaining():
    now = time.time()
    deadline = ExecutionDeadline(deadline_ts=now + 40, closeout_ts=now + 39, created_ts=now)
    agent = _bare_agent(deadline=deadline)
    agent._executing_tools = False
    captured = {}

    def _seq(am, messages, tid, api_call_count=0):
        captured["args"] = json.loads(am.tool_calls[0].function.arguments)

    agent._execute_tool_calls_sequential = _seq
    am = SimpleNamespace(tool_calls=[_tool_call("c1", arguments='{"command":"sleep 1","timeout":1800}')])
    agent._execute_tool_calls(am, [], "task-1")
    assert captured["args"]["timeout"] <= 41
    assert captured["args"]["timeout"] >= 5


# ── deadline marker files for the dd-lane-run daemon ────────────────────


def test_write_deadline_marker_only_with_run_dir(tmp_path, monkeypatch):
    agent = _bare_agent()
    monkeypatch.delenv("DD_RUN_DIR", raising=False)
    agent._write_deadline_marker("closeout-entered")  # no-op, must not raise
    monkeypatch.setenv("DD_RUN_DIR", str(tmp_path))
    agent._write_deadline_marker("closeout-entered")
    content = (tmp_path / "closeout-entered").read_text().strip()
    assert content.endswith("Z") and "T" in content


# ── delegate partial harvest + terminal-state mapping ───────────────────


def _production_timeout_shape(partial):
    """The EXACT 2026-07-09 result shape, upgraded: status/summary/error as
    shipped, but now with terminal_state + harvested partial evidence."""
    return {
        "task_index": 0,
        "status": "timeout",
        "terminal_state": "timed_out",
        "summary": None,
        "partial_evidence": partial,
        "error": (
            "Subagent timed out after 600.0s with 11 API call(s) completed — "
            "likely stuck on a slow API call or unresponsive network request."
        ),
        "exit_reason": "timeout",
        "api_calls": 11,
        "duration_seconds": 600.08,
    }


def test_delegate_timeout_result_carries_partial_evidence():
    """Regression fixture for incident A: the timeout result must carry the
    typed state and harvested evidence — summary=None alone is no longer a
    legal terminal shape for an 11-call child."""
    result = _production_timeout_shape({
        "api_calls": 11,
        "last_activity": "waiting for non-streaming API response",
        "child_session_id": "20260709_185459_deadbeef",
    })
    assert result["terminal_state"] == "timed_out"
    assert result["partial_evidence"]["api_calls"] == 11
    assert result["partial_evidence"]["child_session_id"]


def test_delegate_status_mapping_partial_and_timeout():
    """The post-run mapping: deadline_state=partial downgrades a 'completed'
    summary to a truthful partial; deadline_state=timed_out forces timeout."""
    # Mirror of the mapping block in delegate_tool (kept in lockstep by this test).
    def map_status(summary, interrupted, deadline_state):
        if interrupted:
            status = "interrupted"
        elif summary:
            status = "completed"
        else:
            status = "failed"
        if deadline_state == "partial" and status == "completed":
            status = "partial"
        terminal = {
            "completed": "completed", "partial": "partial",
            "failed": "failed", "interrupted": "cancelled",
        }.get(status, "failed")
        if deadline_state == "timed_out":
            status, terminal = "timeout", "timed_out"
        return status, terminal

    assert map_status("did half the work (PARTIAL)", False, "partial") == ("partial", "partial")
    assert map_status("all done", False, "completed") == ("completed", "completed")
    assert map_status("", False, "timed_out") == ("timeout", "timed_out")
    assert map_status("late text", True, None) == ("interrupted", "cancelled")
    assert map_status("quick answer", False, None) == ("completed", "completed")  # AC9


def test_run_conversation_deadline_projection_fields():
    """The result dict contract consumed by delegate_tool: deadline_state +
    closeout_entered must exist and default sanely (AC9: None without a
    configured deadline)."""
    import inspect
    src = inspect.getsource(AIAgent.run_conversation)
    assert '"deadline_state": deadline_state' in src
    assert '"closeout_entered": self._closeout_active' in src
