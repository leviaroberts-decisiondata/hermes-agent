"""Fleet-repair 0.1 (WTS 7e1d32e9): oneshot must propagate hard failure.

Background: `hermes -z` returned exit 0 unconditionally and `agent.chat()`
discarded the `{"failed": True}` flag from run_conversation — so a lane run
that burned 987s on three API timeouts recorded rc=0, dd-lane-run wrote
state=completed, and the reaper graded it `Outcome: Done / gate: PASS` over a
112-byte error string. A failed run must exit 8 (the distinct oneshot-failure
code) so the rail records state=failed.
"""
from unittest.mock import patch

from hermes_cli import oneshot


def test_failed_run_exits_8(capsys):
    with patch.object(oneshot, "_run_agent",
                      return_value=("API call failed after 3 retries: timeout", True)):
        rc = oneshot.run_oneshot("do the thing")
    assert rc == 8
    captured = capsys.readouterr()
    # The error text still prints (it is the only artifact of the run) but the
    # exit code says it is not a result.
    assert "API call failed" in captured.out
    assert "FAILED" in captured.err


def test_successful_run_exits_0(capsys):
    with patch.object(oneshot, "_run_agent", return_value=("all done", False)):
        rc = oneshot.run_oneshot("do the thing")
    assert rc == 0
    assert "all done" in capsys.readouterr().out


def test_empty_but_unfailed_run_exits_0(capsys):
    with patch.object(oneshot, "_run_agent", return_value=("", False)):
        rc = oneshot.run_oneshot("do the thing")
    assert rc == 0
