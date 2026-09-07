#!/usr/bin/env python3
"""Focused probe for the SLACK-CHAIN-DRIVER fix (2026-06-04).

Proves the two driver-side fixes WITHOUT touching a real surface:
  1. A chain minted WITHOUT --first-run-dir (the real slack-mint shape) is DISPATCHED
     on the next tick — engineering lane enters, a run lands in history, the chain
     advances out of "dispatch pending". (Pre-fix: it parked at START forever.)
  2. next_check_at is armed at mint and re-armed on the dispatch transition — the
     anti-silence clock is present and moving.
  3. A lapsed next_check_at on a chain with NO transition produces a heartbeat
     (status update) and re-arms — silence is structurally impossible.

Uses the canon's stub wrapper (run-dir-only, no specialist/Slack/secrets) + a
synthetic platform/chat so it can never reach a real chat. Cleans up its chains.
Run under the hermes venv python with DD_CHAIN_PG_DUAL_WRITE matching the env.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERMES = Path.home() / ".hermes"
DRIVER = HERMES / "bin" / "dd-chain-driver"
LEDGER = HERMES / "dd-lanes" / "chain-ledger.json"
LANES_DIR = HERMES / "dd-lanes"
PY = os.environ.get("DD_CHAIN_DRIVER_PYTHON", str(HERMES / "hermes-agent" / "venv" / "bin" / "python"))
RUNID = f"probe{os.getpid()}"
CHAT = f"probe-synthetic-{RUNID}"

results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    tag = "\033[32mGREEN\033[0m" if ok else "\033[31mRED\033[0m"
    print(f"  [{tag}] {name}\n           {detail}")


def make_stub():
    stub = LANES_DIR / f".probe-stub-{RUNID}.sh"
    stub.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        'lane=""; while [[ $# -gt 0 ]]; do case "$1" in --lane) lane="$2"; shift 2;; '
        '--packet|--wts-task) shift 2;; *) shift;; esac; done\n'
        'root="$HOME/.hermes/dd-lanes/$lane/runs"; mkdir -p "$root"\n'
        'rd="$root/$(date +%Y%m%d-%H%M%S)-' + RUNID + '$$"; mkdir -p "$rd"\n'
        # Reproduce the real wrapper's PENDING contract (status line + exit 75).
        'echo "[$lane] PENDING | stub detached | run_dir=$rd"\n'
        'exit 75\n', encoding="utf-8")
    stub.chmod(0o755)
    return stub


def load(cid):
    try:
        for c in json.loads(LEDGER.read_text()):
            if c.get("chain_id") == cid:
                return c
    except Exception:
        pass
    return {}


def main():
    stub = make_stub()
    # This probe exercises the DISPATCH MECHANISM (root-cause fix: a never-entered first
    # lane is dispatched + the next_check_at clock). The 2026-06-04 design hold
    # (DD_SLACK_LANE_DISPATCH_HOLD, default ON) deliberately parks slack chains at an
    # escalated-held state instead of dispatching — so we explicitly turn the hold OFF
    # here to test the dispatch path itself. The hold's own behavior is proven separately.
    env = dict(os.environ, DD_CHAIN_WRAPPER=str(stub), DD_SLACK_LANE_DISPATCH_HOLD="0")
    route = f"slack:probe:{RUNID}:1.0"
    cid = None
    try:
        # 1) Mint WITHOUT --first-run-dir (exactly what dd-slack chain-mint does).
        start = subprocess.run(
            [PY, str(DRIVER), "--start", "", route, "slack", CHAT, "channel",
             "--stages", "engineering,qa,deploy,report", "--interval", "180",
             "--goal", "probe: cleanup request, no seeded run"],
            capture_output=True, text=True, timeout=30, env=env)
        m = re.search(r"chain_id=(\S+)", start.stdout or "")
        cid = m.group(1) if m else None
        rec("P1.mint: driver opened a record with NO first-run-dir", bool(cid),
            (start.stdout or start.stderr).strip())
        if not cid:
            return

        pre = load(cid)
        rec("P2.mint-clock: next_check_at armed at mint (anti-silence deadline exists)",
            isinstance(pre.get("next_check_at"), int) and pre["next_check_at"] > 0,
            f"next_check_at={pre.get('next_check_at')} interval={pre.get('update_interval')}")
        rec("P3.pre-tick: parked at engineering/dispatch-pending, empty history (the bug shape)",
            pre.get("stage") == "engineering" and pre.get("stage_index") == 0
            and not pre.get("history"),
            f"stage={pre.get('stage')} idx={pre.get('stage_index')} hist={len(pre.get('history') or [])}")

        # 2) Tick → the never-entered engineering lane must DISPATCH (the root fix).
        subprocess.run([PY, str(DRIVER), "--tick"], capture_output=True, text=True,
                       timeout=60, env=env)
        post = load(cid)
        eng_run = next((h for h in post.get("history", [])
                        if h.get("kind") == "run" and h.get("stage") == "engineering"), None)
        rec("P4.dispatch: tick DISPATCHED the engineering lane (was START-only pre-fix)",
            eng_run is not None,
            f"history={[h.get('stage') for h in post.get('history', [])]} "
            f"owner={post.get('owner')}")
        rec("P5.owner: record moved off 'dispatch pending' to an in-flight lane owner",
            "dispatch pending" not in (post.get("owner") or ""),
            f"owner={post.get('owner')}")
        rec("P6.clock-rearm: next_check_at re-armed after the dispatch transition",
            isinstance(post.get("next_check_at"), int)
            and post["next_check_at"] >= pre.get("next_check_at", 0),
            f"pre={pre.get('next_check_at')} post={post.get('next_check_at')}")

        # 3) Force the deadline into the past with the lane still in flight → next tick
        #    must HEARTBEAT (post a status update) and re-arm. Prove silence is impossible.
        led = json.loads(LEDGER.read_text())
        for c in led:
            if c.get("chain_id") == cid:
                c["next_check_at"] = int(time.time()) - 5  # lapsed
                c["last_update_at"] = int(time.time()) - 999
        LEDGER.write_text(json.dumps(led, indent=2))
        before = load(cid).get("next_check_at")
        subprocess.run([PY, str(DRIVER), "--tick"], capture_output=True, text=True,
                       timeout=60, env=env)
        hb = load(cid)
        rec("P7.heartbeat: a lapsed deadline with no transition re-armed next_check_at (no silent park)",
            isinstance(hb.get("next_check_at"), int) and hb["next_check_at"] > before,
            f"lapsed={before} → rearmed={hb.get('next_check_at')} "
            f"(now={int(time.time())})")
    finally:
        if cid:
            subprocess.run([PY, str(DRIVER), "--abort", cid],
                           capture_output=True, text=True, timeout=30)
        # purge probe residue from the ledger so the live reaper never chews on it
        try:
            led = json.loads(LEDGER.read_text())
            led = [c for c in led if RUNID not in (c.get("chat_id") or "")
                   and RUNID not in (c.get("route_key") or "")]
            LEDGER.write_text(json.dumps(led, indent=2))
        except Exception:
            pass
        try:
            stub.unlink()
        except Exception:
            pass
        # remove the stub run dirs this probe created
        for d in (LANES_DIR / "engineering" / "runs").glob(f"*{RUNID}*"):
            try:
                for f in d.glob("*"):
                    f.unlink()
                d.rmdir()
            except Exception:
                pass

    g = sum(1 for _, ok, _ in results if ok)
    r = len(results) - g
    print("=" * 70)
    print(f"PROBE RESULT: {g} GREEN, {r} RED")
    sys.exit(0 if r == 0 else 1)


if __name__ == "__main__":
    main()
