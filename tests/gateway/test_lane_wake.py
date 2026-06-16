"""Tests for the lane-result ACTIVE WAKE bridge (WTS 331b65f8).

Covers the two halves of the bridge plus the policy contract:
  * emit side (gateway.lane_wake.emit_wake_event): enqueue, idempotency (writer +
    consumer markers), flag gate, no-routing skip, atomic queue file.
  * drain side dedupe logic (already_processed / mark_processed).
  * the structured P1 continuation prompt FORBIDS unauthorized deploy/canary/
    restart/push/merge (req 5).
  * the CLI emit path used by the bash producers (dd-lane-reaper).

These are pure-filesystem unit tests — no gateway process, no network. The drain
*method* itself (GatewayRunner._drain_lane_wake_queue) is exercised at the unit
level via its idempotency primitives; an end-to-end inject is proven by the live
canary, not here (it needs a running gateway + adapter).
"""

import json
import os
import importlib
from pathlib import Path

import pytest


@pytest.fixture()
def wake(tmp_path, monkeypatch):
    """Fresh lane_wake bound to an isolated HERMES_HOME, wake ENABLED."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DD_LANE_WAKE_ENABLED", "1")
    from gateway import lane_wake
    importlib.reload(lane_wake)
    return lane_wake


def _mk_run_dir(tmp_path, run_id="20260606-141249-78059", body="[engineering] PASS | ok | done\n"):
    rd = Path(tmp_path) / "dd-lanes" / "engineering" / "runs" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "stdout.log").write_text(body, encoding="utf-8")
    return rd


def _emit(wake, rd, **over):
    kw = dict(
        run_dir=str(rd), lane="engineering", gate="PASS",
        session_key="agent:main:telegram:dm:8737984752",
        platform="telegram", chat_id="8737984752", chat_type="dm",
        wts_task="331b65f8-39e1-4523-b52d-19fd4461fb52",
    )
    kw.update(over)
    return wake.emit_wake_event(**kw)


def test_emit_enqueues_one_event(wake, tmp_path):
    rd = _mk_run_dir(tmp_path)
    token = _emit(wake, rd)
    assert token.startswith("emitted("), token
    pending = wake.list_pending_events()
    assert len(pending) == 1
    ev = pending[0]
    assert ev["schema"] == "lane-wake/1"
    assert ev["lane"] == "engineering"
    assert ev["gate"] == "PASS"
    assert ev["wts_task"] == "331b65f8-39e1-4523-b52d-19fd4461fb52"
    assert ev["platform"] == "telegram"
    assert ev["chat_id"] == "8737984752"
    # the prompt is precomputed and carries the continuation contract
    assert "[LANE RESULT RETURNED — CONTINUE WORKFLOW]" in ev["prompt"]


def test_emit_is_idempotent_writer_side(wake, tmp_path):
    """A second emit for the SAME (run, wts, kind) is refused before drain runs."""
    rd = _mk_run_dir(tmp_path)
    t1 = _emit(wake, rd)
    t2 = _emit(wake, rd)
    assert t1.startswith("emitted(")
    assert t2.startswith("skipped(dup:"), t2
    assert len(wake.list_pending_events()) == 1


def test_emit_refused_after_processed(wake, tmp_path):
    """Once an event is marked processed (consumer side), re-emit is refused —
    this is what stops a reaper re-run from double-waking P1."""
    rd = _mk_run_dir(tmp_path)
    _emit(wake, rd)
    ev = wake.list_pending_events()[0]
    wake.mark_processed(ev, outcome="injected")
    # queue is now empty and the processed marker exists
    assert wake.list_pending_events() == []
    assert wake.already_processed(ev["idempotency_key"]) is True
    # a duplicate reap tries to emit again → refused as already processed
    t = _emit(wake, rd)
    assert t.startswith("skipped(processed:"), t


def test_claim_processed_is_atomic_consumer_side(wake, tmp_path):
    """Only one drain consumer may claim a queued event for injection."""
    rd = _mk_run_dir(tmp_path)
    _emit(wake, rd)
    ev = wake.list_pending_events()[0]

    assert wake.claim_processed(ev, outcome="claimed") is True
    assert wake.claim_processed(ev, outcome="claimed") is False
    assert wake.already_processed(ev["idempotency_key"]) is True
    marker = wake._processed_dir() / wake._safe_key_filename(ev["idempotency_key"])
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["outcome"] == "claimed"


def test_mark_processed_updates_existing_claim(wake, tmp_path):
    rd = _mk_run_dir(tmp_path)
    _emit(wake, rd)
    ev = wake.list_pending_events()[0]

    assert wake.claim_processed(ev, outcome="claimed") is True
    wake.mark_processed(ev, outcome="injected")

    marker = wake._processed_dir() / wake._safe_key_filename(ev["idempotency_key"])
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["outcome"] == "injected"
    assert wake.list_pending_events() == []


def test_distinct_runs_are_not_deduped(wake, tmp_path):
    rd1 = _mk_run_dir(tmp_path, run_id="run-aaa")
    rd2 = _mk_run_dir(tmp_path, run_id="run-bbb")
    assert _emit(wake, rd1).startswith("emitted(")
    assert _emit(wake, rd2).startswith("emitted(")
    assert len(wake.list_pending_events()) == 2


def test_flag_off_skips_emit(wake, tmp_path, monkeypatch):
    monkeypatch.setenv("DD_LANE_WAKE_ENABLED", "0")
    rd = _mk_run_dir(tmp_path)
    t = _emit(wake, rd)
    assert t == "skipped(disabled)"
    assert wake.list_pending_events() == []


def test_no_routing_skips(wake, tmp_path):
    rd = _mk_run_dir(tmp_path)
    t = _emit(wake, rd, session_key="", platform="", chat_id="")
    assert t == "skipped(no-routing)"
    assert wake.list_pending_events() == []


def test_continuation_prompt_forbids_unauthorized_actions(wake):
    p = wake.build_continuation_prompt(
        wts_task="abc", lane="qa", gate="PASS",
        run_dir="/x/runs/run-1", result_file="lane-result-run-1.md",
    )
    low = p.lower()
    # governor framing + the explicit safety prohibitions (req 5)
    assert "workflow governor" in low
    for forbidden in ("slack canary", "deploy", "restart", "push", "merge"):
        assert forbidden in low, f"missing prohibition: {forbidden}"
    assert "do not" in low
    # the three decision options must be present
    assert "route the next lane" in low
    assert "hold for authorization" in low
    assert "final synthesis" in low


def test_missing_wts_prompt_says_hold(wake):
    p = wake.build_continuation_prompt(wts_task=None, lane="qa", gate="PASS", run_dir="/x/runs/r")
    assert "WTS is unknown/missing" in p
    assert "HOLD" in p


def test_continuation_prompt_carries_structured_fields(wake):
    """P1 orchestration repair Batch B: the continuation prompt P1 receives on wake
    must carry the STRUCTURED fields it needs to govern without a user message —
    lane, run id, WTS task, verdict/gate, and the result artifact path. (req:
    'P1 continuation prompt includes structured fields'.)"""
    p = wake.build_continuation_prompt(
        wts_task="eaf06358-bccc-4986-bbd1-c0bb045106d9",
        lane="engineering",
        gate="PASS",
        run_dir="/Users/openclaw/.hermes/dd-lanes/engineering/runs/20260616-010101-42",
        result_file="/tmp/eng-result.md",
        result_sha="deadbeefcafe0123456789",
    )
    assert "Lane: engineering" in p
    assert "Run id: 20260616-010101-42" in p
    assert "WTS: eaf06358-bccc-4986-bbd1-c0bb045106d9" in p
    assert "Gate: PASS" in p
    assert "/tmp/eng-result.md" in p
    assert "deadbeefcafe" in p  # result sha (truncated) for cross-checking the artifact


def test_engineering_pass_continuation_routes_to_qa_without_user_message(wake):
    """Batch B regression — the named acceptance fixture: an Engineering PASS result
    must produce a P1 continuation that, with NO intervening user/operator message,
    instructs P1 (the governor) to route the NEXT lane (eng → QA) under policy.
    The prompt is the autonomous-continuation contract; it explicitly tells P1 to act
    now and not wait for a user message, and names route_to_lane as the next-lane
    mechanism."""
    p = wake.build_continuation_prompt(
        wts_task="eaf06358-bccc-4986-bbd1-c0bb045106d9",
        lane="engineering", gate="PASS",
        run_dir="/x/runs/eng-run-1", result_file="/tmp/eng.md",
    )
    low = p.lower()
    # Acts WITHOUT a user message (the whole point of the active wake).
    assert "do not wait for a user message" in low or "do not go idle" in low
    # eng PASS → QA is the worked example the contract names.
    assert "engineering pass → qa" in low
    assert "route_to_lane" in low
    # And it remains a GOVERNOR turn, not a chat reply.
    assert "workflow governor" in low


def test_idempotency_key_is_stable(wake):
    k1 = wake.make_idempotency_key(run_id="r1", wts_task="t1", kind="lane-result")
    k2 = wake.make_idempotency_key(run_id="r1", wts_task="t1", kind="lane-result")
    k3 = wake.make_idempotency_key(run_id="r1", wts_task="t2", kind="lane-result")
    assert k1 == k2
    assert k1 != k3
    assert k1 == "lane-result:r1:t1:lane-result"


def test_cli_emit_and_list(wake, tmp_path, capsys):
    """The bash producers shell `python -m gateway.lane_wake --emit ...`; prove the
    CLI enqueues and that --list prints the event without the prompt body."""
    rd = _mk_run_dir(tmp_path)
    rc = wake._main([
        "--emit", "--run-dir", str(rd), "--lane", "engineering", "--gate", "PASS",
        "--session-key", "agent:main:telegram:dm:8737984752",
        "--platform", "telegram", "--chat-id", "8737984752", "--chat-type", "dm",
        "--wts-task", "task-xyz",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "emitted(" in out
    assert len(wake.list_pending_events()) == 1
    # --list prints JSON without the (large) prompt field
    rc2 = wake._main(["--list"])
    assert rc2 == 0
    listed = capsys.readouterr().out.strip().splitlines()
    assert len(listed) == 1
    row = json.loads(listed[0])
    assert "prompt" not in row
    assert row["wts_task"] == "task-xyz"


@pytest.mark.asyncio
async def test_gateway_drain_injects_internal_event_once(wake, tmp_path, monkeypatch):
    """INTEGRATION: the REAL GatewayRunner._drain_lane_wake_queue, driven against a
    queued wake event + a mock adapter, must inject exactly one internal=True
    MessageEvent carrying the continuation prompt, mark the event processed, and
    NOT re-inject on the next pass (consumer-side idempotency)."""
    import asyncio
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    # Enqueue a real wake event for a telegram DM (P1's shape).
    rd = _mk_run_dir(tmp_path)
    assert _emit(wake, rd).startswith("emitted(")

    captured = []

    class _MockAdapter:
        async def handle_message(self, event):
            captured.append(event)

    mock_adapter = _MockAdapter()

    # A minimal stand-in for `self`: only the attributes the drain touches. We bind
    # the REAL method to it so we exercise the production code, not a copy.
    class _StubRunner:
        def __init__(self):
            self._running = True
            self.adapters = {Platform.TELEGRAM: mock_adapter}
            self._drain_lane_wake_queue = GatewayRunner._drain_lane_wake_queue.__get__(self)

        def _build_process_event_source(self, evt):
            # Mirror the production resolver's fallback (parse platform/chat from the
            # event) without needing a real session_store.
            return SessionSource(
                platform=Platform(evt["platform"]),
                chat_id=str(evt["chat_id"]),
                chat_type=evt.get("chat_type") or "dm",
                thread_id=evt.get("thread_id") or None,
            )

    runner = _StubRunner()

    # Skip the 45s startup delay so the test is fast.
    monkeypatch.setattr(asyncio, "sleep", _make_fake_sleep(runner))

    await runner._drain_lane_wake_queue(interval=0.01)

    # Exactly one injection, internal, with the continuation prompt + telegram source.
    assert len(captured) == 1, f"expected one injection, got {len(captured)}"
    ev = captured[0]
    assert ev.internal is True
    assert ev.source.platform == Platform.TELEGRAM
    assert ev.source.chat_id == "8737984752"
    assert "[LANE RESULT RETURNED — CONTINUE WORKFLOW]" in ev.text
    assert "do not" in ev.text.lower()  # the safety prohibition is in the woke prompt

    # The event is marked processed and removed from the queue → no re-wake.
    assert wake.list_pending_events() == []


def _make_fake_sleep(runner):
    """An async sleep stub that stops the drain loop after its first real pass.

    The drain does: sleep(45) [startup], then loop { work; sleep(interval) }. We let
    the startup sleep pass through (no-op), let the first work pass run, then flip
    _running=False on the next sleep so the while-loop exits deterministically.
    """
    state = {"calls": 0}

    async def _fake_sleep(secs):
        state["calls"] += 1
        # calls: 1 = startup delay (no-op), 2 = end of first work pass → stop.
        if state["calls"] >= 2:
            runner._running = False
        return None

    return _fake_sleep


def test_malformed_event_file_does_not_wedge_drain(wake, tmp_path):
    rd = _mk_run_dir(tmp_path)
    _emit(wake, rd)
    # drop a garbage file into the queue
    bad = wake.wake_queue_dir() / "garbage.json"
    bad.write_text("{not json", encoding="utf-8")
    pending = wake.list_pending_events()
    # the good event still lists; the bad file was moved aside
    assert len(pending) == 1
    assert not bad.exists()
    assert (wake.wake_queue_dir() / "garbage.json.bad").exists()


# ── The reaper↔gateway routing CONTRACT (Canary-3 TOCTOU wake-recovery) ──────────
# The live defect: a scrubbed-meta lane run reaped via DISCOVERY has NO session_key,
# so the gateway's _build_process_event_source must build the source from the event's
# flat routing — and it REQUIRES the FULL TRIPLET platform + chat_type + chat_id
# (gateway/run.py: `if not platform_name or not chat_type or not chat_id: return None`).
# The prior reaper fix recovered platform + chat_id but NOT chat_type, so the gateway
# returned None and logged "dropping event with no routing metadata". These tests pin
# that contract against the REAL production resolver (not a stub) so the masking gap
# in test_gateway_drain_injects_internal_event_once (which stubs the resolver and
# defaults chat_type) cannot hide a regression again.

class _ResolverHost:
    """Minimal host carrying the REAL _build_process_event_source bound to it.

    Only `session_store` is referenced, and ONLY when the event has a session_key.
    The scrubbed-discovery path has none, so the resolver goes straight to the flat
    platform/chat_type/chat_id triplet derivation we are exercising.
    """

    def __init__(self):
        from gateway.run import GatewayRunner

        class _EmptyStore:
            _entries = {}

            def _ensure_loaded(self):
                return None

        self.session_store = _EmptyStore()
        self._build_process_event_source = (
            GatewayRunner._build_process_event_source.__get__(self)
        )


def test_gateway_resolver_accepts_recovered_triplet():
    """The REAL resolver builds a usable SessionSource from a scrubbed-discovery wake
    event that carries the recovered platform + chat_type + chat_id (no session_key) —
    i.e. the gateway would NOT drop it and WOULD inject the P1 continuation."""
    from gateway.config import Platform

    host = _ResolverHost()
    # Exactly the event the fixed reaper now emits on the scrubbed-discovery path:
    # session_key empty (scrubbed), platform/chat_type/chat_id recovered from
    # origin_surface / origin_kind / mirror-status.
    evt = {
        "session_key": "",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "8737984752",
        "thread_id": None,
    }
    source = host._build_process_event_source(evt)
    assert source is not None, "gateway DROPPED a fully-recovered wake event"
    assert source.platform == Platform.TELEGRAM
    assert source.chat_id == "8737984752"
    assert source.chat_type == "dm"


def test_gateway_resolver_drops_event_missing_chat_type():
    """NON-VACUITY for the contract: the SAME event WITHOUT chat_type (the pre-fix
    reaper's emitted shape — platform + chat_id only, no session_key) is DROPPED by
    the real resolver. This is precisely the live "dropping event with no routing
    metadata" path; recovering chat_type is what moves it from this branch to accept."""
    host = _ResolverHost()
    evt_missing_ctype = {
        "session_key": "",
        "platform": "telegram",
        "chat_type": "",       # <- the leg the prior fix left empty
        "chat_id": "8737984752",
        "thread_id": None,
    }
    assert host._build_process_event_source(evt_missing_ctype) is None, (
        "resolver accepted a chat_type-less event — the contract this fix relies on "
        "is not actually enforced, so the test would be vacuous"
    )


def test_reaper_emitted_event_carries_chat_type_to_gateway(wake, tmp_path):
    """END-TO-END (emit → real resolver): an emit with the recovered chat_type writes
    it into the queued event payload, and feeding THAT payload to the REAL gateway
    resolver yields a usable source. Proves chat_type survives the emit→event→drain
    contract, not just the function arg."""
    rd = _mk_run_dir(tmp_path)
    # Emit WITHOUT a session_key (scrubbed path) but WITH the recovered triplet.
    token = wake.emit_wake_event(
        run_dir=str(rd), lane="engineering", gate="PASS",
        session_key="", platform="telegram", chat_id="8737984752", chat_type="dm",
        wts_task="5f11ced1-a4f3-484c-9383-ce946940bc63",
    )
    assert token.startswith("emitted("), token
    ev = wake.list_pending_events()[0]
    assert ev["chat_type"] == "dm", "emitted event payload dropped chat_type"
    # The queued payload must satisfy the real gateway resolver.
    host = _ResolverHost()
    source = host._build_process_event_source(ev)
    assert source is not None, "gateway would DROP the reaper-emitted scrubbed-path event"
    assert source.chat_type == "dm"
    assert source.chat_id == "8737984752"
