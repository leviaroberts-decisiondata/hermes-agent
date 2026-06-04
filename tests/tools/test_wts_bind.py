"""Tests for the wts_bind tool (P5 review G1).

wts_bind resolves-or-creates the bound WTS task for a Telegram/governance turn
and runs the sanctioned binder helper IN-PROCESS (gateway python, as openclaw) —
so it works even though ~/.hermes/bin is 0700 and the agent's sandboxed shell
cannot traverse it (the G3 boundary). These tests use a fake binder so no real
Directus/WTS call is made.
"""
import os
import stat
import textwrap

import pytest

import tools.wts_bind_tool as wb


class _Agent:
    """Stand-in carrying a routing key like the gateway attaches per turn."""
    def __init__(self, route_key=None):
        if route_key is not None:
            self._dd_route_key = route_key


@pytest.fixture
def fake_binder(tmp_path, monkeypatch):
    """A fake dd-wts-bind whose stdout/exit are scripted via env vars."""
    binder = tmp_path / "dd-wts-bind"
    binder.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        printf '%s\\n' "$@" > "$ARGV_LOG"
        [[ -n "${FAKE_STDOUT:-}" ]] && printf '%s\\n' "$FAKE_STDOUT"
        [[ -n "${FAKE_STDERR:-}" ]] && printf '%s\\n' "$FAKE_STDERR" >&2
        exit "${FAKE_EXIT:-0}"
    """))
    binder.chmod(binder.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    argv_log = tmp_path / "argv.log"
    monkeypatch.setattr(wb, "_BINDER", binder)
    monkeypatch.setenv("ARGV_LOG", str(argv_log))
    return {"binder": binder, "argv_log": argv_log}


def _argv(fake_binder):
    return fake_binder["argv_log"].read_text().splitlines()


class TestGate:
    def test_check_fn_true_when_binder_present(self, fake_binder):
        assert wb.check_wts_bind_requirements() is True

    def test_check_fn_false_when_binder_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wb, "_BINDER", tmp_path / "nope")
        assert wb.check_wts_bind_requirements() is False


class TestChatResolution:
    def test_explicit_chat_wins(self, fake_binder, monkeypatch):
        monkeypatch.setenv("FAKE_STDOUT", "WTS_TASK_ID=t1\nBOUND=created\nVERIFY=ok")
        wb.wts_bind(goal="x", chat="999", parent_agent=_Agent(route_key="agent:main:telegram:dm:111"))
        argv = _argv(fake_binder)
        assert "--chat" in argv and argv[argv.index("--chat") + 1] == "999"

    def test_chat_derived_from_route_key(self, fake_binder, monkeypatch):
        monkeypatch.setenv("FAKE_STDOUT", "WTS_TASK_ID=t1\nBOUND=created\nVERIFY=ok")
        wb.wts_bind(goal="x", parent_agent=_Agent(route_key="agent:main:telegram:dm:8737984752"))
        argv = _argv(fake_binder)
        assert argv[argv.index("--chat") + 1] == "8737984752"

    def test_no_chat_anywhere_is_honest_error(self, fake_binder):
        out = wb.wts_bind(goal="x", parent_agent=_Agent(route_key="agent:main:slack:channel:C1"))
        assert "could not determine the Telegram chat id" in out
        assert not fake_binder["argv_log"].exists()  # binder never called


class TestBindResults:
    def test_success_relays_keyvalue_block(self, fake_binder, monkeypatch):
        monkeypatch.setenv(
            "FAKE_STDOUT",
            "WTS_TASK_ID=abc-123\nBOUND=created\nVERIFY=ok\nTRACKER=https://tracker/abc-123",
        )
        out = wb.wts_bind(goal="plan it", chat="111", parent_agent=_Agent())
        assert "WTS_TASK_ID=abc-123" in out
        assert "BOUND=created" in out
        # goal forwarded
        argv = _argv(fake_binder)
        assert "--goal" in argv and "plan it" in argv

    def test_nonzero_exit_is_honest_failure_not_fake_id(self, fake_binder, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "5")
        monkeypatch.setenv("FAKE_STDERR", "ERROR=wts_create_failed http=500")
        out = wb.wts_bind(goal="x", chat="111", parent_agent=_Agent())
        assert "FAILED" in out
        assert "WTS_TASK_ID" not in out.split("FAILED")[0]  # no fabricated id before the failure
        assert "do not" in out.lower() or "do NOT" in out

    def test_resolve_only_passes_flag_and_allows_empty_goal(self, fake_binder, monkeypatch):
        monkeypatch.setenv("FAKE_STDOUT", "WTS_TASK_ID=\nBOUND=none\nVERIFY=na")
        out = wb.wts_bind(goal=None, chat="111", resolve_only=True, parent_agent=_Agent())
        argv = _argv(fake_binder)
        assert "--resolve-only" in argv
        assert "BOUND=none" in out

    def test_missing_goal_without_resolve_only_errors(self, fake_binder):
        out = wb.wts_bind(goal="", chat="111", parent_agent=_Agent())
        assert "provide `goal`" in out
        assert not fake_binder["argv_log"].exists()

    def test_thread_and_notes_forwarded(self, fake_binder, monkeypatch):
        monkeypatch.setenv("FAKE_STDOUT", "WTS_TASK_ID=t\nBOUND=created\nVERIFY=ok")
        wb.wts_bind(goal="g", chat="111", thread="th9", notes="ctx", parent_agent=_Agent())
        argv = _argv(fake_binder)
        assert "--thread" in argv and argv[argv.index("--thread") + 1] == "th9"
        assert "--notes" in argv and argv[argv.index("--notes") + 1] == "ctx"
