#!/usr/bin/env python3
"""Probe for the 2026-06-04 DESIGN HOLD: slack-chain lane dispatch is HELD pending
Levi's Model-A/Model-B review.

Proves, WITHOUT touching a real surface (stub wrapper + synthetic slack channel):
  H1 — a slack chain rests `escalated` + `design_hold=True` and DOES NOT dispatch a lane
       (empty history) while the hold is active (default ON).
  H2 — the next_check_at clock is KEPT while held (held, NOT silent).
  H3 — a lapsed deadline fires a heartbeat while held (re-armed, still escalated, still
       no dispatch).
  H4 — REVERSIBLE: with DD_SLACK_LANE_DISPATCH_HOLD=0 the same slack chain DOES dispatch
       (the Model-B path is one flag away, no code change).

Telegram chains are unaffected by the hold (proven by Canon 6+7 staying green).
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

H = Path.home() / ".hermes"
DRIVER = H / "bin" / "dd-chain-driver"
LEDGER = H / "dd-lanes" / "chain-ledger.json"
LANES = H / "dd-lanes"
PY = os.environ.get("DD_CHAIN_DRIVER_PYTHON", str(H / "hermes-agent" / "venv" / "bin" / "python"))
RID = f"hold{os.getpid()}"
CHAT = f"hold-syn-{RID}"


def make_stub():
    stub = LANES / f".hold-stub-{RID}.sh"
    stub.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        'lane=""; while [[ $# -gt 0 ]]; do case "$1" in --lane) lane="$2"; shift 2;; '
        '--packet|--wts-task) shift 2;; *) shift;; esac; done\n'
        'root="$HOME/.hermes/dd-lanes/$lane/runs"; mkdir -p "$root"; '
        'rd="$root/$(date +%s)-' + RID + '$$"; mkdir -p "$rd"\n'
        'echo "[$lane] PENDING | stub | run_dir=$rd"; exit 75\n', encoding="utf-8")
    stub.chmod(0o755)
    return stub


def load(cid):
    for c in json.loads(LEDGER.read_text()):
        if c.get("chain_id") == cid:
            return c
    return {}


def tick(env):
    subprocess.run([PY, str(DRIVER), "--tick"], capture_output=True, text=True, timeout=60, env=env)


def main():
    stub = make_stub()
    env = dict(os.environ, DD_CHAIN_WRAPPER=str(stub))  # HOLD unset → default ON (held)
    route = f"slack:hold:{RID}:1"
    cid = None
    checks = {}
    try:
        s = subprocess.run(
            [PY, str(DRIVER), "--start", "", route, "slack", CHAT, "channel",
             "--stages", "engineering,qa", "--interval", "180", "--goal", "hold probe"],
            capture_output=True, text=True, timeout=30, env=env)
        cid = re.search(r"chain_id=(\S+)", s.stdout).group(1)

        tick(env)
        r = load(cid)
        checks["H1.held"] = (r.get("status") == "escalated" and "HELD" in (r.get("blocker") or "")
                             and not r.get("history") and bool(r.get("design_hold")))
        checks["H2.clock_kept"] = isinstance(r.get("next_check_at"), int)

        # force the deadline to lapse → a heartbeat must fire WHILE held
        led = json.loads(LEDGER.read_text())
        for c in led:
            if c.get("chain_id") == cid:
                c["next_check_at"] = int(time.time()) - 5
                c["last_update_at"] = int(time.time()) - 999
        LEDGER.write_text(json.dumps(led, indent=2))
        before = load(cid).get("next_check_at")
        tick(env)
        hb = load(cid)
        checks["H3.heartbeat"] = (isinstance(hb.get("next_check_at"), int)
                                  and hb["next_check_at"] > before
                                  and hb.get("status") == "escalated"
                                  and not hb.get("history"))

        # reversible: flag OFF → the slack chain dispatches (Model-B path)
        led = json.loads(LEDGER.read_text())
        for c in led:
            if c.get("chain_id") == cid:
                c.update({"status": "active", "blocker": None, "history": [], "stage_index": 0})
                c.pop("design_hold", None)
        LEDGER.write_text(json.dumps(led, indent=2))
        tick(dict(env, DD_SLACK_LANE_DISPATCH_HOLD="0"))
        r3 = load(cid)
        checks["H4.reversible"] = any(h.get("stage") == "engineering" for h in r3.get("history", []))

        for k, v in checks.items():
            tag = "\033[32mGREEN\033[0m" if v else "\033[31mRED\033[0m"
            print(f"  [{tag}] {k}")
    finally:
        if cid:
            subprocess.run([PY, str(DRIVER), "--abort", cid], capture_output=True, text=True, timeout=30)
        try:
            led = [c for c in json.loads(LEDGER.read_text())
                   if RID not in (c.get("chat_id") or "") + (c.get("route_key") or "")]
            LEDGER.write_text(json.dumps(led, indent=2))
        except Exception:
            pass
        stub.unlink(missing_ok=True)
        for d in (LANES / "engineering" / "runs").glob(f"*{RID}*"):
            try:
                for f in d.glob("*"):
                    f.unlink()
                d.rmdir()
            except Exception:
                pass
        try:
            reg = [e for e in json.loads((LANES / "reaper-registry.json").read_text())
                   if RID not in str(e)]
            (LANES / "reaper-registry.json").write_text(json.dumps(reg, indent=2))
        except Exception:
            pass

    r = sum(1 for v in checks.values() if not v)
    print("=" * 60)
    print(f"SLACK-HOLD PROBE: {len(checks)-r} GREEN, {r} RED")
    sys.exit(0 if r == 0 else 1)


if __name__ == "__main__":
    main()
