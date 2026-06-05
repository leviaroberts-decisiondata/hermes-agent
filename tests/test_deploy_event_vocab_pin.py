#!/usr/bin/env python3
"""Session C item 4 — deploy event vocabulary pin.

Proves the `deploy_<status>` event type is constrained to an EXPLICIT allowed set
in the event-append path (dd_chain_pg.append_event), so a drifting/unexpected queue
status cannot mint an unvocabularied deploy_* event. Pure-logic + append-path tests;
no live Directus required (the append path is monkeypatched to capture the call).

Run: python3 tests/test_deploy_event_vocab_pin.py   (exit 0 = all pass)
"""
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
sys.path.insert(0, str(HERMES / "bin"))
import dd_chain_pg as pg  # noqa: E402

FAILS = []


def check(name, cond):
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        FAILS.append(name)


def test_allowed_set_matches_driver_queue_states():
    """The pinned vocabulary == the driver's legit queue-decision states + 2 bookends."""
    expect = {"deploy_submitted", "deploy_verified"}
    for s in ("approved", "deploying", "deployed", "restarted", "observed", "proven",
              "rejected", "denied", "cancelled", "failed"):
        expect.add(f"deploy_{s}")
    check("ALLOWED_DEPLOY_EVENT_TYPES == driver queue states + bookends",
          set(pg.ALLOWED_DEPLOY_EVENT_TYPES) == expect)


def test_gate_accepts_vocab_and_passes_non_deploy():
    for et in ("deploy_approved", "deploy_deployed", "deploy_rejected", "deploy_proven",
               "deploy_submitted", "deploy_verified", "deploy_failed", "deploy_cancelled"):
        check(f"gate accepts in-vocab {et}", pg.is_allowed_deploy_event_type(et))
    for et in ("chain_created", "lane_started", "intake_received", "delivery_audited",
               "branch_published", "wts_bound", "slack_anchor_bound", "chain_done"):
        check(f"gate passes non-deploy {et}", pg.is_allowed_deploy_event_type(et))


def test_gate_rejects_drift():
    for et in ("deploy_yolo", "deploy_pending", "deploy_unknown", "deploy_", "deploy_hacked",
               "deploy_in_progress", "deploy_queued"):
        check(f"gate REJECTS drift {et}", not pg.is_allowed_deploy_event_type(et))


def test_append_event_rejects_drift_without_network():
    """append_event must short-circuit a drifting deploy_* BEFORE any HTTP call.
    We sabotage _api to raise if reached — a rejected drift event must never call it."""
    orig_api = pg._api
    orig_token = pg._token

    def boom(*a, **k):
        raise AssertionError("_api MUST NOT be called for a rejected out-of-vocab event")

    pg._token = lambda: "fake-token-not-used"   # so the token check passes
    pg._api = boom
    try:
        got = pg.append_event("pg-chain-id", "deploy_yolo",
                              idempotency_key="k:deploy_yolo:x",
                              summary="drift", source_record_id="row1")
        check("append_event('deploy_yolo') returns False (rejected)", got is False)
        check("append_event('deploy_yolo') made NO HTTP call", True)  # boom would have raised
    except AssertionError as e:
        check(f"append_event drift made an HTTP call ({e})", False)
    finally:
        pg._api = orig_api
        pg._token = orig_token


def test_append_event_allows_vocab_reaches_api():
    """An in-vocab deploy event MUST reach the append HTTP path (not short-circuited)."""
    orig_api = pg._api
    orig_token = pg._token
    reached = {"called": False, "event_type": None}

    def fake_api(method, path, body=None, ok_404=False):
        reached["called"] = True
        reached["event_type"] = (body or {}).get("event_type")
        return 200, {"data": {"id": "ev1"}}

    pg._token = lambda: "fake-token-not-used"
    pg._api = fake_api
    try:
        got = pg.append_event("pg-chain-id", "deploy_approved",
                              idempotency_key="k:deploy_approved:x",
                              summary="ok", source_record_id="row1")
        check("append_event('deploy_approved') returns True", got is True)
        check("append_event('deploy_approved') reached the HTTP path", reached["called"])
        check("append_event posted event_type=deploy_approved",
              reached["event_type"] == "deploy_approved")
    finally:
        pg._api = orig_api
        pg._token = orig_token


if __name__ == "__main__":
    print("Session C item 4 — deploy event vocabulary pin")
    test_allowed_set_matches_driver_queue_states()
    test_gate_accepts_vocab_and_passes_non_deploy()
    test_gate_rejects_drift()
    test_append_event_rejects_drift_without_network()
    test_append_event_allows_vocab_reaches_api()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        sys.exit(1)
    print("ALL PASS ✅")
    sys.exit(0)
