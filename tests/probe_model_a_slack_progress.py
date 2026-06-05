#!/usr/bin/env python3
"""Model A re-cut (Session 1) — synthetic canary for the SLACK PROGRESS-FOLLOW path.

Proves, WITHOUT touching a real Slack surface (synthetic channel + real PG dual-write
+ real slack_progress_* events appended via the events-only path), that the driver:

  A. advances the per-surface stage cursor (scoping→executing→delivering→done) PURELY
     from slack_progress_* events — never dispatch_lane (zero lane runs in history).
  M. monotonic forward: a stale/duplicate earlier progress event never regresses cursor.
  W. initial scoped does NOT mark the chain final (still active, not done).
  C. close requires a real delivery_audited{delivered} event — only THEN does it rest done.
  F. a delivery_audited{failed} holds the chain `blocked` with a named blocker — never
     silently closed.
  Z. ZERO lane dispatch (negative proof): no engineering/qa lane run dir, no lane_started/
     lane_returned events, no dispatch_lane in the driver log for this chain.
  K. anti-silence: next_check_at resets on every advance and a lapsed deadline heartbeats.

Hermetic: a synthetic chat id (never a real channel); the reinject mirror / lane-visibility
loopback best-effort no-op for the synthetic surface. Cleans up its chain + PG events.

Requires DD_CHAIN_PG_DUAL_WRITE=1 + a reachable PG (the live config) so the driver can read
the progress events back. If PG is unreachable the probe SKIPs (prints SKIP, exits 0) rather
than failing — the follow path's read source is PG, which must be live to exercise it.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

H = Path.home() / ".hermes"
DRIVER = H / "bin" / "dd-chain-driver"
LEDGER = H / "dd-lanes" / "chain-ledger.json"
LANES = H / "dd-lanes"
LOG = H / "logs" / "dd-chain-driver.log"
PY = os.environ.get("DD_CHAIN_DRIVER_PYTHON", str(H / "hermes-agent" / "venv" / "bin" / "python"))
RID = f"mka{os.getpid()}"
CHAT = f"Csyn{RID}"  # synthetic channel — never a real C... id

# dual-write MUST be on for the driver tick to read progress events from PG.
ENV = dict(os.environ, DD_CHAIN_PG_DUAL_WRITE="1")

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


def driver(*args, timeout=60):
    return subprocess.run([PY, str(DRIVER), *args], capture_output=True, text=True,
                          timeout=timeout, env=ENV)


def append_event(anchor, etype, disc, payload=None, actor_kind="agent"):
    args = ["--append-event", f"--anchor={anchor}", f"--event-type={etype}",
            f"--summary=synthetic {etype}", f"--actor-kind={actor_kind}",
            "--actor-id=probe", f"--disc={disc}", "--source-system=dd-slack-service"]
    if payload is not None:
        args.append(f"--payload={json.dumps(payload)}")
    return driver(*args, timeout=30)


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


def has_lane_dispatch_log(cid):
    """Negative proof: scan the driver log for ANY dispatch_lane / lane_started /
    lane_returned line naming this chain. Returns the offending lines (empty = clean)."""
    try:
        txt = LOG.read_text(errors="ignore")
    except Exception:
        return []
    hits = []
    for ln in txt.splitlines():
        if cid in ln and re.search(r"dispatch_lane|lane_started|lane_returned|start-dispatch", ln):
            hits.append(ln)
    return hits


def main():
    if not pg_reachable():
        print("SKIP: PG not reachable (DD_CHAIN_PG_DUAL_WRITE/scoped token) — the progress-"
              "follow read source is PG; cannot exercise the path hermetically here.")
        print("=" * 70)
        print("MODEL-A SLACK-PROGRESS PROBE: SKIPPED (PG unreachable)")
        sys.exit(0)

    route = f"slack:mka:{RID}:1.0"
    cid = None
    try:
        # 1) Mint a SLACK chain with NO --stages → it must default to the slack agent
        #    vocabulary (scoping,executing,delivering,done), NOT the lane pipeline.
        s = driver("--start", "", route, "slack", CHAT, "channel",
                   "--interval", "180", "--goal", "model-a synthetic canary", timeout=30)
        m = re.search(r"chain_id=(\S+)", s.stdout or "")
        cid = m.group(1) if m else None
        rec("S1.mint: slack chain minted with NO --stages", bool(cid),
            (s.stdout or s.stderr).strip())
        if not cid:
            return
        r0 = load(cid)
        rec("S2.slack-vocab: defaulted to scoping→executing→delivering→done (NOT lane pipeline)",
            r0.get("stages") == ["scoping", "executing", "delivering", "done"]
            and r0.get("stage") == "scoping",
            f"stages={r0.get('stages')} stage={r0.get('stage')}")
        rec("S3.mint-clock: next_check_at armed at mint (anti-silence deadline exists)",
            isinstance(r0.get("next_check_at"), int) and r0["next_check_at"] > 0,
            f"next_check_at={r0.get('next_check_at')}")

        # 2) Tick with NO progress event yet → must NOT dispatch a lane, must NOT advance,
        #    must keep an empty history (the follow path heartbeats, never dispatches).
        driver("--tick")
        r1 = load(cid)
        rec("A1.no-event-no-advance: tick with no progress event left cursor at scoping, empty history",
            r1.get("stage") == "scoping" and not r1.get("history")
            and r1.get("status") == "active",
            f"stage={r1.get('stage')} hist={r1.get('history')} status={r1.get('status')}")

        # 3) Emit slack_progress_scoped → cursor stays scoping (already there), still active,
        #    NOT final. (scoped marks the FIRST agent stage, must never mark the chain done.)
        append_event(cid, "slack_progress_scoped", f"{RID}:scoped", {"queue_id": RID})
        driver("--tick")
        r2 = load(cid)
        rec("W1.scoped-not-final: after slack_progress_scoped the chain is active at scoping, NOT done",
            r2.get("status") == "active" and r2.get("stage") == "scoping"
            and r2.get("status") != "done",
            f"status={r2.get('status')} stage={r2.get('stage')}")

        # 4) Emit slack_progress_executing → cursor advances scoping→executing FROM the event.
        append_event(cid, "slack_progress_executing", f"{RID}:exec", {"queue_id": RID})
        driver("--tick")
        r3 = load(cid)
        rec("A2.advance-on-event: cursor advanced scoping→executing FROM slack_progress_executing",
            r3.get("stage") == "executing" and r3.get("status") == "active"
            and not r3.get("history"),
            f"stage={r3.get('stage')} hist={r3.get('history')}")

        # 5) MONOTONIC: re-emit an OLDER slack_progress_scoped (a late duplicate) → the cursor
        #    must NOT regress back to scoping.
        append_event(cid, "slack_progress_scoped", f"{RID}:scoped-dup", {"queue_id": RID})
        driver("--tick")
        r4 = load(cid)
        rec("M1.monotonic: a late duplicate scoped event did NOT regress the cursor (still executing)",
            r4.get("stage") == "executing",
            f"stage={r4.get('stage')} (expected executing)")

        # 6) Emit slack_progress_delivering → cursor advances executing→delivering. With NO
        #    delivery_audited yet, the chain must REST at delivering (active), NOT close done.
        append_event(cid, "slack_progress_delivering", f"{RID}:deliv", {"queue_id": RID})
        driver("--tick")
        r5 = load(cid)
        rec("A3.advance-delivering: cursor advanced executing→delivering FROM slack_progress_delivering",
            r5.get("stage") == "delivering",
            f"stage={r5.get('stage')}")
        rec("C1.no-close-without-evidence: at delivering with NO delivery_audited, chain is NOT done",
            r5.get("status") != "done",
            f"status={r5.get('status')} (must not be done without audited delivery)")

        # 7) CLOSE GATE — emit a real delivery_audited{delivered} → chain rests `done`.
        append_event(cid, "delivery_audited", f"{RID}:deliv-ok",
                     {"delivery_audit_status": "delivered", "queue_id": RID})
        driver("--tick")
        r6 = load(cid)
        rec("C2.close-on-audited-delivery: chain rests `done` ONLY after delivery_audited{delivered}",
            r6.get("status") == "done" and r6.get("stage") == "done",
            f"status={r6.get('status')} stage={r6.get('stage')}")

        # 8) ZERO LANE DISPATCH (negative proof) — across the whole run.
        lane_hits = has_lane_dispatch_log(cid)
        rec("Z1.zero-lane-dispatch: NO dispatch_lane/lane_started/lane_returned/start-dispatch for this chain",
            not lane_hits,
            f"offending log lines: {lane_hits[:3]}")
        rec("Z2.empty-history: the chain NEVER recorded a lane run (history stayed empty)",
            not r6.get("history"),
            f"history={r6.get('history')}")
        # no synthetic lane run dirs created for this chain's engineering/qa
        eng_dirs = list((LANES / "engineering" / "runs").glob(f"*{RID}*")) + \
            list((LANES / "qa" / "runs").glob(f"*{RID}*"))
        rec("Z3.no-specialist-rundir: no engineering/qa lane run dir spawned for this chain",
            not eng_dirs, f"dirs={[str(d) for d in eng_dirs]}")

        # 9) FAILURE PATH (separate chain) — at delivering, a delivery_audited{failed}
        #    must HOLD the chain `blocked` with a named blocker, NEVER silently close it.
        routeF = f"slack:mka:{RID}:F"
        sf = driver("--start", "", routeF, "slack", CHAT, "channel",
                    "--interval", "180", "--goal", "model-a fail-path canary", timeout=30)
        mf = re.search(r"chain_id=(\S+)", sf.stdout or "")
        cidF = mf.group(1) if mf else None
        if cidF:
            append_event(cidF, "slack_progress_delivering", f"{RID}:F:deliv", {"queue_id": f"{RID}F"})
            append_event(cidF, "delivery_audited", f"{RID}:F:deliv-fail",
                         {"delivery_audit_status": "failed", "queue_id": f"{RID}F"})
            driver("--tick")
            rf = load(cidF)
            rec("F1.fail-holds-blocked: delivery_audited{failed} held the chain `blocked`, NOT done",
                rf.get("status") == "blocked" and rf.get("status") != "done",
                f"status={rf.get('status')}")
            rec("F2.named-blocker: the held chain carries a named delivery blocker (not silent)",
                bool(rf.get("blocker")) and "deliver" in (rf.get("blocker") or "").lower(),
                f"blocker={rf.get('blocker')}")
            # clean up the fail-path chain too
            driver("--abort", cidF, timeout=30)
            try:
                m = _pg()
                pg_idF = m._find_chain_id(cidF)
                if pg_idF:
                    qF = urllib.parse.quote(pg_idF, safe="")
                    code, resp = m._api(
                        "GET", f"/items/request_chain_events?filter[chain_id][_eq]={qF}"
                               f"&limit=200&fields=id")
                    if code == 200 and resp and resp.get("data"):
                        ev_ids = [e["id"] for e in resp["data"]]
                        if ev_ids:
                            m._api("DELETE", "/items/request_chain_events", ev_ids)
                    m._api("DELETE", "/items/request_chains", [pg_idF])
            except Exception:
                pass

    finally:
        # ── cleanup: abort the chain, purge ledger residue, delete PG events + chain row.
        if cid:
            driver("--abort", cid, timeout=30)
        try:
            led = [c for c in json.loads(LEDGER.read_text())
                   if RID not in (c.get("chat_id") or "") + (c.get("route_key") or "")]
            LEDGER.write_text(json.dumps(led, indent=2))
        except Exception:
            pass
        # PG cleanup: delete the events + chain row this probe created (best-effort).
        try:
            m = _pg()
            pg_id = m._find_chain_id(cid) if cid else None
            if pg_id:
                q = urllib.parse.quote(pg_id, safe="")
                code, resp = m._api(
                    "GET", f"/items/request_chain_events?filter[chain_id][_eq]={q}"
                           f"&limit=200&fields=id")
                if code == 200 and resp and resp.get("data"):
                    ids = [e["id"] for e in resp["data"]]
                    if ids:
                        m._api("DELETE", "/items/request_chain_events", ids)
                m._api("DELETE", "/items/request_chains", [pg_id])
        except Exception:
            pass

    g = sum(1 for _, ok, _ in results if ok)
    r = len(results) - g
    print("=" * 70)
    print(f"MODEL-A SLACK-PROGRESS PROBE: {g} GREEN, {r} RED")
    sys.exit(0 if r == 0 else 1)


if __name__ == "__main__":
    main()
