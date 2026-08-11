"""route_to_lane records WHO dispatched a lane and WHERE its result may return.

WTS 17cbc96c, Phase B step 1.

Before this, ``_register_pending_with_reaper`` handed the reaper
``session_key, platform, chat_id, chat_type, thread_id, budget, wts_task`` — none
of which identifies the dispatching Hermes instance or the exact originating
session. Levi's Telegram chat id is the same in all five bots, so the reaper's
delivery target was ambiguous by construction.

The fix is ADDITIVE: the positional ``--register`` contract is unchanged (asserted
below, byte for byte) and a versioned ``dispatch-authority`` sidecar is written
alongside it.
"""

import json
import stat
import textwrap
from pathlib import Path

import pytest

import tools.route_to_lane_tool as r2l
from tools import dispatch_authority as da

LEVI_CHAT_ID = "8737984752"
P1 = "default"
ROUTE_KEY = f"agent:main:telegram:dm:{LEVI_CHAT_ID}"


class _Agent:
    """Carries the identity the gateway attaches per turn."""

    def __init__(self, *, route_key=ROUTE_KEY, session_id="p1-telegram-session-1",
                 obs_key=None):
        if route_key is not None:
            self._dd_route_key = route_key
        if session_id is not None:
            self._dd_gateway_session_id = session_id
        if obs_key is not None:
            self._dd_session_key = obs_key


@pytest.fixture(autouse=True)
def _as_p1(monkeypatch):
    monkeypatch.setattr("tools.p1_caller_boundary.active_caller_id", lambda: P1)
    monkeypatch.setattr(da, "active_instance", lambda: P1)


@pytest.fixture()
def fake_reaper(tmp_path, monkeypatch):
    reaper = tmp_path / "bin" / "dd-lane-reaper"
    reaper.parent.mkdir(parents=True, exist_ok=True)
    argv_log = tmp_path / "reaper-argv.log"
    reaper.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf '%s\\n' "$@" > "{argv_log}"
        exit 0
    """), encoding="utf-8")
    reaper.chmod(reaper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(r2l, "_REAPER", reaper)
    return argv_log


@pytest.fixture()
def run_dir(tmp_path):
    rd = tmp_path / "dd-lanes" / "qa" / "runs" / "20260810-173216-69340"
    rd.mkdir(parents=True)
    return rd


def _pending_line(run_dir):
    return (f"[qa] PENDING | run still in flight | #dd-lane-qa ts=9.9 "
            f"run_dir={run_dir}")


def _register(run_dir, agent, **kw):
    return r2l._register_pending_with_reaper(_pending_line(run_dir), agent, **kw)


# ── the exact originating session ────────────────────────────────────────────

class TestOriginatingSession:
    def test_prefers_the_gateways_own_session_id(self):
        agent = _Agent(session_id="p1-sess-A", obs_key="agent:hermes:gateway:p1-sess-B")
        assert r2l._originating_session_id(agent) == "p1-sess-A"

    def test_falls_back_to_the_observability_key_tail(self):
        agent = _Agent(session_id=None, obs_key="agent:hermes:gateway:p1-sess-B")
        assert r2l._originating_session_id(agent) == "p1-sess-B"

    def test_profile_segmented_observability_key_still_yields_the_session(self):
        agent = _Agent(session_id=None, obs_key="agent:hermes:gateway:dd-design:sess-C")
        assert r2l._originating_session_id(agent) == "sess-C"

    def test_no_session_anywhere_is_empty_not_guessed(self):
        assert r2l._originating_session_id(_Agent(session_id=None)) == ""
        assert r2l._originating_session_id(None) == ""

    def test_routing_key_is_never_mistaken_for_a_session(self):
        """The routing key is the SAME string for all five bots — it must never
        stand in for the originating session."""
        agent = _Agent(session_id=None, obs_key=ROUTE_KEY)
        assert r2l._originating_session_id(agent) == ""


# ── the sidecar ──────────────────────────────────────────────────────────────

class TestAuthoritySidecar:
    def test_registration_writes_the_record(self, run_dir, fake_reaper):
        note = _register(run_dir, _Agent(), wts_task="17cbc96c", lane="qa",
                         mission_id="mission-1")
        assert "authority: dispatch-authority/1" in note

        rec = da.read_sidecar(run_dir)
        assert rec is not None
        assert rec["caller_instance"] == P1
        assert rec["destination_instance"] == P1
        assert rec["originating_session_id"] == "p1-telegram-session-1"
        assert rec["run_id"] == run_dir.name
        assert rec["lane"] == "qa"
        assert rec["wts_task"] == "17cbc96c"
        assert rec["mission_id"] == "mission-1"
        assert rec["route_fingerprint"] == da.route_fingerprint("telegram", "dm", LEVI_CHAT_ID)

    def test_positional_register_contract_is_unchanged(self, run_dir, fake_reaper):
        """Backward compatibility: the reaper's argv must be byte-identical to
        the pre-fix call, so an unpatched dd-lane-reaper keeps working."""
        _register(run_dir, _Agent(), wts_task="17cbc96c", lane="qa")
        argv = fake_reaper.read_text(encoding="utf-8").splitlines()
        assert argv == [
            "--register", str(run_dir), ROUTE_KEY,
            "telegram", LEVI_CHAT_ID, "dm",
            "",           # thread_id
            "",           # budget → reaper default
            "17cbc96c",   # wts_task in slot 8
        ]

    def test_sidecar_is_owner_only_and_carries_no_chat_id(self, run_dir, fake_reaper):
        _register(run_dir, _Agent(), lane="qa")
        path = da.sidecar_path(run_dir)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert LEVI_CHAT_ID not in path.read_text(encoding="utf-8")

    def test_no_session_writes_no_record(self, run_dir, fake_reaper):
        """Never invent authority. Without a session the callback has none, and
        is quarantined on return — which the note says out loud."""
        note = _register(run_dir, _Agent(session_id=None), lane="qa")
        assert "NOT RECORDED" in note
        assert "quarantined" in note
        assert da.read_sidecar(run_dir) is None

    def test_unidentified_instance_writes_no_record(self, run_dir, fake_reaper, monkeypatch):
        monkeypatch.setattr(da, "active_instance", lambda: "")
        note = _register(run_dir, _Agent(), lane="qa")
        assert "NOT RECORDED (no instance)" in note
        assert da.read_sidecar(run_dir) is None

    def test_record_names_the_real_caller_not_p1_by_default(self, run_dir, fake_reaper,
                                                            monkeypatch):
        """If some other home ever reaches this code, the record must say so —
        the sidecar is never allowed to launder a caller into P1."""
        monkeypatch.setattr(da, "active_instance", lambda: "azul")
        _register(run_dir, _Agent(), lane="qa")
        rec = da.read_sidecar(run_dir)
        assert rec["caller_instance"] == "azul"
        assert rec["destination_instance"] == "azul"

    def test_registration_failure_still_leaves_the_record(self, run_dir, monkeypatch,
                                                         tmp_path):
        """The record is written BEFORE the reaper call, so a run that is reaped
        immediately (or whose registration fails) is still checkable."""
        reaper = tmp_path / "bin" / "failing-reaper"
        reaper.parent.mkdir(parents=True, exist_ok=True)
        reaper.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
        reaper.chmod(reaper.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(r2l, "_REAPER", reaper)
        note = _register(run_dir, _Agent(), lane="qa")
        assert "FAILED (exit=3)" in note
        assert da.read_sidecar(run_dir) is not None


# ── end to end: the record decides the callback ──────────────────────────────

class TestRecordGovernsTheCallback:
    def _claim(self, run_dir, **over):
        event = {
            "schema": "lane-wake/2",
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "platform": "telegram", "chat_type": "dm", "chat_id": LEVI_CHAT_ID,
            "instance": P1,
            "originating_session_id": "p1-telegram-session-1",
            "wts_task": "17cbc96c",
        }
        event.update(over)
        return da.claim_from_event(event)

    def test_the_dispatching_session_is_admitted(self, run_dir, fake_reaper):
        _register(run_dir, _Agent(), wts_task="17cbc96c", lane="qa")
        rec = da.authority_for_run(run_dir)
        assert da.authority_mismatch(rec, self._claim(run_dir),
                                     receiving_instance=P1) is None

    def test_a_different_session_on_the_same_instance_is_refused(self, run_dir, fake_reaper):
        _register(run_dir, _Agent(), wts_task="17cbc96c", lane="qa")
        rec = da.authority_for_run(run_dir)
        verdict = da.authority_mismatch(
            rec, self._claim(run_dir, originating_session_id="p1-telegram-session-2"),
            receiving_instance=P1,
        )
        assert verdict == "session_mismatch"

    def test_a_client_gateway_cannot_consume_a_p1_dispatch(self, run_dir, fake_reaper):
        _register(run_dir, _Agent(), wts_task="17cbc96c", lane="qa")
        rec = da.authority_for_run(run_dir)
        for client in ("classic", "ptg", "azul", "hyperscience"):
            verdict = da.authority_mismatch(rec, self._claim(run_dir),
                                            receiving_instance=client)
            assert verdict == "destination_mismatch", f"{client}: {verdict}"


# ── the caller authority handed to dd-lane-run ───────────────────────────────

class TestCallerAuthorityEnv:
    """dd-lane-run writes the spawn-time ``wake-target`` sidecar. HERMES_HOME is
    pinned to the SHARED home before the wrapper runs, so the caller's identity
    has to travel explicitly or every dispatch would be labelled "default"."""

    def test_the_gateway_attaches_the_session_id_the_tool_reads(self, monkeypatch):
        """Producer side of ``_dd_gateway_session_id``: the gateway's per-turn
        identity attach must actually set the attribute route_to_lane reads,
        otherwise every dispatch silently falls back to the sanitised
        observability tail."""
        from gateway.run import _attach_dd_context_for_turn

        class _A:
            pass

        agent = _A()
        _attach_dd_context_for_turn(
            agent, run_id="run-1", session_key="agent:hermes:gateway:sess-9",
            route_key=ROUTE_KEY, gateway_session_id="telegram:dm:8737984752",
        )
        assert agent._dd_gateway_session_id == "telegram:dm:8737984752"
        assert r2l._originating_session_id(agent) == "telegram:dm:8737984752"
        # …and the gateway's own instance is recorded on the agent too.
        assert hasattr(agent, "_dd_home_id")

    def test_env_names_are_the_ones_the_patch_reads(self):
        source = Path(r2l.__file__).read_text(encoding="utf-8")
        assert "DD_CALLER_INSTANCE" in source
        assert "DD_CALLER_SESSION_ID" in source

        patch = (Path(__file__).resolve().parents[2]
                 / "bin" / "patches" / "dd-lane-run--wake-target-v2.patch")
        body = patch.read_text(encoding="utf-8")
        assert "DD_CALLER_INSTANCE" in body
        assert "DD_CALLER_SESSION_ID" in body
        assert '"schema": "wake-target/2"' in body
