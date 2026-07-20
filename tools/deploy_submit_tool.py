"""deploy_submit — sanctioned deploy-queue SUBMIT via the capability gate.

Why this exists (P-E / B-4a + B-4c): the Slack happy path must be able to
complete Scope → Execute → Package → Submit-for-approval without manual
engineering intervention. mc-api :8502 `/api/deploy-queue/submit` is gated on
`_require_authenticated` → `_CAP_GATE.check_request_any`, which accepts ANY
validly-signed capability credential (System A OR B). Submit is System-B's
right per the operating model (delivery agents are legitimate producers; they
may QUEUE a deploy but cannot approve/execute — those stay C4 / System-A /
human). This tool is the sanctioned client for that submit: it POSTs through
`capability_egress.post_with_capability`, which reads the turn's per-turn
credential from the contextvar and attaches it as `X-DD-Capability` in-process —
NOT a raw shell `curl`, which carries no credential and cannot be handed one
without leaking it into the model's shell env.

SUBMIT-ONLY by construction: this tool can only reach `/submit`. It has no path
to `/approve`, `/transition`, `/execute`, or `/reject` — those are C4-gated at
the resource (a System-B credential is rejected there), and this tool never
constructs those URLs. The submit→approval boundary is enforced by mc-api, not
by this tool; this tool is just the sanctioned producer client.

A turn with no minted credential (e.g. a turn on a surface that has not minted a
capability) sends no `X-DD-Capability`; the resource then default-denies (403),
and this tool reports that honestly rather than pretending success.

DARK-SHIP NOTE (P-E hard boundary — read carefully): this tool ships DARK behind
an explicit env gate, `DD_DEPLOY_SUBMIT_ENABLED`. Until that flag is set to "1",
the tool's `check_fn` returns False and the registry filters it OUT of EVERY
surface's tool definitions (`registry.get_definitions` excludes tools whose
check_fn is False) — so no live turn can call it, on any platform.

Why the env gate and not just toolset membership: the "deploy" toolset is ALSO
enabled on the `api_server` platform (config.yaml `platform_toolsets.api_server`,
landed by the P1 Telegram deploy work for the System-A api_server loop). The
Slack *delivery* turn runs under that same api_server toolset and mints a signed
System-B (producer-only) credential, which `/api/deploy-queue/submit`'s
`check_request_any` gate ACCEPTS (it requires only a valid signature, A or B).
So toolset membership alone would make this tool LIVE and FUNCTIONAL on the Slack
delivery surface the moment it registered — exactly the boundary B-4b is meant
to security-gate. The env flag is the fail-closed seam: registered (wired so it
CAN be enabled) but unreachable until ONE explicit, reviewable change.

THE ONE-LINE ENABLE PATH (deliberately NOT made here — it is B-4b's gate):
    set `DD_DEPLOY_SUBMIT_ENABLED=1` in the target gateway's environment.
That — and only that — exposes the tool on any surface whose toolset already
includes "deploy" (api_server + telegram today). It must not be flipped without
the independent B-4b security review (capability scope = submit-only,
fail-closed, no C4 reachability, no token leakage). Approve/execute stay C4 and
are unreachable from this tool regardless of the flag.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from tools.registry import registry, tool_error

_LOG = logging.getLogger("dd.deploy_submit")

# mc-api deploy-queue base — localhost only.
_MC_API_BASE = os.getenv("MC_API_BASE_URL", "http://127.0.0.1:8502")
# Shared (home-anchored) hermes bin, where the chain driver lives — mirrors how
# route_to_lane_tool resolves the wrapper. NOT a per-profile HERMES_HOME.
_SHARED_HERMES_BIN = Path.home() / ".hermes" / "bin"

# P3 port-stamping: the SAME registries mc-api reads to resolve a service's port
# and to validate that an agent-submitted service is known. Resolving the port
# HERE (at submit) and stamping it onto the row kills the --deploy-port stopgap
# the chain driver carried for services whose deploy row landed with a null port
# (dd-analystaq, which is in deploy-commands.json but NOT port-registry.json).
_PORT_REGISTRY_PATH = Path.home() / ".openclaw" / "port-registry.json"
_DEPLOY_COMMANDS_PATH = Path.home() / ".openclaw" / "deploy-commands.json"


def _resolve_service_port(service_name: str):
    """Resolve a service's port from the on-disk registries at submit time.

    Mirrors mc-api's `_resolve_service_port` (the authoritative server-side
    resolver) so the stamp the tool sends matches what mc-api would compute —
    the client value is only ever a FALLBACK mc-api honors when its own lookup
    is null (a service present in deploy-commands.json but absent from
    port-registry.json). Resolution order:

      1. port-registry.json category sweep (the canonical port map).
      2. deploy-commands.json — if the service is known there but carries no
         port (today none do), confirm it is a real, known service and return
         None rather than guessing.

    FAIL-SOFT by contract: any error, or an unknown service, returns None. A
    null return must NEVER block a submit — the caller logs loudly and proceeds
    so the human approval gate is never gated on cosmetic port resolution.
    """
    svc = (service_name or "").strip()
    if not svc:
        return None
    try:
        with open(_PORT_REGISTRY_PATH) as f:
            reg = json.load(f)
        for cat in ("infrastructure", "client_facing", "integration", "engines", "personal"):
            entry = reg.get(cat, {}).get(svc) if isinstance(reg.get(cat), dict) else None
            if isinstance(entry, dict) and entry.get("port"):
                return int(entry["port"])
    except Exception as e:  # registry missing/malformed — fail soft, never block
        _LOG.warning("deploy_submit.port_registry_read_failed %s", {"svc": svc, "err": repr(e)})
    # Known in deploy-commands.json but no port column today → explicit None.
    try:
        with open(_DEPLOY_COMMANDS_PATH) as f:
            cmds = json.load(f)
        svc_cmds = cmds.get("services", cmds)
        if isinstance(svc_cmds, dict) and svc in svc_cmds:
            entry = svc_cmds.get(svc) or {}
            if isinstance(entry, dict) and entry.get("port"):
                return int(entry["port"])
    except Exception:
        pass
    return None


def _deploy_submit_enabled() -> bool:
    """Dark-ship gate. The tool is registered (wired) but withheld from every
    surface's tool definitions until this explicit flag is set. This is the
    single, reviewable enable seam B-4b gates — see the module docstring for why
    toolset membership alone is insufficient (api_server already carries the
    "deploy" toolset and the delivery turn mints an accepted signed credential).
    Fail-closed: anything other than the literal "1" keeps it dark."""
    return os.getenv("DD_DEPLOY_SUBMIT_ENABLED", "").strip() == "1"

DEPLOY_SUBMIT_SCHEMA = {
    "name": "deploy_submit",
    "description": (
        "Submit a completed change to the deploy queue for approval, through the "
        "SANCTIONED capability path. Use this instead of curling the deploy-queue "
        "API by hand — it attaches your turn's signed capability credential so the "
        "resource accepts the submission. This QUEUES the deploy for approval; it "
        "does NOT approve or execute it (those remain a System-A / human action). "
        "Assemble the package first: the service_name (required), the changed "
        "files, a one-line diff stat, a diff summary, and your test results. The "
        "turn's bound WTS task id is attached automatically so the queue entry "
        "links back to the tracker. If your turn carries no submit capability, the "
        "resource DENIES with 403 and this tool reports that honestly. Returns the "
        "queue entry id, status, and a conflict flag so you can tell the user "
        "'queued for approval, id=N'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service_name": {
                "type": "string",
                "description": (
                    "The service being deployed (required). mc-api auto-resolves "
                    "the build/restart/test commands from its config for this "
                    "service — you do not supply them."
                ),
            },
            "files_changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of changed file paths (repo-relative).",
            },
            "diff_summary": {
                "type": "string",
                "description": "A short prose summary of what changed and why.",
            },
            "diff_stat": {
                "type": "string",
                "description": (
                    "ONE line of diff stat, e.g. the output of "
                    "`git diff --stat | tail -1` ('3 files changed, 40 "
                    "insertions(+), 5 deletions(-)'). Keep it to a single line — "
                    "the column is varchar(500)."
                ),
            },
            "test_results": {
                "type": "string",
                "description": (
                    "A concise statement of the test outcome (e.g. '42 passed, 0 "
                    "failed'). Prose detail belongs in notes, not here."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "Free-form notes for the approver — context, caveats, manual "
                    "verification done. Prose goes here, not in diff_stat / "
                    "pre_deploy_test."
                ),
            },
            "target_commit": {
                "type": "string",
                "description": "The commit SHA the deploy should build from, if known.",
            },
            "service_port": {
                "type": "integer",
                "description": (
                    "Override the verification port for this deploy. Normally OMIT "
                    "— the tool resolves the service's port from the registry and "
                    "mc-api stamps it. Supply only to force a port for a service "
                    "the registries don't map (the retired --deploy-port escape "
                    "hatch)."
                ),
            },
            "wts_task_id": {
                "type": "string",
                "description": (
                    "Override the WTS task id to link. Normally omit — the turn's "
                    "bound task id is attached automatically."
                ),
            },
        },
        "required": ["service_name"],
    },
}


def deploy_submit(
    service_name: str,
    *,
    files_changed=None,
    diff_summary: str = "",
    diff_stat: str = "",
    test_results: str = "",
    notes: str = "",
    target_commit: str = "",
    wts_task_id: str = "",
    service_port=None,
    parent_agent=None,
) -> str:
    # Tool handlers MUST return a STRING — the agent's tool-result pipeline
    # slices result[:N] for failure detection, and a dict raises on slice. So
    # every return path here is JSON-serialized.
    service_name = (service_name or "").strip()
    if not service_name:
        return tool_error("service_name is required")

    # B-4c: the minimal submit package. The bound WTS task id is stamped onto
    # the turn as X-DD-WTS-Task-Id by dd-slack-service and exposed by the gateway
    # as parent_agent._dd_wts_task_id (same anchor-feed source route_to_lane
    # consumes). An explicit wts_task_id arg always wins; otherwise inherit the
    # turn's bound task so the queue entry links back to the tracker WITHOUT the
    # agent having to remember the id. Neither present → no link (honest).
    resolved_wts = (wts_task_id or "").strip()
    if not resolved_wts:
        bound = getattr(parent_agent, "_dd_wts_task_id", None)
        if bound and str(bound).strip():
            resolved_wts = str(bound).strip()

    # The deploy_queue.wts_task_id column is a UUID. A non-UUID id (e.g. a slug
    # or a malformed bound value) would make Directus 500 and sink the ENTIRE
    # submit. Fail-soft: only attach the link when it is a well-formed UUID;
    # otherwise drop the link (and tell the agent) rather than lose the submit.
    wts_link_dropped = False
    if resolved_wts:
        try:
            import uuid as _uuid
            _uuid.UUID(resolved_wts)
        except (ValueError, AttributeError, TypeError):
            wts_link_dropped = True
            resolved_wts = ""

    # Normalize files_changed to a list of strings.
    if files_changed is None:
        files = []
    elif isinstance(files_changed, str):
        # A single string → treat newline/comma separation defensively.
        files = [f.strip() for f in files_changed.replace(",", "\n").splitlines() if f.strip()]
    else:
        files = [str(f).strip() for f in files_changed if str(f).strip()]

    # diff_stat column is varchar(500) and the deploy shell runner chokes on
    # prose in the test/command fields — keep diff_stat to one line and leave
    # pre_deploy_test empty (prose belongs in notes).
    one_line_stat = (diff_stat or "").strip().splitlines()
    diff_stat_clean = one_line_stat[0].strip() if one_line_stat else ""

    # W2-B2 (Levi's ask): the approval screen must show WHAT is being approved. When
    # the caller didn't supply rich notes, AUTO-COMPOSE a human-readable summary from
    # the closeout summary + commit list + one-line diffstat. notes is the right home
    # (NOT pre_deploy_test — the executor runs that as shell; NOT diff_stat — that is
    # varchar(500) and kept to one line). Caller-supplied notes always win.
    notes_clean = (notes or "").strip()
    if not notes_clean:
        parts = []
        if (diff_summary or "").strip():
            parts.append(f"What this ships: {diff_summary.strip()}")
        if target_commit and target_commit.strip():
            parts.append(f"Target commit: {target_commit.strip()[:12]}")
        if files:
            shown = ", ".join(files[:8]) + (f" (+{len(files)-8} more)" if len(files) > 8 else "")
            parts.append(f"Files ({len(files)}): {shown}")
        if diff_stat_clean:
            parts.append(f"Diffstat: {diff_stat_clean}")
        parts.append("Submitted by DD P1 via the chain driver; approve to take it live, "
                     "then P1 verifies the live service and closes out.")
        notes_clean = "\n".join(parts)

    body = {
        "service_name": service_name,
        "files_changed": files,
        "diff_summary": (diff_summary or "").strip(),
        "diff_stat": diff_stat_clean,
        # pre_deploy_test defaults to "" — prose chokes the shell runner; mc-api
        # auto-resolves the real test command from its service config.
        "pre_deploy_test": "",
        "notes": notes_clean,
    }
    # test_results is a JSON column (the executor later writes a structured
    # {passed, output, exit_code} object). A bare prose string makes Directus
    # 500. Wrap the agent's summary as a JSON object, and only send it when
    # non-empty (else leave the column NULL).
    tr = (test_results or "").strip()
    if tr:
        body["test_results"] = {"summary": tr}
    if target_commit and target_commit.strip():
        body["target_commit"] = target_commit.strip()
    if resolved_wts:
        body["wts_task_id"] = resolved_wts

    # P3 PORT-STAMPING: resolve service_port at submit and stamp it onto the row.
    # An explicit override always wins (kept as the retired --deploy-port escape
    # hatch); otherwise resolve from the same registries mc-api reads. mc-api is
    # authoritative and prefers its OWN resolution — the value we send is only a
    # fallback it honors when its lookup is null (a service in deploy-commands.json
    # but absent from port-registry.json, e.g. dd-analystaq). Resolve to a real int
    # or omit; NEVER block a submit on port resolution — a null is a loud log, not
    # a failure (the human approval gate must not depend on a cosmetic port).
    resolved_port = None
    if service_port is not None and str(service_port).strip().isdigit():
        resolved_port = int(str(service_port).strip())
    else:
        resolved_port = _resolve_service_port(service_name)
    port_unresolved = resolved_port is None
    if resolved_port is not None:
        body["service_port"] = resolved_port
    else:
        # Loud, structured, but non-fatal — the submit proceeds with a null port
        # (the chain driver's verify stage will report "no service_port" honestly).
        _LOG.warning(
            "deploy_submit.service_port_unresolved %s",
            {"service_name": service_name,
             "detail": "no port in port-registry.json or deploy-commands.json; "
                       "row will carry null service_port (health verify will be skipped)"},
        )
    # Tie the queue entry to this turn's session for the audit trail, if known.
    sid = getattr(parent_agent, "session_id", None) or getattr(parent_agent, "_session_id", None)
    if sid and str(sid).strip():
        body["agent_session_id"] = str(sid).strip()

    # ── Admission-time chain-binding check (deploy-friction batch #3) ─────────
    # A submit inheriting/overriding a wts_task_id that CONTRADICTS the live chain's
    # established binding on this route_key would silently link the deploy row to the
    # WRONG tracker task — a drift the bind path (dd-chain-driver --set-wts) already
    # refuses, but the deploy-submit path did not. It is caught only later at
    # parity-audit, never at admission; WTS_LINK_MISMATCH cannot catch it (that only
    # proves the row persisted what was REQUESTED, not that the request was consistent
    # with the chain). Ask the chain driver (authoritative for bindings) for a verdict
    # and refuse BEFORE the row is created. FAIL-OPEN: only a confirmed `contradiction`
    # blocks; no chain / no established binding / any error proceeds. Kill-switch
    # DD_CHAIN_BINDING_ADMISSION = enforce | shadow | off (default enforce).
    _binding_mode = (os.getenv("DD_CHAIN_BINDING_ADMISSION", "enforce").strip().lower()
                     or "enforce")
    if _binding_mode != "off" and resolved_wts:
        _route_key = str(getattr(parent_agent, "_dd_route_key", "") or "").strip()
        if _route_key:
            try:
                import subprocess as _sp
                _helper = _SHARED_HERMES_BIN / "dd-chain-driver"
                _verdict, _vrow = "no-binding", {}
                if _helper.exists():
                    _cp = _sp.run(
                        [sys.executable, str(_helper), "--check-binding",
                         "--wts", resolved_wts, "--route-key", _route_key],
                        capture_output=True, text=True, timeout=20,
                    )
                    if _cp.returncode == 0 and _cp.stdout.strip():
                        _vrow = json.loads(_cp.stdout.strip().splitlines()[-1])
                        _verdict = _vrow.get("verdict", "no-binding")
                if _verdict == "contradiction":
                    _bw = _vrow.get("bound_wts")
                    if _binding_mode == "shadow":
                        _LOG.warning(
                            "deploy_submit.binding_mismatch_shadow %s",
                            {"route_key": _route_key, "bound_wts": _bw,
                             "requested_wts": resolved_wts})
                    else:  # enforce
                        return json.dumps({
                            "ok": False,
                            "denied": True,
                            "reason": "CHAIN_BINDING_MISMATCH",
                            "bound_wts_task_id": _bw,
                            "requested_wts_task_id": resolved_wts,
                            "route_key": _route_key,
                            "message": (
                                "CHAIN_BINDING_MISMATCH: this turn's chain (route "
                                f"{_route_key}) is already bound to WTS task {_bw!r}, but "
                                f"the submit carries wts_task_id={resolved_wts!r}. "
                                "Submitting would link the deploy to the wrong tracker "
                                "task. Re-submit with the chain's bound task (omit "
                                "wts_task_id to inherit it), or reconcile the chain "
                                "binding first."
                            ),
                        })
            except Exception:
                pass  # fail-open: never let the admission check block a legit submit

    from gateway import capability_egress

    url = f"{_MC_API_BASE}/api/deploy-queue/submit"
    # Board finding 2026-07-15 (canary ae201016, WTS-null deploy row): the API
    # reads the WTS link from the x-wts-task-id HEADER; the body field alone was
    # silently dropped, the tool still reported "linked", and dedupe then reused
    # the linkless row. Send BOTH (header authoritative, body belt) and verify
    # by authoritative readback below — never claim linkage without proof.
    _wts_headers = {"x-wts-task-id": resolved_wts} if resolved_wts else None
    try:
        resp = capability_egress.post_with_capability(
            url, json_body=body, extra_headers=_wts_headers)
    except Exception as e:
        return tool_error(f"deploy-queue submit request failed: {type(e).__name__}: {e}")

    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text[:500]}

    if resp.status_code == 403:
        # The gate denied — surface it honestly; do not pretend the deploy queued.
        return json.dumps({
            "ok": False,
            "denied": True,
            "status_code": 403,
            "detail": payload.get("detail"),
            "reason": payload.get("reason"),
            "message": (
                "DENIED by the capability gate — this turn carries no signed "
                "capability credential to submit a deploy. (Submit needs a minted "
                "per-turn capability presented as X-DD-Capability.)"
            ),
        })

    if resp.status_code >= 400:
        return json.dumps({
            "ok": False,
            "status_code": resp.status_code,
            "detail": payload.get("detail") if isinstance(payload, dict) else None,
            "result": payload,
        })

    # Success — report id/status/conflict so the agent can say
    # "queued for approval, id=N".
    entry_id = payload.get("id") if isinstance(payload, dict) else None
    status = payload.get("status") if isinstance(payload, dict) else None
    conflict_flag = bool(payload.get("conflict_flag")) if isinstance(payload, dict) else False
    out = {
        "ok": True,
        "status_code": resp.status_code,
        "id": entry_id,
        "status": status,
        "conflict_flag": conflict_flag,
        "wts_task_id": resolved_wts or None,
        "wts_link_dropped": wts_link_dropped,
        # P3: echo what the queue row will carry for the verification port. mc-api
        # may override with its own resolution; this is the value the tool stamped.
        "service_port": (payload.get("service_port")
                         if isinstance(payload, dict) and payload.get("service_port") is not None
                         else resolved_port),
        "service_port_unresolved": port_unresolved,
        "message": (
            f"Queued for approval, id={entry_id}. Approval/execute remain a "
            f"System-A / human action — this only submitted the request."
            + (" NOTE: a conflict was flagged (another pending entry for this "
               "service / overlapping files) — review before approving."
               if conflict_flag else "")
            + (" NOTE: the bound WTS task id was not a valid UUID, so the queue "
               "entry was NOT linked to a tracker task."
               if wts_link_dropped else "")
        ),
    }
    if isinstance(payload, dict) and payload.get("conflict_details"):
        out["conflict_details"] = payload["conflict_details"]
    if isinstance(payload, dict) and payload.get("depends_on"):
        out["depends_on"] = payload["depends_on"]

    # ── AUTHORITATIVE READBACK (board finding: submit response ≠ row truth). ──
    # Re-read the row and report what it ACTUALLY carries. A requested WTS link
    # that did not persist is a typed WTS_LINK_MISMATCH — the agent must NOT
    # report the deploy as tracker-linked, and dedupe reuse of such a row is a
    # defect to surface, never to paper over.
    if entry_id:
        try:
            import httpx as _httpx
            with _httpx.Client(timeout=15.0) as _c:
                _rb = _c.get(f"{_MC_API_BASE}/api/deploy-queue/{entry_id}")
            _row = _rb.json() if _rb.status_code < 400 else None
            if isinstance(_row, dict):
                out["row_readback"] = {
                    "status": _row.get("status"),
                    "wts_task_id": _row.get("wts_task_id"),
                    "target_commit": (str(_row.get("target_commit") or "")[:12] or None),
                }
                if resolved_wts:
                    if str(_row.get("wts_task_id") or "").strip() == resolved_wts:
                        out["wts_link"] = "verified"
                    else:
                        out["wts_link"] = "WTS_LINK_MISMATCH"
                        out["message"] += (
                            " ⚠ WTS_LINK_MISMATCH: authoritative readback shows the row's "
                            f"wts_task_id={_row.get('wts_task_id')!r}, not the requested link. "
                            "Do NOT report this deploy as tracker-linked; surface this defect "
                            "and have Deploy Ops reconcile the row before approval."
                        )
            else:
                out["wts_link"] = "unverified(readback-failed)"
        except Exception:
            out["wts_link"] = "unverified(readback-failed)"

    # W2-B2: STAMP the row id onto the active chain record so the chain driver's
    # queue-decision watch can poll it for the human's approve/reject and then
    # advance deploy → report → done (the trigger the deploy stage was missing).
    # Best-effort + fail-soft: a missing chain / missing helper never affects the
    # submit result the caller sees. Keyed on the SAME (wts_task, route_key) the
    # driver keys chains on — route_key is the gateway-attached _dd_route_key.
    if entry_id:
        try:
            route_key = str(getattr(parent_agent, "_dd_route_key", "") or "").strip()
            if resolved_wts and route_key:
                helper = _SHARED_HERMES_BIN / "dd-chain-driver"
                if helper.exists():
                    import subprocess as _sp
                    _sp.run(
                        [sys.executable, str(helper),
                         "--set-deploy-row", resolved_wts, route_key, str(entry_id),
                         "--deploy-service", service_name],
                        capture_output=True, text=True, timeout=20,
                    )
        except Exception:
            pass  # never let chain bookkeeping affect the submit result

    return json.dumps(out)


registry.register(
    name="deploy_submit",
    toolset="deploy",
    schema=DEPLOY_SUBMIT_SCHEMA,
    # Dark-ship: withheld from every surface's definitions until the explicit
    # B-4b enable flag is set. See _deploy_submit_enabled / module docstring.
    check_fn=_deploy_submit_enabled,
    handler=lambda args, **kw: deploy_submit(
        service_name=args.get("service_name"),
        files_changed=args.get("files_changed"),
        diff_summary=args.get("diff_summary", ""),
        diff_stat=args.get("diff_stat", ""),
        test_results=args.get("test_results", ""),
        notes=args.get("notes", ""),
        target_commit=args.get("target_commit", ""),
        wts_task_id=args.get("wts_task_id", ""),
        service_port=args.get("service_port"),
        parent_agent=kw.get("parent_agent"),
    ),
    emoji="📦",
)

# registry.register() also adopts a tool's check_fn as the TOOLSET-LEVEL check
# when the toolset has none yet (registry.py: "if check_fn and toolset not in
# self._toolset_checks"). deploy_approve/deploy_transition register with no
# check_fn, so our per-tool dark-ship gate would otherwise become the check for
# the WHOLE "deploy" toolset — wrongly reporting the proven System-A
# approve/transition path as "unavailable" whenever DD_DEPLOY_SUBMIT_ENABLED is
# unset (is_toolset_available / get_available_toolsets are UI/reporting surfaces;
# enabled_toolsets resolution does NOT consult them, so live tools are unaffected
# either way — but the reporting must stay honest). Undo that side effect: our
# gate is per-TOOL only, never the toolset gate. Idempotent and scoped — only
# clears the slot if it is OUR function.
if registry._toolset_checks.get("deploy") is _deploy_submit_enabled:
    del registry._toolset_checks["deploy"]
