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

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from tools.p1_caller_boundary import require_p1_caller
from tools.registry import registry, tool_error

_SHARED_HOME = Path.home() / ".hermes"
_BINDER = _SHARED_HOME / "bin" / "dd-wts-bind"


# ── INSTANCE-NAMESPACED ANCHORS (WTS 17cbc96c) ───────────────────────────────
# The anchor store lives at ~/.hermes/dd-lanes/telegram-anchors.json. All five
# Hermes homes run as the SAME unix user, so all five write the SAME file — and
# the key was `tg:<chat_id>`, with Levi's chat id identical across every bot.
# One key therefore meant five different conversations, and a client's binding
# and P1's binding were literally the same entry. That is how client work got
# bound to a P1 anchor on 2026-08-10.
#
# New keys are `tg:<instance>:<chat_id>[:<thread>]`. Old `tg:<chat_id>` keys are
# LEGACY and AMBIGUOUS: they are never resolved, and never migrated on a guess.


def _anchors_path() -> Path:
    """The shared Telegram anchor store (also read by ~/.hermes/bin/dd-wts-bind)."""
    return _SHARED_HOME / "dd-lanes" / "telegram-anchors.json"


def active_instance() -> str:
    """This Hermes instance's id, or "" when unidentifiable (never P1)."""
    try:
        from hermes_cli.profiles import get_active_home_id

        return get_active_home_id() or ""
    except Exception:
        return ""


def anchor_key(instance: str, chat_id: str, thread: "str | None" = None) -> str:
    """The namespaced anchor identity: ``tg:<instance>:<chat_id>[:<thread>]``."""
    key = f"tg:{instance}:{chat_id}"
    if thread and str(thread).strip():
        key = f"{key}:{str(thread).strip()}"
    return key


def legacy_anchor_key(chat_id: str, thread: "str | None" = None) -> str:
    """The pre-fix, instance-less identity: ``tg:<chat_id>[:<thread>]``."""
    key = f"tg:{chat_id}"
    if thread and str(thread).strip():
        key = f"{key}:{str(thread).strip()}"
    return key


def _load_anchors() -> dict:
    try:
        data = json.loads(_anchors_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_anchors(anchors: dict) -> bool:
    """Atomic write, mirroring dd-wts-bind's own save so the two never disagree."""
    try:
        path = _anchors_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".anchors.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(anchors, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, str(path))
        return True
    except Exception:
        return False


def _entry_instance(entry: dict) -> str:
    """Instance recorded ON a legacy entry — the only authoritative provenance.

    A legacy entry was written before instances existed, so it carries none.
    Anything that DOES carry one was stamped deliberately: either by this tool
    when it created/migrated the entry, or by an operator performing the
    documented repair. Nothing is inferred from the chat id, the owner string or
    the file's location — all three are identical across the five homes.
    """
    if not isinstance(entry, dict):
        return ""
    for field in ("hermes_instance", "instance"):
        value = str(entry.get(field) or "").strip()
        if value:
            return value
    return ""


def classify_anchor(anchors: dict, instance: str, chat_id: str,
                    thread: "str | None" = None) -> tuple:
    """Decide how this turn's anchor may be used. Returns ``(status, key)``.

    ``namespaced``        — an instance-scoped anchor exists; use it.
    ``absent``            — nothing bound here; a fresh namespaced bind is safe.
    ``legacy_migratable`` — a legacy entry carries authoritative provenance for
                            THIS instance; migrate it and record the migration.
    ``legacy_ambiguous``  — a legacy entry exists with no provenance. It may
                            belong to any of the five homes. FAIL CLOSED.
    """
    ns_key = anchor_key(instance, chat_id, thread)
    legacy_key = legacy_anchor_key(chat_id, thread)
    anchors = anchors if isinstance(anchors, dict) else {}

    if (anchors.get(ns_key) or {}).get("task_id"):
        return ("namespaced", ns_key)

    legacy = anchors.get(legacy_key) or {}
    if legacy.get("task_id") and not legacy.get("superseded_by"):
        owner = _entry_instance(legacy)
        if owner == instance:
            return ("legacy_migratable", legacy_key)
        if owner:
            # Provenance says it belongs to a DIFFERENT home. Then it is not
            # ambiguous and it is not ours: leave it completely alone (client
            # evidence) and bind fresh under our own namespaced key.
            return ("absent", ns_key)
        return ("legacy_ambiguous", legacy_key)

    return ("absent", ns_key)


def migrate_legacy_anchor(anchors: dict, instance: str, chat_id: str,
                          thread: "str | None" = None) -> "dict | None":
    """Copy an authoritatively-owned legacy anchor onto its namespaced key.

    Idempotent, and PRESERVING: the legacy entry is kept and marked
    ``superseded_by`` rather than deleted, so ptg-hermes / azul-hermes /
    hyperscience-hermes evidence stays reconstructable. Returns the updated
    anchors dict, or None if the migration is not authorised.
    """
    status, legacy_key = classify_anchor(anchors, instance, chat_id, thread)
    if status != "legacy_migratable":
        return None
    ns_key = anchor_key(instance, chat_id, thread)
    legacy = dict(anchors.get(legacy_key) or {})
    now = int(time.time())
    migrated = dict(legacy)
    migrated.update({
        "hermes_instance": instance,
        "migrated_from": legacy_key,
        "migrated_at": now,
        "migration_provenance": "hermes_instance recorded on the legacy entry",
        "migration_wts": "17cbc96c-a70f-46e7-af23-1458d04b5368",
    })
    anchors[ns_key] = migrated
    legacy["superseded_by"] = ns_key
    legacy["superseded_at"] = now
    anchors[legacy_key] = legacy
    return anchors


def _legacy_ambiguous_error(legacy_key: str, ns_key: str, instance: str) -> str:
    """The refusal a human reads mid-turn. It must end the confusion, not start it.

    Levi asked for this directly: when the anchor gate refuses, the message has to
    name the exact key, the exact instance, and the ONE line that repairs it —
    otherwise a correct refusal is indistinguishable from a broken tool. The two
    repair paths are stated as literal commands so neither the human nor the model
    has to invent one.
    """
    return tool_error(
        f"wts_bind: REFUSED — anchor key {legacy_key} has no recorded owner, and "
        f"this Hermes instance ({instance}) has no anchor of its own for this chat "
        f"(it would be {ns_key}).\n"
        f"WHY: all five Hermes homes share one anchor file "
        f"(~/.hermes/dd-lanes/telegram-anchors.json) and Levi's Telegram chat id is "
        f"the same for every bot, so {legacy_key} does not say which home bound it. "
        f"Resolving it here would either attach {instance}'s work to another home's "
        f"task or claim another home's task as this one's — the 2026-08-10 failure "
        f"(WTS 17cbc96c). Nothing was created, resolved or mutated.\n"
        f"REPAIR (one line, once you know from evidence that {instance} owns it):\n"
        f"    bin/dd-anchor-repair --stamp {legacy_key} --instance {instance} "
        f'--evidence "<how you know>"\n'
        f"  Then re-run wts_bind: it migrates {legacy_key} -> {ns_key} and keeps the "
        f"legacy entry as superseded (nothing is ever deleted).\n"
        f"SEE FIRST: bin/dd-anchor-repair --preflight --instance {instance} "
        f"lists every anchor key that would refuse here.\n"
        f"OR SKIP IT: wts_bind(..., force_new=true) binds a NEW task under {ns_key} "
        f"and leaves {legacy_key} untouched."
    )


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

    # ── ANCHOR NAMESPACING + LEGACY GATE (WTS 17cbc96c) ──
    # Runs BEFORE the binder is invoked, so a refusal creates no task, no anchor
    # and no Directus write.
    instance = active_instance()
    if not instance:
        return tool_error(
            "wts_bind: refused — this process cannot identify its Hermes instance, "
            "so any anchor it wrote would be ambiguous across homes. An "
            "unidentified instance is never P1 (WTS 17cbc96c). Nothing was bound."
        )
    ns_key = anchor_key(instance, chat_id, thread)
    if not force_new:
        anchors = _load_anchors()
        status, key = classify_anchor(anchors, instance, chat_id, thread)
        if status == "legacy_ambiguous":
            return _legacy_ambiguous_error(key, ns_key, instance)
        if status == "legacy_migratable":
            migrated = migrate_legacy_anchor(anchors, instance, chat_id, thread)
            if migrated is None or not _save_anchors(migrated):
                return tool_error(
                    f"wts_bind: the legacy anchor {key} is authoritatively owned by "
                    f"this instance but the migration to {ns_key} could not be "
                    f"written. Refusing rather than binding against an unmigrated "
                    f"ambiguous key. Nothing was created or mutated."
                )

    # The binder derives its anchor key verbatim as ``tg:<--chat>[:<--thread>]``
    # (~/.hermes/bin/dd-wts-bind), and uses --chat for nothing else. Passing the
    # instance-scoped token through that slot yields ``tg:<instance>:<chat_id>``
    # with NO change to the shared helper — the shape the fix requires, realized
    # without touching a live 0700 binary. If dd-wts-bind ever gains a native
    # --anchor-key flag, switch to it and delete this note.
    cmd = [str(_BINDER), "--chat", f"{instance}:{chat_id}"]
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
