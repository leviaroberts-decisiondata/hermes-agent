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


def _run_dir(tmp_path, run_id="20260810-173216-69340"):
    rd = Path(tmp_path) / "dd-lanes" / "engineering" / "runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "stdout.log").write_text("[engineering] PASS | ok | done\n", encoding="utf-8")
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
