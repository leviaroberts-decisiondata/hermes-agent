"""route_to_lane — first-class P1→specialist-lane handoff tool (Phase A).

Why this exists (WTS f875d95c, punch-list aa752852):
  P1 used to hand-shell the lane wrapper with the WRONG syntax
  (`dd-visible-lane-run qa <packet>`), which the wrapper rejected with
  `unknown arg: qa` (exit 2) — yet P1 reported "now sending it through the
  wrapper" as if the handoff succeeded. The QA lane never ran.

This tool fixes the three Phase-A issues:
  I1  — emit the CORRECT invocation: `dd-visible-lane-run --lane <lane>
        --packet <path> [--wts-task <id>]`. P1 calls this tool; it never
        hand-builds wrapper args, so the positional-arg break is impossible.
  I2  — verify honestly: capture the wrapper exit_code AND the normalized
        status line it prints. Report SUCCESS only when exit==0 AND the lane
        actually ran and returned a result line. On non-zero exit / no result,
        return a FAILED result with the captured stderr — never a false success.
  I3  — attach to the ACTIVE bound task: pass `--wts-task <id>` through to the
        wrapper so the handoff artifact lands on the thread's active WTS task;
        the per-lane standing "lane log" bucket is only a loud last-resort
        (the wrapper logs a WARN when it falls back).

Read-only on comms.db is fine; this tool does not write Slack/DB directly — all
posting/attachment happens inside the sanctioned wrapper (dd-slack-service API).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from tools.registry import registry, tool_error

_HERMES_HOME = Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
# The wrapper + lane config live under the SHARED ~/.hermes (home-anchored),
# not a per-profile HERMES_HOME — mirror how the context tree resolves.
_SHARED_HOME = Path.home() / ".hermes"
_WRAPPER = _SHARED_HOME / "bin" / "dd-visible-lane-run"
_CHANNELS_JSON = _SHARED_HOME / "dd-lanes" / "channels.json"
_LANE_DIR = _SHARED_HOME / "dd-lanes"

# Wrapper exit codes that mean "the lane never ran" (arg/config errors), vs a
# run that executed but the specialist returned a non-zero gate.
_WRAPPER_PREFLIGHT_CODES = {2, 3, 5, 6, 7, 8, 10}
# exit 75 (EX_TEMPFAIL) = the lane RAN but is still in flight (detached); the
# run survives and is collectable later via `dd-lane-run --poll <run_dir>`.
# This is an HONEST PENDING — neither success nor hard failure.
_WRAPPER_PENDING_CODE = 75


def _known_lanes() -> list[str]:
    try:
        cfg = json.loads(_CHANNELS_JSON.read_text(encoding="utf-8"))
        return sorted((cfg.get("lanes") or {}).keys())
    except Exception:
        return []


def check_route_to_lane_requirements() -> bool:
    """Gate: available only when the sanctioned wrapper + lane config exist.

    Returns a BOOLEAN — the registry's get_definitions() filters the tool out
    unless this returns True (a None/str return would be treated as falsy and
    silently drop the tool from the model's tool list).
    """
    if not _WRAPPER.exists() or not os.access(_WRAPPER, os.X_OK):
        return False
    if not _CHANNELS_JSON.exists():
        return False
    return True


def _write_packet(lane: str, goal: str, context: Optional[str], wts_task: Optional[str]) -> Path:
    """Materialize the handoff packet the wrapper sends to the lane.

    Includes a `WTS: <id>` line when an active task is supplied so the durable
    record is unambiguous even if the --wts-task flag path changes.
    """
    safe_lane = "".join(c for c in lane if c.isalnum() or c in "-_") or "lane"
    out_dir = _LANE_DIR / safe_lane
    out_dir.mkdir(parents=True, exist_ok=True)
    title = (goal or "").strip().splitlines()[0][:80] if goal else f"{lane} lane handoff"
    lines = [f"# DD P1 -> {lane} lane handoff: {title}", ""]
    if wts_task:
        lines.append(f"WTS: {wts_task}")
        lines.append("")
    lines.append("## Goal")
    lines.append((goal or "").strip())
    if context and context.strip():
        lines.append("")
        lines.append("## Context")
        lines.append(context.strip())
    # Deterministic-ish filename; timestamp is supplied by the caller turn, not here.
    fname = f"{safe_lane}-handoff.md"
    packet = out_dir / fname
    packet.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return packet


def route_to_lane(
    lane: str,
    goal: str,
    context: Optional[str] = None,
    wts_task: Optional[str] = None,
    packet: Optional[str] = None,
    parent_agent=None,
) -> str:
    """Route a unit of work to a specialist lane via the sanctioned visible wrapper.

    Returns a concise, HONEST status string. Never reports success unless the
    lane actually ran and returned a result.
    """
    lane = (lane or "").strip()
    if not lane:
        return tool_error("route_to_lane: 'lane' is required (e.g. qa, design, engineering).")
    known = _known_lanes()
    if known and lane not in known:
        return tool_error(
            f"route_to_lane: unknown lane '{lane}'. Known lanes: {', '.join(known)}."
        )

    # WS8 §4 (the active-task FEED): when the caller did not pass wts_task,
    # DEFAULT it to the thread's bound id carried into the turn from the Slack
    # anchor (dd-slack-service stamps X-DD-WTS-Task-Id → the gateway exposes it
    # as parent_agent._dd_wts_task_id). This closes I3 WITHOUT P1 remembering the
    # id. An EXPLICIT wts_task arg always wins (the model may override). Neither
    # present → empty → the wrapper fails honest (no bucket). Default-OFF behind
    # ROUTE_TO_LANE_WTS_FEED=1 so the legacy (caller-supplied-only) path is
    # byte-identical until the Phase-B cutover.
    wts_source = "explicit" if (wts_task and wts_task.strip()) else "none"
    if (not (wts_task or "").strip()) and os.getenv("ROUTE_TO_LANE_WTS_FEED") == "1":
        bound = getattr(parent_agent, "_dd_wts_task_id", None)
        if bound and str(bound).strip():
            wts_task = str(bound).strip()
            wts_source = "anchor-feed"

    # Resolve / build the packet.
    if packet:
        packet_path = Path(packet).expanduser()
        if not packet_path.is_file():
            return tool_error(f"route_to_lane: packet not found: {packet_path}")
    else:
        if not (goal or "").strip():
            return tool_error("route_to_lane: provide 'goal' (or an explicit 'packet' path).")
        packet_path = _write_packet(lane, goal, context, wts_task)

    # I1: build the CORRECT invocation — flags, never positional.
    cmd = [str(_WRAPPER), "--lane", lane, "--packet", str(packet_path)]
    if wts_task and wts_task.strip():
        cmd += ["--wts-task", wts_task.strip()]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return tool_error(
            f"route_to_lane: handoff to '{lane}' TIMED OUT (>900s). The lane did NOT "
            f"confirm completion — do NOT report this as done. Packet: {packet_path}"
        )
    except Exception as exc:
        return tool_error(f"route_to_lane: failed to invoke wrapper: {exc}")

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    code = proc.returncode

    # I2: HONEST verification.
    # The wrapper prints exactly one normalized status line on success:
    #   [<lane>] <GATE> | <oneline> | #<channel> ts=<...> ...
    # Success requires BOTH exit==0 AND that status line present.
    ran_ok = code == 0 and out.startswith(f"[{lane}]")
    if ran_ok:
        # Surface any non-fatal delivery warnings (e.g. I3 bucket fallback) loudly.
        warn = ""
        if err:
            # keep only WARN/warn lines so P1 sees the bucket-fallback notice
            warn_lines = [l for l in err.splitlines() if "warn" in l.lower() or "WARN" in l]
            if warn_lines:
                warn = "\n[handoff warnings]\n" + "\n".join(warn_lines[:8])
        return (
            f"HANDOFF OK — routed to '{lane}' lane and the lane returned.\n"
            f"{out}"
            f"{warn}"
        )

    # PENDING — the lane RAN and is still in flight (detached); honest middle
    # state. NOT success (do not report done), NOT a hard failure.
    if code == _WRAPPER_PENDING_CODE:
        return tool_error(
            f"HANDOFF PENDING — the '{lane}' lane started and is still running "
            f"(detached). Do NOT report this as completed; poll for the result "
            f"before reconciling.\n"
            f"packet: {packet_path}\n"
            f"{out or '(no status line)'}"
        )

    # FAILED — be explicit; the lane did NOT complete a real run.
    if code in _WRAPPER_PREFLIGHT_CODES:
        reason = "the wrapper rejected the call before the lane ran (arg/config error)"
    elif code != 0:
        reason = "the lane ran but exited non-zero / did not return a clean result"
    else:
        reason = "the wrapper exited 0 but printed no normalized status line (lane did not confirm)"
    return tool_error(
        f"HANDOFF FAILED — did NOT reach a completed '{lane}' lane run; {reason} "
        f"(exit={code}). Do NOT report this handoff as succeeded.\n"
        f"packet: {packet_path}\n"
        f"stdout: {out or '(empty)'}\n"
        f"stderr: {err[:1500] or '(empty)'}"
    )


ROUTE_TO_LANE_SCHEMA = {
    "name": "route_to_lane",
    "description": (
        "Hand a unit of work to a DecisionData specialist LANE (e.g. qa, design, "
        "engineering) and get its result back — the sanctioned, VISIBLE handoff. "
        "Use this instead of running the work yourself or shelling the lane wrapper "
        "by hand. It emits the correct wrapper invocation, runs the lane "
        "synchronously, posts the visible handoff+result to the lane's Slack thread, "
        "attaches BOTH the handoff (REQUEST) and the result (RESPONSE) to the ACTIVE "
        "bound WTS task (precedence: wts_task arg > packet 'WTS:' line > the thread's "
        "anchor-bound id auto-fed into the turn > none; there is NO per-lane bucket on "
        "the Slack path), and returns an HONEST status — it reports FAILED if the lane "
        "did not run. "
        "NEVER tell the user a handoff succeeded unless this tool returns HANDOFF OK."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lane": {
                "type": "string",
                "description": "Specialist lane to route to (e.g. 'qa', 'design', 'engineering'). Must be a lane defined in dd-lanes/channels.json.",
            },
            "goal": {
                "type": "string",
                "description": "What the lane should accomplish — self-contained; the lane does not see your conversation. Becomes the handoff packet.",
            },
            "context": {
                "type": "string",
                "description": "Background the lane needs: paths, prior decisions, acceptance criteria, constraints.",
            },
            "wts_task": {
                "type": "string",
                "description": "The thread's ACTIVE bound WTS task id. Usually you can OMIT this on Slack-originated work — the thread's bound id is fed into the turn automatically and used as the default. Pass it explicitly only to override (e.g. a governance/Telegram handoff where you created the WTS task yourself). When neither is present the handoff is NOT attached to a durable record (no bucket).",
            },
            "packet": {
                "type": "string",
                "description": "Optional: path to a pre-written handoff packet .md. When given, goal/context are ignored and this file is sent verbatim.",
            },
        },
        "required": ["lane"],
    },
}


registry.register(
    name="route_to_lane",
    toolset="delegation",
    schema=ROUTE_TO_LANE_SCHEMA,
    handler=lambda args, **kw: route_to_lane(
        lane=args.get("lane"),
        goal=args.get("goal"),
        context=args.get("context"),
        wts_task=args.get("wts_task"),
        packet=args.get("packet"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_route_to_lane_requirements,
    emoji="🛤️",
)
