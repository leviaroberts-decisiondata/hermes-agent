"""Fleet-repair 0.2 (WTS 7e1d32e9): the delegation supervisor must not launder
exhaustion or hard failure into success.

Background: `elif summary: status = "completed"` meant (a) a child that ran out
of iteration budget mid-task reported completed with a green check, because
_handle_max_iterations always produces a summary; and (b) a hard API failure
reported completed, because its error text IS the final_response. Status now
follows how the run actually ended, and a partial child renders its own glyph.

Also covers 0.4: a child that degraded to a fallback model carries its
fallback_events on the delegation entry.
"""
import threading

from tools.delegate_tool import _run_single_child


class _StubChild:
    """The minimum surface _run_single_child touches on a child agent."""

    def __init__(self, result, fallback_events=None):
        self._result = result
        self.tool_progress_callback = None
        self._delegate_saved_tool_names = []
        self._credential_pool = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.model = "test-model"
        self.current_iteration = 0
        self.current_tool = None
        if fallback_events is not None:
            self._fallback_events = fallback_events

    def run_conversation(self, user_message=None, task_id=None):
        return self._result


def _entry_for(result, **child_kwargs):
    child = _StubChild(result, **child_kwargs)
    return _run_single_child(0, "test goal", child=child, parent_agent=None)


def test_max_iterations_child_reports_partial_not_completed():
    entry = _entry_for({
        "final_response": "got through 3 of 10 steps",
        "completed": False,          # ran out of iteration budget
        "messages": [],
        "api_calls": 5,
    })
    assert entry["status"] == "partial"
    assert entry["terminal_state"] == "partial"
    assert entry["exit_reason"] == "max_iterations"


def test_hard_api_failure_reports_failed_not_completed():
    entry = _entry_for({
        "final_response": "API call failed after 3 retries: timed out after 300s",
        "completed": False,
        "failed": True,
        "error": "timed out after 300s",
        "messages": [],
        "api_calls": 3,
    })
    assert entry["status"] == "failed"
    assert entry["terminal_state"] == "failed"
    assert entry["exit_reason"] == "error"


def test_completed_child_still_reports_completed():
    entry = _entry_for({
        "final_response": "all ten steps done",
        "completed": True,
        "messages": [],
        "api_calls": 8,
    })
    assert entry["status"] == "completed"
    assert entry["terminal_state"] == "completed"
    assert entry["exit_reason"] == "completed"


def test_closeout_partial_still_reports_partial():
    entry = _entry_for({
        "final_response": "wrapped up early under closeout",
        "completed": True,
        "deadline_state": "partial",
        "messages": [],
        "api_calls": 4,
    })
    assert entry["status"] == "partial"
    assert entry["terminal_state"] == "partial"


def test_fallback_events_surface_on_entry():
    events = [{"from_model": "gpt-5.6-sol", "to_model": "gpt-5-mini",
               "to_provider": "copilot", "reason": "rate_limit",
               "at": "2026-07-30T00:00:00"}]
    entry = _entry_for(
        {"final_response": "done", "completed": True, "messages": [], "api_calls": 1},
        fallback_events=events,
    )
    assert entry["fallback_events"] == events


def test_no_fallback_events_is_empty_list():
    entry = _entry_for(
        {"final_response": "done", "completed": True, "messages": [], "api_calls": 1},
    )
    assert entry["fallback_events"] == []
