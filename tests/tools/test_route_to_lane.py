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


@pytest.fixture(autouse=True)
def _as_canonical_p1_home(monkeypatch):
    """Run these tests as the canonical P1 instance.

    tests/conftest.py sandboxes HERMES_HOME to a per-test tempdir, so the
    P1 dispatch boundary (tools/p1_caller_boundary, WTS 17cbc96c) correctly
    reports an unidentified caller and refuses — production fails closed on
    exactly that. These modules exercise the *authorised* P1 path, so they
    declare that identity explicitly rather than the boundary being loosened
    to accommodate a test harness. Refusal behaviour itself is covered in
    tests/tools/test_p1_caller_boundary.py.
    """
    monkeypatch.setattr("tools.p1_caller_boundary.active_caller_id", lambda: "default")
    # d96e0165d moved the boundary from an id comparison to
    # hermes_cli.profiles.is_p1_internal_home(), which reads the REAL
    # HERMES_HOME — so patching the id alone stopped declaring P1 and every
    # test in this module was refused rather than exercised. Patch both.
    monkeypatch.setattr("tools.p1_caller_boundary._is_p1_internal", lambda: True)
    # The dispatch-authority record (WTS 17cbc96c) reads the instance from the
    # same trusted source; declare it here too so the authorised path is
    # exercised. Refusal behaviour is covered in the boundary/authority suites.
    monkeypatch.setattr("tools.dispatch_authority.active_instance", lambda: "default")
    # These contract tests exercise the current accepted-dispatch mode.
    monkeypatch.setenv("DD_LANE_ACCEPTED_DISPATCH", "1")


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


def _assert_accepted_handoff(out, *, run_id):
    """Detached acceptance carries its run identity and waits for real closeout."""
    assert out.startswith("ACCEPTED —")
    lines = out.splitlines()
    assert "state: accepted" in lines
    assert f"run_id: {run_id}" in lines
    assert any(line.startswith("lease: ") and line.removeprefix("lease: ").strip()
               for line in lines)
    assert any(line.startswith("packet: ") for line in lines)
    assert "do not report this as completed" in out.lower()
    assert "wait for that closeout" in out.lower()
    assert "HANDOFF OK" not in out
    assert "HANDOFF FAILED" not in out


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


# ── G2: loud, actionable unknown-lane failure (never a silent drop) ───────
class TestG2LoudUnknownLane:
    """A real-but-not-Slack lane (pmo/architecture/…) must produce a LOUD error
    that names the lane as real and gives the exact Telegram-runner command —
    not the bare 3-lane list that let P1 silently drop the PMO step. A truly
    unknown lane must tell the model to surface it to the user."""

    def _with_telegram_targets(self, fake_tree, monkeypatch, runner_exists=True):
        lanes_dir = fake_tree["lanes_dir"]
        # Superset registry: the 3 Slack lanes + the 7 Telegram-only lanes.
        (lanes_dir / "telegram-targets.json").write_text(
            '{"lanes":{"design":{},"engineering":{},"qa":{},'
            '"pmo":{},"architecture":{},"product":{},"knowledge":{},'
            '"devops":{},"engineering-2":{},"engineering-3":{}}}',
            encoding="utf-8",
        )
        monkeypatch.setattr(r2l, "_TELEGRAM_TARGETS_JSON", lanes_dir / "telegram-targets.json")
        runner = fake_tree["wrapper"].parent / "dd-telegram-visible-lane-run"
        tg_argv_log = fake_tree["argv_log"].parent / "telegram-argv.log"
        tg_env_log = fake_tree["argv_log"].parent / "telegram-env.log"
        if runner_exists:
            runner.write_text(textwrap.dedent(f"""\
                #!/usr/bin/env bash
                printf '%s\\n' "$@" > "{tg_argv_log}"
                printf 'HERMES_ROUTE_KEY=%s\\nHERMES_SESSION_KEY=%s\\nDD_WTS_TASK_ID=%s\\n' "${{HERMES_ROUTE_KEY:-}}" "${{HERMES_SESSION_KEY:-}}" "${{DD_WTS_TASK_ID:-}}" > "{tg_env_log}"
                lane=""
                while [[ $# -gt 0 ]]; do
                  case "$1" in --lane) lane="$2"; shift 2 ;; *) shift ;; esac
                done
                printf '[%s] PASS | telegram lane routed | @lane-bot mid=1 | wts=ok\\n' "$lane"
                exit 0
            """), encoding="utf-8")
            runner.chmod(0o755)
        monkeypatch.setattr(r2l, "_TELEGRAM_RUNNER", runner)
        fake_tree["telegram_argv_log"] = tg_argv_log
        fake_tree["telegram_env_log"] = tg_env_log
        return runner

    def test_telegram_only_lane_names_runner_and_refuses_drop(self, fake_tree, monkeypatch):
        runner = self._with_telegram_targets(fake_tree, monkeypatch)
        out = r2l.route_to_lane(lane="pmo", goal="plan the work")
        # Routes the real Telegram-only lane directly through the sanctioned
        # Telegram wrapper, instead of telling P1 to hand-shell it.
        assert "HANDOFF OK" in out
        assert "[pmo] PASS" in out
        assert "dd-telegram-visible-lane-run" not in out  # no manual shell hint
        assert not fake_tree["argv_log"].exists()  # Did NOT invoke the Slack wrapper.
        argv = fake_tree["telegram_argv_log"].read_text(encoding="utf-8").splitlines()
        assert "--lane" in argv and argv[argv.index("--lane") + 1] == "pmo"
        assert "--packet" in argv

    def test_telegram_only_lane_passes_route_and_wts_env_to_runner(self, fake_tree, monkeypatch):
        self._with_telegram_targets(fake_tree, monkeypatch)

        class Parent:
            _dd_route_key = "agent:main:telegram:dm:8737984752"
            _dd_session_key = "agent:hermes:gateway:telegram:8737984752"

        out = r2l.route_to_lane(
            lane="pmo",
            goal="plan the work",
            wts_task="a6a469c4-7a40-4b8d-a47d-d7b621e8ddbb",
            parent_agent=Parent(),
        )

        assert "HANDOFF OK" in out
        env_lines = fake_tree["telegram_env_log"].read_text(encoding="utf-8").splitlines()
        assert "HERMES_ROUTE_KEY=agent:main:telegram:dm:8737984752" in env_lines
        assert "HERMES_SESSION_KEY=agent:main:telegram:dm:8737984752" in env_lines
        assert "DD_WTS_TASK_ID=a6a469c4-7a40-4b8d-a47d-d7b621e8ddbb" in env_lines

    def test_telegram_only_lane_when_runner_unreachable_still_loud(self, fake_tree, monkeypatch):
        self._with_telegram_targets(fake_tree, monkeypatch, runner_exists=False)
        out = r2l.route_to_lane(lane="architecture", goal="review")
        assert "telegram-only specialist lane" in out.lower()
        # No silent drop even when the runner can't be reached from this surface.
        assert "not reachable" in out.lower() or "could not be reached" in out.lower()
        assert "drop" in out.lower()

    def test_truly_unknown_lane_tells_model_to_surface_to_user(self, fake_tree, monkeypatch):
        self._with_telegram_targets(fake_tree, monkeypatch)
        out = r2l.route_to_lane(lane="frobnicate", goal="x")
        assert "unknown lane" in out.lower()
        assert "not in any lane registry" in out.lower()
        assert "user" in out.lower()  # instructs surfacing to the user
        assert not fake_tree["argv_log"].exists()


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

    def test_failed_output_is_redacted_before_return(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "2")
        monkeypatch.setenv("FAKE_STDOUT", "token=sk-live-secret1234567890")
        monkeypatch.setenv("FAKE_STDERR", "Authorization: Bearer very-secret-token-1234567890")
        monkeypatch.setattr(r2l, "redact_sensitive_text", lambda s: s.replace("sk-live-secret1234567890", "[REDACTED]").replace("very-secret-token-1234567890", "[REDACTED]"))

        out = r2l.route_to_lane(lane="qa", goal="review")

        assert "[REDACTED]" in out
        assert "sk-live-secret1234567890" not in out
        assert "very-secret-token-1234567890" not in out

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
        # reported as ACCEPTED — never as done, never as a hard failure.
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PENDING | run still in flight | #dd-lane-qa ts=9.9")
        out = r2l.route_to_lane(lane="qa", goal="review")
        _assert_accepted_handoff(out, run_id="(pending)")


# ── P-D / G5: gate-aware OK — a FAILED specialist is never reported OK ─────────
class TestG5GateAwareOK:
    """A specialist can exit 0 while its result body declares a failure verdict;
    the wrapper stamps the honest FAIL onto the status line (exit-code-first), and
    route_to_lane must report FAILED — never a false HANDOFF OK — when the parsed
    gate is a failure verdict, even on a clean (exit 0) wrapper exit."""

    def test_exit0_but_gate_fail_reported_failed_not_ok(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] FAIL | tests 3/7 failed | #dd-lane-qa ts=1.1")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF OK" not in out
        assert "HANDOFF FAILED" in out
        assert "FAIL gate" in out
        assert "tests 3/7 failed" in out  # the failure detail surfaces to the caller

    def test_exit0_but_gate_blocked_reported_failed(self, fake_tree, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] BLOCKED | needs WTS token | #dd-lane-qa ts=2.2")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF OK" not in out
        assert "HANDOFF FAILED" in out
        assert "BLOCKED gate" in out

    def test_exit0_and_gate_pass_still_ok(self, fake_tree, monkeypatch):
        # Regression guard: a genuine PASS is still HANDOFF OK (G5 must not
        # over-block).
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | all green | #dd-lane-qa ts=3.3")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF OK" in out

    def test_exit0_and_gate_warn_is_not_a_failure(self, fake_tree, monkeypatch):
        # WARN is an advisory PASS-with-notes, not a failure verdict → still OK.
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] WARN | minor lint nits | #dd-lane-qa ts=4.4")
        out = r2l.route_to_lane(lane="qa", goal="review")
        assert "HANDOFF OK" in out

    def test_parse_status_gate_extracts_token(self):
        assert r2l._parse_status_gate("[qa] FAIL | x | #c ts=1", "qa") == "FAIL"
        assert r2l._parse_status_gate("[qa] PASS | x", "qa") == "PASS"
        assert r2l._parse_status_gate("[design] BLOCKED | y", "design") == "BLOCKED"
        # no normalized line for this lane → None
        assert r2l._parse_status_gate("random chatter", "qa") is None
        assert r2l._parse_status_gate("", "qa") is None


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
    """Stands in for the gateway agent; carries the X-DD-WTS-Task-Id binding and
    (P-C) the X-DD session_key used to route the reaper's result-return."""
    def __init__(self, bound=None, session_key=None, route_key=None):
        self._dd_wts_task_id = bound
        if session_key is not None:
            self._dd_session_key = session_key
        if route_key is not None:
            self._dd_route_key = route_key


def _gateway_attached_agent(source, bound=None):
    """Build a stand-in agent whose ``_dd_*`` keys are set by the REAL gateway
    attach path — NOT authored here.

    Increment-1 invariant ("consume, never inject"): the test must never write
    the ``agent:main:…`` routing literal onto the agent itself; it must let the
    production code derive it from a ``SessionSource`` exactly as a live turn
    does. We reproduce the two production steps the gateway runs per turn:

      1. ``build_session_key(source)``            → the *routing* key (agent:main:…)
      2. ``_dd_observability_session_key(sid)``   → the scrubbed *obs* key
      3. ``_attach_dd_context_for_turn(..., session_key=obs, route_key=routing)``
         which is what assigns ``_dd_session_key`` (obs) + ``_dd_route_key``
         (routing) on the agent — the SAME call the gateway makes at
         gateway/run.py:10980.

    The returned agent therefore carries keys the *gateway code* produced. If the
    route-key fix is reverted (route_key collapses back into the obs key), the
    derived ``_dd_route_key`` becomes unparseable and registration skips — so the
    assertion genuinely depends on the live key derivation, not a literal.
    """
    from gateway.session import build_session_key
    from gateway.run import _dd_observability_session_key, _attach_dd_context_for_turn

    routing_key = build_session_key(source)
    obs_key = _dd_observability_session_key("20260604_070452_cdd73484")

    class _Agent:
        pass

    agent = _Agent()
    _attach_dd_context_for_turn(
        agent,
        run_id="run-test",
        session_key=obs_key,        # what the gateway puts on _dd_session_key
        route_key=routing_key,      # what the gateway puts on _dd_route_key
        wts_task_id=bound,
    )
    return agent, routing_key, obs_key


class TestWS8ActiveTaskFeed:
    BOUND = "11112222-3333-4444-5555-666677778888"
    EXPLICIT = "99990000-aaaa-bbbb-cccc-ddddeeeeffff"

    def test_feed_on_by_default_uses_bound_anchor(self, fake_tree, monkeypatch):
        # P-D / G7: the feed is now DEFAULT-ON. With NO env var set + an omitted
        # wts_task + a bound anchor on parent_agent, the wrapper gets the bound id
        # so every handoff carries a durable tracker link by default.
        monkeypatch.delenv("ROUTE_TO_LANE_WTS_FEED", raising=False)
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=7.7")
        r2l.route_to_lane(lane="qa", goal="review", parent_agent=_FakeAgent(self.BOUND))
        argv = _argv(fake_tree)
        assert "--wts-task" in argv
        assert argv[argv.index("--wts-task") + 1] == self.BOUND
        # the packet's WTS: line also carries the fed id
        pkt = Path(argv[argv.index("--packet") + 1])
        assert f"WTS: {self.BOUND}" in pkt.read_text(encoding="utf-8")

    def test_explicit_opt_out_disables_the_feed(self, fake_tree, monkeypatch):
        # P-D / G7: ROUTE_TO_LANE_WTS_FEED=0 (or false/off/no) is the EXPLICIT
        # opt-out → the bound id on parent_agent is IGNORED (legacy
        # caller-supplied-only behaviour).
        for off in ("0", "false", "off", "no"):
            monkeypatch.setenv("ROUTE_TO_LANE_WTS_FEED", off)
            monkeypatch.setenv("FAKE_EXIT", "0")
            monkeypatch.setenv("FAKE_STDOUT", "[qa] PASS | ok | #dd-lane-qa ts=7.7")
            r2l.route_to_lane(lane="qa", goal="review", parent_agent=_FakeAgent(self.BOUND))
            assert "--wts-task" not in _argv(fake_tree), f"opt-out '{off}' should disable the feed"

    def test_feed_on_explicit_1_uses_bound_anchor(self, fake_tree, monkeypatch):
        # Explicit ROUTE_TO_LANE_WTS_FEED=1 also feeds (back-compat with the
        # pre-G7 enable flag).
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


# ── P-C / B-1a: the PENDING branch registers the detached run with the reaper ──
class TestPCReaperRegistration:
    """When a handoff returns PENDING, route_to_lane must register the detached
    run with dd-lane-reaper so its closeout returns to THIS caller's session.

    The registration is best-effort + fail-soft: it never changes the ACCEPTED
    disposition, but when it can it hands the reaper the run_dir + the caller's
    session routing (parsed from parent_agent._dd_session_key).
    """

    SK = "agent:main:telegram:dm:8737984752"
    # A PENDING status line that carries the run_dir segment dd-visible-lane-run
    # now appends on the detached path.
    PENDING_LINE = (
        "[qa] PENDING | run still in flight | #dd-lane-qa ts=9.9 "
        "https://app.slack.com/x | run_dir=/tmp/dd-lanes/qa/runs/RUN-XYZ"
    )

    @pytest.fixture
    def fake_reaper(self, tmp_path, monkeypatch):
        """A scriptable dd-lane-reaper that records its argv."""
        reaper = tmp_path / "bin" / "dd-lane-reaper"
        reaper.parent.mkdir(parents=True, exist_ok=True)
        argv_log = tmp_path / "reaper-argv.log"
        reaper.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf '%s\\n' "$@" > "{argv_log}"
            exit "${{FAKE_REAPER_EXIT:-0}}"
        """), encoding="utf-8")
        reaper.chmod(reaper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setattr(r2l, "_REAPER", reaper)
        return {"reaper": reaper, "argv_log": argv_log}

    def _reaper_argv(self, fake_reaper):
        return fake_reaper["argv_log"].read_text(encoding="utf-8").splitlines()

    def test_pending_registers_run_with_reaper(self, fake_tree, fake_reaper, monkeypatch):
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", self.PENDING_LINE)
        out = r2l.route_to_lane(
            lane="qa", goal="review", parent_agent=_FakeAgent(session_key=self.SK)
        )
        _assert_accepted_handoff(out, run_id="RUN-XYZ")
        argv = self._reaper_argv(fake_reaper)
        # --register <run_dir> <session_key> <platform> <chat_id> <chat_type> [thread]
        assert argv[0] == "--register"
        assert argv[1] == "/tmp/dd-lanes/qa/runs/RUN-XYZ"
        assert argv[2] == self.SK
        assert argv[3] == "telegram"
        assert argv[4] == "8737984752"
        assert argv[5] == "dm"
        # The caller-facing message tells P1 the reaper will return the result.
        assert "reaper-registration: OK" in out

    def test_wts_task_threaded_into_reaper_register_slot8(self, fake_tree, fake_reaper, monkeypatch):
        # WTS 331b65f8 req 3/4: an explicit wts_task must reach the reaper's 8th
        # --register positional so the reaper attaches the FINAL detached result to
        # that task on reap (not just the synchronous PENDING placeholder). The
        # reaper order is: --register run_dir session_key platform chat_id chat_type
        # thread_id budget wts_task. thread_id (slot 6) is empty for a DM and budget
        # (slot 7) is the empty "use default" sentinel — both MUST be present so
        # wts_task lands in slot 8.
        WTS = "331b65f8-39e1-4523-b52d-19fd4461fb52"
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", self.PENDING_LINE)
        out = r2l.route_to_lane(
            lane="qa", goal="review", wts_task=WTS,
            parent_agent=_FakeAgent(session_key=self.SK),
        )
        _assert_accepted_handoff(out, run_id="RUN-XYZ")
        argv = self._reaper_argv(fake_reaper)
        # positional indices: 0 --register, 1 run_dir, 2 sk, 3 plat, 4 chat, 5 ctype,
        # 6 thread_id (empty for DM), 7 budget (empty), 8 wts_task.
        assert argv[0] == "--register"
        assert len(argv) >= 9, f"reaper argv too short, wts_task not threaded: {argv}"
        assert argv[6] == "", f"slot 6 (thread_id) should be empty for a DM: {argv!r}"
        assert argv[7] == "", f"slot 7 (budget) should be the empty default sentinel: {argv!r}"
        assert argv[8] == WTS, f"slot 8 must be the wts_task: {argv!r}"
        # the caller-facing note reflects that the final WTS attach is wired
        assert f"wts_task={WTS}" in out

    def test_no_wts_task_register_marks_none_and_no_final_attach(self, fake_tree, fake_reaper, monkeypatch):
        # No bound/explicit task → slot 8 is empty and the note says so honestly,
        # so the absence of a durable final WTS attach is VISIBLE (not silent).
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", self.PENDING_LINE)
        out = r2l.route_to_lane(
            lane="qa", goal="review",
            parent_agent=_FakeAgent(session_key=self.SK),
        )
        argv = self._reaper_argv(fake_reaper)
        assert argv[0] == "--register"
        # slot 8 present but empty (no task)
        assert len(argv) >= 9
        assert argv[8] == ""
        assert "wts_task=none" in out

    def test_pending_without_run_dir_segment_skips_registration(self, fake_tree, fake_reaper, monkeypatch):
        # Older wrapper / no run_dir on the line → registration is skipped, but
        # the accepted result is still returned honestly (fail-soft).
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", "[qa] PENDING | in flight | #dd-lane-qa ts=9.9")
        out = r2l.route_to_lane(
            lane="qa", goal="review", parent_agent=_FakeAgent(session_key=self.SK)
        )
        _assert_accepted_handoff(out, run_id="(pending)")
        assert not fake_reaper["argv_log"].exists()  # reaper never invoked
        assert "no run_dir" in out.lower()

    def test_pending_without_session_key_skips_registration(self, fake_tree, fake_reaper, monkeypatch):
        # No parseable caller session_key → cannot target a session for re-inject.
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", self.PENDING_LINE)
        out = r2l.route_to_lane(lane="qa", goal="review", parent_agent=_FakeAgent())
        _assert_accepted_handoff(out, run_id="RUN-XYZ")
        assert not fake_reaper["argv_log"].exists()
        assert "no parseable caller session_key" in out.lower()

    # The MC-Live observability grouping key the real gateway puts on
    # ``_dd_session_key`` — scrubbed of chat ids, so it NEVER parses for routing.
    OBS_SK = "agent:hermes:gateway:20260604_070452_cdd73484"

    def test_real_gateway_uses_route_key_not_observability_key(self, fake_tree, fake_reaper, monkeypatch):
        # REGRESSION (Levi P1 canary 2026-06-04): the live gateway sets
        # _dd_session_key to the *observability* key (agent:hermes:gateway:…),
        # which has no chat_id, and the *routing* key (agent:main:…) on
        # _dd_route_key. Registration must use the route key and succeed —
        # earlier this skipped with "no parseable caller session_key" and the
        # closeout never returned to Levi's Telegram.
        #
        # Increment-1 invariant: the routing key is NOT authored here. We build a
        # SessionSource (the input a synthetic user supplies) and let the REAL
        # gateway attach path derive _dd_route_key from it via build_session_key
        # + _attach_dd_context_for_turn. The chat id 8737984752 is *source input*;
        # the agent:main:… key the reaper sees is *derived by production code*.
        from gateway.session import SessionSource
        from gateway.config import Platform
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="8737984752", chat_type="dm")
        agent, routing_key, obs_key = _gateway_attached_agent(source)
        # The derived keys must match production shapes — and crucially DIFFER
        # (route key carries the chat id; obs key does not). If a regression
        # collapses them, this guard catches it before the reaper assertion.
        assert routing_key == "agent:main:telegram:dm:8737984752"
        assert routing_key.startswith("agent:main:")
        assert obs_key.startswith("agent:hermes:gateway:")
        assert getattr(agent, "_dd_route_key") == routing_key
        assert getattr(agent, "_dd_session_key") == obs_key

        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", self.PENDING_LINE)
        out = r2l.route_to_lane(lane="qa", goal="review", parent_agent=agent)
        _assert_accepted_handoff(out, run_id="RUN-XYZ")
        argv = self._reaper_argv(fake_reaper)
        assert argv[0] == "--register"
        assert argv[2] == routing_key       # the DERIVED routing key, not a literal
        assert argv[3] == "telegram"
        assert argv[4] == "8737984752"
        assert argv[5] == "dm"
        assert "reaper-registration: OK" in out

    def test_observability_key_alone_skips_registration(self, fake_tree, fake_reaper, monkeypatch):
        # The exact failure mode hit in production: ONLY the observability key is
        # present (no _dd_route_key). It can't parse → registration is skipped,
        # honestly and fail-soft.
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", self.PENDING_LINE)
        out = r2l.route_to_lane(
            lane="qa", goal="review", parent_agent=_FakeAgent(session_key=self.OBS_SK)
        )
        _assert_accepted_handoff(out, run_id="RUN-XYZ")
        assert not fake_reaper["argv_log"].exists()
        assert "no parseable caller session_key" in out.lower()

    def test_register_failure_never_breaks_the_pending_result(self, fake_tree, fake_reaper, monkeypatch):
        # The reaper invocation fails (non-zero) → acceptance is unchanged;
        # only the note reflects the failure.
        monkeypatch.setenv("FAKE_EXIT", "75")
        monkeypatch.setenv("FAKE_STDOUT", self.PENDING_LINE)
        monkeypatch.setenv("FAKE_REAPER_EXIT", "3")
        out = r2l.route_to_lane(
            lane="qa", goal="review", parent_agent=_FakeAgent(session_key=self.SK)
        )
        _assert_accepted_handoff(out, run_id="RUN-XYZ")
        assert "reaper-registration: FAILED" in out

    def test_parse_session_origin_dm_and_thread(self):
        dm = r2l._parse_session_origin("agent:main:telegram:dm:8737984752")
        assert dm == {"platform": "telegram", "chat_type": "dm", "chat_id": "8737984752"}
        th = r2l._parse_session_origin("agent:main:slack:thread:C123:1699.45")
        assert th["thread_id"] == "1699.45" and th["platform"] == "slack"
        # group/channel: the 6th element is NOT treated as a thread (could be a uid).
        grp = r2l._parse_session_origin("agent:main:slack:channel:C9:U7")
        assert "thread_id" not in grp
        assert r2l._parse_session_origin("garbage") is None
        assert r2l._parse_session_origin("") is None


# ── Phase 3 (canary 72abe1ee 2026-06-17): exact-target verification contract ──
#
# If the packet names a URL/port/path (Acceptance URL: …), the lane closeout
# cannot say HANDOFF OK unless that exact target is verified in the result body.
# Backend / canonical proof cannot substitute for the named review surface.
# Browser DOM contradiction wins over backend proof.
class TestPhase3ExactTargetAcceptance:
    """The packet → result verification contract.

    Tests are split between (a) the pure helpers that parse targets out of free
    text (no fake_tree needed) and (b) the route_to_lane integration that
    downgrades HANDOFF OK to NOT-ACCEPTED when the packet's named target(s) are
    not verified by the lane.
    """

    # ── Pure helpers: target extraction + verdict ──
    def test_extract_targets_from_named_acceptance_url(self):
        body = (
            "## Goal\n"
            "Fix the regressed Settings page.\n\n"
            "Acceptance URL: http://100.94.241.120:8641/settings\n"
            "Target: http://100.94.241.120:8641/ai\n"
        )
        got = r2l._parse_requested_targets(body)
        assert "http://100.94.241.120:8641/settings" in got
        assert "http://100.94.241.120:8641/ai" in got
        # A casual URL in goal-prose (no acceptance-token prefix) is NOT a target.
        assert "http://example.com/context" not in got

    def test_extract_ignores_casual_url_in_prose(self):
        # A "see http://x" mention without an acceptance-token line must NOT
        # become a hard acceptance gate (would over-block prose handoffs).
        body = (
            "## Goal\n"
            "See http://example.com/context for the design doc.\n"
        )
        assert r2l._parse_requested_targets(body) == []

    def test_parse_verified_targets_from_result_body(self):
        body = (
            "Engineering result\n"
            "Verified: http://100.94.241.120:8641/settings ok\n"
            "Browser: http://100.94.241.120:8641/ai matches new UI\n"
        )
        got = r2l._parse_verified_targets(body)
        assert "http://100.94.241.120:8641/settings" in got
        assert "http://100.94.241.120:8641/ai" in got

    def test_parse_verified_ignores_negative_browser_line(self):
        body = (
            "Browser: http://100.94.241.120:8641/settings DOES NOT MATCH expected UI\n"
            "Verified: http://100.94.241.120:8640/settings ok\n"
        )
        got = r2l._parse_verified_targets(body)
        # The contradicting line is NOT a verification.
        assert "http://100.94.241.120:8641/settings" not in got
        # The canonical 8640 IS verified, but for a different host:port.
        assert "http://100.94.241.120:8640/settings" in got

    def test_browser_contradiction_detected(self):
        body = (
            "All proof rows green.\n"
            "Browser: 8641/settings DOES NOT MATCH the new UI — still shows old Settings.\n"
        )
        assert r2l._has_browser_contradiction(body) is True

    def test_evaluate_canary_scenario_8640_proof_8641_request(self):
        # The exact canary failure: user named :8641, Deploy Ops returned :8640
        # proof. The verdict must be NOT accepted on target.
        packet = (
            "## Goal\nApp Starter Kit settings cleanup.\n\n"
            "Acceptance URL: http://100.94.241.120:8641/settings\n"
        )
        result = (
            "[deploy-ops] PASS | deploy aligned on canonical target\n"
            "Verified: http://100.94.241.120:8640/settings ok\n"
        )
        v = r2l.evaluate_exact_target_acceptance(packet, result)
        assert v["status"] == "not_accepted_on_target"
        assert v["target_match"] is False
        assert "http://100.94.241.120:8641/settings" in v["missing"]
        assert "http://100.94.241.120:8641/settings" in v["requested_acceptance_targets"]

    def test_evaluate_browser_contradicts_overrides_deploy_proof(self):
        # Deploy Ops claims 8641 aligned, but browser DOM contradicts. The
        # verdict must be browser_contradiction — never accepted.
        packet = "Acceptance URL: http://100.94.241.120:8641/settings\n"
        result = (
            "[deploy-ops] PASS | deploy claims aligned on 8641\n"
            "Verified: http://100.94.241.120:8641/settings ok\n"
            "Browser: http://100.94.241.120:8641/settings DOES NOT MATCH — still old UI\n"
        )
        v = r2l.evaluate_exact_target_acceptance(packet, result)
        assert v["status"] == "browser_contradiction"
        assert v["browser_contradicts"] is True
        assert v["target_match"] is False

    def test_evaluate_browser_proof_on_exact_url_satisfies(self):
        packet = "Acceptance URL: http://100.94.241.120:8641/settings\n"
        result = (
            "[deploy-ops] PASS | deploy aligned\n"
            "Verified: http://100.94.241.120:8641/settings ok\n"
            "Browser: http://100.94.241.120:8641/settings matches new UI\n"
        )
        v = r2l.evaluate_exact_target_acceptance(packet, result)
        assert v["status"] == "ok"
        assert v["target_match"] is True
        assert v["missing"] == []

    def test_evaluate_no_targets_named_is_ok(self):
        # A handoff that does NOT name a specific acceptance URL must NOT be
        # blocked by this gate — keep existing behaviour for non-UI work.
        packet = "## Goal\nWrite a Markdown explainer.\n"
        result = "[design] PASS | wrote the explainer\n"
        v = r2l.evaluate_exact_target_acceptance(packet, result)
        assert v["status"] == "ok"
        assert v["target_match"] is True
        assert v["requested_acceptance_targets"] == []

    # ── route_to_lane integration: HANDOFF OK → NOT-ACCEPTED downgrade ──
    def _drive_with_packet(self, fake_tree, monkeypatch, *, goal, result_stdout):
        # Force route_to_lane to build a packet from `goal`, then have the fake
        # wrapper print the canned `result_stdout` (which carries the lane's
        # result body + the normalized status line).
        monkeypatch.setenv("FAKE_EXIT", "0")
        monkeypatch.setenv("FAKE_STDOUT", result_stdout)
        return r2l.route_to_lane(lane="qa", goal=goal)

    def test_handoff_8640_proof_for_8641_request_downgraded(self, fake_tree, monkeypatch):
        # The canary: packet asks for :8641, wrapper returns clean PASS with the
        # wrapper-side targets segment listing only :8640 as verified. HANDOFF OK
        # must be DOWNGRADED to NOT-ACCEPTED so P1 cannot close it as done.
        goal = (
            "App Starter Kit settings cleanup.\n"
            "Acceptance URL: http://100.94.241.120:8641/settings"
        )
        result_stdout = (
            "[qa] PASS | deploy aligned on canonical target | #dd-lane-qa ts=1.1 | "
            "targets=verified=http://100.94.241.120:8640/settings;contradicts=0"
        )
        out = self._drive_with_packet(
            fake_tree, monkeypatch, goal=goal, result_stdout=result_stdout
        )
        assert "HANDOFF OK" not in out, out
        assert "HANDOFF CHANGED-NOT-ACCEPTED" in out
        assert "http://100.94.241.120:8641/settings" in out  # missing target named
        assert "blocked on exact-target verification" in out.lower()

    def test_handoff_browser_contradicts_blocks_done(self, fake_tree, monkeypatch):
        goal = (
            "Settings page regression fix.\n"
            "Acceptance URL: http://100.94.241.120:8641/settings"
        )
        result_stdout = (
            "[qa] PASS | deploy claims aligned on 8641 | #dd-lane-qa ts=2.2 | "
            "targets=verified=http://100.94.241.120:8641/settings;contradicts=1"
        )
        out = self._drive_with_packet(
            fake_tree, monkeypatch, goal=goal, result_stdout=result_stdout
        )
        assert "HANDOFF OK" not in out, out
        assert "HANDOFF NOT-ACCEPTED" in out
        assert "browser dom contradicts" in out.lower()

    def test_handoff_named_target_and_matching_browser_proof_is_ok(self, fake_tree, monkeypatch):
        goal = "Acceptance URL: http://100.94.241.120:8641/settings"
        result_stdout = (
            "[qa] PASS | aligned and verified | #dd-lane-qa ts=3.3 | "
            "targets=verified=http://100.94.241.120:8641/settings;contradicts=0"
        )
        out = self._drive_with_packet(
            fake_tree, monkeypatch, goal=goal, result_stdout=result_stdout
        )
        assert "HANDOFF OK" in out
        # The echoed target-verification block tells P1 what was matched.
        assert "exact-target verification" in out
        assert "http://100.94.241.120:8641/settings" in out
        assert "target_match: true" in out

    def test_parse_targets_segment_extracts_verified_and_contradicts(self):
        out = (
            "[qa] PASS | aligned | #c ts=1 | "
            "targets=verified=http://x:8641/settings+http://x:8641/ai;contradicts=0"
        )
        seg = r2l._parse_targets_segment(out, "qa")
        assert seg is not None
        assert "http://x:8641/settings" in seg["verified"]
        assert "http://x:8641/ai" in seg["verified"]
        assert seg["contradicts"] is False

        out2 = (
            "[qa] PASS | aligned | #c ts=1 | "
            "targets=verified=http://x:8641/settings;contradicts=1"
        )
        seg2 = r2l._parse_targets_segment(out2, "qa")
        assert seg2["contradicts"] is True

        # No segment → None (legacy wrapper)
        assert r2l._parse_targets_segment("[qa] PASS | x | #c ts=1", "qa") is None

    def test_handoff_no_named_targets_unchanged(self, fake_tree, monkeypatch):
        # Regression guard: a packet that names NO acceptance URL must NOT be
        # blocked by the Phase 3 gate (otherwise design/research handoffs break).
        goal = "Write a Markdown explainer for the project channel."
        result_stdout = "[qa] PASS | wrote explainer | #dd-lane-qa ts=4.4"
        out = self._drive_with_packet(
            fake_tree, monkeypatch, goal=goal, result_stdout=result_stdout
        )
        assert "HANDOFF OK" in out
        assert "CHANGED-NOT-ACCEPTED" not in out
        assert "NOT-ACCEPTED" not in out

    def test_bare_port_request_matched_by_full_url_proof(self):
        # Levi's pattern: ask "verify the :8641 surface", proof line carries the
        # full http://host:8641/ URL. They must reconcile.
        packet = "Acceptance target: 100.94.241.120:8641/settings\n"
        result = "Verified: http://100.94.241.120:8641/settings ok\n"
        v = r2l.evaluate_exact_target_acceptance(packet, result)
        assert v["status"] == "ok"

    def test_normalize_drops_default_ports_and_trailing_slash(self):
        assert r2l._normalize_target("HTTP://Example.com:80/") == "http://example.com"
        assert r2l._normalize_target("https://Example.COM:443/foo") == "https://example.com/foo"
        assert r2l._normalize_target("http://x:8641/settings.") == "http://x:8641/settings"
