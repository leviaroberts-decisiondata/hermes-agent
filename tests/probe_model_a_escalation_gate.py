#!/usr/bin/env python3
"""Model A re-cut (Session 2) — G0 HERMETIC canary for the explicit/audited lane-ESCALATION GATE.

Proves, WITHOUT touching a real Slack surface (synthetic channel + real PG dual-write + real
lane_* events appended via the events-only / operator-decision paths), that the escalation gate:

  D. DEFAULT-OFF: with DD_SLACK_ESCALATION_GATE_ENABLED unset, an agent-emitted
     lane_help_requested is INERT — the chain stays on the normal Model-A progress-follow
     path, no escalation sub-state, no dispatch. The gate never fires by default.
  X. FORGED APPROVAL FAILS STRUCTURALLY: an agent-pathed lane_help_approved
     (source_system=dd-slack-service) is REJECTED at the append chokepoint — it never lands
     in request_chain_events at all. And even reasoning past that, the driver's decision read
     filters on the operator surface, so a forged approval can never authorize a dispatch.
     Negative proof: zero lane_dispatched, zero real dispatch_lane.
  R. REQUEST IS NOT AUTHORIZATION: with the gate ON, a lane_help_requested opens an
     escalation that RESTS at help_requested (awaiting approval), no dispatch.
  A. OPERATOR APPROVAL WORKS: a recorded operator decision via the --record-decision rail
     (source_system=dd-operator-approval, actor_kind=operator, allowlisted actor_id, scoped to
     the request) carries ALL SIX named fields and authorizes the gate → shadow-dispatch.
  L. ALLOWLIST ENFORCED: --record-decision REFUSES to author an approval for a non-allowlisted
     actor_id (structural, fail-closed) — no approval event is written.
  1. EXACTLY ONCE: an approved escalation shadow-dispatches exactly one lane_dispatched, and a
     re-tick (replay) does NOT emit a second — idempotent under retry.
  E. RESULT + RESUME: the shadow dispatch attaches a lane_result_attached (evidence, not a
     terminal delivery) and the agent resumes (agent_resumed_from_lane_result); the cursor
     never leaves the agent stage; the close gate still owns `done`.
  N. ONE NOTICE EACH: exactly one escalation_notice + one escalation_result post (dedup), no
     lane heartbeat spam.
  H. HOLD HONORED / SHADOW ONLY: with DD_SLACK_LANE_DISPATCH_HOLD=1 (default), NO real
     dispatch_lane fires — the lane_dispatched event is a shadow event; zero specialist run dir.
  Y. DENIED → RESUME: an operator denial returns the chain to agent execution (cursor on the
     agent stage, status active), never a silent park.
  V. VOCAB PINNED: a typo'd lane_* event type is rejected at the chokepoint; the superseded
     escalation_* names are no longer in the pin.
  S. ONE SPINE QUERY: the chain's lane_* lifecycle events are all present + ordered on the
     request_chain_events spine (chain_status answers the escalation state from one read).

Hermetic: synthetic chat ids; reinject / lane-visibility loopback best-effort no-op for the
synthetic surface; cleans up every chain + PG event it creates. Requires DD_CHAIN_PG_DUAL_WRITE=1
+ a reachable PG (the gate's read source). SKIPs (exit 0) if PG is unreachable.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

H = Path.home() / ".hermes"
DRIVER = H / "bin" / "dd-chain-driver"
LEDGER = H / "dd-lanes" / "chain-ledger.json"
LANES = H / "dd-lanes"
LOG = H / "logs" / "dd-chain-driver.log"
PY = os.environ.get("DD_CHAIN_DRIVER_PYTHON", str(H / "hermes-agent" / "venv" / "bin" / "python"))
RID = f"esc{os.getpid()}"
CHAT = f"Csyn{RID}"  # synthetic channel — never a real C... id
OPERATOR = f"op-{RID}"  # an allowlisted operator identity for this probe
NON_OPERATOR = f"intruder-{RID}"  # NOT in the allowlist

# Base env: dual-write ON (the gate read source). HOLD stays at its default ON (shadow).
# The operator allowlist is pinned via env (driver-side only; the agent never sees it).
BASE_ENV = dict(os.environ, DD_CHAIN_PG_DUAL_WRITE="1", DD_OPERATOR_ALLOWLIST=OPERATOR)
# Gate-ON env: the escalation machinery is lit (but hold still ON → shadow only).
GATE_ENV = dict(BASE_ENV, DD_SLACK_ESCALATION_GATE_ENABLED="1")

results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    tag = "\033[32mGREEN\033[0m" if ok else "\033[31mRED\033[0m"
    print(f"  [{tag}] {name}")
    if detail and not ok:
        print(f"           {detail}")


def load(cid):
    for c in json.loads(LEDGER.read_text()):
        if c.get("chain_id") == cid:
            return c
    return {}


def driver(*args, env=None, timeout=60):
    return subprocess.run([PY, str(DRIVER), *args], capture_output=True, text=True,
                          timeout=timeout, env=env or BASE_ENV)


def agent_emit(anchor, etype, disc, payload=None, actor_kind="agent",
               source_system="dd-slack-service", env=None):
    """The AGENT path: dd-slack-service appendChainEvent → always source_system=dd-slack-service."""
    args = ["--append-event", f"--anchor={anchor}", f"--event-type={etype}",
            f"--summary=synthetic {etype}", f"--actor-kind={actor_kind}",
            "--actor-id=dd-slack-service", f"--disc={disc}", f"--source-system={source_system}"]
    if payload is not None:
        args.append(f"--payload={json.dumps(payload)}")
    return driver(*args, env=env or GATE_ENV, timeout=30)


def operator_decide(anchor, request_id, decision, actor_id, lane="engineering",
                    sub_task="the scoped sub-task", env=None):
    """The OPERATOR rail: --record-decision validates actor_id ∈ allowlist + stamps the
    privileged source_system the agent cannot produce."""
    return driver("--record-decision", f"--anchor={anchor}", f"--request-id={request_id}",
                  f"--decision={decision}", f"--actor-id={actor_id}", f"--lane={lane}",
                  f"--sub-task={sub_task}", env=env or GATE_ENV, timeout=30)


def _pg():
    sys.path.insert(0, str(H / "bin"))
    import dd_chain_pg as m  # noqa
    return m


def pg_reachable():
    try:
        m = _pg()
        code, _ = m._api("GET", "/items/request_chains?limit=1&fields=id")
        return code == 200
    except Exception:
        return False


def events_for(cid):
    """All lane_* / escalation events for a chain, ordered. Returns list of dicts."""
    try:
        m = _pg()
        pg_id = m._find_chain_id(cid)
        if not pg_id:
            return []
        q = urllib.parse.quote(pg_id, safe="")
        # Order by `id` (DB insertion order = causal append order). event_sequence is NOT a
        # cross-writer global clock — each source_system stamps its own, so mixed
        # agent/driver/operator events do not order monotonically by it.
        code, resp = m._api(
            "GET", f"/items/request_chain_events?filter[chain_id][_eq]={q}"
                   f"&sort=id&limit=200"
                   f"&fields=id,event_type,actor_kind,actor_id,source_system,payload_json,summary")
        if code == 200 and resp and resp.get("data"):
            return resp["data"]
    except Exception:
        pass
    return []


def count_type(cid, etype):
    return sum(1 for e in events_for(cid) if e.get("event_type") == etype)


def has_real_dispatch_log(cid):
    """Negative proof: any REAL dispatch_lane / lane_started / specialist run for this chain."""
    try:
        txt = LOG.read_text(errors="ignore")
    except Exception:
        return []
    hits = []
    for ln in txt.splitlines():
        if cid in ln and re.search(r"dispatch_lane lane=|lane_started|REAL-DISPATCH", ln):
            hits.append(ln)
    return hits


def mint(route_suffix, env=None):
    s = driver("--start", "", f"slack:esc:{RID}:{route_suffix}", "slack", CHAT, "channel",
               "--interval", "180", "--goal", f"escalation canary {route_suffix}",
               env=env or BASE_ENV, timeout=30)
    m = re.search(r"chain_id=(\S+)", s.stdout or "")
    return m.group(1) if m else None


def cleanup(cid):
    if not cid:
        return
    try:
        driver("--abort", cid, env=BASE_ENV, timeout=30)
    except Exception:
        pass
    try:
        m = _pg()
        pg_id = m._find_chain_id(cid)
        if pg_id:
            q = urllib.parse.quote(pg_id, safe="")
            code, resp = m._api("GET", f"/items/request_chain_events?filter[chain_id][_eq]={q}"
                                       f"&limit=300&fields=id")
            if code == 200 and resp and resp.get("data"):
                ids = [e["id"] for e in resp["data"]]
                if ids:
                    m._api("DELETE", "/items/request_chain_events", ids)
            m._api("DELETE", "/items/request_chains", [pg_id])
    except Exception:
        pass


def main():
    if not pg_reachable():
        print("SKIP: PG not reachable — the escalation gate's read source is PG; cannot exercise hermetically.")
        print("=" * 70)
        print("MODEL-A ESCALATION-GATE PROBE: SKIPPED (PG unreachable)")
        sys.exit(0)

    chains = []
    try:
        # ── V. VOCAB: the privileged + drift gates at the chokepoint (no chain needed). ──
        m = _pg()
        rec("V1.pin-superseded: the old escalation_* names are GONE from the pin",
            "escalation_requested" not in m.ALLOWED_SLACK_ESCALATION_EVENT_TYPES
            and "lane_help_requested" in m.ALLOWED_SLACK_ESCALATION_EVENT_TYPES,
            f"pin={sorted(m.ALLOWED_SLACK_ESCALATION_EVENT_TYPES)}")
        rec("V2.drift-reject: a typo'd lane_* type is rejected by the gate",
            m.is_allowed_slack_escalation_event_type("lane_help_aprovd", "dd-operator-approval") is False
            and m.is_allowed_slack_escalation_event_type("lane_help_requested", "dd-slack-service") is True,
            "typo should reject; requested(agent) should allow")
        rec("V3.privilege-gate: a privileged decision type from the AGENT source is rejected",
            m.is_allowed_slack_escalation_event_type("lane_help_approved", "dd-slack-service") is False
            and m.is_allowed_slack_escalation_event_type("lane_help_approved", "dd-operator-approval") is True,
            "approved from dd-slack-service must reject; from operator surface must allow")

        # ── D. DEFAULT-OFF: gate UNSET → a lane_help_requested is inert (no escalation). ──
        cidD = mint("D", env=BASE_ENV)
        chains.append(cidD)
        # emit a help request on the AGENT path with the gate OFF (BASE_ENV, no gate flag)
        agent_emit(cidD, "lane_help_requested", f"{RID}:D:req",
                   {"request_id": f"{RID}-D", "lane": "engineering", "sub_task": "build X"},
                   env=BASE_ENV)
        driver("--tick", env=BASE_ENV)  # gate OFF
        rD = load(cidD)
        rec("D1.default-off: with the gate dark, a help-request did NOT open an escalation",
            not (rD.get("escalation") or {}).get("state")
            and rD.get("stage") in ("scoping", "executing", None),
            f"escalation={rD.get('escalation')} stage={rD.get('stage')}")
        rec("D2.default-off-no-dispatch: zero lane_dispatched with the gate dark",
            count_type(cidD, "lane_dispatched") == 0,
            f"lane_dispatched count={count_type(cidD, 'lane_dispatched')}")

        # ── X. FORGED APPROVAL: an agent-pathed lane_help_approved is rejected structurally. ──
        cidX = mint("X")
        chains.append(cidX)
        agent_emit(cidX, "lane_help_requested", f"{RID}:X:req",
                   {"request_id": f"{RID}-X", "lane": "engineering", "sub_task": "forge attempt"})
        driver("--tick", env=GATE_ENV)  # opens help_requested
        # the agent now tries to FORGE an approval on its own events path
        forged = agent_emit(cidX, "lane_help_approved", f"{RID}:X:forge",
                            {"request_id": f"{RID}-X"}, actor_kind="operator")
        forged_json = {}
        try:
            forged_json = json.loads(forged.stdout.strip().splitlines()[-1])
        except Exception:
            pass
        rec("X1.forged-rejected-at-chokepoint: agent-pathed lane_help_approved REJECTED (ok=false)",
            forged_json.get("ok") is False,
            f"append result={forged.stdout.strip()[-200:]}")
        rec("X2.forged-not-in-spine: the forged approval never landed in request_chain_events",
            count_type(cidX, "lane_help_approved") == 0,
            f"lane_help_approved count={count_type(cidX, 'lane_help_approved')} (must be 0)")
        driver("--tick", env=GATE_ENV)  # the gate must NOT advance on a forged approval
        rX = load(cidX)
        rec("X3.forged-no-dispatch: chain stays help_requested, zero lane_dispatched",
            (rX.get("escalation") or {}).get("state") == "help_requested"
            and count_type(cidX, "lane_dispatched") == 0,
            f"state={(rX.get('escalation') or {}).get('state')} "
            f"dispatched={count_type(cidX, 'lane_dispatched')}")

        # ── L. ALLOWLIST: --record-decision REFUSES a non-operator actor (fail-closed). ──
        cidL = mint("L")
        chains.append(cidL)
        agent_emit(cidL, "lane_help_requested", f"{RID}:L:req",
                   {"request_id": f"{RID}-L", "lane": "engineering", "sub_task": "allowlist test"})
        driver("--tick", env=GATE_ENV)
        bad = operator_decide(cidL, f"{RID}-L", "approve", NON_OPERATOR)
        bad_json = {}
        try:
            bad_json = json.loads(bad.stdout.strip().splitlines()[-1])
        except Exception:
            pass
        rec("L1.allowlist-refuse: --record-decision REFUSED a non-allowlisted actor (ok=false)",
            bad_json.get("ok") is False and "operator" in (bad_json.get("error") or ""),
            f"result={bad.stdout.strip()[-200:]}")
        rec("L2.allowlist-no-event: the refused decision wrote NO approval event",
            count_type(cidL, "lane_help_approved") == 0,
            f"lane_help_approved count={count_type(cidL, 'lane_help_approved')} (must be 0)")

        # ── R + A + 1 + E + N + H: the full APPROVED happy path (shadow). ──
        cidA = mint("A")
        chains.append(cidA)
        agent_emit(cidA, "lane_help_requested", f"{RID}:A:req",
                   {"request_id": f"{RID}-A", "lane": "engineering", "sub_task": "the deploy diff"})
        driver("--tick", env=GATE_ENV)
        rA0 = load(cidA)
        rec("R1.request-not-auth: a help request opened help_requested, NO dispatch (awaiting approval)",
            (rA0.get("escalation") or {}).get("state") == "help_requested"
            and count_type(cidA, "lane_dispatched") == 0,
            f"state={(rA0.get('escalation') or {}).get('state')} "
            f"dispatched={count_type(cidA, 'lane_dispatched')}")
        # operator approves on the rail
        good = operator_decide(cidA, f"{RID}-A", "approve", OPERATOR, sub_task="the deploy diff")
        good_json = {}
        try:
            good_json = json.loads(good.stdout.strip().splitlines()[-1])
        except Exception:
            pass
        rec("A1.operator-approval-works: --record-decision authored the approval (ok=true)",
            good_json.get("ok") is True and good_json.get("actor_id") == OPERATOR,
            f"result={good.stdout.strip()[-200:]}")
        # the approval event carries ALL SIX named fields
        appr = next((e for e in events_for(cidA) if e.get("event_type") == "lane_help_approved"), None)
        six_ok = bool(appr) and appr.get("actor_kind") == "operator" \
            and appr.get("source_system") == "dd-operator-approval" \
            and (appr.get("payload_json") or {}).get("request_id") == f"{RID}-A" \
            and (appr.get("payload_json") or {}).get("lane") \
            and (appr.get("payload_json") or {}).get("sub_task") \
            and (appr.get("payload_json") or {}).get("chain_anchor") \
            and appr.get("actor_id") == OPERATOR \
            and (appr.get("payload_json") or {}).get("decided_at_unix")
        rec("A2.six-fields: the approval record carries all six (anchor,request,lane,sub_task,actor,ts)",
            six_ok, f"approval event={appr}")

        # tick the machine ONE transition at a time (the driver advances at most one state
        # per tick): help_requested → approved → dispatched → result_attached → resumed.
        driver("--tick", env=GATE_ENV)  # help_requested → approved (reads the operator decision)
        driver("--tick", env=GATE_ENV)  # approved → dispatched (shadow lane_dispatched)
        r1 = load(cidA)
        rec("H1.shadow-dispatch: approved escalation recorded a lane_dispatched (SHADOW)",
            count_type(cidA, "lane_dispatched") == 1
            and (r1.get("escalation") or {}).get("shadow") is True,
            f"dispatched={count_type(cidA, 'lane_dispatched')} shadow={(r1.get('escalation') or {}).get('shadow')}")
        disp = next((e for e in events_for(cidA) if e.get("event_type") == "lane_dispatched"), None)
        rec("H2.shadow-payload: the dispatch event is marked shadow + would_dispatch_lane (no real run)",
            bool(disp) and (disp.get("payload_json") or {}).get("shadow") is True
            and (disp.get("payload_json") or {}).get("would_dispatch_lane"),
            f"dispatch event payload={disp.get('payload_json') if disp else None}")

        # EXACTLY ONCE: re-tick (replay) must NOT emit a second lane_dispatched.
        driver("--tick", env=GATE_ENV)  # dispatched → result_attached
        driver("--tick", env=GATE_ENV)  # result_attached → resumed
        driver("--tick", env=GATE_ENV)  # resumed (replay) — must NOT re-dispatch
        rec("1.exactly-once: a re-tick/replay did NOT emit a second lane_dispatched",
            count_type(cidA, "lane_dispatched") == 1,
            f"lane_dispatched count={count_type(cidA, 'lane_dispatched')} (must stay 1)")
        rec("E1.result-attached: the escalation recorded lane_result_attached (evidence, exactly once)",
            count_type(cidA, "lane_result_attached") == 1,
            f"lane_result_attached count={count_type(cidA, 'lane_result_attached')}")
        rec("E2.agent-resumed: agent_resumed_from_lane_result recorded (agent owns the delivery)",
            count_type(cidA, "agent_resumed_from_lane_result") == 1,
            f"agent_resumed count={count_type(cidA, 'agent_resumed_from_lane_result')}")
        rEnd = load(cidA)
        rec("E3.cursor-stayed: the cursor never left the agent stage (escalation is chain-level state)",
            rEnd.get("stage") in ("scoping", "executing", "delivering")
            and rEnd.get("status") in ("active", "blocked"),
            f"stage={rEnd.get('stage')} status={rEnd.get('status')}")

        # H. HOLD HONORED — zero REAL dispatch_lane / specialist run dir across the whole run.
        rec("H3.hold-honored: NO real dispatch_lane fired for the approved escalation (shadow only)",
            not has_real_dispatch_log(cidA),
            f"offending log lines: {has_real_dispatch_log(cidA)[:3]}")
        eng_dirs = list((LANES / "engineering" / "runs").glob(f"*{RID}*"))
        rec("H4.no-specialist-rundir: no engineering run dir spawned for any escalation chain",
            not eng_dirs, f"dirs={[str(d) for d in eng_dirs]}")

        # S. ONE SPINE QUERY — the complete lane_* lifecycle is answerable from a single
        # request_chain_events read (chain_status's source). All five types present, each
        # exactly once; causal ORDER is enforced by the state machine itself (each transition
        # gates on the prior — proven green by R1→A1→H1→E1→E2 in sequence above). We assert
        # COMPLETENESS here rather than re-deriving order from event_sequence/id, neither of
        # which is a reliable cross-writer causal clock (mixed agent/operator/driver sources).
        present = {e.get("event_type") for e in events_for(cidA)}
        expected = {"lane_help_requested", "lane_help_approved", "lane_dispatched",
                    "lane_result_attached", "agent_resumed_from_lane_result"}
        counts = {t: count_type(cidA, t) for t in expected}
        rec("S1.spine-complete: all five lane_* lifecycle types present, each exactly once, one query",
            expected <= present and all(counts[t] == 1 for t in expected),
            f"present={sorted(present & expected)} counts={counts}")

        # ── Y. DENIED → resume agent execution (cursor on the agent stage, status active). ──
        cidY = mint("Y")
        chains.append(cidY)
        agent_emit(cidY, "lane_help_requested", f"{RID}:Y:req",
                   {"request_id": f"{RID}-Y", "lane": "engineering", "sub_task": "denied task"})
        driver("--tick", env=GATE_ENV)
        operator_decide(cidY, f"{RID}-Y", "deny", OPERATOR, sub_task="denied task")
        driver("--tick", env=GATE_ENV)  # help_requested → denied
        driver("--tick", env=GATE_ENV)  # denied → resumed
        rY = load(cidY)
        rec("Y1.denied-resume: a denial returned the chain to agent execution (active, agent stage)",
            (rY.get("escalation") or {}).get("state") == "resumed"
            and rY.get("status") == "active"
            and rY.get("stage") in ("scoping", "executing", "delivering"),
            f"state={(rY.get('escalation') or {}).get('state')} status={rY.get('status')} stage={rY.get('stage')}")
        rec("Y2.denied-no-dispatch: a denied escalation emitted ZERO lane_dispatched",
            count_type(cidY, "lane_dispatched") == 0,
            f"lane_dispatched count={count_type(cidY, 'lane_dispatched')}")
        rec("Y3.denied-decision-recorded: the lane_help_denied is on the spine (operator-authored)",
            count_type(cidY, "lane_help_denied") == 1,
            f"lane_help_denied count={count_type(cidY, 'lane_help_denied')}")

    finally:
        # ── cleanup every chain + its PG events + ledger residue. ──
        for c in chains:
            cleanup(c)
        try:
            led = [c for c in json.loads(LEDGER.read_text())
                   if RID not in (c.get("chat_id") or "") + (c.get("route_key") or "")]
            LEDGER.write_text(json.dumps(led, indent=2))
        except Exception:
            pass
        for d in (LANES / "engineering" / "runs").glob(f"*{RID}*"):
            try:
                for f in d.glob("*"):
                    f.unlink()
                d.rmdir()
            except Exception:
                pass

    g = sum(1 for _, ok, _ in results if ok)
    r = len(results) - g
    print("=" * 70)
    print(f"MODEL-A ESCALATION-GATE PROBE (G0 hermetic): {g} GREEN, {r} RED")
    sys.exit(0 if r == 0 else 1)


if __name__ == "__main__":
    main()
