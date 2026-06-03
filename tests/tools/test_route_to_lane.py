"""Phase A — route_to_lane tool: correct invocation, honest verification, active-task attach.

Covers WTS f875d95c (I1/I2/I3). Uses a fake wrapper script so the wrapper's
exit code + stdout are deterministic, and asserts:
  I1 — the tool invokes `--lane <lane> --packet <path>` (never positional).
  I2 — exit!=0 / no status line → reported FAILED (never false success);
       exit==0 + status line → HANDOFF OK.
  I3 — when wts_task is passed, `--wts-task <id>` is forwarded to the wrapper.
"""
import os
import stat
import textwrap
from pathlib import Path

import pytest

import tools.route_to_lane_tool as r2l


@pytest.fixture
def fake_tree(tmp_path, monkeypatch):
    """Build a fake ~/.hermes/{bin,dd-lanes} with a scriptable wrapper.

    The fake wrapper writes its argv to argv.log and exits with the code +
    stdout/stderr dictated by env vars, so each test drives a precise outcome.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lanes_dir = tmp_path / "dd-lanes"
    lanes_dir.mkdir()
    (lanes_dir / "channels.json").write_text(
        '{"service":{"base_url":"http://localhost:8700"},'
        '"lanes":{"qa":{"channel_id":"C_QA","channel_name":"dd-lane-qa","default_agent":"qa-review"},'
        '"design":{"channel_id":"C_D","channel_name":"dd-lane-design","default_agent":"dd-design"}}}',
        encoding="utf-8",
    )
    argv_log = tmp_path / "argv.log"
    wrapper = bin_dir / "dd-visible-lane-run"
    wrapper.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # fake wrapper: record argv, emit scripted stdout/stderr/exit
        printf '%s\\n' "$@" > "{argv_log}"
        [[ -n "${{FAKE_STDOUT:-}}" ]] && printf '%s\\n' "$FAKE_STDOUT"
        [[ -n "${{FAKE_STDERR:-}}" ]] && printf '%s\\n' "$FAKE_STDERR" >&2
        exit "${{FAKE_EXIT:-0}}"
    """), encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Point the tool at the fake tree.
    monkeypatch.setattr(r2l, "_SHARED_HOME", tmp_path)
    monkeypatch.setattr(r2l, "_WRAPPER", wrapper)
    monkeypatch.setattr(r2l, "_CHANNELS_JSON", lanes_dir / "channels.json")
    monkeypatch.setattr(r2l, "_LANE_DIR", lanes_dir)
    return {"argv_log": argv_log, "wrapper": wrapper, "lanes_dir": lanes_dir}


def _argv(fake_tree):
    return fake_tree["argv_log"].read_text(encoding="utf-8").splitlines()


class TestRegistryGate:
    """The check_fn must return a BOOLEAN — registry.get_definitions() filters
    the tool out of the model's tool list on any falsy (None/str) return."""

    def test_check_fn_returns_true_when_available(self):
        # The real wrapper + channels.json exist in the live tree.
        assert r2l.check_route_to_lane_requirements() is True

    def test_tool_surfaces_in_registry_definitions(self):
        import tools.route_to_lane_tool  # ensure registered
        from tools.registry import registry
        defs = registry.get_definitions({"route_to_lane"}, quiet=True)
        names = {d["function"]["name"] for d in defs}
        assert "route_to_lane" in names, (
            "route_to_lane was filtered out of registry definitions — check_fn "
            "must return True, not None/str."
        )


# ── I1: correct invocation ────────────────────────────────────────────────
class TestI1CorrectInvocation:
    def test_emits_lane_and_packet_flags(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | looks good | #dd-lane-qa ts=1.1")
        r2l.route_to_lane(lane="qa", goal="review the split")
        argv = _argv(fake_tree)
        assert "--lane" in argv and argv[argv.index("--lane") + 1] == "qa"
        assert "--packet" in argv
        pkt = argv[argv.index("--packet") + 1]
        assert pkt.endswith(".md") and Path(pkt).is_file()
        # NEVER positional: the lane name must not appear as a bare first arg.
        assert argv[0] != "qa"

    def test_rejects_unknown_lane_before_invoking(self, fake_tree):
        out = r2l.route_to_lane(lane="bogus", goal="x")
        assert "unknown lane" in out.lower()
        assert not fake_tree["argv_log"].exists()  # wrapper never called


# ── I2: honest verification ───────────────────────────────────────────────
class TestI2HonestVerification:
    def test_success_when_exit0_and_status_line(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | sync guarantee holds | #dd-lane-qa ts=2.2")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF OK" in out
        assert "[qa] PASS" in out

    def test_failed_on_arg_error_exit2(self, fake_tree, monkeypatch):
        # Simulate the original break: wrapper rejects with exit 2, no status line.
        monkeypatch.setenv("FAKE_EXIT", "2")
        monkeypatch.setenv("FAKE_STDERR", "unknown arg: qa")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF FAILED" in out
        assert "HANDOFF OK" not in out
        assert "do not report this handoff as succeeded" in out.lower()

    def test_failed_when_exit0_but_no_status_line(self, fake_tree, monkeypatch):
        # exit 0 but the lane never printed its normalized line → NOT success.
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "some unrelated chatter")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF FAILED" in out
        assert "did not confirm" in out.lower()

    def test_failed_on_lane_nonzero_gate(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "1")
        monkeypatch.setenv("FAKE_STDOUT", "")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF FAILED" in out

    def test_pending_is_honest_middle_state_not_success_not_fail(self, fake_tree, monkeypatch):
        # exit 75 = the lane ran but is still in flight (detached). Must be
        # reported as PENDING — never as done, never as a hard failure.
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PENDING | run still in flight | #dd-lane-qa ts=9.9")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF PENDING" in out
        assert "HANDOFF OK" not in out
        assert "do not report this as completed" in out.lower()


# ── I3: attach to active task + bucket-fallback surfaced ──────────────────
class TestI3ActiveTaskAttach:
    def test_wts_task_forwarded_as_flag(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=3.3")
        r2l.route_to_lane(lane="qa", goal="review", wts_task="aa752852-dead-beef")
        argv = _argv(fake_tree)
        assert "--wts-task" in argv
        assert argv[argv.index("--wts-task") + 1] == "aa752852-dead-beef"

    def test_no_wts_task_means_no_flag(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=4.4")
        r2l.route_to_lane(lane="qa", goal="review")
        assert "--wts-task" not in _argv(fake_tree)

    def test_packet_embeds_wts_line_when_active_task_given(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=5.5")
        r2l.route_to_lane(lane="qa", goal="review", wts_task="abc12345-1111-2222-3333-444455556666")
        argv = _argv(fake_tree)
        pkt = Path(argv[argv.index("--packet") + 1])
        assert "WTS: abc12345-1111-2222-3333-444455556666" in pkt.read_text(encoding="utf-8")

    def test_bucket_fallback_warning_surfaced_to_p1(self, fake_tree, monkeypatch):
        # Wrapper succeeded but emitted the I3 LOUD bucket-fallback WARN on stderr;
        # the tool must relay it (not swallow it) so P1 knows it hit the bucket.
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=6.6")
        monkeypatch.setenv("FAKE_STDERR", "WARN[I3]: no active/packet WTS task — falling back to the standing per-lane 'lane log' bucket")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF OK" in out
        assert "WARN[I3]" in out and "last resort" in out.lower() or "lane log" in out.lower()


# ── WS8 §4: the active-task FEED — default --wts-task from the turn's bound id ──
class _FakeAgent:
    """Stands in for the gateway agent; carries the X-DD-WTS-Task-Id binding."""
    def __init__(self, bound=None):
        self._dd_wts_task_id = bound


class TestWS8ActiveTaskFeed:
    BOUND = "11112222-3333-4444-5555-666677778888"
    EXPLICIT = "99990000-aaaa-bbbb-cccc-ddddeeeeffff"

    def test_feed_off_by_default_no_bound_used(self, fake_tree, monkeypatch):
        # Flag OFF (default): the bound id on parent_agent is IGNORED — legacy
        # caller-supplied-only behaviour is byte-identical.
        monkeypatch.delenv("ROUTE_TO_LANE_WTS_FEED", raising=False)
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=7.7")
        r2l.route_to_lane(lane="qa", goal="review", parent_agent=_FakeAgent(self.BOUND))
        assert "--wts-task" not in _argv(fake_tree)

    def test_feed_on_defaults_wts_from_bound_anchor(self, fake_tree, monkeypatch):
        # Flag ON + omitted wts_task + bound context → wrapper gets the bound id.
        monkeypatch.setenv("ROUTE_TO_LANE_WTS_FEED", "1")
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=8.8")
        r2l.route_to_lane(lane="qa", goal="review", parent_agent=_FakeAgent(self.BOUND))
        argv = _argv(fake_tree)
        assert "--wts-task" in argv
        assert argv[argv.index("--wts-task") + 1] == self.BOUND
        # the packet's WTS: line also carries the fed id
        pkt = Path(argv[argv.index("--packet") + 1])
        assert f"WTS: {self.BOUND}" in pkt.read_text(encoding="utf-8")

    def test_explicit_wts_task_overrides_the_feed(self, fake_tree, monkeypatch):
        # Flag ON but an EXPLICIT arg is supplied → explicit wins (the model may
        # override, e.g. a governance handoff to a P1-created task).
        monkeypatch.setenv("ROUTE_TO_LANE_WTS_FEED", "1")
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=8.9")
        r2l.route_to_lane(lane="qa", goal="review", wts_task=self.EXPLICIT,
                          parent_agent=_FakeAgent(self.BOUND))
        argv = _argv(fake_tree)
        assert argv[argv.index("--wts-task") + 1] == self.EXPLICIT

    def test_feed_on_but_unbound_context_means_no_flag(self, fake_tree, monkeypatch):
        # Flag ON, no explicit arg, parent_agent has no bound id → empty → the
        # wrapper fails honest (no flag, no bucket).
        monkeypatch.setenv("ROUTE_TO_LANE_WTS_FEED", "1")
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=9.0")
        r2l.route_to_lane(lane="qa", goal="review", parent_agent=_FakeAgent(None))
        assert "--wts-task" not in _argv(fake_tree)

    def test_feed_on_but_no_parent_agent_is_safe(self, fake_tree, monkeypatch):
        # Defensive: no parent_agent at all (e.g. a non-gateway invocation) must
        # not raise — getattr default handles it.
        monkeypatch.setenv("ROUTE_TO_LANE_WTS_FEED", "1")
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=9.1")
        out = r2l.route_to_lane(lane="qa", goal="review", parent_agent=None)
        assert "HANDOFF OK" in out
        assert "--wts-task" not in _argv(fake_tree)
