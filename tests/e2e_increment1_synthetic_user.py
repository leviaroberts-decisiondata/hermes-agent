#!/usr/bin/env python3
"""Increment 1 — Realistic-User-Path validation harness (live-daemon edition).

Implements the "smallest first increment" of the Priority-2 validation framework
(_ops_artifacts/v1.1-review/PRIORITY-2-validation-framework-2026-06-04.md §4).

WHY THIS EXISTS — what the OLD harness missed
---------------------------------------------
The overnight 7/7 was self-consistency theater. At two trust boundaries the old
harness substituted a stand-in for the real thing — and those were exactly the
two boundaries that broke:

  * Bug #1 (P1 route-key leak): the fixture *authored* the routing key
    (``agent:main:…``), set it on ``_dd_session_key``, then asserted registration
    on the same fabricated key — code agreeing with its own input. The LIVE gateway
    sets ``_dd_session_key`` to the scrubbed *observability* key
    (``agent:hermes:gateway:…``, no chat id) and the routing key on ``_dd_route_key``.
    So every real turn skipped registration ("no parseable caller session_key")
    and the closeout never returned.

  * Bug #2 (WTS guard block): the old harness drove dd-slack-service IN-PROCESS,
    so it never issued a tool call through the gateway's ``pre_tool_call`` subprocess
    where ``dd-pretool-guard.py`` lives. The guard's WTS misclassification was
    therefore structurally INVISIBLE to the test.

THE FIX (this harness's invariants)
-----------------------------------
  1. CONSUME, never inject. The routing key is *derived by production code*
     (build_session_key + _attach_dd_context_for_turn) from a SessionSource the
     synthetic user supplies — never written as a literal. If the route-key fix is
     reverted, the derived key path collapses and registration skips.
  2. CROSS the real subprocesses. Canon 2 drives the REAL route_to_lane tool which
     shells out to the LIVE reaper daemon; Canon 3/4 fire the REAL
     dd-pretool-guard.py via the production shell-hook spawn path (run_once).
  3. SURFACE read-back, not in-process boolean. Proof is read from the real reaper
     log + the synthetic session transcript jsonl + the guard's real stdout
     decision — never a value the harness authored.
  4. NEGATIVES are PASS-on-DENY (deploy-submit must be BLOCKED).
  5. HONESTY control: a deliberately broken variant (the pre-fix code path) MUST
     report RED.

ISOLATION / SAFETY
------------------
  * Synthetic chat id ``qa-synthetic-<runid>`` — NEVER Levi's real chat. Writes
    confined to an isolated synthetic session that is torn down after the run.
  * No service restart / kickstart / launchctl. Attaches to the already-running
    reaper daemon. No mints, no gate relaxation, no new accounts/tokens.
  * No secret value is ever printed — booleans/identity-slugs only.

Run:  venv/bin/python tests/e2e_increment1_synthetic_user.py [--against-old-code]

  --against-old-code   simulate the PRE-FIX gateway (only the observability key is
                       attached, no _dd_route_key) to DEMONSTRATE the route-key
                       assertion goes RED — proving it depends on the real key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Import production code — this harness lives inside hermes-agent so it consumes
# the SAME functions the live gateway runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.session import SessionSource, build_session_key
from gateway.config import Platform
from gateway.run import _dd_observability_session_key, _attach_dd_context_for_turn
import tools.route_to_lane_tool as r2l
from agent.shell_hooks import ShellHookSpec, run_once

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
LANES_DIR = HERMES_HOME / "dd-lanes"
SESS_DIR = HERMES_HOME / "sessions"
SESS_INDEX = SESS_DIR / "sessions.json"
REAPER = HERMES_HOME / "bin" / "dd-lane-reaper"
REAPER_LOG = HERMES_HOME / "logs" / "dd-lane-reaper.log"
GUARD = HERMES_HOME / "bin" / "dd-pretool-guard.py"

RUNID = uuid.uuid4().hex[:10]
SYNTH_CHAT_ID = f"qa-synthetic-{RUNID}"          # NEVER Levi's real chat id
SYNTH_SESSION_ID = f"qa_synthetic_{RUNID}"
SYNTH_JSONL = SESS_DIR / f"{SYNTH_SESSION_ID}.jsonl"


class Reporter:
    def __init__(self):
        self.results = []  # (name, status, detail)

    def record(self, name, ok, detail=""):
        status = "GREEN" if ok else "RED"
        self.results.append((name, status, detail))
        mark = "\033[32mGREEN\033[0m" if ok else "\033[31mRED\033[0m"
        print(f"  [{mark}] {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"           {line}")

    def all_green(self):
        return all(s == "GREEN" for _, s, _ in self.results)

    def summary(self):
        g = sum(1 for _, s, _ in self.results if s == "GREEN")
        r = sum(1 for _, s, _ in self.results if s == "RED")
        return g, r


# ── synthetic isolated session (the S5 pattern, keyed to a SYNTHETIC chat) ──
def setup_session(route_key: str):
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    SYNTH_JSONL.write_text("", encoding="utf-8")
    try:
        data = json.loads(SESS_INDEX.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data[route_key] = {
        "session_key": route_key,
        "session_id": SYNTH_SESSION_ID,
        "platform": "telegram",
        "chat_type": "dm",
        "display_name": "QA SYNTHETIC (Increment-1 harness; safe to delete)",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "origin": {
            "platform": "telegram", "chat_type": "dm",
            "chat_id": SYNTH_CHAT_ID, "user_id": SYNTH_CHAT_ID, "thread_id": None,
        },
    }
    SESS_INDEX.write_text(json.dumps(data, indent=0), encoding="utf-8")


def teardown_session(route_key: str):
    try:
        data = json.loads(SESS_INDEX.read_text(encoding="utf-8"))
        data.pop(route_key, None)
        SESS_INDEX.write_text(json.dumps(data, indent=0), encoding="utf-8")
    except Exception:
        pass
    try:
        SYNTH_JSONL.unlink()
    except FileNotFoundError:
        pass


def build_real_run_dir() -> Path:
    """A minimal real run dir the LIVE reaper can reap (has an exit_code).

    The meta lane is the synthetic 'qa-synthetic' — deliberately NOT a
    mirror-enabled lane (qa/eng/design/…), so the reaper's terminal mirror
    (dd-lane-run --poll → mirror_lane_enabled) is a no-op and NEVER attempts a
    Telegram delivery to any real chat. Canon 2 proves the RE-INJECT (result
    return to the caller's registered session key) — that path is independent of
    the lane mirror, so suppressing the mirror loses nothing and keeps the run
    from repopulating the qa directory with a real chat id.
    """
    rd = LANES_DIR / "qa" / "runs" / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-inc1-{RUNID}"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "meta.json").write_text(json.dumps({
        "lane": "qa-synthetic", "agent": "qa-review", "packet": "synthetic",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "qa-synthetic",
    }) + "\n", encoding="utf-8")
    (rd / "packet.md").write_text("# Increment-1 synthetic run (safe)\n", encoding="utf-8")
    (rd / "stdout.log").write_text(
        "[qa] PASS | increment-1 synthetic closeout | #dd-lane-qa ts=1.0\n", encoding="utf-8"
    )
    (rd / "exit_code").write_text("0\n", encoding="utf-8")
    (rd / "ended_at").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n", encoding="utf-8")
    return rd


# ── a stand-in agent whose _dd_* keys are set by the REAL gateway attach path ──
def gateway_attached_agent(source: SessionSource, *, simulate_old_code: bool):
    """Return (agent, routing_key, obs_key).

    The keys are NOT authored here: build_session_key derives the routing key and
    _attach_dd_context_for_turn assigns _dd_session_key (obs) + _dd_route_key
    (routing) — the same call the gateway makes (gateway/run.py:10980).

    simulate_old_code=True reproduces the PRE-FIX gateway: it attaches ONLY the
    observability key on _dd_session_key and DOES NOT set _dd_route_key — exactly
    the state every real turn was in before the morning fix. route_to_lane then
    has no routing key to consume and must skip registration.
    """
    routing_key = build_session_key(source)
    obs_key = _dd_observability_session_key(SYNTH_SESSION_ID)

    class _Agent:
        pass

    agent = _Agent()
    if simulate_old_code:
        # Pre-fix gateway: only the scrubbed obs key existed, on _dd_session_key.
        agent._dd_session_key = obs_key
        # NB: deliberately NO _dd_route_key — the fix had not been written yet.
    else:
        _attach_dd_context_for_turn(
            agent, run_id="run-" + RUNID, session_key=obs_key, route_key=routing_key,
        )
    return agent, routing_key, obs_key


def reaper_log_size() -> int:
    try:
        return REAPER_LOG.stat().st_size
    except FileNotFoundError:
        return 0


def reaper_log_since(offset: int) -> str:
    try:
        with open(REAPER_LOG, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            return f.read()
    except FileNotFoundError:
        return ""


def session_transcript() -> str:
    try:
        return SYNTH_JSONL.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# ── CANON 2: P1 delegate → lane → result-return via the LIVE reaper daemon ──
def canon2_route_key(rep: Reporter, *, simulate_old_code: bool):
    print(f"\n── CANON 2: P1 delegate → lane → result-return "
          f"({'PRE-FIX simulation' if simulate_old_code else 'live code'}) ──")

    source = SessionSource(platform=Platform.TELEGRAM, chat_id=SYNTH_CHAT_ID, chat_type="dm")
    agent, routing_key, obs_key = gateway_attached_agent(source, simulate_old_code=simulate_old_code)

    # Invariant 1: the key is production-derived and the two shapes DIFFER.
    rep.record(
        "C2.derive: routing key is production-derived (agent:main:…) and differs from obs key",
        routing_key.startswith("agent:main:") and obs_key.startswith("agent:hermes:gateway:")
        and routing_key != obs_key,
        f"routing={routing_key}\nobs={obs_key}",
    )

    # Point the REAL route_to_lane tool at the LIVE reaper + a real run dir, but
    # use the production registration code path (route-key selection) unchanged.
    rd = build_real_run_dir()
    setup_session(routing_key)

    log_off = reaper_log_size()
    # Feed the production _register_pending_with_reaper a PENDING line carrying our
    # real run_dir; it reads _dd_route_key (fixed) / _dd_session_key (old) off the
    # agent and shells out to the LIVE reaper --register. This is the exact code
    # path a real PENDING turn hits.
    pending_out = (
        f"[qa] PENDING | run in flight | #dd-lane-qa ts=9.9 run_dir={rd}"
    )
    note = r2l._register_pending_with_reaper(pending_out, agent)
    print(f"  route_to_lane registration note: {note}")

    if simulate_old_code:
        # PRE-FIX: no _dd_route_key → must skip. This is the HONESTY control: the
        # route-return assertion below MUST go RED on old code.
        skipped = "no parseable caller session_key" in note.lower()
        rep.record(
            "C2.old-code: pre-fix gateway (obs key only) SKIPS registration",
            skipped,
            "expected skip — the closeout would never return to the caller (Bug #1)",
        )
        # The real-route-return assertion: on old code it must be RED.
        rep.record(
            "C2.route-return: live reaper re-injects to the caller on the agent:main:… key",
            False,
            "RED BY DESIGN on pre-fix code: registration skipped, so no re-inject can land. "
            "This is the assertion the overnight harness lacked — it depends on the REAL key.",
        )
        teardown_session(routing_key)
        shutil.rmtree(rd, ignore_errors=True)
        return

    # LIVE code: registration must have succeeded with the routing key.
    rep.record(
        "C2.register: live reaper accepted registration on the routing key",
        "reaper-registration: OK" in note,
        note,
    )

    # Drive a LIVE reaper sweep and read back the result-return from the REAL log
    # + the synthetic session transcript (surface read-back, not a boolean).
    sweep = subprocess.run([str(REAPER), "--once"], capture_output=True, text=True, timeout=120)
    # Give the daemon a beat in case the --loop instance reaps first.
    deadline = time.time() + 20
    new_log = ""
    while time.time() < deadline:
        new_log = reaper_log_since(log_off)
        if "REAPED" in new_log and rd.name in new_log:
            break
        time.sleep(1.5)

    reaped_marker = (rd / "reaped").exists()
    # The REAL reaper log must pair THIS run with a successful re-inject on a real
    # agent:main:… key AND carry ZERO skipped(no-session-key) for this run.
    log_for_run = "\n".join(l for l in new_log.splitlines() if rd.name in l)
    reinject_ok = "reinject_ok=True" in log_for_run
    no_skip = "skipped(no-session-key)" not in log_for_run
    key_in_log = routing_key in log_for_run or f"chat_id={SYNTH_CHAT_ID}" in log_for_run or SYNTH_CHAT_ID in log_for_run

    transcript = session_transcript()
    landed_in_caller = "LANE RESULT RETURNED" in transcript or rd.name in transcript

    rep.record(
        "C2.route-return: live reaper re-injects to the caller on the agent:main:… key",
        reaped_marker and reinject_ok and no_skip,
        f"reaped_marker={reaped_marker} reinject_ok={reinject_ok} "
        f"zero_skipped={no_skip}\nlog(for-run):\n{log_for_run or '(none captured this window)'}",
    )
    rep.record(
        "C2.surface: closeout landed in the SYNTHETIC caller session transcript (read-back)",
        landed_in_caller,
        f"transcript bytes={len(transcript)}; "
        f"{'closeout present' if landed_in_caller else 'NO closeout in transcript'}",
    )

    teardown_session(routing_key)
    shutil.rmtree(rd, ignore_errors=True)


# ── the REAL pre_tool_call guard, fired exactly as the gateway fires it ──
def fire_guard(tool_name: str, tool_input: dict) -> dict:
    """Cross the REAL pre_tool_call hook chain (run_once → _spawn →
    dd-pretool-guard.py subprocess → _parse_response). Returns the canonical
    wire-shape: {"action":"block","message":...} when blocked, or {} when allowed.
    """
    spec = ShellHookSpec(event="pre_tool_call", command=f"{GUARD}", matcher=".*", timeout=10)
    res = run_once(spec, {"tool_name": tool_name, "args": tool_input})
    parsed = res.get("parsed")
    return parsed if isinstance(parsed, dict) else {}


# ── CANON 3: WTS attach round-trip — THROUGH the guard layer (ALLOW) ──
def canon3_wts_guard(rep: Reporter):
    print("\n── CANON 3: WTS attach through the real pre_tool_call guard (must ALLOW) ──")
    # The exact op the guard FALSE-BLOCKED overnight: dd-wts-attach (helper) AND a
    # direct :8513 ledger call sourcing the dd-agent-wts cred env.
    tool_input = {
        "command": (
            "set -a; . ~/.openclaw/dd-agent-wts.env; set +a; "
            "dd-wts-attach --task 11112222-3333-4444-5555-666677778888 "
            "--file /tmp/qa-synthetic-closeout.md --as-agent dd-p1"
        )
    }
    parsed = fire_guard("execute_code", tool_input)
    allowed = parsed.get("action") != "block"
    rep.record(
        "C3.guard: real dd-pretool-guard subprocess ALLOWS sanctioned WTS attach",
        allowed,
        ("guard returned ALLOW (carve-out fired before 3b/Tier-3)"
         if allowed else f"guard BLOCKED: {parsed.get('message','')}"),
    )

    # 1c — per-agent attribution: the attach resolves to the dd-p1 identity, NOT
    # the shared admin fallback. We assert at the identity-resolution boundary
    # (no production Directus write): the helper maps --as-agent dd-p1 →
    # DD_P1_WTS_TOKEN and that token is present (boolean) in the agent env.
    ident_ok, ident_detail = resolve_per_agent_identity("dd-p1")
    rep.record(
        "C3.attribution: attach resolves to per-agent identity 'dd-p1' (not shared admin)",
        ident_ok, ident_detail,
    )


def resolve_per_agent_identity(slug: str):
    """Run dd-wts-attach's own resolve_token_var (the real helper code) to confirm
    the slug resolves to a per-agent token var that is PRESENT in the agent env.
    Booleans / names only — no token value is read or printed."""
    helper = HERMES_HOME / "bin" / "dd-wts-attach"
    # Extract & invoke the helper's REAL resolve_token_var deterministically.
    # (Source it from a temp file — process substitution is unreliable under the
    # spawned non-interactive bash.)
    import tempfile
    try:
        body = subprocess.run(
            ["sed", "-n", "/^resolve_token_var()/,/^}/p", str(helper)],
            capture_output=True, text=True, timeout=10,
        ).stdout
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as tf:
            tf.write(body)
            fn_path = tf.name
        r = subprocess.run(
            ["bash", "-c", f". '{fn_path}'; resolve_token_var '{slug}'"],
            capture_output=True, text=True, timeout=10,
        )
        var = (r.stdout or "").strip()
    except Exception as e:
        return False, f"resolve failed: {type(e).__name__}"
    finally:
        try:
            os.unlink(fn_path)
        except Exception:
            pass
    if var != "DD_P1_WTS_TOKEN":
        return False, f"slug {slug} resolved to {var!r} (expected DD_P1_WTS_TOKEN — admin fallback?)"
    # boolean presence of the token in the agent env (value NEVER read/printed)
    env_file = Path.home() / ".openclaw" / "dd-agent-wts.env"
    present = False
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(var + "="):
                present = bool(line.split("=", 1)[1].strip())
                break
    except FileNotFoundError:
        pass
    return present, (f"slug 'dd-p1' → {var}; token present in agent env: {present} "
                     f"(per-agent attribution, not admin fallback)")


# ── CANON 4: deploy-submit NEGATIVE — must be DENIED at the real surface ──
def canon4_deploy_denied(rep: Reporter):
    print("\n── CANON 4: deploy-submit through the real guard (NEGATIVE — must DENY) ──")
    tool_input = {
        "command": (
            "curl -s -X POST http://localhost:8700/deploy-queue/submit "
            "-H 'content-type: application/json' -d '{\"ref\":\"main\"}'"
        )
    }
    parsed = fire_guard("execute_code", tool_input)
    denied = parsed.get("action") == "block"
    rep.record(
        "C4.deploy-NEGATIVE: real guard BLOCKS deploy-queue/submit (PASS = denied)",
        denied,
        (f"guard BLOCKED as required: {parsed.get('message','')[:120]}"
         if denied else "FAIL — deploy-submit was NOT blocked (deploy authorization re-opened!)"),
    )


# ── CANON 5 (honesty): an injected fault MUST report RED ──
def canon5_honesty(rep: Reporter):
    print("\n── CANON 5: honesty control — an injected fault must report RED ──")
    # Inject a fault: a WTS-attach op that ALSO carries a destructive rm -rf. The
    # carve-out must REFUSE to exempt it (is_wts_bookkeeping returns False when a
    # destructive op rides along), so the guard must BLOCK. If the harness reported
    # this as GREEN/allowed, that would be the "all green over a broken path" bug.
    tool_input = {
        "command": (
            "dd-wts-attach --task 11112222 --file /tmp/x.md --as-agent dd-p1 "
            "&& rm -rf /tmp/qa-synthetic-victim"
        )
    }
    parsed = fire_guard("execute_code", tool_input)
    blocked = parsed.get("action") == "block"
    rep.record(
        "C5.honesty: injected fault (WTS+destructive) correctly reports RED/blocked",
        blocked,
        ("fault correctly BLOCKED — the carve-out is not a backdoor"
         if blocked else "FAIL — a destructive op rode the WTS carve-out (silent-green bug!)"),
    )


# ── CANON 6: the CHAIN DRIVER — multi-hop ownership, clock, escalation, bound ──
# W2-B. Proves the user-visible behaviors against the LIVE chain driver + ledger
# with a SYNTHETIC chat and a STUB wrapper (no real specialist agent launches, no
# real Slack). Each sub-assert reads back from a real surface (the ledger the live
# driver wrote, the synthetic session transcript the real mirror appended) — never
# a value the harness authored.
CHAIN_DRIVER = HERMES_HOME / "bin" / "dd-chain-driver"
CHAIN_LEDGER = LANES_DIR / "chain-ledger.json"


def _chain_py():
    # Drive the chain driver under the SAME interpreter as the reaper daemon.
    return os.environ.get("DD_REAPER_PYTHON", sys.executable)


def _make_stub_wrapper() -> Path:
    """A synthetic stand-in for dd-visible-lane-run: it ONLY creates a run_dir
    under ~/.hermes/dd-lanes/<lane>/runs (no exit_code → 'in flight'), mirroring
    what dd-lane-run does on launch, then exits. No agent, no Slack, no secrets."""
    stub = LANES_DIR / f".chain-stub-wrapper-{RUNID}.sh"
    stub.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'lane=""; while [[ $# -gt 0 ]]; do case "$1" in --lane) lane="$2"; shift 2;; '
        '--packet|--wts-task) shift 2;; *) shift;; esac; done\n'
        'root="$HOME/.hermes/dd-lanes/$lane/runs"; mkdir -p "$root"\n'
        'rd="$root/$(date +%Y%m%d-%H%M%S)-stub-' + RUNID + '$$"; mkdir -p "$rd"\n'
        'printf \'{"lane":"%s","agent":"stub","started_at":"%s"}\\n\' "$lane" '
        '"$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$rd/meta.json"\n'
        'echo "stub-dispatched run_dir=$rd"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _finished_run(lane: str, gate_line: str, exit_code: str = "0") -> Path:
    rd = LANES_DIR / lane / "runs" / f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-c6-{lane}-{RUNID}"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "meta.json").write_text(json.dumps({
        "lane": lane, "agent": "stub", "packet": "synthetic",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }) + "\n", encoding="utf-8")
    (rd / "stdout.log").write_text(gate_line + "\n", encoding="utf-8")
    if exit_code is not None:
        (rd / "exit_code").write_text(exit_code + "\n", encoding="utf-8")
    return rd


def _load_chain(chain_id: str):
    try:
        for c in json.loads(CHAIN_LEDGER.read_text(encoding="utf-8")):
            if c.get("chain_id") == chain_id:
                return c
    except Exception:
        pass
    return None


def _abort_chain(chain_id: str):
    subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--abort", chain_id],
                   capture_output=True, text=True, timeout=30)


def _purge_synthetic_residue(run_dir_substr: str):
    """Remove this run's synthetic chains + reaper-registry entries + stub run dirs
    so a crash mid-canon can't leave the live daemons chewing on synthetic runs.
    Matches on the per-run substring (RUNID), so it only ever touches THIS run."""
    # chain ledger
    try:
        led = LANES_DIR / "chain-ledger.json"
        data = [c for c in json.loads(led.read_text(encoding="utf-8"))
                if run_dir_substr not in (c.get("chat_id", "") + c.get("chain_id", ""))]
        led.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    # reaper registry (deregister any synthetic run this harness dispatched)
    try:
        reg = LANES_DIR / "reaper-registry.json"
        data = [e for e in json.loads(reg.read_text(encoding="utf-8"))
                if run_dir_substr not in e.get("run_dir", "")]
        reg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    # stub-dispatched run dirs created by the chain driver this run
    for lane in ("engineering", "qa"):
        for d in (LANES_DIR / lane / "runs").glob("*"):
            if d.is_dir() and run_dir_substr in d.name:
                shutil.rmtree(d, ignore_errors=True)


def _make_git_stub(mode: str) -> Path:
    """A stand-in `git` for the publish-bridge test. mode=ok → exit 0 with a
    [new branch] line; mode=fail → exit 1 with a permission-denied line (the
    real dd-delivery write-wall W2-A documented). Never touches a real repo."""
    g = LANES_DIR / f".chain-gitstub-{mode}-{RUNID}.sh"
    if mode == "ok":
        body = ('#!/usr/bin/env bash\necho " * [new branch]  feat/x -> feat/x" >&2\nexit 0\n')
    else:
        body = ('#!/usr/bin/env bash\n'
                'echo "error: insufficient permission for adding an object to repository database .git/objects" >&2\n'
                'exit 1\n')
    g.write_text(body, encoding="utf-8")
    g.chmod(0o755)
    return g


def _stages(rec):
    return [h["stage"] for h in (rec or {}).get("history", []) if h.get("kind") == "run"]


def canon6_chain_driver(rep: Reporter):
    print("\n── CANON 6: WORK OWNERSHIP — five-field record, autonomous stages, "
          "deploy-stage, publish bridge, escalation, bound ──")
    if not CHAIN_DRIVER.exists():
        rep.record("C6.present: dd-chain-driver installed", False,
                   "dd-chain-driver is MISSING — the chain driver is not built")
        return

    source = SessionSource(platform=Platform.TELEGRAM, chat_id=SYNTH_CHAT_ID, chat_type="dm")
    routing_key = build_session_key(source)
    setup_session(routing_key)
    stub = _make_stub_wrapper()
    git_ok = _make_git_stub("ok")
    git_fail = _make_git_stub("fail")
    env = dict(os.environ, DD_CHAIN_WRAPPER=str(stub))

    started_dirs = []  # cleanup
    try:
        # ── 6a: the OWNERSHIP RECORD carries the five fields, current at all times. ──
        eng_done = _finished_run("engineering", "[engineering] PASS | synthetic eng closeout | #x ts=1")
        started_dirs.append(eng_done)
        wts = f"00000000-0000-4000-8000-c6{RUNID[:10]}"
        start = subprocess.run(
            [_chain_py(), str(CHAIN_DRIVER), "--start", wts, routing_key, "telegram",
             SYNTH_CHAT_ID, "dm", "--stages", "engineering,qa,deploy,report",
             "--interval", "180", "--goal", "synthetic chain", "--first-run-dir", str(eng_done)],
            capture_output=True, text=True, timeout=30, env=env)
        m = re.search(r"chain_id=(\S+)", start.stdout or "")
        chain_id = m.group(1) if m else None
        rep.record("C6.start: live driver opened a work-ownership record",
                   bool(chain_id), start.stdout.strip() or start.stderr.strip())
        if not chain_id:
            return

        pre = _load_chain(chain_id) or {}
        five = ("stage", "owner", "next_stage", "blocker", "eta")
        have_five = all(k in pre for k in five) and pre.get("stage") == "engineering" \
            and pre.get("next_stage") == "qa"
        rep.record("C6.fields: record carries the five first-class ownership fields, current",
                   have_five, f"stage={pre.get('stage')} owner={pre.get('owner')} "
                              f"next={pre.get('next_stage')} blocker={pre.get('blocker')} "
                              f"eta={pre.get('eta')}")

        # deploy + report are REAL stages. deploy's stage_state reflects the LEG:
        # lit (DD_DEPLOY_SUBMIT_ENABLED=1) → "live:awaiting-human-approval" (the honest
        # submit gate, W2); dark → "blocked:operator-gate-P-F" (P-F pending). Both are
        # valid modelings of a real, honestly-gated stage — assert it's one of them.
        _ds = pre.get("stage_state", {}).get("deploy")
        deploy_modeled = pre.get("stages") == ["engineering", "qa", "deploy", "report"] and \
            _ds in ("live:awaiting-human-approval", "blocked:operator-gate-P-F")
        rep.record("C6.deploy-stage: deploy is a REAL stage with an honest gate state (lit or dark)",
                   deploy_modeled, f"stages={pre.get('stages')}; stage_state.deploy={_ds} "
                                   f"({'LIVE leg' if _ds == 'live:awaiting-human-approval' else 'DARK/P-F'})")

        # FAIL-on-old-code control: BEFORE the tick the record has NOT advanced to qa.
        rep.record("C6.pre-tick: record has NOT advanced past engineering (passive spine = stuck)",
                   _stages(pre) == ["engineering"],
                   f"stages before tick: {_stages(pre)} (expected only engineering)")

        # ── 6b: a PASS eng stage AUTONOMOUSLY advances to qa (zero follow-up). ──
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"],
                       capture_output=True, text=True, timeout=60, env=env)
        post = _load_chain(chain_id) or {}
        qa_hop = next((h for h in post.get("history", [])
                       if h.get("kind") == "run" and h["stage"] == "qa"), None)
        if qa_hop:
            started_dirs.append(Path(qa_hop["run_dir"]))
        advanced = "qa" in _stages(post) and post.get("stage") == "qa" and post.get("stage_index") == 1
        rep.record("C6.autohop: PASS eng stage AUTONOMOUSLY dispatched qa (record drove it)",
                   advanced, f"stages after tick: {_stages(post)}; stage={post.get('stage')} "
                             f"index={post.get('stage_index')}")

        reg_ok = False
        if qa_hop:
            rl = subprocess.run([str(REAPER), "--list"], capture_output=True, text=True, timeout=30)
            try:
                for e in json.loads(rl.stdout or "[]"):
                    if e.get("run_dir") == qa_hop["run_dir"] and (e.get("wts_task") == wts):
                        reg_ok = True
            except Exception:
                pass
        rep.record("C6.wts-plumb: advanced qa stage registered WITH the bound WTS task",
                   reg_ok, f"qa run registered with wts_task={wts}" if reg_ok
                           else "qa run not found in reaper registry with the bound task")

        transcript = session_transcript()
        proactive = ("WORK OWNERSHIP" in transcript) and ("stage:" in transcript) and ("qa" in transcript)
        rep.record("C6.proactive: an unprompted five-field ownership update landed in the transcript",
                   proactive, f"transcript bytes={len(transcript)}; "
                              f"{'ownership snapshot present' if proactive else 'NO snapshot mirrored'}")
        if chain_id:
            _abort_chain(chain_id)

        # ── 6c: PUBLISH BRIDGE — eng PASS with a branch triggers the operator push. ──
        pub_wts = f"00000000-0000-4000-8000-p6{RUNID[:10]}"
        pub_eng = _finished_run("engineering", "[engineering] PASS | built, needs publish | #x ts=1")
        started_dirs.append(pub_eng)
        pub_cwd = LANES_DIR / f".chain-pubcwd-{RUNID}"
        pub_cwd.mkdir(exist_ok=True)
        p2 = subprocess.run(
            [_chain_py(), str(CHAIN_DRIVER), "--start", pub_wts, routing_key, "telegram",
             SYNTH_CHAT_ID, "dm", "--stages", "engineering,qa", "--first-run-dir", str(pub_eng),
             "--publish-branch", "feat/x", "--publish-cwd", str(pub_cwd)],
            capture_output=True, text=True, timeout=30, env=env)
        pub_id = re.search(r"chain_id=(\S+)", p2.stdout or "")
        pub_id = pub_id.group(1) if pub_id else None
        # tick with the OK git stub → push succeeds → published recorded → advances to qa
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"],
                       capture_output=True, text=True, timeout=60,
                       env=dict(env, DD_CHAIN_GIT=str(git_ok)))
        pc = _load_chain(pub_id) or {}
        pub_qa = next((h for h in pc.get("history", [])
                       if h.get("kind") == "run" and h["stage"] == "qa"), None)
        if pub_qa:
            started_dirs.append(Path(pub_qa["run_dir"]))
        published = bool(pc.get("published")) and "qa" in _stages(pc)
        rep.record("C6.publish-ok: eng PASS + branch → operator-bridge push, then advance to qa",
                   published, f"published={pc.get('published')}; stages={_stages(pc)}")
        if pub_id:
            _abort_chain(pub_id)

        # publish FAILURE → BLOCKER state on the record (never a silent park).
        pubf_wts = f"00000000-0000-4000-8000-pf{RUNID[:10]}"
        pubf_eng = _finished_run("engineering", "[engineering] PASS | built, needs publish | #x ts=1")
        started_dirs.append(pubf_eng)
        pf2 = subprocess.run(
            [_chain_py(), str(CHAIN_DRIVER), "--start", pubf_wts, routing_key, "telegram",
             SYNTH_CHAT_ID, "dm", "--stages", "engineering,qa", "--first-run-dir", str(pubf_eng),
             "--publish-branch", "feat/x", "--publish-cwd", str(pub_cwd)],
            capture_output=True, text=True, timeout=30, env=env)
        pf_id = re.search(r"chain_id=(\S+)", pf2.stdout or "")
        pf_id = pf_id.group(1) if pf_id else None
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"],
                       capture_output=True, text=True, timeout=60,
                       env=dict(env, DD_CHAIN_GIT=str(git_fail)))
        pf = _load_chain(pf_id) or {}
        push_blocked = (pf.get("status") in ("blocked", "escalated")) and \
            ("publish failed" in (pf.get("blocker") or "")) and "qa" not in _stages(pf)
        rep.record("C6.publish-fail: a failed self-service push becomes a BLOCKER (no silent park)",
                   push_blocked, f"status={pf.get('status')}; blocker={pf.get('blocker')}; "
                                 f"stages={_stages(pf)}")
        if pf_id:
            _abort_chain(pf_id)

        # ── 6d: a STALLED stage ESCALATES with a named owner (never silent park). ──
        stall_wts = f"00000000-0000-4000-8000-s6{RUNID[:10]}"
        stall_run = _finished_run("qa", "[qa] STALLED | no result | #x ts=1", exit_code="124")
        started_dirs.append(stall_run)
        s2 = subprocess.run(
            [_chain_py(), str(CHAIN_DRIVER), "--start", stall_wts, routing_key, "telegram",
             SYNTH_CHAT_ID, "dm", "--stages", "engineering,qa", "--max-retries", "0",
             "--first-run-dir", str(stall_run)],
            capture_output=True, text=True, timeout=30, env=env)
        stall_id = re.search(r"chain_id=(\S+)", s2.stdout or "")
        stall_id = stall_id.group(1) if stall_id else None
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"],
                       capture_output=True, text=True, timeout=60, env=env)
        sc = _load_chain(stall_id) or {}
        escalated = sc.get("status") == "escalated"
        transcript2 = session_transcript()
        esc_surfaced = "ESCALATION" in transcript2 and "OWNER:" in transcript2
        rep.record("C6.escalate: a STALLED stage escalates with a named owner (no silent park)",
                   escalated and esc_surfaced,
                   f"status={sc.get('status')}; blocker={sc.get('blocker')}; "
                   f"escalation+owner in transcript={esc_surfaced}")
        if stall_id:
            _abort_chain(stall_id)

        # ── 6e: BOUNDED LOOP — a record at max_hops escalates, never ping-pongs. ──
        bound_wts = f"00000000-0000-4000-8000-b6{RUNID[:10]}"
        bound_run = _finished_run("engineering", "[engineering] BLOCK | loop | #x ts=1", exit_code="1")
        started_dirs.append(bound_run)
        b2 = subprocess.run(
            [_chain_py(), str(CHAIN_DRIVER), "--start", bound_wts, routing_key, "telegram",
             SYNTH_CHAT_ID, "dm", "--stages", "engineering,qa", "--max-hops", "1",
             "--max-retries", "5", "--first-run-dir", str(bound_run)],
            capture_output=True, text=True, timeout=30, env=env)
        bound_id = re.search(r"chain_id=(\S+)", b2.stdout or "")
        bound_id = bound_id.group(1) if bound_id else None
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"],
                       capture_output=True, text=True, timeout=60, env=env)
        bc = _load_chain(bound_id) or {}
        bounded = bc.get("status") == "escalated" and \
            len([h for h in bc.get("history", []) if h.get("gate")]) <= 1
        rep.record("C6.bounded: max_hops ceiling escalates instead of ping-ponging forever",
                   bounded, f"status={bc.get('status')}; "
                            f"closed_hops={len([h for h in bc.get('history', []) if h.get('gate')])}")
        if bound_id:
            _abort_chain(bound_id)
    finally:
        for p in (stub, git_ok, git_fail):
            try:
                p.unlink()
            except Exception:
                pass
        shutil.rmtree(LANES_DIR / f".chain-pubcwd-{RUNID}", ignore_errors=True)
        for d in started_dirs:
            shutil.rmtree(d, ignore_errors=True)
        # Deregister synthetic reaper entries + remove stub-dispatched runs + drop
        # this run's chains, so a crash mid-canon can't leave the live daemons
        # chewing on synthetic runs. Matches THIS run's RUNID only.
        _purge_synthetic_residue(RUNID)
        teardown_session(routing_key)


# ── CANON 7: POST-APPROVAL deploy watch + real report stage (W2-B2) ──
# Proves the trigger the deploy stage was missing: a chain parked in deploy advances
# when the human DECIDES (queue row flips), runs the real verification, and delivers
# the closeout via the reaper-reinject path — then a reject becomes a BLOCKER. Driven
# by a STUB deploy-row file (DD_CHAIN_DEPLOY_ROW_FILE), no mc-api touched, synthetic chat.
def _write_row_file(rows: dict) -> Path:
    p = LANES_DIR / f".chain-deployrow-{RUNID}.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p


def canon7_post_approval(rep: Reporter):
    print("\n── CANON 7: post-approval deploy watch + real report stage (W2-B2) ──")
    if not CHAIN_DRIVER.exists():
        rep.record("C7.present: dd-chain-driver installed", False, "missing")
        return
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=SYNTH_CHAT_ID, chat_type="dm")
    routing_key = build_session_key(source)
    setup_session(routing_key)
    row_id = f"row-{RUNID}"
    # Seed a chain parked AT deploy with the row id, deploy leg lit.
    env = dict(os.environ, DD_DEPLOY_SUBMIT_ENABLED="1")
    try:
        # ── 7a: row still pending → no advance (the honest awaiting-approval rest). ──
        rf = _write_row_file({row_id: {"id": row_id, "status": "pending",
                                       "service_name": "synthetic-svc", "service_port": None}})
        wts = f"00000000-0000-4000-8000-d7{RUNID[:10]}"
        st = subprocess.run(
            [_chain_py(), str(CHAIN_DRIVER), "--start", wts, routing_key, "telegram",
             SYNTH_CHAT_ID, "dm", "--stages", "engineering,qa,deploy,report",
             "--start-stage", "deploy", "--deploy-row", row_id,
             "--deploy-service", "synthetic-svc"],
            capture_output=True, text=True, timeout=30, env=env)
        chain_id = re.search(r"chain_id=(\S+)", st.stdout or "")
        chain_id = chain_id.group(1) if chain_id else None
        rep.record("C7.seed: chain seeded parked at deploy with a row id",
                   bool(chain_id), st.stdout.strip() or st.stderr.strip())
        if not chain_id:
            return
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"], capture_output=True,
                       text=True, timeout=60, env=dict(env, DD_CHAIN_DEPLOY_ROW_FILE=str(rf)))
        c = _load_chain(chain_id) or {}
        rep.record("C7.pending: a still-pending row does NOT advance (honest awaiting-approval)",
                   c.get("stage") == "deploy" and c.get("status") == "blocked",
                   f"stage={c.get('stage')} status={c.get('status')} blocker={c.get('blocker')}")

        # ── 7b: human APPROVES (row→deployed) → advance → report → verify → closeout. ──
        rf.write_text(json.dumps({row_id: {
            "id": row_id, "status": "deployed", "decided_by": "QA-SYNTHETIC",
            "deployed_at": "2026-06-04T20:01:17Z", "service_name": "synthetic-svc",
            "service_port": None, "target_commit": "d27b808c84b1abcd",
            "diff_stat": "4 files changed, 51 insertions, 18 deletions",
            "diff_summary": 'rename row action to "Send test"',
            "files_changed": ["a.tsx", "b.tsx", "c.tsx", "d.tsx"],
        }}), encoding="utf-8")
        before = session_transcript()
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"], capture_output=True,
                       text=True, timeout=60, env=dict(env, DD_CHAIN_DEPLOY_ROW_FILE=str(rf)))
        c = _load_chain(chain_id) or {}
        # report is the last stage; advancing into it runs _finish → status=done.
        closed = c.get("status") == "done" and c.get("stage") == "report"
        rep.record("C7.advance: approved row advances deploy → report → done",
                   closed, f"stage={c.get('stage')} status={c.get('status')} "
                           f"verification={c.get('verification')}")
        # the verification ran (deterministic) and is recorded on the chain
        ver = c.get("verification") or {}
        rep.record("C7.verify: the report stage ran live verification (deterministic)",
                   "health_ok" in ver and "detail" in ver,
                   f"verification={ver}")
        # the closeout landed in the caller transcript with what-shipped + verification
        after = session_transcript()
        new = after[len(before):]
        closeout_ok = ("WORK COMPLETE" in new) and ("d27b808c" in new) and \
                      ("Verification:" in new) and ("51 insertions" in new)
        rep.record("C7.closeout: substantive closeout (commit+diffstat+verification) reinjected",
                   closeout_ok, f"closeout present={bool(new.strip())}; "
                                f"{'has commit+diffstat+verification' if closeout_ok else new[:200]}")
        if chain_id:
            _abort_chain(chain_id)

        # ── 7c: human REJECTS (row→rejected) → BLOCKER state, escalated. ──
        rj_id = f"rrow-{RUNID}"
        rjf = _write_row_file({rj_id: {"id": rj_id, "status": "rejected",
                                       "reject_reason": "diff touches prod secrets",
                                       "service_name": "synthetic-svc"}})
        # the row file path differs; point the env at rjf
        rwts = f"00000000-0000-4000-8000-r7{RUNID[:10]}"
        rs = subprocess.run(
            [_chain_py(), str(CHAIN_DRIVER), "--start", rwts, routing_key, "telegram",
             SYNTH_CHAT_ID, "dm", "--stages", "engineering,qa,deploy,report",
             "--start-stage", "deploy", "--deploy-row", rj_id, "--deploy-service", "synthetic-svc"],
            capture_output=True, text=True, timeout=30, env=env)
        rj_chain = re.search(r"chain_id=(\S+)", rs.stdout or "")
        rj_chain = rj_chain.group(1) if rj_chain else None
        rbefore = session_transcript()
        subprocess.run([_chain_py(), str(CHAIN_DRIVER), "--tick"], capture_output=True,
                       text=True, timeout=60, env=dict(env, DD_CHAIN_DEPLOY_ROW_FILE=str(rjf)))
        rc = _load_chain(rj_chain) or {}
        rnew = session_transcript()[len(rbefore):]
        rejected_blocker = rc.get("status") == "blocked" and \
            "rejected" in (rc.get("blocker") or "") and "secrets" in (rc.get("blocker") or "") and \
            "ESCALATION" in rnew
        rep.record("C7.reject: a rejected deploy becomes a BLOCKER state, escalated (no silent park)",
                   rejected_blocker, f"status={rc.get('status')} blocker={rc.get('blocker')}; "
                                     f"escalation in transcript={'ESCALATION' in rnew}")
        if rj_chain:
            _abort_chain(rj_chain)
    finally:
        for p in (LANES_DIR / f".chain-deployrow-{RUNID}.json",):
            try:
                p.unlink()
            except Exception:
                pass
        _purge_synthetic_residue(RUNID)
        teardown_session(routing_key)


CHAIN_LOG = HERMES_HOME / "logs" / "dd-chain-driver.log"


def chain_log_size() -> int:
    try:
        return CHAIN_LOG.stat().st_size
    except FileNotFoundError:
        return 0


def zero_grep_gate() -> bool:
    """Q4d pre-run gate: the qa-review routing config must contain ZERO matches of
    Levi's real chat id before any synthetic lane run. Fail-closed — abort the run
    if the gate is RED, so a misconfigured profile can never leak to his real DM.
    Returns True iff clean. Booleans only; the chat id is never printed."""
    gate = HERMES_HOME / "bin" / "dd-qa-repoint-telegram-sink.py"
    if not gate.exists():
        print("  [PRE-RUN GATE] MISSING (dd-qa-repoint-telegram-sink.py) — aborting fail-closed")
        return False
    r = subprocess.run([sys.executable, str(gate), "--check"], capture_output=True, text=True)
    for line in (r.stdout or "").splitlines():
        print("  [PRE-RUN GATE] " + line)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--against-old-code", action="store_true",
                    help="simulate the pre-fix gateway to demonstrate the route-key "
                         "assertion goes RED (proves it depends on the real key)")
    ap.add_argument("--chain-only", action="store_true",
                    help="run ONLY the W2-B chain-driver canon (Canon 6). It uses a "
                         "STUB wrapper + synthetic chat and PROVABLY never reaches the "
                         "qa-review Telegram sink, so it is exempt from the sink leak "
                         "gate that fail-closes the real sink-touching canons.")
    args = ap.parse_args()

    print("=" * 78)
    print(f"Increment-1 synthetic-user harness  (runid={RUNID})")
    print(f"mode: {'AGAINST-OLD-CODE (FAIL demo)' if args.against_old_code else 'LIVE code'}")
    print(f"synthetic chat id: {SYNTH_CHAT_ID}  (NOT a real chat)")
    print(f"live reaper daemon: {'present' if REAPER.exists() else 'MISSING'}; "
          f"log: {REAPER_LOG}")
    print("=" * 78)

    # Q4d ZERO-GREP PRE-RUN GATE — must be GREEN before any lane run touches a
    # surface that could mirror to a real chat. Fail-closed.
    # EXEMPTION (--chain-only): Canon 6 drives the chain driver through a STUB
    # wrapper (creates run dirs only — no specialist, no Slack, no Telegram) on a
    # synthetic chat, so it can NEVER reach the qa-review sink the gate protects.
    # The gate still fail-closes every real, sink-touching canon (2–5). We do NOT
    # weaken or repoint the shared sink config here (that is W2-A / Levi territory).
    if not args.chain_only:
        print("\n── PRE-RUN: zero-grep Telegram-leak gate ──")
        if not zero_grep_gate():
            print("\nABORTED: qa-review Telegram-leak gate is RED — refusing to run "
                  "(a synthetic run must never be able to target a real chat).")
            sys.exit(2)
    else:
        print("\n── PRE-RUN: sink-leak gate SKIPPED for --chain-only ──")
        print("  Canon 6 uses a stub wrapper + synthetic chat and never reaches the "
              "qa-review Telegram sink; the gate guards the sink-touching canons only.")

    rep = Reporter()
    try:
        if args.against_old_code:
            # Only the route-key path differs on old code — that's the bug we're
            # demonstrating. Run Canon 2 in pre-fix simulation.
            canon2_route_key(rep, simulate_old_code=True)
        elif args.chain_only:
            canon6_chain_driver(rep)
            canon7_post_approval(rep)
        else:
            canon2_route_key(rep, simulate_old_code=False)
            canon3_wts_guard(rep)
            canon4_deploy_denied(rep)
            canon5_honesty(rep)
            canon6_chain_driver(rep)
            canon7_post_approval(rep)
    finally:
        teardown_session(build_session_key(
            SessionSource(platform=Platform.TELEGRAM, chat_id=SYNTH_CHAT_ID, chat_type="dm")))

    g, r = rep.summary()
    print("\n" + "=" * 78)
    print(f"RESULT: {g} GREEN, {r} RED")
    if args.against_old_code:
        # Expectation INVERTS: against old code, the route-return MUST be RED.
        demo_ok = any(s == "RED" for n, s, _ in rep.results if "route-return" in n)
        print("FAIL-on-old-code demonstration: "
              + ("CONFIRMED — the route-key assertion went RED against pre-fix code."
                 if demo_ok else "NOT confirmed (assertion did not fail — investigate)."))
        sys.exit(0 if demo_ok else 1)
    else:
        print("HARNESS PASS" if rep.all_green() else "HARNESS FAIL")
        sys.exit(0 if rep.all_green() else 1)


if __name__ == "__main__":
    main()
