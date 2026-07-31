"""Fleet-repair 1.2 (WTS 7e1d32e9): the gateway clarify transport.

The turn parks on a worker thread, the question posts to the operator's chat,
and the operator's next non-command message resumes it; a recognized command
cancels; a timeout degrades to proceed-on-stated-assumption (operator decision
2026-07-30). One clarify per turn.
"""
import asyncio
import threading
import time

import pytest

from gateway.run import _GatewayClarifyTransport


class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))
        return True


class _FakeSource:
    platform = "telegram"
    chat_id = "12345"
    thread_id = None
    user_id = "levi"


class _FakeRunner:
    def __init__(self, adapter, loop):
        self.adapters = {"telegram": adapter}
        self._pending_clarifies = {}
        self._main_loop = loop

    def _session_key_for_source(self, source):
        return f"{source.platform}:{source.chat_id}"


@pytest.fixture()
def loop_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)


def _mk(loop):
    adapter = _FakeAdapter()
    runner = _FakeRunner(adapter, loop)
    transport = _GatewayClarifyTransport(runner, _FakeSource())
    return adapter, runner, transport


def test_answer_resumes_turn_with_choice_mapping(loop_thread, monkeypatch):
    monkeypatch.setenv("DD_CLARIFY_WAIT_SECS", "30")
    adapter, runner, transport = _mk(loop_thread)

    def answer_soon():
        key = "telegram:12345"
        for _ in range(100):
            pending = runner._pending_clarifies.get(key)
            if pending:
                pending["answer"] = "2"  # numbered reply maps to choice text
                pending["event"].set()
                return
            time.sleep(0.05)

    threading.Thread(target=answer_soon, daemon=True).start()
    result = transport("Prod or preview?", ["production", "isolated preview"])
    assert result == "Operator answered: isolated preview"
    assert adapter.sent and "Prod or preview?" in adapter.sent[0][1]
    assert "1. production" in adapter.sent[0][1]
    assert runner._pending_clarifies == {}


def test_command_cancels_and_instructs_assumption(loop_thread, monkeypatch):
    monkeypatch.setenv("DD_CLARIFY_WAIT_SECS", "30")
    adapter, runner, transport = _mk(loop_thread)

    def cancel_soon():
        for _ in range(100):
            pending = runner._pending_clarifies.get("telegram:12345")
            if pending:
                pending["cancelled"] = "/new from operator"
                pending["event"].set()
                return
            time.sleep(0.05)

    threading.Thread(target=cancel_soon, daemon=True).start()
    result = transport("Which env?", None)
    assert "clarify cancelled" in result and "stated assumption" in result


def test_timeout_degrades_to_stated_assumption(loop_thread, monkeypatch):
    monkeypatch.setenv("DD_CLARIFY_WAIT_SECS", "1")
    adapter, runner, transport = _mk(loop_thread)
    transport.WAIT_SLICE_SECS = 0.2
    result = transport("Anyone there?", None)
    assert "clarify timeout" in result
    assert "asked, unanswered, proceeded on" in result
    # the operator got both the question and the proceeding notice
    assert len(adapter.sent) == 2
    assert "proceeding on my stated assumption" in adapter.sent[1][1]
    assert runner._pending_clarifies == {}


def test_one_clarify_per_turn_and_reset(loop_thread, monkeypatch):
    monkeypatch.setenv("DD_CLARIFY_WAIT_SECS", "1")
    adapter, runner, transport = _mk(loop_thread)
    transport.WAIT_SLICE_SECS = 0.2
    transport("q1", None)
    second = transport("q2", None)
    assert "already used this turn" in second
    transport.reset_for_turn()
    third = transport("q3", None)
    assert "already used" not in third


def test_parked_time_extends_execution_deadline(loop_thread, monkeypatch):
    monkeypatch.setenv("DD_CLARIFY_WAIT_SECS", "1")
    adapter, runner, transport = _mk(loop_thread)
    transport.WAIT_SLICE_SECS = 0.2

    class _Deadline:
        def __init__(self):
            self.deadline_ts = 1000.0
            self.closeout_ts = 800.0

    class _Agent:
        def __init__(self):
            self.execution_deadline = _Deadline()

        def _touch_activity(self, desc):
            pass

    agent = _Agent()
    transport.agent = agent
    transport("waiting", None)
    assert agent.execution_deadline.deadline_ts > 1000.0
    assert agent.execution_deadline.closeout_ts > 800.0
