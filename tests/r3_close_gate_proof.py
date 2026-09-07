#!/usr/bin/env python3
"""R3 close-gate proof — terminal user-update delivery audit, REAL flows.

Proves three things through the REAL driver (real ledger, real state machine, real
gateway.mirror.mirror_to_session delivery — NOT a monkeypatch):
  A. delivery SUCCEEDS  -> delivery_audit_status=delivered, chain rests done.
  B. delivery FAILS     -> delivery_audit_status=failed, chain HELD (status=blocked,
                           blocker names the undelivered terminal update) — no silent close.
  C. retry RESOLVES     -> once a session exists, the held chain's next tick delivers,
                           audit flips to delivered, chain rests done.

Run under the reaper's python (ABI/session libs). Self-cleans the ledger + PG rows.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
sys.path.insert(0, str(HERMES / "hermes-agent"))
sys.path.insert(0, str(HERMES / "bin"))

from gateway.session import SessionSource, build_session_key  # noqa: E402
from gateway.config import Platform  # noqa: E402
try:
    from gateway.run import _attach_dd_context_for_turn  # noqa: E402
except Exception:
    _attach_dd_context_for_turn = None

CHAIN_DRIVER = HERMES / "bin" / "dd-chain-driver"
LEDGER = HERMES / "dd-lanes" / "chain-ledger.json"
PY = os.environ.get("DD_REAPER_PYTHON", sys.executable)
RUNID = uuid.uuid4().hex[:10]

ENV = dict(os.environ, DD_CHAIN_PG_DUAL_WRITE="1")


def _stub_wrapper() -> Path:
    p = HERMES / "dd-lanes" / f".r3-proof-stub-{RUNID}.sh"
    p.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'lane=""; while [[ $# -gt 0 ]]; do case "$1" in --lane) lane="$2"; shift 2;; '
        '--packet|--wts-task) shift 2;; *) shift;; esac; done\n'
        'root="$HOME/.hermes/dd-lanes/$lane/runs"; mkdir -p "$root"\n'
        'rd="$root/$(date +%Y%m%d-%H%M%S)-r3proof$$-$RANDOM"; mkdir -p "$rd"\n'
        'printf \'{"lane":"%s","agent":"stub"}\\n\' "$lane" > "$rd/meta.json"\n'
        'printf \'0\\n\' > "$rd/exit_code"; printf \'[%s] PASS | r3 proof\\n\' "$lane" > "$rd/stdout.log"\n'
        'echo "$rd"\n')
    p.chmod(0o755)
    return p


def _finished(lane: str) -> Path:
    root = HERMES / "dd-lanes" / lane / "runs"
    root.mkdir(parents=True, exist_ok=True)
    rd = root / f"{time.strftime('%Y%m%d-%H%M%S')}-r3seed-{RUNID}-{uuid.uuid4().hex[:6]}"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "meta.json").write_text(json.dumps({"lane": lane, "agent": "stub"}))
    (rd / "exit_code").write_text("0\n")
    (rd / "stdout.log").write_text(f"[{lane}] PASS\n")
    return rd


from gateway.mirror import _SESSIONS_INDEX  # noqa: E402

_REGISTERED_CHATS = set()


def _route_for(chat_id: str) -> str:
    src = SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm")
    return build_session_key(src)


def _make_session(chat_id: str):
    """Register a REAL, discoverable gateway session entry in sessions.json (the same
    index mirror_to_session resolves against) so the driver's terminal closeout
    actually delivers. SYNTHETIC chat ids only — never Levi's real chat. Removed in
    cleanup. The session_id key embeds the chat so it is unique per synthetic chat."""
    sid = f"r3-proof-session-{chat_id}"
    rk = _route_for(chat_id)
    try:
        data = json.loads(_SESSIONS_INDEX.read_text(encoding="utf-8")) \
            if _SESSIONS_INDEX.exists() else {}
    except Exception:
        data = {}
    # Key per-chat so two synthetic chats don't share an entry (DM keys don't embed
    # the chat id, so we suffix the synthetic chat to keep them distinct in the index).
    key = f"{rk}:{chat_id}"
    data[key] = {
        "session_key": rk, "session_id": sid,
        "platform": "telegram", "chat_type": "dm",
        "origin": {"platform": "telegram", "chat_id": chat_id, "chat_type": "dm",
                   "user_id": chat_id, "thread_id": None},
    }
    _SESSIONS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    _SESSIONS_INDEX.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _REGISTERED_CHATS.add((key, chat_id))
    return rk


def _cleanup_sessions():
    if not _REGISTERED_CHATS:
        return
    try:
        data = json.loads(_SESSIONS_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return
    for key, _chat in _REGISTERED_CHATS:
        data.pop(key, None)
    _SESSIONS_INDEX.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _drive(wts, route, chat, stages="engineering,report"):
    stub = _stub_wrapper()
    eng = _finished("engineering")
    env = dict(ENV, DD_CHAIN_WRAPPER=str(stub))
    subprocess.run([PY, str(CHAIN_DRIVER), "--start", wts, route, "telegram", chat, "dm",
                    "--goal", "R3 proof", "--stages", stages, "--first-run-dir", str(eng)],
                   capture_output=True, text=True, timeout=30, env=env)
    subprocess.run([PY, str(CHAIN_DRIVER), "--tick"], capture_output=True, text=True,
                   timeout=60, env=env)
    return env


def _load(route):
    try:
        for c in json.loads(LEDGER.read_text()):
            if route in (c.get("route_key") or ""):
                return c
    except Exception:
        pass
    return None


def _tick(env):
    subprocess.run([PY, str(CHAIN_DRIVER), "--tick"], capture_output=True, text=True,
                   timeout=60, env=env)


def main():
    results = []

    # ── A: delivery SUCCEEDS (real session exists) → delivered + done ──
    chatA = f"r3-proofA-{RUNID}"
    _make_session(chatA)
    wtsA = str(uuid.uuid4())
    routeA = _route_for(chatA)
    envA = _drive(wtsA, routeA, chatA)
    recA = _load(routeA) or {}
    okA = recA.get("status") == "done" and recA.get("delivery_audit_status") == "delivered"
    results.append(("A.delivered: real delivery → audit=delivered, chain rests done",
                    okA, f"status={recA.get('status')} audit={recA.get('delivery_audit_status')}"))

    # ── B: delivery FAILS (no session) → failed + HELD ──
    chatB = f"r3-proofB-NO-SESSION-{RUNID}"   # never created → mirror can't resolve
    wtsB = str(uuid.uuid4())
    routeB = _route_for(chatB)
    envB = _drive(wtsB, routeB, chatB)
    recB = _load(routeB) or {}
    okB = (recB.get("status") == "blocked"
           and recB.get("delivery_audit_status") == "failed"
           and "NOT delivered" in (recB.get("blocker") or ""))
    results.append(("B.held: failed delivery → audit=failed, chain HELD (no silent close)",
                    okB, f"status={recB.get('status')} audit={recB.get('delivery_audit_status')} "
                         f"blocker={(recB.get('blocker') or '')[:60]}"))

    # ── C: retry RESOLVES — now create the session, tick → delivers → done ──
    _make_session(chatB)
    _tick(envB)
    recC = _load(routeB) or {}
    okC = recC.get("status") == "done" and recC.get("delivery_audit_status") == "delivered"
    results.append(("C.retry: session appears → next tick delivers → audit=delivered, done",
                    okC, f"status={recC.get('status')} audit={recC.get('delivery_audit_status')}"))

    # cleanup ledger (abort our chains) + PG rows. Delete by the EXACT chain anchors
    # (the PG row's route_key == the driver chain_id {wts_uuid}:{hash}, which does NOT
    # contain RUNID — a RUNID pattern never matches, so match the anchors we created).
    anchors = []
    for route in (routeA, routeB):
        rec = _load(route)
        if rec and rec.get("chain_id"):
            anchors.append(rec["chain_id"])
            subprocess.run([PY, str(CHAIN_DRIVER), "--abort", rec["chain_id"]],
                           capture_output=True, text=True, timeout=15)
    try:
        if anchors:
            in_list = ",".join("'" + a.replace("'", "") + "'" for a in anchors)
            subprocess.run(["sudo", "-n", "-u", "leviroberts",
                            "/opt/homebrew/opt/postgresql@16/bin/psql", "-d", "directus", "-c",
                            f"delete from request_chain_events where chain_id in "
                            f"(select id from request_chains where route_key in ({in_list})); "
                            f"delete from request_chain_runs where chain_id in "
                            f"(select id from request_chains where route_key in ({in_list})); "
                            f"delete from request_chains where route_key in ({in_list});"],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        pass
    _cleanup_sessions()

    print("=" * 70)
    print(f"R3 CLOSE-GATE PROOF (runid={RUNID})")
    print("=" * 70)
    g = 0
    for name, ok, detail in results:
        print(f"  [{'GREEN' if ok else 'RED'}] {name}")
        print(f"         {detail}")
        g += 1 if ok else 0
    print(f"\nRESULT: {g}/{len(results)} GREEN")
    sys.exit(0 if g == len(results) else 1)


if __name__ == "__main__":
    main()
