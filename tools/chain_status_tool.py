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
import re
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


# ── stale / disagreement detection vs the in-context [WORK OWNERSHIP] snapshot ──
#
# WHY (WS-5 terminal-mirror gap, read side): the JSON ledger is write-authoritative;
# PG is a read mirror. A terminal/escalated transition can land in the ledger while the
# PG row stays FROZEN at an older active state with pg_write_error=None (Session C
# §2.2b). The driver-side fix (verify-after-write + terminal-inclusive catch-up) closes
# that on the WRITE path — but a row can still be momentarily stale between the missed
# sync and the next catch-up tick, and the cutover guardrails require P1 to NOT silently
# trust a PG row that disagrees with the authoritative ledger.
#
# The driver pushes the authoritative ledger truth into THIS turn's context as a
# `[WORK OWNERSHIP …]` snapshot (gateway.mirror → session transcript → _session_messages).
# So we cross-check the PG row against the most-recent such snapshot and, on disagreement,
# flag it LOUDLY and tell P1 to trust the snapshot/ledger over the row — exactly the
# tiebreaker the guardrails name. This catches the pg_write_error=None class the row's own
# mirror-flag cannot (the flag is not even materialized onto the PG row).
_WORK_OWNERSHIP_RE = re.compile(r"\[WORK OWNERSHIP\b", re.IGNORECASE)
_SNAP_STAGE_RE = re.compile(r"\bstage:\s*([^·|\n]+)", re.IGNORECASE)
_SNAP_OWNER_RE = re.compile(r"\bowner:\s*([^·|\n]+)", re.IGNORECASE)
_SNAP_BLOCKER_RE = re.compile(r"\bblocker:\s*([^·|\n]+)", re.IGNORECASE)
# Snapshot wording that means the authoritative ledger considers the chain escalated /
# parked-on-a-human (NOT actively progressing). Mirrors the driver's own owner/blocker
# language for the escalated + deploy-gate states (dd-chain-driver refresh_fields).
_ESCALATION_SIGNALS = (
    "escalated to you", "escalating the deploy", "awaiting your",
    "needs you", "awaiting your unblock", "awaiting your design",
    "held — awaiting", "awaiting human deploy approval",
)


def _latest_work_ownership_snapshot(parent_agent) -> "dict | None":
    """Parse the MOST RECENT [WORK OWNERSHIP …] snapshot from this turn's session
    messages (the authoritative-ledger truth the driver mirrored into context). Returns
    {stage, owner, blocker, escalated, raw} or None if no snapshot is present. Pure
    string parsing; never raises (returns None on any trouble)."""
    try:
        msgs = getattr(parent_agent, "_session_messages", None) or []
    except Exception:
        return None
    best = None
    for m in msgs:  # in order; keep the LAST snapshot seen
        try:
            content = m.get("content") if isinstance(m, dict) else None
        except Exception:
            content = None
        if not isinstance(content, str) or not _WORK_OWNERSHIP_RE.search(content):
            continue
        stage_m = _SNAP_STAGE_RE.search(content)
        owner_m = _SNAP_OWNER_RE.search(content)
        blocker_m = _SNAP_BLOCKER_RE.search(content)
        low = content.lower()
        best = {
            "stage": (stage_m.group(1).strip() if stage_m else None),
            "owner": (owner_m.group(1).strip() if owner_m else None),
            "blocker": (blocker_m.group(1).strip() if blocker_m else None),
            "escalated": any(sig in low for sig in _ESCALATION_SIGNALS),
            "raw": content[:400],
        }
    return best


def _norm_stage(s) -> str:
    return " ".join(str(s or "").lower().split()).strip()


def _detect_disagreement(chain: dict, snap: "dict | None") -> "str | None":
    """Compare the PG row against the in-context authoritative snapshot. Return a human
    warning string on disagreement, else None. Conservative: only fires on a CONCRETE
    contradiction (stage mismatch, or the snapshot says escalated/awaiting-you while the
    PG row reads a non-terminal active/blocked status) — not on mere absence of a
    snapshot. The snapshot/ledger always wins the tiebreaker per the cutover guardrails."""
    if not snap:
        return None
    g = chain.get
    pg_status = _norm_stage(g("status"))
    pg_stage = _norm_stage(g("current_stage"))
    snap_stage = _norm_stage(snap.get("stage"))
    reasons = []
    # (1) stage contradiction
    if snap_stage and pg_stage and snap_stage != pg_stage:
        reasons.append(f"stage: snapshot/ledger='{snap_stage}' vs PG row='{pg_stage}'")
    # (2) the authoritative snapshot reads escalated / awaiting-you, but the PG row is
    # still a live non-terminal status — the exact §2.2b freeze (escalated→frozen active).
    if snap.get("escalated") and pg_status in ("active", "blocked"):
        reasons.append(
            f"escalation: snapshot/ledger reads ESCALATED / awaiting-you "
            f"(blocker='{snap.get('blocker') or '?'}') vs PG row status='{pg_status}'")
    if not reasons:
        return None
    return (
        "⚠ STALE MIRROR — the request_chains row DISAGREES with the authoritative "
        "[WORK OWNERSHIP] snapshot in your context (the ledger truth the driver mirrored "
        "into this turn). " + " ; ".join(reasons) + ". TRUST THE SNAPSHOT / LEDGER, not "
        "this PG row — the row is a read mirror and may be momentarily frozen behind a "
        "terminal/escalated transition. Tell Levi the ledger state, NOT the row state."
    )


# ── turn-context resolution ──────────────────────────────────────────────────
def _parse_route_alias(key: str) -> "tuple[str | None, str | None]":
    """Parse a gateway routing key / session key into (channel, thread).

    Shape: agent:main:<platform>:<chat_type>:<chat_id>[:<thread_ts>[:...]]. The
    chat_id is the source_channel_id; the OPTIONAL 6th segment is the thread ts
    (present for threaded Slack turns). Returns (None, None) if the string is not
    a recognizable agent:main routing key. Pure string parsing; never raises.

    This is the bridge for the graduation criterion: a grader/operator reasonably
    has the GATEWAY routing key (the alias the turn was dispatched under), but the
    chain row is keyed under route_key = the DRIVER chain anchor (chain_id), a
    different value. We do NOT store the gateway key as a column (Session C item 5
    keeps route_key = the driver anchor) — instead we DERIVE the channel (+thread)
    from the alias and resolve the chain by its stored source_* origin, so a query
    by the key an operator actually holds returns the turn with no inside knowledge.
    """
    parts = str(key or "").strip().split(":")
    if len(parts) >= 5 and parts[0] == "agent" and parts[1] == "main":
        channel = parts[4] or None
        thread = parts[5] if len(parts) >= 6 and parts[5] else None
        return channel, thread
    return None, None


def _resolve_channel(parent_agent) -> "str | None":
    """Derive the Slack channel / Telegram chat id from this turn's routing key
    (agent:main:<platform>:<chat_type>:<chat_id>[:...]) — same shape wts_bind uses."""
    for attr in ("_dd_route_key", "_dd_session_key"):
        channel, _thread = _parse_route_alias(getattr(parent_agent, attr, "") or "")
        if channel:
            return channel
    return None


def _resolve_wts_task(parent_agent) -> "str | None":
    tid = getattr(parent_agent, "_dd_wts_task_id", None)
    return str(tid).strip() if tid else None


def _resolve_thread(parent_agent) -> "str | None":
    """The turn's thread ts, when carried on the agent. Prefer an explicit
    _dd_thread_ts; else the OPTIONAL 6th segment of the routing key."""
    t = getattr(parent_agent, "_dd_thread_ts", None)
    if t and str(t).strip():
        return str(t).strip()
    for attr in ("_dd_route_key", "_dd_session_key"):
        _ch, thread = _parse_route_alias(getattr(parent_agent, attr, "") or "")
        if thread:
            return thread
    return None


def _resolve_message(parent_agent) -> "str | None":
    """The turn's originating message ts / queue id, when carried on the agent."""
    for attr in ("_dd_message_ts", "_dd_queue_id"):
        v = getattr(parent_agent, attr, None)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _by_field(field: str, value: str) -> "list | None":
    """Resolve the newest chain whose `field` column == value. Returns the row
    list (len 0/1) on a 200, or None on a non-200 so the caller can distinguish a
    clean miss from a read error. The field name is from a fixed allowlist below —
    never interpolated from model input — so this cannot widen the query surface."""
    qf = urllib.parse.quote(value)
    code, resp = _api_get(
        f"/items/request_chains?filter[{field}][_eq]={qf}&sort=-created_at&limit=1")
    if code != 200:
        return None
    return (resp or {}).get("data") or []


# ── candidates listing (the no-resolve fallback) ─────────────────────────────
# Fields surfaced in the candidates view. STRICTLY non-credential: ids, the
# ask snippet, origin, and lifecycle position only — never a token, owner email,
# or anything credential-ish. Kept narrow on purpose.
_CAND_FIELDS = (
    "id,route_key,title,ask_summary,source_surface,source_channel_id,"
    "status,current_stage,updated_at,created_at"
)
_CAND_LIMIT = 8


def _candidates() -> "list[dict]":
    """The N most-recently-active chains across ALL surfaces, read-only.

    Ranked so a chain that is actually MOVING ranks above stale/never-ticked
    probe rows whose updated_at is null. Directus sorts nulls first on a
    descending sort, which would float a never-ticked probe to the top — so we
    over-fetch and re-rank in Python: rows WITH a timestamp first (newest first),
    null-timestamp rows last. Returns [] on any read failure — the caller
    degrades to guidance text, never a fabricated chain.
    """
    try:
        # Over-fetch (2×) so the Python re-rank has real movers to pull forward
        # even when several null-timestamp probe rows exist.
        code, resp = _api_get(
            f"/items/request_chains?sort=-updated_at,-created_at"
            f"&limit={_CAND_LIMIT * 2}&fields={_CAND_FIELDS}")
        if code != 200:
            return []
        rows = (resp or {}).get("data") or []

        def _rank(r):
            # (has-timestamp DESC, updated_at DESC, created_at DESC) — Python sort
            # is stable & ascending, so negate via a tuple where True>False.
            ts = r.get("updated_at") or ""
            return (1 if ts else 0, ts, r.get("created_at") or "")

        rows.sort(key=_rank, reverse=True)
        return rows[:_CAND_LIMIT]
    except Exception:
        pass
    return []


def _fmt_candidates(cands: list, reason: str) -> str:
    """Render the read-only candidates listing P1 offers when no chain resolved
    from args/turn. One compact line per chain; never fabricates a status."""
    head = (
        "chain_status: " + reason + "\n"
        "No chain resolved for this turn (note: a Telegram DM turn cannot auto-resolve a "
        "Slack-keyed chain — pass an explicit selector). Here are the most recently active "
        "chains across ALL surfaces (READ-ONLY) — pass chain_id= or channel= to select one. "
        "Do NOT answer about work status from memory or chat history; ground it in one of "
        "these or say you could not resolve it."
    )
    if not cands:
        return head + "\n  (no chains found in the request_chains spine)"
    lines = [head, ""]
    for c in cands:
        g = c.get
        snippet = (g("title") or g("ask_summary") or "(no title)")
        snippet = " ".join(str(snippet).split())[:80]
        origin = f"{g('source_surface') or '?'}"
        ch = g("source_channel_id")
        if ch:
            origin += f" {ch}"
        lines.append(
            f"  • {g('id')}  [route_key={g('route_key')}]\n"
            f"      ask: {snippet}\n"
            f"      origin: {origin}  |  status: {g('status')}  |  "
            f"stage: {g('current_stage')}  |  updated_at: {g('updated_at') or '(never)'}"
        )
    lines.append("")
    lines.append("Select with: chain_status(chain_id=\"<id-or-route_key>\") "
                 "or chain_status(channel=\"<channel/chat id>\").")
    return "\n".join(lines)


def _fmt_chain(chain: dict, events: list) -> str:
    """Compose the canonical "where are we" answer from a request_chains row + its
    recent events. The five ownership fields lead (stage · owner · next · blocker ·
    ETA), then provenance + delivery + the last few lifecycle events."""
    g = chain.get
    lines = []
    title = (g("title") or g("ask_summary") or "(no title)")
    lines.append(f"CHAIN {g('id')}  [route_key={g('route_key')}]")
    # The CANONICAL chain key — the standardized, queryable identity for THIS
    # turn's chain. Surfaced explicitly so the next query (operator or grader) can
    # use `chain_status(chain_id=...)` against it directly, no anchor guessing.
    lines.append(f"  chain key:  {g('route_key')}  (canonical — query with chain_id=)")
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
    thread_ts: "str | None" = None,
    message_id: "str | None" = None,
    route_alias: "str | None" = None,
    parent_agent=None,
) -> str:
    """Answer "where are we?" from request_chains. Resolution order:
      1. explicit chain_id (the route_key anchor or the row uuid)
      2. explicit route_alias (the GATEWAY routing key) → derive channel+thread → resolve
      3. explicit thread_ts → newest chain on that Slack thread (source_thread_id)
      4. explicit message_id (message/queue id) → chain by source_message_id
      5. explicit wts_task → newest chain bound to it
      6. this turn's bound WTS task (_dd_wts_task_id) → newest chain
      7. this turn's thread ts (from the turn / routing-key 6th seg) → newest chain
      8. this turn's message ts / queue id → chain by source_message_id
      9. this turn's channel/chat (from the routing key) → newest active chain

    NATURAL-KEY resolution (route_alias / thread_ts / message_id / channel) is a
    first-class path so a grader or operator needs NO inside knowledge of the
    driver chain anchor: the row is keyed under route_key=<driver chain_id>, but
    a single query by the key an operator reasonably HAS — the gateway routing
    alias, the Slack thread ts, the originating message/queue id, or the channel —
    resolves the same turn. The canonical chain key (route_key anchor) is then
    surfaced in the answer (and in the audited delivery record's payload) so it is
    explicitly queryable and standardized for the next query.

    Returns the structured five-field status + recent events. When NOTHING
    resolves, or when an explicit selector MISSES, it returns a READ-ONLY
    candidates listing — the most recently active chains across all surfaces, with
    guidance to pass a selector to pick one. Never a fabricated status, and never a
    bare dead-end error.
    """
    if not check_chain_status_requirements():
        return tool_error(
            "chain_status: the scoped chain-reader token is not available on this "
            "surface — fall back to the in-context [WORK OWNERSHIP …] snapshot for "
            "status; do NOT claim a request_chains read you could not make."
        )

    # Track whether the model passed an explicit selector that MISSED, so the
    # no-resolve fallback can say "that selector matched nothing — here are the
    # candidates" rather than a bare "nothing resolved".
    missed = []
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
            if data:
                return _answer_for(data, f"chain_id={cid}", parent_agent)
            # explicit selector missed — offer candidates rather than dead-end.
            missed.append(f"chain_id={cid!r}")

        # 2. explicit route_alias — the GATEWAY routing key an operator holds.
        # Derive channel (+thread) and resolve by the stored source_* origin. The
        # alias is NOT a stored column; this is the no-inside-knowledge bridge.
        if route_alias and route_alias.strip():
            ra = route_alias.strip()
            al_ch, al_thread = _parse_route_alias(ra)
            data = None
            if al_thread:
                data = _by_field("source_thread_id", al_thread)
            if not data and al_ch:
                data = _by_field("source_channel_id", al_ch)
            if data:
                return _answer_for(data, f"route_alias={ra}", parent_agent)
            missed.append(f"route_alias={ra!r}")

        # 3. explicit thread_ts → newest chain on that Slack thread.
        if thread_ts and thread_ts.strip():
            tt = thread_ts.strip()
            data = _by_field("source_thread_id", tt)
            if data:
                return _answer_for(data, f"thread_ts={tt}", parent_agent)
            missed.append(f"thread_ts={tt!r}")

        # 4. explicit message_id (originating message ts / queue id).
        if message_id and message_id.strip():
            mid = message_id.strip()
            data = _by_field("source_message_id", mid)
            if data:
                return _answer_for(data, f"message_id={mid}", parent_agent)
            missed.append(f"message_id={mid!r}")

        # 5/6. WTS task (explicit, else the turn's bound task)
        explicit_wts = bool((wts_task or "").strip())
        wid = (wts_task or "").strip() or _resolve_wts_task(parent_agent)
        if wid:
            qf = urllib.parse.quote(wid)
            code, resp = _api_get(
                f"/items/request_chains?filter[wts_task_id][_eq]={qf}"
                f"&sort=-created_at&limit=1")
            data = (resp or {}).get("data") if code == 200 else None
            if data:
                return _answer_for(data, f"wts_task={wid}", parent_agent)
            if explicit_wts:
                missed.append(f"wts_task={wid!r}")
            # fall through to thread/message/channel if no chain is bound to the task

        # 7. this turn's thread ts — resolve by the stored source_thread_id. A
        # threaded turn's chain is most precisely keyed by its thread.
        turn_thread = _resolve_thread(parent_agent)
        if turn_thread:
            data = _by_field("source_thread_id", turn_thread)
            if data:
                return _answer_for(data, f"thread_ts={turn_thread}", parent_agent)

        # 8. this turn's originating message ts / queue id.
        turn_msg = _resolve_message(parent_agent)
        if turn_msg:
            data = _by_field("source_message_id", turn_msg)
            if data:
                return _answer_for(data, f"message_id={turn_msg}", parent_agent)

        # 9. channel/chat from the turn — newest active chain on this surface.
        explicit_ch = bool((channel or "").strip())
        ch = (channel or "").strip() or _resolve_channel(parent_agent)
        if ch:
            qf = urllib.parse.quote(ch)
            code, resp = _api_get(
                f"/items/request_chains?filter[source_channel_id][_eq]={qf}"
                f"&sort=-created_at&limit=1")
            data = (resp or {}).get("data") if code == 200 else None
            if data:
                return _answer_for(data, f"channel={ch}", parent_agent)
            if explicit_ch:
                missed.append(f"channel={ch!r}")

        # Nothing resolved (or every explicit selector missed). Return a
        # READ-ONLY candidates listing instead of a bare error so the model can
        # SELECT the right chain — and so it never falls back to memory/chat.
        if missed:
            reason = (f"the selector(s) {', '.join(missed)} matched no chain.")
        else:
            reason = ("no chain_id/wts_task/channel given and none could be "
                      "auto-resolved from this turn.")
        return _fmt_candidates(_candidates(), reason)
    except Exception as exc:
        # Even on read failure, try to offer candidates; if that also fails the
        # listing degrades to honest guidance. NEVER fabricate a status.
        try:
            return _fmt_candidates(
                _candidates(),
                f"the read errored ({type(exc).__name__}); these are the most "
                f"recent chains I could still list.")
        except Exception:
            return tool_error(
                f"chain_status: read failed ({type(exc).__name__}) — do NOT fabricate a "
                f"status; use the in-context [WORK OWNERSHIP …] snapshot instead."
            )


def _answer_for(data, scope_desc: str, parent_agent=None) -> str:
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
    # STALE-MIRROR / DISAGREEMENT DETECTION (WS-5): cross-check the row against the
    # authoritative [WORK OWNERSHIP] snapshot in this turn's context. On a concrete
    # contradiction (stage mismatch, or snapshot-escalated vs PG-active) lead with a
    # loud warning so P1 reports the LEDGER truth, never the frozen row.
    warning = None
    try:
        warning = _detect_disagreement(chain, _latest_work_ownership_snapshot(parent_agent))
    except Exception:
        warning = None
    head = ("WHERE ARE WE — sourced from the request_chains spine "
            "(read-only; JSON ledger remains authoritative):\n")
    if warning:
        head = warning + "\n\n" + head
    return head + _fmt_chain(chain, events)


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
        "It also CROSS-CHECKS the row against the authoritative [WORK OWNERSHIP] snapshot in your "
        "context and, on disagreement (e.g. the ledger says escalated/needs-you while the PG row "
        "still reads active), leads with a loud STALE-MIRROR warning — report the LEDGER/snapshot "
        "state, never the frozen row. "
        "Never fabricate a status: if no chain resolves (e.g. a Telegram DM turn cannot "
        "auto-resolve a Slack-keyed chain) — or if an explicit selector misses — it returns a "
        "READ-ONLY list of the most recently active chains across all surfaces so you can pass "
        "chain_id= or channel= to select the right one. NEVER answer a status question from "
        "memory/chat history when this tool errors or lists candidates — select a chain or say "
        "you could not resolve it."
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
                "description": "Resolve the newest chain on this Slack channel / Telegram chat id (source_channel_id). Usually OMIT — derived from the turn's routing key.",
            },
            "thread_ts": {
                "type": "string",
                "description": "Resolve the newest chain on this Slack thread ts (source_thread_id). A natural key a grader/operator has from the thread itself — no driver-anchor knowledge needed.",
            },
            "message_id": {
                "type": "string",
                "description": "Resolve the chain by its originating Slack message ts / response-queue id (source_message_id). Another no-inside-knowledge natural key.",
            },
            "route_alias": {
                "type": "string",
                "description": "Resolve by the GATEWAY routing key (e.g. 'agent:main:slack:channel:C0…[:<thread_ts>]'). The chain row is keyed under the DRIVER anchor, not this alias, so the tool derives the channel (+thread) from the alias and resolves by the stored origin — letting a query by the key an operator actually holds return the turn.",
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
        thread_ts=args.get("thread_ts"),
        message_id=args.get("message_id"),
        route_alias=args.get("route_alias"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_chain_status_requirements,
    emoji="🧭",
)
