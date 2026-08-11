"""wts_bind — resolve-or-create a bound WTS task for a Telegram/governance turn.

Why this exists (P5 review G1 / clause C5):
  On Slack turns the thread's bound WTS task is auto-fed into the turn
  (slack_active_anchors -> X-DD-WTS-Task-Id -> parent_agent._dd_wts_task_id) and
  route_to_lane defaults --wts-task to it, so REQUEST+RESPONSE land on ONE
  tracker task automatically. Telegram has NO anchor, so _dd_wts_task_id is None
  and the handoff silently falls back to inline evidence (confirmed across 40
  sessions). The doc-recommended shortest path is "contract + a resolve-or-create
  helper": p1-default.md now tells P1 to bind first, and THIS tool is how it binds.

Why a TOOL and not a shell command (the G3 interaction):
  The sanctioned helper lives at ~/.hermes/bin/dd-wts-bind, but ~/.hermes/bin is
  0700 openclaw (the uid-separation side effect the review files as G3). The
  agent's *sandboxed* terminal/code-exec surface cannot traverse it (Permission
  denied, exit 126) — so P1 cannot shell the helper directly. route_to_lane has
  the same constraint and solves it by running the wrapper as a subprocess from
  the gateway's OWN python process (which is openclaw and CAN traverse 0700 bin).
  We mirror that exactly: this tool runs in-process, so the bind works on a real
  Telegram turn without loosening the G3 boundary.

Token hygiene: the helper sources the Directus service token in-process and
never prints it; this tool only relays the helper's machine-parseable KEY=VALUE
stdout. No secret ever reaches argv/stdout/the model.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tools.p1_caller_boundary import require_p1_caller
from tools.registry import registry, tool_error

_SHARED_HOME = Path.home() / ".hermes"
_BINDER = _SHARED_HOME / "bin" / "dd-wts-bind"


def check_wts_bind_requirements() -> bool:
    """Gate: available only when the sanctioned binder helper exists + is exec.

    Returns a BOOLEAN — the registry filters the tool out on any non-True return.
    """
    return _BINDER.exists() and os.access(_BINDER, os.X_OK)


def _resolve_chat(chat: "str | None", parent_agent) -> "str | None":
    """Resolve the Telegram chat id: explicit arg wins, else derive from the
    caller's routing session key (agent:main:{platform}:{chat_type}:{chat_id})."""
    if chat and str(chat).strip():
        return str(chat).strip()
    for attr in ("_dd_route_key", "_dd_session_key"):
        key = str(getattr(parent_agent, attr, "") or "").strip()
        parts = key.split(":")
        # agent:main:telegram:dm:<chat_id>[:...]
        if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main" and parts[2] == "telegram":
            return parts[4]
    return None


def wts_bind(
    goal: str,
    chat: "str | None" = None,
    thread: "str | None" = None,
    notes: "str | None" = None,
    resolve_only: bool = False,
    force_new: bool = False,
    parent_agent=None,
) -> str:
    """Resolve-or-create the turn's bound WTS task and return its id + tracker link.

    Returns the helper's KEY=VALUE block (WTS_TASK_ID / BOUND / VERIFY / TRACKER /
    ANCHOR_KEY) on success, or an honest tool_error on failure — NEVER a fabricated id.
    """
    # Authority first — before any binder invocation, so a refused caller creates
    # no WTS anchor and no task (WTS 17cbc96c).
    denied = require_p1_caller("wts_bind")
    if denied:
        return denied
    if not check_wts_bind_requirements():
        return tool_error(
            "wts_bind: the binder helper (~/.hermes/bin/dd-wts-bind) is not "
            "available on this surface — do NOT claim the handoff is WTS-tracked; "
            "surface the blocker to the user."
        )
    chat_id = _resolve_chat(chat, parent_agent)
    if not chat_id:
        return tool_error(
            "wts_bind: could not determine the Telegram chat id (pass `chat` "
            "explicitly, or this turn carries no parseable telegram routing key). "
            "Without it there is no thread to anchor the task to."
        )
    if resolve_only and force_new:
        return tool_error("wts_bind: resolve_only=true and force_new=true cannot be combined.")
    if not resolve_only and not (goal or "").strip():
        return tool_error("wts_bind: provide `goal` (the unit of work) unless resolve_only=true.")

    cmd = [str(_BINDER), "--chat", chat_id]
    if thread and str(thread).strip():
        cmd += ["--thread", str(thread).strip()]
    if force_new:
        cmd += ["--force-new"]
    if resolve_only:
        cmd += ["--resolve-only"]
    else:
        cmd += ["--goal", goal.strip()]
        if notes and str(notes).strip():
            cmd += ["--notes", str(notes).strip()]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return tool_error("wts_bind: the binder timed out (>60s); the task was NOT confirmed bound.")
    except Exception as exc:
        return tool_error(f"wts_bind: failed to invoke the binder: {exc}")

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return tool_error(
            f"wts_bind: the binder FAILED (exit={proc.returncode}). Do NOT report the "
            f"handoff as WTS-tracked; surface this to the user.\n"
            f"stdout: {out or '(empty)'}\nstderr: {err[:800] or '(empty)'}"
        )
    # Resolve-only with no live task is a clean 'none' (not an error).
    return (
        "WTS bind OK — pass WTS_TASK_ID below to route_to_lane(wts_task=…) / "
        "dd-telegram-visible-lane-run --wts-task so REQUEST+RESPONSE land on ONE task.\n"
        f"{out}"
    )


WTS_BIND_SCHEMA = {
    "name": "wts_bind",
    "description": (
        "Resolve-or-create the bound WTS (tracker) task for a Telegram / governance "
        "(System-A) turn, so the REQUEST you hand off and the specialist's RESPONSE both "
        "attach to ONE tracker task (clause C5). On Slack the bound task is auto-fed and you "
        "don't need this; on Telegram there is no anchor, so call this FIRST for any "
        "governance handoff, then pass the returned WTS_TASK_ID to route_to_lane(wts_task=…) "
        "or dd-telegram-visible-lane-run --wts-task. It resolves an existing task for this "
        "thread (follow-ups reuse ONE task) or creates one (source_type=telegram, milestone "
        "auto-resolved), persists the thread→task anchor, and returns the id + tracker link "
        "VERIFIED against Directus. NEVER claim a handoff is WTS-tracked unless this returns a "
        "real WTS_TASK_ID."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "One-line description of the unit of work (becomes the task title/description). Required unless resolve_only=true.",
            },
            "chat": {
                "type": "string",
                "description": "Telegram chat id to anchor on. Usually OMIT — it is derived from this turn's routing key. Pass only to override.",
            },
            "thread": {
                "type": "string",
                "description": "Optional thread/message id to scope the anchor more tightly than the whole chat.",
            },
            "notes": {
                "type": "string",
                "description": "Optional longer context stored on the created task's notes.",
            },
            "resolve_only": {
                "type": "boolean",
                "description": "If true, only resolve an existing bound task for this thread; do NOT create one. Returns BOUND=none when there is no live task.",
            },
            "force_new": {
                "type": "boolean",
                "description": "If true, create a fresh WTS task for a new/unrelated work unit even if this chat/thread has an existing anchor. Do not combine with resolve_only.",
            },
        },
        "required": [],
    },
}


registry.register(
    name="wts_bind",
    toolset="delegation",
    schema=WTS_BIND_SCHEMA,
    handler=lambda args, **kw: wts_bind(
        goal=args.get("goal"),
        chat=args.get("chat"),
        thread=args.get("thread"),
        notes=args.get("notes"),
        resolve_only=bool(args.get("resolve_only", False)),
        force_new=bool(args.get("force_new", False)),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_wts_bind_requirements,
    emoji="🔗",
)
