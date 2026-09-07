#!/usr/bin/env python3
"""WS-5 — seven-state ledger↔PG parity proof (terminal-mirror gap fix in place).

The reviewer requires parity re-run across SEVEN states before the authority flip can
even be considered:
    active, blocked, escalated, done, failed delivery, rejected deploy, restart recovery

This harness creates a synthetic chain in EACH state, mirrors it via the SAME entrypoint
the driver calls (dd_chain_pg.pg_sync — the single materializer), then compares the PG
row field-for-field against the projection the dual-write uses (chain_row_from_rec), the
exact comparison dd-chain-parity-check makes. For 'restart recovery' it round-trips the
row back through recover_ledger() (PG → ledger-shaped record) and asserts the lifecycle
fields survive. The terminal/escalated states exercise the verify-after-write + terminal
catch-up fix directly.

Synthetic anchors only (prefix ws5-7state:) — DELETED on exit via the admin token. NEVER
touches the live ledger or any organic chain (cd605a8b is never referenced).

Run:  set -a; . ~/.openclaw/.env; set +a
      DD_CHAIN_PG_DUAL_WRITE=1 ~/.hermes/hermes-agent/venv/bin/python \
          ~/.hermes/hermes-agent/tests/test_seven_state_parity.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
sys.path.insert(0, str(HERMES / "bin"))
import dd_chain_pg as pg  # noqa: E402

RUNID = uuid.uuid4().hex[:8]
PREFIX = f"ws5-7state-{RUNID}"
FAILS = []
CREATED = []  # anchors created, for cleanup

# Same fields dd-chain-parity-check compares (field-for-field ledger↔PG).
COMPARE_FIELDS = [
    "title", "ask_summary", "source_surface", "source_channel_id",
    "source_thread_id", "wts_task_id", "status", "current_stage",
    "current_owner_kind", "current_owner_id", "next_stage", "blocker_summary",
    "eta_at", "route_key",
]

_ADMIN_TOKEN = ""


def _admin_token():
    global _ADMIN_TOKEN
    if _ADMIN_TOKEN:
        return _ADMIN_TOKEN
    envf = Path.home() / ".openclaw" / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DIRECTUS_TOKEN="):
                _ADMIN_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return _ADMIN_TOKEN


def _admin_api(method, path):
    tok = _admin_token()
    if not tok:
        return 0, None
    req = urllib.request.Request(pg.PG_BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


def _norm(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    return v


def _base(anchor, status, stage, **over):
    nowt = int(time.time())
    rec = {
        "chain_id": anchor, "goal": f"WS-5 seven-state proof: {status}",
        "platform": "telegram", "chat_id": f"7s-{RUNID}", "thread_id": None,
        "wts_task": None, "status": status, "stage": stage,
        "next_stage": over.pop("next_stage", None), "blocker": over.pop("blocker", None),
        "eta": over.pop("eta", None), "owner": over.pop("owner", "DD P1 (me)"),
        "stages": over.pop("stages", ["engineering", "qa", "deploy", "report"]),
        "created_at": nowt, "updated_at": nowt, "last_update_at": nowt,
        "history": over.pop("history", []), "pg_write_error": "SENTINEL",
    }
    rec.update(over)
    return rec


def _pg_row(anchor):
    pid = pg._find_chain_id(anchor)
    if not pid:
        return None
    code, resp = _admin_api("GET", f"/items/request_chains/{urllib.parse.quote(pid, safe='')}")
    return (resp or {}).get("data") if code == 200 else None


def _parity(rec):
    """Field-for-field parity of the PG row vs the projection (same as the gate)."""
    anchor = rec["chain_id"]
    row = _pg_row(anchor)
    if not row:
        return False, ["NO request_chains row"]
    projected = pg.chain_row_from_rec(rec)
    mism = []
    for fld in COMPARE_FIELDS:
        want, got = _norm(projected.get(fld)), _norm(row.get(fld))
        if want != got:
            mism.append(f"{fld}: ledger={want!r} pg={got!r}")
    return (not mism), mism


def _state(label, rec):
    CREATED.append(rec["chain_id"])
    ok_sync = pg.pg_sync(rec)
    parity, mism = _parity(rec)
    flag_ok = rec.get("pg_write_error") is None
    row = _pg_row(rec["chain_id"])
    mark = "✅" if (parity and flag_ok) else "❌"
    print(f"  {mark} {label:18s} status={rec['status']:10s} stage={rec['stage']:12s} "
          f"PG={row and row.get('status')}/{row and row.get('current_stage')}  "
          f"parity={parity} flag_clear={flag_ok}")
    if not parity:
        for m in mism:
            print(f"        - {m}")
    if not (parity and flag_ok):
        FAILS.append(label)
    return rec


def _cleanup():
    for anchor in CREATED:
        pid = pg._find_chain_id(anchor)
        if not pid:
            continue
        q = urllib.parse.quote(pid, safe="")
        for coll in ("request_chain_events", "request_chain_runs"):
            code, resp = _admin_api("GET", f"/items/{coll}?filter[chain_id][_eq]={q}&limit=-1&fields=id")
            for r in (resp or {}).get("data") or []:
                _admin_api("DELETE", f"/items/{coll}/{r['id']}")
        _admin_api("DELETE", f"/items/request_chains/{pid}")


def main():
    if not pg.dual_write_enabled():
        print("FATAL: DD_CHAIN_PG_DUAL_WRITE not '1'.")
        return 2
    if not _admin_token():
        print("FATAL: admin DIRECTUS_TOKEN not available for cleanup.")
        return 2
    print(f"== WS-5 seven-state ledger↔PG parity (prefix={PREFIX}) ==")
    try:
        # 1. ACTIVE — engineering in flight
        _state("active", _base(f"{PREFIX}:active", "active", "engineering",
                               next_stage="qa", owner="engineering lane (run r1)",
                               history=[{"kind": "run", "stage": "engineering",
                                         "run_dir": "/x/runs/r1", "gate": None,
                                         "dispatched_at": int(time.time())}]))
        # 2. BLOCKED — qa FAIL held
        _state("blocked", _base(f"{PREFIX}:blocked", "blocked", "qa",
                                blocker="could not run the qa stage", next_stage="deploy",
                                owner="qa lane — resolving the blocker"))
        # 3. ESCALATED — needs-you (the §2.2b state; exercises verify-after-write)
        _state("escalated", _base(f"{PREFIX}:escalated", "escalated", "qa",
                                  blocker="could not dispatch the qa stage",
                                  owner="DD P1 (me) — escalated to you; awaiting your unblock/redirect"))
        # 4. DONE — terminal closeout delivered
        _state("done", _base(f"{PREFIX}:done", "done", "report", next_stage=None,
                             owner="DD P1 (me) — work complete",
                             delivery_audit_status="delivered",
                             delivery_audit_detail="terminal closeout delivered (audited)",
                             delivery_audited_at=int(time.time()),
                             verification={"detail": "live verification: change present"}))
        # 5. FAILED DELIVERY — work done but terminal closeout could NOT be delivered
        #    (R3 close-gate held: status=blocked, delivery_audit_status=failed)
        _state("failed-delivery", _base(f"{PREFIX}:faildel", "blocked", "report",
                                        blocker="terminal closeout delivery failed (re-delivering)",
                                        owner="DD P1 (me) — re-delivering the closeout",
                                        delivery_audit_status="failed",
                                        delivery_audit_detail="gateway.mirror returned no session"))
        # 6. REJECTED DEPLOY — deploy decided NO
        _state("rejected-deploy", _base(f"{PREFIX}:rejdep", "blocked", "deploy",
                                        blocker="deploy rejected: diff touches prod secrets",
                                        owner="DD P1 (me) — escalating the deploy decision to you",
                                        next_stage="report",
                                        deploy_row_id=f"dq-{RUNID}",
                                        deploy_row={"status": "rejected",
                                                    "decided_by": "operator",
                                                    "service_name": "svc-x"}))

        # 7. RESTART RECOVERY — reconstruct a record from PG alone (round-trip), assert
        #    lifecycle fields survive. Uses the escalated row (a terminal state — proves
        #    recovery does NOT depend on the chain being active).
        print("  -- restart recovery (PG → ledger-shaped record round-trip) --")
        rec_esc = _base(f"{PREFIX}:escalated", "escalated", "qa",
                        blocker="could not dispatch the qa stage")
        row = _pg_row(rec_esc["chain_id"])
        recovered = pg._rec_from_pg_row(row, [], []) if row else None
        rok = bool(recovered) and recovered.get("status") == "escalated" \
            and recovered.get("chain_id") == rec_esc["chain_id"] \
            and recovered.get("stage") == "qa"
        mark = "✅" if rok else "❌"
        print(f"  {mark} restart-recovery   recovered status={recovered and recovered.get('status')} "
              f"stage={recovered and recovered.get('stage')} chain_id-match="
              f"{bool(recovered) and recovered.get('chain_id') == rec_esc['chain_id']}")
        if not rok:
            FAILS.append("restart-recovery")

    finally:
        _cleanup()
        print(f"  (cleaned up {len(CREATED)} synthetic PG chain(s))")

    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} STATE(S) FAILED PARITY: {', '.join(FAILS)}")
        return 1
    print("RESULT: ALL SEVEN STATES AT PARITY (ledger↔PG agree, flags clear, recovery intact).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
