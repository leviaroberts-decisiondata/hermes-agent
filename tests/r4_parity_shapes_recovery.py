#!/usr/bin/env python3
"""R4 — parity gate shapes (b)(c)(d) + (e) PG recovery, REAL driver transitions.

Drives real chains through the actual driver state machine (real ledger, real stub
dispatch — the same seam Canon 6 uses) with dual-write ON, then runs the parity tool
on each, proving JSON↔PG parity for the reviewer's required shapes:

  (b) blocked chain            — a lane FAIL routed back, held blocked
  (c) rejected-deploy chain    — queue-watch sees a rejected row → blocker
  (d) lane start/return/closeout — multi-hop eng→qa→report to done

Then (e): restart-from-PG. We snapshot the live ledger, reconstruct each chain from
Postgres alone via `dd-chain-driver --recover`, and assert the recovered records match
the ledger on the lifecycle fields. NO authority flip — JSON stays authoritative.

(a) happy-path Slack software request is WS-3 (not this session) — documented deferred.
(f) deliverable/Folio shape is optional — included as a light check (outcome_type).

Run under the reaper python. Self-cleans ledger + PG rows.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
sys.path.insert(0, str(HERMES / "bin"))
sys.path.insert(0, str(HERMES / "hermes-agent"))
CHAIN_DRIVER = HERMES / "bin" / "dd-chain-driver"
PARITY = HERMES / "bin" / "dd-chain-parity-check"
LEDGER = HERMES / "dd-lanes" / "chain-ledger.json"
PY = os.environ.get("DD_REAPER_PYTHON", sys.executable)
RUNID = uuid.uuid4().hex[:10]
ENVBASE = dict(os.environ, DD_CHAIN_PG_DUAL_WRITE="1")


_REG_KEYS = []


def _register_session(chat_id: str):
    """Register a discoverable synthetic session so the R3 close-gate's terminal
    delivery succeeds (SYNTHETIC chat only; removed in cleanup)."""
    try:
        from gateway.session import SessionSource, build_session_key
        from gateway.config import Platform
        from gateway.mirror import _SESSIONS_INDEX
    except Exception:
        return
    rk = build_session_key(SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id,
                                         chat_type="dm"))
    try:
        data = json.loads(_SESSIONS_INDEX.read_text()) if _SESSIONS_INDEX.exists() else {}
    except Exception:
        data = {}
    key = f"{rk}:{chat_id}"
    data[key] = {"session_key": rk, "session_id": f"r4-sess-{chat_id}",
                 "platform": "telegram", "chat_type": "dm",
                 "origin": {"platform": "telegram", "chat_id": chat_id,
                            "chat_type": "dm", "user_id": chat_id, "thread_id": None}}
    _SESSIONS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _SESSIONS_INDEX.write_text(json.dumps(data, indent=2))
    _REG_KEYS.append((str(_SESSIONS_INDEX), key))


def _cleanup_sessions():
    if not _REG_KEYS:
        return
    from pathlib import Path as _P
    idx = _P(_REG_KEYS[0][0])
    try:
        data = json.loads(idx.read_text())
    except Exception:
        return
    for _f, key in _REG_KEYS:
        data.pop(key, None)
    idx.write_text(json.dumps(data, indent=2))


def _stub():
    p = HERMES / "dd-lanes" / f".r4-stub-{RUNID}.sh"
    p.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'lane=""; gate="PASS"\n'
        'while [[ $# -gt 0 ]]; do case "$1" in --lane) lane="$2"; shift 2;; '
        '--packet|--wts-task) shift 2;; *) shift;; esac; done\n'
        # The stub honors a per-lane gate file so we can force a FAIL for shape (b).
        'gatef="$HOME/.hermes/dd-lanes/.r4-gate-'+RUNID+'-$lane"\n'
        '[[ -f "$gatef" ]] && gate="$(cat "$gatef")"\n'
        'root="$HOME/.hermes/dd-lanes/$lane/runs"; mkdir -p "$root"\n'
        'rd="$root/$(date +%Y%m%d-%H%M%S)-r4stub$$-$RANDOM"; mkdir -p "$rd"\n'
        'printf \'{"lane":"%s","agent":"stub"}\\n\' "$lane" > "$rd/meta.json"\n'
        'if [[ "$gate" == "FAIL" ]]; then printf \'1\\n\' > "$rd/exit_code"; '
        'printf \'[%s] FAIL | blocker: forced fail for shape-b\\n\' "$lane" > "$rd/stdout.log"; '
        'else printf \'0\\n\' > "$rd/exit_code"; printf \'[%s] PASS | r4\\n\' "$lane" > "$rd/stdout.log"; fi\n'
        'echo "$rd"\n')
    p.chmod(0o755)
    return p


def _finished(lane, gate="PASS"):
    root = HERMES / "dd-lanes" / lane / "runs"
    root.mkdir(parents=True, exist_ok=True)
    rd = root / f"{time.strftime('%Y%m%d-%H%M%S')}-r4seed-{RUNID}-{uuid.uuid4().hex[:6]}"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "meta.json").write_text(json.dumps({"lane": lane, "agent": "stub"}))
    (rd / "exit_code").write_text("0\n" if gate == "PASS" else "1\n")
    (rd / "stdout.log").write_text(f"[{lane}] {gate} | r4 seed"
                                   + ("" if gate == "PASS" else " | blocker: seed fail") + "\n")
    return rd


def _drv(args, env):
    return subprocess.run([PY, str(CHAIN_DRIVER)] + args, capture_output=True,
                          text=True, timeout=60, env=env)


def _tick(env):
    _drv(["--tick"], env)


def _load(route):
    try:
        for c in json.loads(LEDGER.read_text()):
            if route in (c.get("route_key") or ""):
                return c
    except Exception:
        pass
    return None


def _parity(chain_id, env):
    r = subprocess.run([PY, str(PARITY), "--chain", chain_id, "--json"],
                       capture_output=True, text=True, timeout=40, env=env)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"ok": False, "results": [{"mismatches": [r.stdout[:200] + r.stderr[:200]]}]}


def main():
    results = []
    stub = _stub()
    env = dict(ENVBASE, DD_CHAIN_WRAPPER=str(stub))

    # ── (d) lane start/return/closeout: eng PASS → qa PASS → report → done ──
    # Register a session so the terminal closeout delivers (R3 close-gate → done).
    _register_session(f"r4d-{RUNID}")
    wd = str(uuid.uuid4()); rd_route = f"agent:main:telegram:dm:r4d-{RUNID}"
    eng = _finished("engineering")
    _drv(["--start", wd, rd_route, "telegram", f"r4d-{RUNID}", "dm", "--goal",
          "R4 shape-d lane lifecycle", "--stages", "engineering,qa,report",
          "--first-run-dir", str(eng)], env)
    _tick(env); time.sleep(2); _tick(env); time.sleep(1)
    recd = _load(rd_route) or {}
    pj = _parity(recd.get("chain_id", ""), env)
    okd = recd.get("status") == "done" and pj.get("ok")
    results.append(("(d) lane start/return/closeout → done at PARITY", okd,
                    f"status={recd.get('status')} parity_ok={pj.get('ok')} "
                    f"events={(pj.get('results') or [{}])[0].get('events')} "
                    f"runs={(pj.get('results') or [{}])[0].get('runs')}"))

    # ── (b) blocked chain: a qa FAIL with retries exhausted → blocked/escalated ──
    # Force the qa lane stub to FAIL so the chain routes back + (retries=0) escalates.
    (HERMES / "dd-lanes" / f".r4-gate-{RUNID}-qa").write_text("FAIL")
    wb = str(uuid.uuid4()); rb_route = f"agent:main:telegram:dm:r4b-{RUNID}"
    engb = _finished("engineering")
    _drv(["--start", wb, rb_route, "telegram", f"r4b-{RUNID}", "dm", "--goal",
          "R4 shape-b blocked", "--stages", "engineering,qa,report",
          "--first-run-dir", str(engb), "--max-retries", "0"], env)
    _tick(env); time.sleep(2); _tick(env); time.sleep(1); _tick(env); time.sleep(1)
    recb = _load(rb_route) or {}
    pjb = _parity(recb.get("chain_id", ""), env)
    okb = recb.get("status") in ("blocked", "escalated") and pjb.get("ok")
    results.append(("(b) blocked chain (qa FAIL, no retry → escalated) at PARITY", okb,
                    f"status={recb.get('status')} blocker={(recb.get('blocker') or '')[:50]} "
                    f"parity_ok={pjb.get('ok')}"))

    # ── (c) rejected-deploy chain: seed parked at deploy w/ a row, feed a rejected
    #        row via the stub row-file → queue-watch → blocker. ──
    rowid = str(uuid.uuid4())
    rowf = HERMES / "dd-lanes" / f".r4-deeprow-{RUNID}.json"
    rowf.write_text(json.dumps({rowid: {"id": rowid, "status": "rejected",
                                        "reject_reason": "diff touches prod secrets",
                                        "service_name": "synthetic-svc"}}))
    wc = str(uuid.uuid4()); rc_route = f"agent:main:telegram:dm:r4c-{RUNID}"
    envc = dict(env, DD_DEPLOY_SUBMIT_ENABLED="1", DD_CHAIN_DEPLOY_ROW_FILE=str(rowf))
    _drv(["--start", wc, rc_route, "telegram", f"r4c-{RUNID}", "dm", "--goal",
          "R4 shape-c rejected deploy", "--stages", "engineering,qa,deploy,report",
          "--start-stage", "deploy", "--deploy-row", rowid,
          "--deploy-service", "synthetic-svc"], envc)
    _tick(envc); time.sleep(1)
    recc = _load(rc_route) or {}
    pjc = _parity(recc.get("chain_id", ""), envc)
    okc = recc.get("status") == "blocked" and "reject" in (recc.get("blocker") or "").lower() \
        and pjc.get("ok")
    results.append(("(c) rejected-deploy chain → blocker at PARITY", okc,
                    f"status={recc.get('status')} blocker={(recc.get('blocker') or '')[:50]} "
                    f"parity_ok={pjc.get('ok')}"))

    # ── (f) optional: a deliverable/Folio-shaped chain (explicit outcome fields) ──
    # The driver doesn't take Folio flags, but the projection honors an explicit
    # outcome_type/delivery_mechanism on the ledger record — so we assert the helper
    # projects them. (Light check; the driver wiring for deliverable chains is WS-3+.)
    okf = None
    try:
        import dd_chain_pg as pg
        frec = {"chain_id": f"r4f-{RUNID}", "goal": "Folio deliverable",
                "outcome_type": "deliverable", "delivery_mechanism": "folio_pdf",
                "stages": ["engineering", "report"]}
        row = pg.chain_row_from_rec(frec)
        okf = row.get("outcome_type") == "deliverable" and row.get("delivery_mechanism") == "folio_pdf"
        results.append(("(f) deliverable/Folio shape projects outcome_type+delivery_mechanism", okf,
                        f"outcome_type={row.get('outcome_type')} delivery={row.get('delivery_mechanism')}"))
    except Exception as exc:
        results.append(("(f) deliverable/Folio shape", False, f"err {type(exc).__name__}"))

    # ── (e) restart-from-PG: reconstruct each chain from Postgres alone ──
    rec_env = dict(env, DD_CHAIN_PG_RECOVERY="1")
    recovered = subprocess.run([PY, str(CHAIN_DRIVER), "--recover", "--all"],
                               capture_output=True, text=True, timeout=60, env=rec_env)
    try:
        recovered_list = json.loads(recovered.stdout)
    except Exception:
        recovered_list = []
    by_anchor = {r.get("chain_id"): r for r in recovered_list}
    oke = True
    detail_e = []
    for route, rec in (("d", recd), ("b", recb), ("c", recc)):
        anchor = rec.get("chain_id")
        rr = by_anchor.get(anchor)
        if not rr:
            oke = False; detail_e.append(f"{route}:MISSING"); continue
        # lifecycle fields must match what the ledger holds
        match = (rr.get("status") == rec.get("status")
                 and rr.get("stage") == rec.get("stage")
                 and rr.get("next_stage") == rec.get("next_stage")
                 and rr.get("wts_task") == rec.get("wts_task"))
        oke = oke and match
        detail_e.append(f"{route}:{'ok' if match else 'MISMATCH'}")
    results.append(("(e) restart-from-PG reconstructs lifecycle state (no authority flip)",
                    oke, "recovered " + ", ".join(detail_e)))

    # cleanup
    for rec in (recd, recb, recc):
        if rec.get("chain_id"):
            _drv(["--abort", rec["chain_id"]], env)
    try:
        like = f"%{RUNID}%"
        subprocess.run(["sudo", "-n", "-u", "leviroberts",
                        "/opt/homebrew/opt/postgresql@16/bin/psql", "-d", "directus", "-c",
                        f"delete from request_chain_events where chain_id in "
                        f"(select id from request_chains where route_key like '{like}'); "
                        f"delete from request_chain_runs where chain_id in "
                        f"(select id from request_chains where route_key like '{like}'); "
                        f"delete from request_chains where route_key like '{like}';"],
                       capture_output=True, text=True, timeout=20)
    except Exception:
        pass
    for f in HERMES.glob(f"dd-lanes/.r4-*{RUNID}*"):
        try:
            f.unlink()
        except Exception:
            pass
    _cleanup_sessions()

    print("=" * 72)
    print(f"R4 PARITY SHAPES + RECOVERY (runid={RUNID})")
    print("=" * 72)
    g = 0
    for name, ok, detail in results:
        mark = "GREEN" if ok else ("N/A" if ok is None else "RED")
        print(f"  [{mark}] {name}")
        print(f"         {detail}")
        if ok:
            g += 1
    needed = sum(1 for _, ok, _ in results if ok is not None)
    print(f"\nRESULT: {g}/{needed} GREEN  (note: (a) happy-path Slack = WS-3, deferred)")
    sys.exit(0 if g == needed else 1)


if __name__ == "__main__":
    main()
