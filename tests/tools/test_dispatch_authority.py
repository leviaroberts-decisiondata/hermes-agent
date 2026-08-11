"""Dispatch authority: the record that makes a lane callback checkable.

WTS 17cbc96c, Phase B.

The whole fixture is one line of the incident: **the same Telegram chat id
(8737984752) reaches Levi through five different bots**, so a callback that says
"telegram, chat 8737984752" has named a transport, not an authority. Every test
below keeps that chat id constant and varies only the instance, the session, the
run or the work — because that is exactly the axis the crossover travelled on.

These are pure-filesystem unit tests: no gateway, no reaper, no network.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from tools import dispatch_authority as da

# One human, one Telegram chat id, five bots.
LEVI_CHAT_ID = "8737984752"
INSTANCES = ("default", "classic", "ptg", "azul", "hyperscience")
P1 = "default"


def _record(tmp_path, *, instance=P1, session="p1-sess-1", run_id="20260810-173216-69340",
            lane="engineering", wts_task="17cbc96c-a70f-46e7-af23-1458d04b5368",
            mission_id=None, chain_id=None, chat_id=LEVI_CHAT_ID, write=True):
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    rec = da.build_authority(
        caller_instance=instance,
        destination_instance=instance,
        originating_session_id=session,
        run_id=run_id,
        run_dir=str(run_dir),
        lane=lane,
        wts_task=wts_task,
        mission_id=mission_id,
        chain_id=chain_id,
        platform="telegram",
        chat_type="dm",
        chat_id=chat_id,
    )
    if write:
        assert da.write_sidecar(run_dir, rec) is not None
    return run_dir, rec


def _callback(*, instance=P1, session="p1-sess-1", run_id="20260810-173216-69340",
              wts_task="17cbc96c-a70f-46e7-af23-1458d04b5368", mission_id="",
              chain_id="", chat_id=LEVI_CHAT_ID, destination=None):
    """A wake event as the reaper would emit it."""
    ev = {
        "schema": "lane-wake/2",
        "run_id": run_id,
        "lane": "engineering",
        "gate": "PASS",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": chat_id,
        "wts_task": wts_task,
        "mission_id": mission_id,
        "chain_id": chain_id,
    }
    if instance is not None:
        ev["instance"] = instance
    if destination is not None:
        ev["destination_instance"] = destination
    if session is not None:
        ev["originating_session_id"] = session
    return ev


def _verdict(run_dir, event, receiver=P1):
    return da.authority_mismatch(
        da.authority_for_run(run_dir), da.claim_from_event(event),
        receiving_instance=receiver,
    )


# ── the record itself ────────────────────────────────────────────────────────

class TestSidecar:
    def test_round_trip(self, tmp_path):
        run_dir, rec = _record(tmp_path)
        read = da.read_sidecar(run_dir)
        assert read == rec
        assert read["schema"] == "dispatch-authority/1"
        assert read["caller_instance"] == P1
        assert read["originating_session_id"] == "p1-sess-1"

    def test_written_owner_only(self, tmp_path):
        run_dir, _ = _record(tmp_path)
        mode = stat.S_IMODE(os.stat(da.sidecar_path(run_dir)).st_mode)
        assert mode == 0o600, oct(mode)

    def test_carries_no_chat_id_and_no_session_key(self, tmp_path):
        """The record must add NO new identifier surface to disk.

        The destination is stored as a fingerprint, so the file can prove a
        callback is for the same chat without containing the chat id.
        """
        run_dir, _ = _record(tmp_path)
        raw = da.sidecar_path(run_dir).read_text(encoding="utf-8")
        assert LEVI_CHAT_ID not in raw
        assert "agent:main:telegram" not in raw
        assert da.route_fingerprint("telegram", "dm", LEVI_CHAT_ID) in raw

    def test_caller_instance_is_not_a_model_argument(self, monkeypatch):
        """Identity comes from the process, not from whatever is passed in.

        ``build_authority`` accepts an explicit instance for the one caller that
        legitimately knows better (route_to_lane, which pins HERMES_HOME before
        spawning the wrapper) — but the DEFAULT is always the running process.
        """
        monkeypatch.setattr(da, "active_instance", lambda: "azul")
        rec = da.build_authority(originating_session_id="s")
        assert rec["caller_instance"] == "azul"
        assert rec["destination_instance"] == "azul"

    def test_absent_sidecar_reads_as_none(self, tmp_path):
        assert da.read_sidecar(tmp_path / "nope") is None

    def test_foreign_or_malformed_sidecar_is_not_authority(self, tmp_path):
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        da.sidecar_path(run_dir).write_text("{not json", encoding="utf-8")
        assert da.read_sidecar(run_dir) is None
        da.sidecar_path(run_dir).write_text(
            json.dumps({"schema": "something-else/1", "caller_instance": "default"}),
            encoding="utf-8",
        )
        assert da.read_sidecar(run_dir) is None, "unknown schema read as authority"


# ── the comparison: same chat id, different everything else ──────────────────

class TestInstanceIsolation:
    def test_valid_p1_callback_matches(self, tmp_path):
        run_dir, _ = _record(tmp_path)
        assert _verdict(run_dir, _callback()) is None

    @pytest.mark.parametrize("client", [i for i in INSTANCES if i != P1])
    def test_same_chat_id_different_instance_does_not_match(self, tmp_path, client):
        """The incident, reduced: PTG/Azul dispatch, P1's gateway drains it."""
        run_dir, _ = _record(tmp_path, instance=client, session=f"{client}-sess-1")
        verdict = _verdict(run_dir, _callback(instance=client, session=f"{client}-sess-1"))
        assert verdict == "destination_mismatch", verdict

    @pytest.mark.parametrize("client", [i for i in INSTANCES if i != P1])
    def test_client_callback_forging_p1_identity_does_not_match(self, tmp_path, client):
        """A callback that CLAIMS to be P1 over a client's dispatch record."""
        run_dir, _ = _record(tmp_path, instance=client, session=f"{client}-sess-1")
        verdict = _verdict(run_dir, _callback(instance=P1, session=f"{client}-sess-1"))
        assert verdict == "instance_mismatch", verdict

    @pytest.mark.parametrize("receiver", [i for i in INSTANCES if i != P1])
    def test_p1_dispatch_is_not_admitted_by_another_gateway(self, tmp_path, receiver):
        """Every one of the five gateways drains its own queue; only the
        destination may consume a P1 result."""
        run_dir, _ = _record(tmp_path)
        verdict = _verdict(run_dir, _callback(), receiver=receiver)
        assert verdict == "destination_mismatch", verdict

    def test_every_instance_pair_is_distinguishable(self, tmp_path):
        """No two of the five homes can consume each other's callbacks."""
        for dispatcher in INSTANCES:
            run_dir, _ = _record(
                tmp_path / dispatcher, instance=dispatcher, session=f"{dispatcher}-s",
            )
            event = _callback(instance=dispatcher, session=f"{dispatcher}-s")
            for receiver in INSTANCES:
                verdict = _verdict(run_dir, event, receiver=receiver)
                if receiver == dispatcher:
                    assert verdict is None, f"{dispatcher}->{receiver} refused: {verdict}"
                else:
                    assert verdict == "destination_mismatch", (
                        f"{dispatcher}->{receiver} admitted (verdict={verdict})"
                    )


class TestSessionAndWorkIdentity:
    def test_same_instance_wrong_session_does_not_match(self, tmp_path):
        run_dir, _ = _record(tmp_path, session="p1-sess-1")
        verdict = _verdict(run_dir, _callback(session="p1-sess-2"))
        assert verdict == "session_mismatch", verdict

    def test_wrong_run_id_does_not_match(self, tmp_path):
        run_dir, _ = _record(tmp_path, run_id="20260810-173216-69340")
        verdict = _verdict(run_dir, _callback(run_id="20260810-174145-32360"))
        assert verdict == "run_mismatch", verdict

    def test_wrong_wts_task_does_not_match(self, tmp_path):
        run_dir, _ = _record(tmp_path, wts_task="17cbc96c-a70f-46e7-af23-1458d04b5368")
        verdict = _verdict(run_dir, _callback(wts_task="2f1a2e80-9c00-41bb-95f5-0a38a20412cb"))
        assert verdict == "wts_mismatch", verdict

    def test_wrong_mission_does_not_match(self, tmp_path):
        run_dir, _ = _record(tmp_path, mission_id="mission-a")
        verdict = _verdict(run_dir, _callback(mission_id="mission-b"))
        assert verdict == "mission_mismatch", verdict

    def test_wrong_chain_does_not_match(self, tmp_path):
        run_dir, _ = _record(tmp_path, chain_id="chain-a")
        verdict = _verdict(run_dir, _callback(chain_id="chain-b"))
        assert verdict == "chain_mismatch", verdict

    def test_dropped_field_does_not_match(self, tmp_path):
        """A callback that simply OMITS a field the record carries must not be
        admitted — otherwise stripping fields is a bypass."""
        run_dir, _ = _record(tmp_path, mission_id="mission-a")
        verdict = _verdict(run_dir, _callback(mission_id=""))
        assert verdict == "mission_mismatch", verdict

    def test_wrong_chat_does_not_match(self, tmp_path):
        run_dir, _ = _record(tmp_path, chat_id=LEVI_CHAT_ID)
        verdict = _verdict(run_dir, _callback(chat_id="1111111111"))
        assert verdict == "route_mismatch", verdict


class TestFailClosed:
    def test_missing_instance_fails_closed(self, tmp_path):
        """A LEGACY callback — no instance at all. It must never default to P1."""
        run_dir, _ = _record(tmp_path)
        verdict = _verdict(run_dir, _callback(instance=None))
        assert verdict == "missing_instance", verdict

    def test_missing_session_fails_closed(self, tmp_path):
        run_dir, _ = _record(tmp_path)
        verdict = _verdict(run_dir, _callback(session=None))
        assert verdict == "missing_session", verdict

    def test_empty_authority_fails_closed(self, tmp_path):
        run_dir, _ = _record(tmp_path)
        assert da.authority_mismatch(da.authority_for_run(run_dir), {},
                                     receiving_instance=P1) == "authority_absent"
        assert da.authority_mismatch(da.authority_for_run(run_dir), None,
                                     receiving_instance=P1) == "authority_absent"

    def test_callback_addressed_elsewhere_is_refused_here(self, tmp_path):
        """The claim's OWN destination is checked, not just the record's.

        A callback that says "deliver me to azul" must not be delivered by P1
        just because P1 happens to hold a matching dispatch record — a shared
        queue means every gateway sees every event.
        """
        run_dir, _ = _record(tmp_path)
        verdict = _verdict(run_dir, _callback(destination="azul"), receiver=P1)
        assert verdict == "destination_mismatch", verdict

    def test_unidentified_receiver_fails_closed(self, tmp_path):
        """A gateway that cannot name itself is not anyone's destination."""
        run_dir, _ = _record(tmp_path)
        verdict = _verdict(run_dir, _callback(), receiver="")
        assert verdict == "unidentified_receiver", verdict

    def test_no_dispatch_record_fails_closed(self, tmp_path):
        """A well-formed claim with nothing to check it against is refused."""
        run_dir, _ = _record(tmp_path, write=False)
        verdict = _verdict(run_dir, _callback())
        assert verdict == "no_dispatch_record", verdict

    def test_record_without_identity_fails_closed(self, tmp_path):
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        da.sidecar_path(run_dir).write_text(
            json.dumps({"schema": "dispatch-authority/1", "run_id": "r1"}), encoding="utf-8",
        )
        verdict = _verdict(run_dir, _callback(run_id="r1"))
        assert verdict == "record_incomplete", verdict


# ── wake-target: readers tolerate v1 AND v2 ──────────────────────────────────

def _write_wake_target(run_dir, payload):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "wake-target").write_text(json.dumps(payload), encoding="utf-8")


class TestWakeTargetCompat:
    BASE = {
        "session_key": f"agent:main:telegram:dm:{LEVI_CHAT_ID}",
        "platform": "telegram", "chat_type": "dm", "chat_id": LEVI_CHAT_ID,
        "lane": "engineering", "run_id": "r1",
    }

    def test_v1_is_readable_but_carries_no_authority(self, tmp_path):
        """Every historical run on disk is v1. It must be READ (so routing still
        works) and must yield NO authority (so it fails closed, not open)."""
        run_dir = tmp_path / "runs" / "r1"
        _write_wake_target(run_dir, {"schema": "wake-target/1", **self.BASE})
        assert da.read_wake_target(run_dir) is not None
        assert da.authority_for_run(run_dir) is None

    def test_v2_supplies_authority(self, tmp_path):
        run_dir = tmp_path / "runs" / "r1"
        _write_wake_target(run_dir, {
            "schema": "wake-target/2", **self.BASE,
            "instance": P1, "destination_instance": P1, "session_id": "p1-sess-1",
        })
        rec = da.authority_for_run(run_dir)
        assert rec is not None
        assert rec["caller_instance"] == P1
        assert rec["originating_session_id"] == "p1-sess-1"
        assert rec["authority_source"] == "wake-target/2"
        assert _verdict(run_dir, _callback(run_id="r1", wts_task="")) is None

    def test_v2_from_a_client_does_not_reach_p1(self, tmp_path):
        run_dir = tmp_path / "runs" / "r1"
        _write_wake_target(run_dir, {
            "schema": "wake-target/2", **self.BASE,
            "instance": "azul", "destination_instance": "azul", "session_id": "azul-1",
        })
        verdict = _verdict(run_dir, _callback(instance="azul", session="azul-1",
                                              run_id="r1", wts_task=""))
        assert verdict == "destination_mismatch", verdict

    def test_partially_populated_v2_carries_no_authority(self, tmp_path):
        run_dir = tmp_path / "runs" / "r1"
        _write_wake_target(run_dir, {"schema": "wake-target/2", **self.BASE,
                                     "instance": P1})  # no session_id
        assert da.authority_for_run(run_dir) is None

    def test_dispatch_sidecar_wins_over_wake_target(self, tmp_path):
        run_dir, _ = _record(tmp_path, run_id="r1", session="p1-sess-1")
        _write_wake_target(run_dir, {
            "schema": "wake-target/2", **self.BASE,
            "instance": "azul", "session_id": "azul-1",
        })
        rec = da.authority_for_run(run_dir)
        assert rec["caller_instance"] == P1
        assert rec["originating_session_id"] == "p1-sess-1"


class TestEvidenceHygiene:
    def test_summary_carries_no_chat_id_prompt_or_closeout(self, tmp_path):
        run_dir, rec = _record(tmp_path)
        event = _callback()
        event["prompt"] = "SECRET CONTINUATION PROMPT with transcript"
        event["closeout"] = "the whole lane closeout"
        summary = da.summarize_for_evidence(event, rec)
        blob = json.dumps(summary)
        assert LEVI_CHAT_ID not in blob
        assert "SECRET CONTINUATION PROMPT" not in blob
        assert "closeout" not in summary
        # …but it stays diagnosable.
        assert summary["claimed_instance"] == P1
        assert summary["record_instance"] == P1
        assert summary["route_fingerprint"] == da.route_fingerprint(
            "telegram", "dm", LEVI_CHAT_ID
        )

    def test_fingerprint_is_stable_and_discriminating(self):
        a = da.route_fingerprint("telegram", "dm", LEVI_CHAT_ID)
        assert a == da.route_fingerprint("telegram", "dm", LEVI_CHAT_ID)
        assert a != da.route_fingerprint("telegram", "dm", "1111111111")
        assert a != da.route_fingerprint("slack", "dm", LEVI_CHAT_ID)
        assert LEVI_CHAT_ID not in a


class TestEventStamping:
    def test_emit_side_reads_authority_from_the_run(self, tmp_path):
        run_dir, _ = _record(tmp_path, run_id="r1")
        event = {"run_id": "r1"}
        da.stamp_event_authority(event, run_dir=run_dir)
        assert event["instance"] == P1
        assert event["originating_session_id"] == "p1-sess-1"
        assert event["authority_source"] == da.SCHEMA

    def test_stamping_invents_nothing_when_there_is_no_record(self, tmp_path):
        run_dir, _ = _record(tmp_path, write=False)
        event = {"run_id": "r1", "run_dir": str(run_dir)}
        da.stamp_event_authority(event, run_dir=run_dir)
        assert "instance" not in event
        assert "originating_session_id" not in event

    def test_explicit_authority_is_not_overwritten(self, tmp_path):
        run_dir, _ = _record(tmp_path)
        event = {"instance": "azul", "originating_session_id": "azul-1"}
        da.stamp_event_authority(event, run_dir=run_dir)
        assert event["instance"] == "azul", "sidecar silently relabelled the claim"
