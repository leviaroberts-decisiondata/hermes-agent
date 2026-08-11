"""Pre-model admission + quarantine for lane callbacks (WTS 17cbc96c, Phase C).

``gateway/lane_wake.py`` is where a detached specialist lane's result re-enters a
gateway, and the drain's very next act is to inject an internal ``MessageEvent``
— i.e. to start a model turn that reads the callback's instructions and acts on
them (WTS writes, attachments, further lane dispatch). On 2026-08-10 that door
had no lock: destination was resolved from ``(platform, chat_id)``, Levi's
Telegram chat id is identical across all five bots, so PTG's and Azul's lane
results were injected into P1's session and P1 wrote reconciliation notes onto
client WTS records.

These tests pin the lock. The fixture is the incident's: the SAME chat id
``8737984752`` across five instances, so nothing can pass by looking familiar.

The zero-side-effect test drives the REAL ``GatewayRunner._drain_lane_wake_queue``
and asserts that a refused callback touches none of: the model/adapter, Telegram
send, any subprocess helper (the reaper, the WTS binder, the attach helper), the
WTS tools, or lane dispatch.
"""

import calendar
import importlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

LEVI_CHAT_ID = "8737984752"
INSTANCES = ("default", "classic", "ptg", "azul", "hyperscience")
P1 = "default"
P1_SESSION = "p1-telegram-session-1"


@pytest.fixture()
def wake(tmp_path, monkeypatch):
    """lane_wake bound to an isolated HERMES_HOME, running AS P1."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DD_LANE_WAKE_ENABLED", "1")
    from gateway import lane_wake
    from tools import dispatch_authority

    importlib.reload(lane_wake)
    monkeypatch.setattr(dispatch_authority, "active_instance", lambda: P1)
    return lane_wake


def _run_dir(tmp_path, run_id="20260810-173216-69340", lane="engineering", meta=True):
    """A directory that LOOKS like a real dispatch, because admission requires it.

    The lane-tree shape (``<lane>/runs/<run_id>``) plus ``meta.json`` are the
    structural containment the independent review added: a callback naming a
    directory that is not a real lane run is refused before its sidecar is even
    read. Fixtures must therefore be real runs — the forged shapes get their own
    tests in TestStructuralContainment below.
    """
    rd = Path(tmp_path) / "dd-lanes" / lane / "runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "stdout.log").write_text("[engineering] PASS | ok | done\n", encoding="utf-8")
    if meta:
        (rd / "meta.json").write_text(
            json.dumps({"lane": lane, "run_id": run_id, "lane_run_id": run_id,
                        "routing_present": True}),
            encoding="utf-8")
    return rd


def _authorise(rd, *, instance=P1, session=P1_SESSION, wts_task="17cbc96c", **over):
    from tools import dispatch_authority as da

    rec = da.build_authority(
        caller_instance=instance, destination_instance=instance,
        originating_session_id=session,
        run_id=Path(str(rd)).name, run_dir=str(rd), lane="engineering",
        wts_task=wts_task, platform="telegram", chat_type="dm", chat_id=LEVI_CHAT_ID,
        **over,
    )
    da.write_sidecar(rd, rec)
    return rec


def _emit(wake, rd, **over):
    kw = dict(
        run_dir=str(rd), lane="engineering", gate="PASS",
        session_key=f"agent:main:telegram:dm:{LEVI_CHAT_ID}",
        platform="telegram", chat_id=LEVI_CHAT_ID, chat_type="dm",
        wts_task="17cbc96c",
    )
    kw.update(over)
    token = wake.emit_wake_event(**kw)
    assert token.startswith("emitted("), token
    return wake.list_pending_events()[0]


# ── verdicts ─────────────────────────────────────────────────────────────────

class TestAdmissionVerdicts:
    def test_valid_p1_callback_is_admitted(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        assert event["instance"] == P1
        assert event["originating_session_id"] == P1_SESSION
        verdict = wake.admit_callback(event)
        assert verdict.ok, verdict.reason

    @pytest.mark.parametrize("client", [i for i in INSTANCES if i != P1])
    def test_client_callback_in_p1s_queue_is_refused(self, wake, tmp_path, client):
        """The incident. A client's lane result reaches P1's shared reaper, and
        P1's gateway must refuse it — same chat id, different authority."""
        rd = _run_dir(tmp_path)
        _authorise(rd, instance=client, session=f"{client}-sess-1")
        event = _emit(wake, rd)
        verdict = wake.admit_callback(event)
        assert not verdict.ok
        assert verdict.reason == "destination_mismatch", verdict.reason

    def test_same_instance_wrong_session_is_refused(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd, session=P1_SESSION)
        event = _emit(wake, rd)
        event["originating_session_id"] = "p1-telegram-session-2"
        verdict = wake.admit_callback(event)
        assert verdict.reason == "session_mismatch", verdict.reason

    def test_wrong_run_is_refused(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event["run_id"] = "20260810-174145-32360"
        assert wake.admit_callback(event).reason == "run_mismatch"

    def test_wrong_wts_task_is_refused(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd, wts_task="17cbc96c")
        event = _emit(wake, rd)
        event["wts_task"] = "2f1a2e80-9c00-41bb-95f5-0a38a20412cb"
        assert wake.admit_callback(event).reason == "wts_mismatch"

    def test_wrong_chain_is_refused(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd, chain_id="chain-a")
        event = _emit(wake, rd)
        event["chain_id"] = "chain-b"
        assert wake.admit_callback(event).reason == "chain_mismatch"

    def test_legacy_callback_without_instance_fails_closed(self, wake, tmp_path):
        """A pre-fix ``lane-wake/1`` event. It must NOT default to P1."""
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event["schema"] = "lane-wake/1"
        event.pop("instance", None)
        event.pop("destination_instance", None)
        event.pop("originating_session_id", None)
        verdict = wake.admit_callback(event)
        assert verdict.reason == "missing_instance", verdict.reason

    def test_callback_without_session_fails_closed(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event.pop("originating_session_id")
        assert wake.admit_callback(event).reason == "missing_session"

    def test_unknown_schema_fails_closed(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event["schema"] = "lane-wake/99"
        assert wake.admit_callback(event).reason == "schema_unsupported"

    def test_forged_callback_without_a_dispatch_record_fails_closed(self, wake, tmp_path):
        """A well-formed event naming P1 for a run P1 never dispatched."""
        rd = _run_dir(tmp_path)
        event = _emit(wake, rd, instance=P1, originating_session_id=P1_SESSION)
        assert wake.admit_callback(event).reason == "no_dispatch_record"

    def test_unidentified_gateway_admits_nothing(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        assert wake.admit_callback(event, receiving_instance="").reason == (
            "unidentified_receiver"
        )

    def test_stale_callback_fails_closed(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event["enqueued_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 172800)
        )
        assert wake.admit_callback(event).reason == "stale"
        # 0 disables the freshness window (operations escape hatch), and the
        # rest of the authority check still holds.
        assert wake.admit_callback(event, max_age_secs=0).ok

    def test_replayed_callback_fails_closed(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        assert wake.admit_callback(event).ok
        wake.claim_processed(event, outcome="injected")
        assert wake.admit_callback(event).reason == "replayed"

    def test_admission_has_no_side_effects(self, wake, tmp_path):
        """The verdict function itself writes nothing — quarantine is the
        caller's single, explicit act."""
        rd = _run_dir(tmp_path)
        _authorise(rd, instance="azul", session="azul-1")
        event = _emit(wake, rd)
        before = sorted(p.name for p in Path(tmp_path).rglob("*"))
        assert not wake.admit_callback(event).ok
        assert sorted(p.name for p in Path(tmp_path).rglob("*")) == before


# ── quarantine evidence ──────────────────────────────────────────────────────

class TestQuarantineEvidence:
    def test_evidence_records_reason_and_metadata(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd, instance="ptg", session="ptg-sess-1")
        event = _emit(wake, rd)
        verdict = wake.admit_callback(event)
        path = wake.quarantine_callback(event, verdict)
        assert path is not None and path.exists()
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert rec["schema"] == "lane-callback-quarantine/1"
        assert rec["reason"] == "destination_mismatch"
        assert rec["receiving_instance"] == P1
        assert rec["evidence"]["claimed_instance"] == "ptg"
        assert rec["evidence"]["record_instance"] == "ptg"
        assert rec["evidence"]["lane"] == "engineering"
        assert rec["evidence"]["run_id"] == Path(str(rd)).name

    def test_evidence_contains_no_chat_id_prompt_or_closeout(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd, instance="azul", session="azul-1")
        event = _emit(wake, rd, closeout="CONFIDENTIAL CLOSEOUT BODY")
        verdict = wake.admit_callback(event)
        path = wake.quarantine_callback(event, verdict)
        raw = path.read_text(encoding="utf-8")
        assert LEVI_CHAT_ID not in raw, "quarantine evidence leaked the chat id"
        assert "CONFIDENTIAL CLOSEOUT BODY" not in raw
        assert "LANE RESULT RETURNED" not in raw, "evidence leaked the model prompt"
        assert "prompt" not in json.loads(raw)["evidence"]

    def test_evidence_is_owner_only(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd, instance="azul", session="azul-1")
        event = _emit(wake, rd)
        path = wake.quarantine_callback(event, wake.admit_callback(event))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_quarantine_is_listable_for_audit(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd, instance="azul", session="azul-1")
        event = _emit(wake, rd)
        wake.quarantine_callback(event, wake.admit_callback(event))
        listed = wake.list_quarantined()
        assert len(listed) == 1
        assert listed[0]["reason"] == "destination_mismatch"


# ── the whole point: a refused callback changes NOTHING ──────────────────────

class _MockAdapter:
    """Stands in for the platform adapter. ``handle_message`` IS the model entry
    point — the drain calling it is what starts a P1 turn."""

    def __init__(self):
        self.handled = []
        self.sent = []

    async def handle_message(self, event):
        self.handled.append(event)

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))


def _make_fake_sleep(runner):
    state = {"calls": 0}

    async def _fake_sleep(_secs):
        state["calls"] += 1
        if state["calls"] >= 2:
            runner._running = False

    return _fake_sleep


def _runner(adapter):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from gateway.session import SessionSource

    class _StubRunner:
        def __init__(self):
            self._running = True
            self.adapters = {Platform.TELEGRAM: adapter}
            self._drain_lane_wake_queue = GatewayRunner._drain_lane_wake_queue.__get__(self)

        def _build_process_event_source(self, evt):
            return SessionSource(
                platform=Platform(evt["platform"]),
                chat_id=str(evt["chat_id"]),
                chat_type=evt.get("chat_type") or "dm",
                thread_id=evt.get("thread_id") or None,
            )

    return _StubRunner()


@pytest.fixture()
def no_side_effects(monkeypatch):
    """Trip-wires on every mutation path a callback could reach.

    subprocess.run covers the sanctioned helpers (dd-lane-reaper, dd-wts-bind,
    dd-wts-attach, the lane wrappers) — all of them are subprocesses, so one
    wire catches WTS mutation, attachment, chain advance and lane dispatch even
    if a future code path invents a new helper.
    """
    calls = {"subprocess": [], "route_to_lane": [], "wts_bind": [], "chain_status": []}

    def _boom(*a, **kw):
        calls["subprocess"].append(a[0] if a else kw.get("args"))
        raise AssertionError(f"quarantined callback ran a subprocess: {a[:1]}")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    import tools.route_to_lane_tool as r2l
    import tools.wts_bind_tool as wb

    monkeypatch.setattr(r2l, "route_to_lane",
                        lambda *a, **kw: calls["route_to_lane"].append(kw) or "")
    monkeypatch.setattr(wb, "wts_bind",
                        lambda *a, **kw: calls["wts_bind"].append(kw) or "")
    return calls


class TestZeroSideEffectQuarantine:
    @pytest.mark.asyncio
    async def test_refused_callback_never_reaches_the_model(
        self, wake, tmp_path, monkeypatch, no_side_effects
    ):
        import asyncio

        rd = _run_dir(tmp_path)
        _authorise(rd, instance="azul", session="azul-sess-1")
        _emit(wake, rd)

        adapter = _MockAdapter()
        runner = _runner(adapter)
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)

        # The model was never invoked and Telegram was never written to.
        assert adapter.handled == [], "a quarantined callback started a model turn"
        assert adapter.sent == [], "a quarantined callback wrote to Telegram"
        # No WTS mutation, attachment, chain advance or lane dispatch.
        assert no_side_effects["subprocess"] == []
        assert no_side_effects["route_to_lane"] == []
        assert no_side_effects["wts_bind"] == []
        # It was consumed (no noisy retry) and recorded as evidence.
        assert wake.list_pending_events() == []
        evidence = wake.list_quarantined()
        assert len(evidence) == 1
        assert evidence[0]["reason"] == "destination_mismatch"

    @pytest.mark.asyncio
    async def test_refused_callback_does_not_fall_back_to_p1s_session(
        self, wake, tmp_path, monkeypatch, no_side_effects
    ):
        """Not even a 'best effort' delivery to whoever is listening."""
        import asyncio

        rd = _run_dir(tmp_path)
        _authorise(rd, instance="ptg", session="ptg-sess-1")
        _emit(wake, rd)

        adapter = _MockAdapter()
        runner = _runner(adapter)
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)
        assert adapter.handled == []

        # A second drain pass must not resurrect it either.
        runner._running = True
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)
        assert adapter.handled == []

    @pytest.mark.asyncio
    async def test_legacy_event_is_quarantined_not_delivered(
        self, wake, tmp_path, monkeypatch, no_side_effects
    ):
        """A pre-fix event already sitting in the queue at upgrade time."""
        import asyncio

        rd = _run_dir(tmp_path)
        legacy = {
            "schema": "lane-wake/1",
            "idempotency_key": "lane-result:legacy:none:lane-result",
            "kind": "lane-result",
            "run_dir": str(rd),
            "run_id": Path(str(rd)).name,
            "lane": "engineering",
            "gate": "PASS",
            "session_key": f"agent:main:telegram:dm:{LEVI_CHAT_ID}",
            "platform": "telegram",
            "chat_id": LEVI_CHAT_ID,
            "chat_type": "dm",
            "prompt": "[LANE RESULT RETURNED — CONTINUE WORKFLOW]",
            "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        qdir = wake.wake_queue_dir()
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")

        adapter = _MockAdapter()
        runner = _runner(adapter)
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)

        assert adapter.handled == [], "a legacy unscoped callback was delivered to P1"
        assert wake.list_quarantined()[0]["reason"] == "missing_instance"

    @pytest.mark.asyncio
    async def test_valid_p1_callback_is_still_admitted_exactly_once(
        self, wake, tmp_path, monkeypatch
    ):
        """NON-REGRESSION. The lock must not close on the legitimate path."""
        import asyncio

        rd = _run_dir(tmp_path)
        _authorise(rd)
        _emit(wake, rd)

        adapter = _MockAdapter()
        runner = _runner(adapter)
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)

        assert len(adapter.handled) == 1, f"expected one injection, got {len(adapter.handled)}"
        injected = adapter.handled[0]
        assert injected.internal is True
        assert "[LANE RESULT RETURNED — CONTINUE WORKFLOW]" in injected.text
        assert wake.list_quarantined() == []
        assert wake.list_pending_events() == []

        # Replay: a second pass over the same key injects nothing more.
        runner._running = True
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)
        assert len(adapter.handled) == 1


# ── structural containment, through the real admission path (review Fix 2) ───
# An independent reviewer hand-wrote a wake event AND a matching dispatch sidecar
# naming instance="default", and it was ADMITTED: every field the gate compared
# was self-consistent, and nothing checked that the run it described was real.
#
# The containment below is the answer, and it is deliberately NOT a signature.
# Everything here runs as the same unix user; a hostile process with that uid can
# rewrite the checker itself. What these tests pin is the confused-deputy case:
# misrouted, replayed, and fabricated-by-accident callbacks.

class TestStructuralContainment:
    def test_a_fabricated_run_dir_outside_the_lane_tree_is_refused(self, wake, tmp_path):
        """The reviewer's forgery, verbatim: a scratch directory with a perfect
        sidecar and a perfectly-addressed callback."""
        forged = tmp_path / "scratch" / "runs" / "20260810-173216-69340"
        forged.mkdir(parents=True)
        (forged / "meta.json").write_text(
            json.dumps({"lane": "engineering", "run_id": forged.name}), encoding="utf-8")
        _authorise(forged)
        event = _emit(wake, forged)
        assert event["instance"] == P1, "fixture precondition: the forgery names P1"
        verdict = wake.admit_callback(event)
        assert not verdict.ok
        assert verdict.reason == "run_dir_escape", verdict.reason

    def test_a_fabricated_run_dir_without_meta_json_is_refused(self, wake, tmp_path):
        """Right place, right shape, but no dispatch ever happened here."""
        rd = _run_dir(tmp_path, meta=False)
        _authorise(rd)
        event = _emit(wake, rd)
        assert wake.admit_callback(event).reason == "dispatch_footprint_absent"

    def test_a_symlinked_run_dir_is_refused(self, wake, tmp_path):
        """A planted link makes an outside directory look contained."""
        real = tmp_path / "outside"
        real.mkdir()
        (real / "meta.json").write_text(
            json.dumps({"lane": "engineering", "run_id": "20260810-173216-69340"}),
            encoding="utf-8")
        link = tmp_path / "dd-lanes" / "engineering" / "runs" / "20260810-173216-69340"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real, target_is_directory=True)
        _authorise(link)
        event = _emit(wake, link)
        assert wake.admit_callback(event).reason == "run_dir_escape"

    def test_a_dotdot_run_dir_is_refused(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event["run_dir"] = str(rd.parent / ".." / "runs" / rd.name)
        assert wake.admit_callback(event).reason == "run_dir_escape"

    def test_a_callback_naming_no_run_dir_is_refused(self, wake, tmp_path):
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event["run_dir"] = ""
        assert wake.admit_callback(event).reason == "run_dir_absent"

    def _sidecar_for_another_run(self, rd, run_id):
        from tools import dispatch_authority as da

        rec = da.build_authority(
            caller_instance=P1, destination_instance=P1,
            originating_session_id=P1_SESSION,
            run_id=run_id, run_dir=str(rd), lane="engineering", wts_task="17cbc96c",
            platform="telegram", chat_type="dm", chat_id=LEVI_CHAT_ID,
        )
        da.write_sidecar(rd, rec)
        return rec

    def test_a_sidecar_from_another_run_is_refused(self, wake, tmp_path):
        """A REAL run directory carrying a REAL sidecar — for a different run.
        The record/claim comparison catches this one first."""
        rd = _run_dir(tmp_path)
        self._sidecar_for_another_run(rd, "20260810-174145-32360")
        event = _emit(wake, rd)
        assert wake.admit_callback(event).reason == "run_mismatch"

    def test_a_sidecar_and_callback_that_agree_with_each_other_but_not_the_directory(
        self, wake, tmp_path
    ):
        """Both halves lie consistently. The DIRECTORY NAME is the anchor: it is
        the one value that is pinned to a real dispatch by containment, so a
        genuine run's directory cannot be reused to carry another run's record."""
        rd = _run_dir(tmp_path)
        self._sidecar_for_another_run(rd, "20260810-174145-32360")
        event = _emit(wake, rd)
        event["run_id"] = "20260810-174145-32360"  # agrees with the sidecar
        assert wake.admit_callback(event).reason == "run_id_mismatch"

    def test_containment_is_measured_against_this_homes_lane_tree(self, wake, tmp_path):
        """The root comes from HERMES_HOME, never from the event — otherwise a
        callback could nominate the tree it is judged against."""
        other_home = tmp_path / "another-home"
        rd = _run_dir(other_home)
        _authorise(rd)
        event = _emit(wake, rd)
        assert wake.admit_callback(event).reason == "run_dir_escape"

    def test_containment_precedes_reading_the_sidecar(self, wake, tmp_path, monkeypatch):
        """An event naming a directory outside the tree does not get a file read
        from that directory and interpreted as authority."""
        from tools import dispatch_authority as da

        forged = tmp_path / "scratch" / "runs" / "20260810-173216-69340"
        forged.mkdir(parents=True)
        _authorise(forged)
        event = _emit(wake, forged)

        reads = []
        real_read = da.read_sidecar
        monkeypatch.setattr(da, "read_sidecar",
                            lambda rd: reads.append(str(rd)) or real_read(rd))
        assert not wake.admit_callback(event).ok
        assert reads == [], f"sidecar was read from an uncontained path: {reads}"

    @pytest.mark.asyncio
    async def test_a_forged_callback_never_reaches_the_model(
        self, wake, tmp_path, monkeypatch, no_side_effects
    ):
        """End to end, through the REAL drain: zero side effects."""
        import asyncio

        forged = tmp_path / "scratch" / "runs" / "20260810-173216-69340"
        forged.mkdir(parents=True)
        (forged / "meta.json").write_text(
            json.dumps({"lane": "engineering", "run_id": forged.name}), encoding="utf-8")
        _authorise(forged)
        _emit(wake, forged)

        adapter = _MockAdapter()
        runner = _runner(adapter)
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)

        assert adapter.handled == [], "a forged callback started a model turn"
        assert adapter.sent == []
        assert no_side_effects["subprocess"] == []
        assert wake.list_quarantined()[0]["reason"] == "run_dir_escape"


# ── the scrubbed-meta callback is not a forgery (review: wts_task asymmetry) ──

class TestWtsTaskAsymmetryThroughAdmission:
    def test_a_callback_silent_about_wts_task_is_still_admitted(self, wake, tmp_path):
        """The reaper's scrubbed-meta recovery path emits no wts_task, while the
        dispatch record (written by route_to_lane, which knew it) has one. That is
        a normal P1 callback; quarantining it drops real work."""
        rd = _run_dir(tmp_path)
        _authorise(rd, wts_task="17cbc96c-a70f-46e7-af23-1458d04b5368")
        event = _emit(wake, rd, wts_task=None)
        assert event["wts_task"] is None, "fixture precondition"
        verdict = wake.admit_callback(event)
        assert verdict.ok, verdict.reason

    def test_a_callback_naming_a_different_wts_task_is_still_refused(self, wake, tmp_path):
        """The crossover shape — P1's run, a client's task — must still refuse."""
        rd = _run_dir(tmp_path)
        _authorise(rd, wts_task="17cbc96c-a70f-46e7-af23-1458d04b5368")
        event = _emit(wake, rd, wts_task="2f1a2e80-9c00-41bb-95f5-0a38a20412cb")
        assert wake.admit_callback(event).reason == "wts_mismatch"


# ── freshness must not depend on the season (review: DST) ────────────────────
# tests/conftest.py pins ``TZ=UTC`` for determinism, and that is PRECISELY why
# this bug reached production and survived review: under UTC, ``time.timezone``
# is 0 and ``time.mktime`` treats the UTC struct correctly, so the broken
# expression is indistinguishable from the correct one. Every test here therefore
# sets a DST-observing zone explicitly. Without that, mutating the fix back to
# ``time.mktime(parsed) + time.timezone`` is caught by nothing.

@pytest.fixture()
def dst_zone(monkeypatch):
    """Run in America/Denver (MST/MDT) — a real zone that observes DST."""
    monkeypatch.setenv("TZ", "America/Denver")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestEventAgeIsDstSafe:
    def test_a_just_enqueued_event_is_not_an_hour_old(self, wake, tmp_path, dst_zone):
        """`enqueued_at` is UTC. The old code did mktime(utc_struct)+timezone,
        which reads a UTC struct as LOCAL and corrects with the STANDARD offset —
        wrong by exactly 3600s for the whole of daylight saving. Measured
        2026-08-11 in MDT: age=3600 for an event enqueued this instant."""
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        age = wake._event_age_secs(event)
        assert age is not None
        assert 0 <= age < 60, f"a just-enqueued event reported age={age}s"

    def test_age_is_dst_safe_in_both_halves_of_the_year(self, wake, monkeypatch, dst_zone):
        """Pin it in January (MST) AND July (MDT), so the bug cannot come back the
        next time the clocks change — and so a run in either half of the year is
        equally good evidence."""
        for moment in ("2026-01-15T12:00:00Z", "2026-07-15T12:00:00Z"):
            fixed = calendar.timegm(time.strptime(moment, "%Y-%m-%dT%H:%M:%SZ"))
            monkeypatch.setattr(time, "time", lambda: fixed + 30)
            assert wake._event_age_secs({"enqueued_at": moment}) == 30, moment
            monkeypatch.undo()

    def test_the_zone_this_runs_in_really_does_observe_dst(self, dst_zone):
        """Guards the guard: if the fixture stopped taking effect, the DST tests
        above would silently become vacuous — which is the exact way this bug
        survived in the first place."""
        assert time.daylight, "the test zone does not observe DST; the DST tests are vacuous"
        assert time.timezone != 0, "TZ is still UTC; the DST tests are vacuous"

    def test_a_genuinely_old_event_is_still_stale(self, wake, tmp_path):
        """The fix must not blunt the check it lives in."""
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        event["enqueued_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
        assert wake.admit_callback(event, max_age_secs=3600).reason == "stale"
        assert wake.admit_callback(event, max_age_secs=10800).ok

    def test_a_fresh_event_survives_a_tight_freshness_window(self, wake, tmp_path):
        """This is the failure the DST bug actually caused: with the window
        tightened below an hour, every fresh callback was quarantined — in
        summer only."""
        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        assert wake.admit_callback(event, max_age_secs=300).ok

    def test_an_unparseable_timestamp_is_not_an_age(self, wake):
        assert wake._event_age_secs({"enqueued_at": "yesterday"}) is None
        assert wake._event_age_secs({}) is None


# ── a quarantine is not a delivery (review Fix 3) ────────────────────────────
# gateway/run.py records the refusal in the wake queue's processed marker, and
# ~/.hermes/bin/dd-lane-reaper reads that marker back cross-process. It used to
# wrap ANY non-empty outcome as `wake=gateway-accepted(<outcome>,<key>)`, log
# "gateway drain injected the P1 continuation (canonical proof)", and grade the
# run orch=DONE. So a refused callback was reported as the strongest success
# signal the rail has. The reaper half is patched in
# bin/patches/dd-lane-reaper--authority-gate.patch and executed as bash by
# tests/tools/test_dd_lane_reaper_authority_patch.py; this half pins the token
# that patch reads.

class TestQuarantineOutcomeToken:
    @pytest.mark.asyncio
    async def test_the_marker_says_refused_not_something_acceptance_shaped(
        self, wake, tmp_path, monkeypatch, no_side_effects
    ):
        import asyncio

        rd = _run_dir(tmp_path)
        _authorise(rd, instance="ptg", session="ptg-1")
        event = _emit(wake, rd)
        key = event["idempotency_key"]

        adapter = _MockAdapter()
        runner = _runner(adapter)
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)

        marker = wake._processed_dir() / wake._safe_key_filename(key)
        assert marker.exists(), "the refused callback was not consumed"
        outcome = json.loads(marker.read_text(encoding="utf-8"))["outcome"]

        assert outcome.startswith("refused-"), outcome
        assert outcome == f"refused-quarantine:destination_mismatch"
        # The token the reaper reads must not be mistakable for delivery.
        for acceptance in ("injected", "claimed", "accepted", "processed", "ok"):
            assert acceptance not in outcome, (
                f"the refusal token contains {acceptance!r}, which a consumer "
                f"pattern-matching for acceptance would read as success")

    def test_the_refusal_token_has_exactly_one_definition(self, wake):
        assert wake.WAKE_OUTCOME_REFUSED == "refused-quarantine"
        assert wake.refused_outcome("session_mismatch") == (
            "refused-quarantine:session_mismatch")
        assert wake.refused_outcome("") == "refused-quarantine:unspecified"

    @pytest.mark.asyncio
    async def test_an_admitted_callback_still_records_an_acceptance_outcome(
        self, wake, tmp_path, monkeypatch
    ):
        """NON-REGRESSION: the canonical injection proof must survive."""
        import asyncio

        rd = _run_dir(tmp_path)
        _authorise(rd)
        event = _emit(wake, rd)
        key = event["idempotency_key"]

        adapter = _MockAdapter()
        runner = _runner(adapter)
        monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))
        await runner._drain_lane_wake_queue(interval=0.01)

        marker = wake._processed_dir() / wake._safe_key_filename(key)
        outcome = json.loads(marker.read_text(encoding="utf-8"))["outcome"]
        assert not outcome.startswith("refused-"), outcome
        assert len(adapter.handled) == 1
