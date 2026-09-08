"""Read-only P1 engineering lane capacity selector + recovery reconciliation gate.

This module intentionally derives state from existing lane run directories. It does
not create or update any availability ledger and it does not mutate reaper state.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from tools.registry import registry, tool_error

def _decisiondata_shared_home() -> Path:
    configured = os.getenv("DD_SHARED_HERMES_HOME")
    if configured:
        return Path(configured)
    canonical = Path("/Users/openclaw/.hermes")
    if canonical.exists():
        return canonical
    return Path.home() / ".hermes"


_SHARED_HOME = _decisiondata_shared_home()
_DEFAULT_LANES_ROOT = _SHARED_HOME / "dd-lanes"
_ENGINEERING_LANES = (
    ("engineering", "dd-engineer-1", "Engineer 1"),
    ("engineering-2", "dd-engineer-2", "Engineer 2"),
    ("engineering-3", "dd-engineer-3", "Engineer 3"),
)
_FAILURE_GATES = {"FAIL", "FAILED", "BLOCK", "BLOCKED", "ERROR", "STALLED", "NEEDS-YOU", "NEEDSYOU"}
_SUCCESS_GATES = {"PASS", "WARN"}


@dataclass
class RunState:
    lane: str
    agent: str
    label: str
    run_id: Optional[str]
    run_dir: Optional[str]
    status: str
    exit_code: Optional[int] = None
    gate: Optional[str] = None
    has_publish_ready: bool = False
    has_conflict: bool = False
    canonical: str = "run_dir_facts"
    reasons: tuple[str, ...] = ()
    age_seconds: Optional[int] = None
    pid: Optional[int] = None
    pid_alive: Optional[bool] = None
    wts_task: Optional[str] = None
    surface_match: bool = False


@dataclass
class PoolSelection:
    status: str
    selected_lane: Optional[str]
    selected_agent: Optional[str]
    reason: str
    lanes: list[RunState]


@dataclass
class RecoveryDecision:
    recommendation: str
    reason: str
    matches: list[RunState]


def _read_text(path: Path, max_chars: int = 80_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def _read_first_existing(run_dir: Path, names: tuple[str, ...]) -> str:
    for name in names:
        text = _read_text(run_dir / name)
        if text.strip():
            return text
    return ""


def _parse_exit_code(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"(?:exit_code\s*=\s*)?(-?\d+)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _parse_pid(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"(?:pid\s*=\s*)?(\d+)", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _pid_alive(pid: Optional[int]) -> Optional[bool]:
    if not pid:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _parse_gate(text: str) -> Optional[str]:
    if not text:
        return None
    patterns = (
        r"\bGate\s*:\s*([A-Za-z_-]+)",
        r"\bgate\s*:\s*([A-Za-z_-]+)",
        r"\bGATE\s*=\s*([A-Za-z_-]+)",
        r"\[(?:engineering|engineering-2|engineering-3)\]\s+([A-Za-z_-]+)\b",
    )
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).upper().replace("-", "_")
    return None


def _extract_wts(text: str) -> Optional[str]:
    m = re.search(r"\bWTS\s*:\s*([0-9a-fA-F-]{36})\b", text or "", re.I)
    if not m:
        m = re.search(r"\bwts_task(?:_id)?[=:]\s*([0-9a-fA-F-]{36})\b", text or "", re.I)
    return m.group(1).lower() if m else None


def _combined_run_text(run_dir: Path) -> str:
    chunks = []
    for name in ("packet.md", "handoff.md", "request.md", "stdout.log", "result.md", "reaped"):
        text = _read_text(run_dir / name)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _run_started_at(run_dir: Path) -> Optional[float]:
    """Return the original run start time when lane metadata recorded it.

    Reaper marker writes mutate the run directory mtime. Capacity decisions must
    not treat a marker rewrite as a fresh run, or alive over-budget work can be
    misclassified as newly busy and route duplicate recovery to the wrong lane.
    """
    text = _read_text(run_dir / "meta.json")
    if text:
        try:
            meta = json.loads(text)
            value = meta.get("started_at") or meta.get("created_at")
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str) and value.strip():
                raw = value.strip()
                try:
                    return float(raw)
                except ValueError:
                    pass
                from datetime import datetime, timezone

                iso = raw.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(iso)
                except ValueError:
                    dt = None
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
        except Exception:
            pass
    try:
        return run_dir.stat().st_mtime
    except Exception:
        return None


def _latest_run_dir(lanes_root: Path, lane: str) -> Optional[Path]:
    runs = lanes_root / lane / "runs"
    if not runs.is_dir():
        return None
    children = [p for p in runs.iterdir() if p.is_dir()]
    if not children:
        return None
    return max(children, key=lambda p: (p.name, p.stat().st_mtime))


def classify_run_dir(
    run_dir: Path | str,
    *,
    lane: str = "engineering",
    agent: str = "dd-engineer-1",
    label: str = "Engineer 1",
    now: Optional[float] = None,
    soft_budget_seconds: int = 20 * 60,
) -> RunState:
    run_dir = Path(run_dir)
    now = time.time() if now is None else now
    reasons: list[str] = []

    exit_text = _read_first_existing(run_dir, ("exit_code", "exit_code.txt"))
    reaped_text = _read_text(run_dir / "reaped")
    stdout_text = _read_text(run_dir / "stdout.log")
    combined = _combined_run_text(run_dir)
    exit_code = _parse_exit_code(exit_text)
    reaped_exit = _parse_exit_code(reaped_text)
    pid = _parse_pid(_read_first_existing(run_dir, ("pid", "pid.txt")))
    alive = _pid_alive(pid)
    gate = _parse_gate(stdout_text) or _parse_gate(reaped_text)
    has_publish_ready = "PUBLISH-READY:" in stdout_text or "PUBLISH-READY:" in combined
    stale_synthetic_stall = (
        bool(re.search(r"\bmode\s*=\s*stalled\b", reaped_text, re.I))
        or reaped_exit == 124
        or (run_dir / "STALLED").exists()
    )
    actual_success = exit_code == 0 or gate in _SUCCESS_GATES or has_publish_ready
    has_conflict = stale_synthetic_stall and actual_success

    started_at = _run_started_at(run_dir)
    age_seconds = max(0, int(now - started_at)) if started_at is not None else None

    canonical = "run_dir_facts"
    if has_conflict:
        canonical = "actual_exit_stdout"
        reasons.append("stale stalled/124 marker contradicted by actual exit/stdout; actual result is canonical")

    if exit_code is not None:
        if exit_code == 0 or gate in _SUCCESS_GATES or has_publish_ready:
            status = "idle"
            reasons.append("actual exit/stdout indicates completed result")
        elif stale_synthetic_stall and not actual_success:
            status = "busy_or_suspect"
            reasons.append("terminal stalled marker without confirming actual completion")
        else:
            status = "idle"
            reasons.append(f"exit_code={exit_code} present")
    elif pid is not None:
        if alive is True:
            if age_seconds is not None and age_seconds > soft_budget_seconds:
                status = "over_budget"
                reasons.append("pid alive with no exit_code and over soft budget")
            else:
                status = "busy"
                reasons.append("pid present/alive with no exit_code")
        elif alive is False:
            status = "busy_or_suspect"
            reasons.append("pid is dead/missing but no exit_code exists")
        else:
            status = "busy_or_suspect"
            reasons.append("pid state unknown and no exit_code exists")
    elif stale_synthetic_stall:
        status = "busy_or_suspect"
        reasons.append("stalled marker exists without actual exit_code")
    elif not run_dir.exists():
        status = "idle"
        reasons.append("no run directory")
    else:
        # A run dir without a final result can still be newly materialized before
        # pid/exit files are written. Do not count it as idle silently.
        status = "busy_or_suspect"
        reasons.append("run dir lacks final exit/result facts")

    return RunState(
        lane=lane,
        agent=agent,
        label=label,
        run_id=run_dir.name if run_dir.exists() else None,
        run_dir=str(run_dir) if run_dir.exists() else None,
        status=status,
        exit_code=exit_code,
        gate=gate,
        has_publish_ready=has_publish_ready,
        has_conflict=has_conflict,
        canonical=canonical,
        reasons=tuple(reasons),
        age_seconds=age_seconds,
        pid=pid,
        pid_alive=alive,
        wts_task=_extract_wts(combined),
    )


def inspect_engineering_pool(*, lanes_root: Path | str = _DEFAULT_LANES_ROOT, now: Optional[float] = None) -> list[RunState]:
    lanes_root = Path(lanes_root)
    states: list[RunState] = []
    for lane, agent, label in _ENGINEERING_LANES:
        run_dir = _latest_run_dir(lanes_root, lane)
        if run_dir is None:
            states.append(RunState(lane=lane, agent=agent, label=label, run_id=None, run_dir=None, status="idle", reasons=("no runs",)))
        else:
            states.append(classify_run_dir(run_dir, lane=lane, agent=agent, label=label, now=now))
    return states


def select_engineering_lane(
    *,
    lanes_root: Path | str = _DEFAULT_LANES_ROOT,
    preferred_lane: Optional[str] = None,
    affinity_reason: Optional[str] = None,
    now: Optional[float] = None,
) -> PoolSelection:
    states = inspect_engineering_pool(lanes_root=lanes_root, now=now)
    by_lane = {s.lane: s for s in states}
    affinity_reason = (affinity_reason or "").strip()

    if preferred_lane:
        preferred = by_lane.get(preferred_lane)
        if preferred is None:
            return PoolSelection("invalid_preference", None, None, f"Unknown preferred engineering lane: {preferred_lane}", states)
        if preferred.status == "idle":
            return PoolSelection("selected", preferred.lane, preferred.agent, f"{preferred.label} is idle; selected preferred lane.", states)
        if affinity_reason:
            return PoolSelection(
                "selected_with_affinity",
                preferred.lane,
                preferred.agent,
                f"Kept with {preferred.label} despite active/suspect work because: {affinity_reason}",
                states,
            )
        return PoolSelection(
            "affinity_required",
            None,
            None,
            f"{preferred.label} is {preferred.status}; same-active-engineer reuse requires an explicit affinity reason.",
            states,
        )

    idle = [s for s in states if s.status == "idle"]
    if idle:
        selected = idle[0]
        busy_before = [s for s in states[: states.index(selected)] if s.status != "idle"]
        if busy_before:
            observed = "; ".join(f"{s.label} has unresolved run {s.run_id or '(unknown)'} ({s.status})" for s in busy_before)
            reason = f"{observed}; {selected.label} idle."
        else:
            reason = f"{selected.label} idle."
        return PoolSelection("selected", selected.lane, selected.agent, reason, states)

    return PoolSelection(
        "no_capacity",
        None,
        None,
        "All engineering lanes are active/suspect; return no-capacity/queue rather than spawning duplicate work.",
        states,
    )


def _iter_recent_run_dirs(lanes_root: Path, limit_per_lane: int = 10) -> list[tuple[str, str, str, Path]]:
    out: list[tuple[str, str, str, Path]] = []
    for lane, agent, label in _ENGINEERING_LANES:
        runs = lanes_root / lane / "runs"
        if not runs.is_dir():
            continue
        children = sorted([p for p in runs.iterdir() if p.is_dir()], key=lambda p: (p.name, p.stat().st_mtime), reverse=True)
        for child in children[:limit_per_lane]:
            out.append((lane, agent, label, child))
    return sorted(out, key=lambda item: (item[3].name, item[3].stat().st_mtime), reverse=True)


def recovery_reconciliation_gate(
    *,
    wts_task: str,
    surface: Optional[str] = None,
    lanes_root: Path | str = _DEFAULT_LANES_ROOT,
    now: Optional[float] = None,
    limit_per_lane: int = 10,
) -> RecoveryDecision:
    lanes_root = Path(lanes_root)
    wts_task = (wts_task or "").strip().lower()
    surface_norm = (surface or "").strip().lower()
    matches: list[RunState] = []
    triggers: list[str] = []

    for lane, agent, label, run_dir in _iter_recent_run_dirs(lanes_root, limit_per_lane=limit_per_lane):
        combined = _combined_run_text(run_dir).lower()
        run_wts = _extract_wts(combined)
        if wts_task and run_wts != wts_task:
            continue
        if surface_norm and surface_norm not in combined:
            continue
        state = classify_run_dir(run_dir, lane=lane, agent=agent, label=label, now=now)
        state.surface_match = bool(surface_norm)
        actionable = False
        if state.gate == "PASS":
            triggers.append(f"{label} run {state.run_id} has PASS")
            actionable = True
        if state.has_publish_ready:
            triggers.append(f"{label} run {state.run_id} has PUBLISH-READY")
            actionable = True
        if state.status == "over_budget":
            triggers.append(f"{label} run {state.run_id} is alive over-budget")
            actionable = True
        if state.has_conflict:
            triggers.append(f"{label} run {state.run_id} has stale stall vs completed-result conflict")
            actionable = True
        if actionable:
            matches.append(state)

    if matches:
        return RecoveryDecision(
            "reconcile_before_recovery",
            "; ".join(dict.fromkeys(triggers)) + "; reconcile/decide before launching duplicate recovery engineering.",
            matches,
        )
    return RecoveryDecision("proceed_to_recovery", "No newer PASS/PUBLISH-READY, alive over-budget run, or contradictory stall-vs-complete state found.", [])


def _jsonable(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return obj


def _tool_select_engineering_pool(args, **_kw) -> str:
    try:
        selection = select_engineering_lane(
            lanes_root=args.get("lanes_root") or _DEFAULT_LANES_ROOT,
            preferred_lane=args.get("preferred_lane"),
            affinity_reason=args.get("affinity_reason"),
        )
        return json.dumps(_jsonable(selection), indent=2)
    except Exception as exc:
        return tool_error(f"select_engineering_pool: {type(exc).__name__}: {exc}")


def _tool_recovery_gate(args, **_kw) -> str:
    try:
        wts_task = args.get("wts_task") or ""
        if not wts_task.strip():
            return tool_error("p1_recovery_reconciliation_gate: wts_task is required")
        decision = recovery_reconciliation_gate(
            wts_task=wts_task,
            surface=args.get("surface"),
            lanes_root=args.get("lanes_root") or _DEFAULT_LANES_ROOT,
        )
        return json.dumps(_jsonable(decision), indent=2)
    except Exception as exc:
        return tool_error(f"p1_recovery_reconciliation_gate: {type(exc).__name__}: {exc}")


registry.register(
    name="select_engineering_pool",
    toolset="delegation",
    schema={
        "name": "select_engineering_pool",
        "description": "Read-only P1 engineering pool selector over existing engineering/engineering-2/engineering-3 lane run dirs. No new availability ledger is created. Returns first idle lane or no-capacity; same-active reuse requires affinity_reason.",
        "parameters": {
            "type": "object",
            "properties": {
                "preferred_lane": {"type": "string", "description": "Optional lane to reuse (engineering, engineering-2, engineering-3). If active, affinity_reason is required."},
                "affinity_reason": {"type": "string", "description": "Explicit dependency/file/surface/branch continuity reason for reusing an active engineer."},
                "lanes_root": {"type": "string", "description": "Override lane root for tests; defaults to ~/.hermes/dd-lanes."},
            },
        },
    },
    handler=_tool_select_engineering_pool,
    emoji="🧭",
)

registry.register(
    name="p1_recovery_reconciliation_gate",
    toolset="delegation",
    schema={
        "name": "p1_recovery_reconciliation_gate",
        "description": "Read-only recovery gate: before launching duplicate recovery engineering, scan recent same-WTS/same-surface engineering lane run dirs for PASS, PUBLISH-READY, alive over-budget, or stale stall-vs-complete contradictions. Returns reconcile_before_recovery or proceed_to_recovery.",
        "parameters": {
            "type": "object",
            "properties": {
                "wts_task": {"type": "string", "description": "WTS task UUID to match."},
                "surface": {"type": "string", "description": "Optional surface/subsystem text to match in packet/stdout."},
                "lanes_root": {"type": "string", "description": "Override lane root for tests; defaults to ~/.hermes/dd-lanes."},
            },
            "required": ["wts_task"],
        },
    },
    handler=_tool_recovery_gate,
    emoji="♻️",
)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read-only P1 engineering pool selector / recovery reconciliation gate")
    sub = parser.add_subparsers(dest="cmd", required=False)
    sel = sub.add_parser("select")
    sel.add_argument("--lanes-root", default=str(_DEFAULT_LANES_ROOT))
    sel.add_argument("--preferred-lane")
    sel.add_argument("--affinity-reason")
    rec = sub.add_parser("recover")
    rec.add_argument("--lanes-root", default=str(_DEFAULT_LANES_ROOT))
    rec.add_argument("--wts-task", required=True)
    rec.add_argument("--surface")
    ns = parser.parse_args(argv)
    if ns.cmd in (None, "select"):
        result = select_engineering_lane(lanes_root=ns.lanes_root, preferred_lane=ns.preferred_lane, affinity_reason=ns.affinity_reason)
    else:
        result = recovery_reconciliation_gate(wts_task=ns.wts_task, surface=ns.surface, lanes_root=ns.lanes_root)
    print(json.dumps(_jsonable(result), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
