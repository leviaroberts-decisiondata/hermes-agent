"""chain_status — answer "where are we?" from the request_chains spine (WS-4 read cutover).

Why this exists (V1.2 build plan WS-4 / P1 read cutover):
  P1 owns active work and must answer status questions ("where are we?", "what's
  the state of my request?") from a STRUCTURED record, not by reconstructing it
  from chat history. The authoritative driver ledger already does this in-context
  via [WORK OWNERSHIP …] snapshots; this tool lets P1 also SOURCE that answer on
  demand from the promoted `request_chains` spine (Directus/Postgres :8513) — the
  canonical, queryable promotion of the chain-driver ledger.

  IMPORTANT (cutover guardrails, binding): the JSON ledger REMAINS write-
  authoritative during the cutover window. This tool is READ-ONLY. It does NOT
  flip authority and does NOT demote the ledger. Live parity (dd-chain-parity-
  check) holds on the live set before this read is trusted; where PG and the
  ledger disagree on a terminal/aged chain (a best-effort mirror miss), the
  in-context [WORK OWNERSHIP …] snapshot — which reflects the authoritative
  ledger — remains the tiebreaker. State that honestly rather than over-trusting
  a stale row.

Why a TOOL and not a shell command (mirrors wts_bind's G3 rationale):
  The sanctioned helpers live under ~/.hermes/bin (0700 openclaw) — the agent's
  SANDBOXED shell cannot traverse it (Permission denied, exit 126). So P1 cannot
  curl Directus or shell a bin/ helper directly. This tool runs IN-PROCESS in the
  gateway (which is openclaw and CAN read the 0600 reader token), exactly like
  wts_bind / route_to_lane solve the same boundary.

Read identity (single-materializer boundary):
  Reads use a dedicated least-privilege "Chain Reader" Directus identity
  (CHAIN_READER_DIRECTUS_TOKEN, ~/.openclaw/dd-chain-reader.env, 0600) that has
  READ-ONLY permission on the three chain collections and NOTHING else — no
  create/update/delete, no tasks, no users. P1 deliberately does NOT get the chain
  DRIVER's write token: P1 reads the spine, only the driver materializes it.

Token hygiene: the token is read from its 0600 file in-process and used solely as
a bearer header; it never reaches argv, stdout, or the model.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tools.registry import registry, tool_error

_PG_BASE = os.environ.get("DD_CHAIN_PG_URL", "http://localhost:8513").rstrip("/")
_READER_TOKEN_FILE = Path(os.environ.get(
    "DD_CHAIN_READER_TOKEN_FILE", str(Path.home() / ".openclaw" / "dd-chain-reader.env")))
_READER_TOKEN_VAR = "CHAIN_READER_DIRECTUS_TOKEN"

_TOKEN_CACHE: "str | None" = None


def _reader_token() -> "str | None":
    """Load the scoped READ-ONLY chain-reader token from its 0600 env file. Cached.
    Value never logged or returned — only used as a bearer header."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE is not None:
        return _TOKEN_CACHE or None
    tok = os.environ.get(_READER_TOKEN_VAR, "").strip()
    if not tok and _READER_TOKEN_FILE.exists():
        try:
            for line in _READER_TOKEN_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(_READER_TOKEN_VAR + "="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            tok = ""
    _TOKEN_CACHE = tok or ""
    return _TOKEN_CACHE or None


def check_chain_status_requirements() -> bool:
    """Gate: available only when the scoped reader token is resolvable.

    Returns a BOOLEAN — the registry filters the tool out on any non-True return.
    Probing the file (not the value) is enough; the value is never surfaced.
    """
    return bool(_reader_token())


def _api_get(path: str) -> "tuple[int, dict | None]":
    tok = _reader_token()
    if not tok:
        raise RuntimeError("no scoped chain-reader token available")
    req = urllib.request.Request(f"{_PG_BASE}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode()
        except Exception:
            pass
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


# ── turn-context resolution ──────────────────────────────────────────────────
def _resolve_channel(parent_agent) -> "str | None":
    """Derive the Slack channel / Telegram chat id from this turn's routing key
    (agent:main:<platform>:<chat_type>:<chat_id>[:...]) — same shape wts_bind uses."""
    for attr in ("_dd_route_key", "_dd_session_key"):
        key = str(getattr(parent_agent, attr, "") or "").strip()
        parts = key.split(":")
        if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main":
            return parts[4]
    return None


def _resolve_wts_task(parent_agent) -> "str | None":
    tid = getattr(parent_agent, "_dd_wts_task_id", None)
    return str(tid).strip() if tid else None


def _fmt_chain(chain: dict, events: list) -> str:
    """Compose the canonical "where are we" answer from a request_chains row + its
    recent events. The five ownership fields lead (stage · owner · next · blocker ·
    ETA), then provenance + delivery + the last few lifecycle events."""
    g = chain.get
    lines = []
    title = (g("title") or g("ask_summary") or "(no title)")
    lines.append(f"CHAIN {g('id')}  [route_key={g('route_key')}]")
    lines.append(f"  ask:        {title[:300]}")
    lines.append(f"  status:     {g('status')}")
    lines.append(f"  stage:      {g('current_stage')}")
    lines.append(f"  owner:      {g('current_owner_kind')} / {g('current_owner_id')}")
    lines.append(f"  next stage: {g('next_stage')}")
    lines.append(f"  blocker:    {g('blocker_summary') or '(none)'}")
    lines.append(f"  ETA:        {g('eta_at') or '(none stated)'}")
    lines.append(f"  WTS task:   {g('wts_task_id') or '(unbound)'}")
    lines.append(f"  outcome:    {g('outcome_type')} via {g('delivery_mechanism')}")
    if g("delivery_audit_status"):
        lines.append(f"  delivery:   {g('delivery_audit_status')} — "
                     f"{(g('delivery_audit_detail') or '')[:120]}")
    if g("terminal_summary"):
        lines.append(f"  closeout:   {(g('terminal_summary') or '')[:200]}")
    lines.append(f"  origin:     {g('source_surface')} {g('source_channel_id') or ''} "
                 f"(msg {g('source_message_id') or '-'})")
    lines.append(f"  updated_at: {g('updated_at')}")
    if g("pg_write_error"):
        lines.append(f"  ⚠ mirror:   pg_write_error set — this row's last mirror write "
                     f"FAILED; trust the in-context [WORK OWNERSHIP] snapshot / ledger over "
                     f"this row until it clears: {(g('pg_write_error') or '')[:120]}")
    if events:
        lines.append("  recent events:")
        for ev in events[-6:]:
            ts = ev.get("occurred_at") or ev.get("created_at") or ""
            lines.append(f"    [{ev.get('event_sequence')}] {ev.get('event_type')} "
                         f"{(ev.get('summary') or '')[:90]}  {ts}")
    return "\n".join(lines)


def chain_status(
    chain_id: "str | None" = None,
    wts_task: "str | None" = None,
    channel: "str | None" = None,
    parent_agent=None,
) -> str:
    """Answer "where are we?" from request_chains. Resolution order:
      1. explicit chain_id (the route_key anchor or the row uuid)
      2. explicit wts_task → newest chain bound to it
      3. this turn's bound WTS task (_dd_wts_task_id) → newest chain
      4. this turn's channel/chat (from the routing key) → newest active chain

    Returns the structured five-field status + recent events, or an honest
    tool_error / 'no chain' note (never a fabricated status).
    """
    if not check_chain_status_requirements():
        return tool_error(
            "chain_status: the scoped chain-reader token is not available on this "
            "surface — fall back to the in-context [WORK OWNERSHIP …] snapshot for "
            "status; do NOT claim a request_chains read you could not make."
        )

    try:
        # 1. explicit chain_id — match route_key anchor OR the row uuid.
        if chain_id and chain_id.strip():
            cid = chain_id.strip()
            qf = urllib.parse.quote(cid)
            code, resp = _api_get(
                f"/items/request_chains?filter[route_key][_eq]={qf}&sort=-created_at&limit=1")
            data = (resp or {}).get("data") if code == 200 else None
            if not data:
                code, resp = _api_get(f"/items/request_chains/{qf}")
                data = [resp["data"]] if (code == 200 and (resp or {}).get("data")) else None
            return _answer_for(data, f"chain_id={cid}")

        # 2/3. WTS task (explicit, else the turn's bound task)
        wid = (wts_task or "").strip() or _resolve_wts_task(parent_agent)
        if wid:
            qf = urllib.parse.quote(wid)
            code, resp = _api_get(
                f"/items/request_chains?filter[wts_task_id][_eq]={qf}"
                f"&sort=-created_at&limit=1")
            data = (resp or {}).get("data") if code == 200 else None
            if data:
                return _answer_for(data, f"wts_task={wid}")
            # fall through to channel if no chain is bound to the task

        # 4. channel/chat from the turn — newest active chain on this surface.
        ch = (channel or "").strip() or _resolve_channel(parent_agent)
        if ch:
            qf = urllib.parse.quote(ch)
            code, resp = _api_get(
                f"/items/request_chains?filter[source_channel_id][_eq]={qf}"
                f"&sort=-created_at&limit=1")
            data = (resp or {}).get("data") if code == 200 else None
            return _answer_for(data, f"channel={ch}")

        return tool_error(
            "chain_status: could not resolve a chain — no chain_id given, no WTS task "
            "bound to this turn, and no channel/chat id in this turn's routing key. "
            "Pass chain_id=, wts_task=, or channel= explicitly."
        )
    except Exception as exc:
        return tool_error(
            f"chain_status: read failed ({type(exc).__name__}) — do NOT fabricate a "
            f"status; use the in-context [WORK OWNERSHIP …] snapshot instead."
        )


def _answer_for(data, scope_desc: str) -> str:
    if not data:
        return (f"chain_status: no request_chains row for {scope_desc}. "
                f"Either no chain has been minted for this work yet, or it predates "
                f"the dual-write. Use the in-context [WORK OWNERSHIP …] snapshot for status.")
    chain = data[0]
    cid = chain.get("id")
    events = []
    try:
        qf = urllib.parse.quote(str(cid))
        code, resp = _api_get(
            f"/items/request_chain_events?filter[chain_id][_eq]={qf}"
            f"&sort=event_sequence&limit=40"
            f"&fields=event_type,summary,event_sequence,occurred_at,created_at")
        if code == 200:
            events = (resp or {}).get("data") or []
    except Exception:
        events = []
    return ("WHERE ARE WE — sourced from the request_chains spine "
            "(read-only; JSON ledger remains authoritative):\n"
            + _fmt_chain(chain, events))


CHAIN_STATUS_SCHEMA = {
    "name": "chain_status",
    "description": (
        "Answer \"where are we?\" / \"what's the state of my request?\" by reading the "
        "request_chains spine (the canonical, queryable promotion of the chain-driver "
        "ledger) — the structured five-field status (stage · owner · next stage · blocker · "
        "ETA) plus provenance, delivery, and recent lifecycle events. READ-ONLY: it never "
        "mutates the chain and the JSON ledger stays authoritative, so when a [WORK OWNERSHIP "
        "…] snapshot is already in your context those five fields ARE the answer — use this to "
        "SOURCE or CONFIRM a status answer on demand from the table of record. Resolves the "
        "chain automatically from this turn's bound WTS task or channel; pass chain_id=, "
        "wts_task=, or channel= to target a specific one. If a row's mirror write last failed "
        "(pg_write_error set) the tool flags it — trust the ledger/snapshot over a flagged row. "
        "Never fabricate a status: if no chain resolves, the tool says so."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chain_id": {
                "type": "string",
                "description": "Target a specific chain by its route_key anchor (e.g. 'no-task:cd605a8b' or '<wts>:<hash>') or row uuid. Usually OMIT — it's resolved from the turn.",
            },
            "wts_task": {
                "type": "string",
                "description": "Resolve the newest chain bound to this WTS task id. Usually OMIT — the turn's bound task is used automatically.",
            },
            "channel": {
                "type": "string",
                "description": "Resolve the newest chain on this Slack channel / Telegram chat id. Usually OMIT — derived from the turn's routing key.",
            },
        },
        "required": [],
    },
}


registry.register(
    name="chain_status",
    toolset="delegation",
    schema=CHAIN_STATUS_SCHEMA,
    handler=lambda args, **kw: chain_status(
        chain_id=args.get("chain_id"),
        wts_task=args.get("wts_task"),
        channel=args.get("channel"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_chain_status_requirements,
    emoji="🧭",
)
