"""deploy_transition — walk the realization ladder via the capability gate.

Companion to deploy_approve (Option-3 Phase 2). After a deploy is approved, the
realization state machine must be walked rung by rung — deployed → restarted →
observed → proven — each rung carrying its REQUIRED evidence. This tool is the
sanctioned, C4-mediated path for that walk: it goes through the same in-process
capability egress (post_with_capability) so the turn's signed credential
authorizes the mutation at mc-api :8502 /transition.

The realization DB CHECK rejects empty / whitespace-only evidence; this tool
requires an evidence payload and surfaces the resource's rejection cleanly
rather than silently failing.

Returns a STRING (JSON) — tool handlers must, because the agent's tool-result
pipeline slices result[:500] for failure detection (a dict raises
"unhashable type: 'slice'").
"""
from __future__ import annotations

import json
import os

from tools.registry import registry, tool_error

_MC_API_BASE = os.getenv("MC_API_BASE_URL", "http://127.0.0.1:8502")

# Ladder states + the evidence kind each rung requires (mirrors
# realization_state_machine.REQUIRED_EVIDENCE — for the model's guidance only;
# the resource is the source of truth and re-validates).
_LADDER = {
    "deployed": "deploy_output / exit codes proving the deploy pipeline ran",
    "restarted": "pid/launchctl proof the service process was restarted",
    "observed": "post-restart health-check observation",
    "proven": "evidence/QA link tying observation to acceptance",
}

DEPLOY_TRANSITION_SCHEMA = {
    "name": "deploy_transition",
    "description": (
        "Walk a deploy-queue entry one rung up the realization ladder "
        "(deployed → restarted → observed → proven) through the SANCTIONED "
        "System-A capability path. Each rung REQUIRES evidence — the resource "
        "rejects empty/whitespace evidence. Use this instead of curling "
        "/transition by hand. If your turn is not System-A the resource denies "
        "with 403 and this tool reports it honestly. Returns the resource status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "The deploy-queue entry id to transition.",
            },
            "to": {
                "type": "string",
                "enum": ["deployed", "restarted", "observed", "proven"],
                "description": "Target realization state (the next rung).",
            },
            "evidence": {
                "type": "object",
                "description": (
                    "Required evidence for this rung, keyed by kind. Must be "
                    "non-empty (whitespace-only is rejected). Examples: "
                    "{\"deploy_output\": \"...\"}, {\"restart\": \"launchctl pid=...\"}, "
                    "{\"health\": \"GET /health 200\"}, {\"acceptance\": \"<link>\"}."
                ),
            },
            "by": {
                "type": "string",
                "description": "Who is recording the transition (audit trail).",
            },
        },
        "required": ["entry_id", "to", "evidence"],
    },
}


def deploy_transition(entry_id: str, to: str, evidence: dict, by: str = "system-a") -> str:
    if not entry_id:
        return tool_error("entry_id is required")
    if not to:
        return tool_error("to (target state) is required")
    # Surface the empty-evidence contract BEFORE the network call so the model
    # gets a clean, actionable error (the resource enforces this too).
    if not isinstance(evidence, dict) or not evidence:
        return tool_error(
            f"evidence is required and must be a non-empty object for '{to}'. "
            f"This rung expects: {_LADDER.get(to, 'evidence of the rung')}."
        )
    if all(
        (v is None) or (isinstance(v, str) and not v.strip())
        or (isinstance(v, (dict, list)) and len(v) == 0)
        for v in evidence.values()
    ):
        return tool_error(
            "evidence values are all empty/whitespace — the realization gate "
            "rejects that. Provide real evidence for the rung."
        )

    from gateway import capability_egress

    url = f"{_MC_API_BASE}/api/deploy-queue/{entry_id}/transition"
    try:
        resp = capability_egress.post_with_capability(
            url, json_body={"to": to, "evidence": evidence, "by": by}
        )
    except Exception as e:
        return tool_error(f"deploy-queue request failed: {type(e).__name__}: {e}")

    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text[:500]}

    if resp.status_code == 403:
        return json.dumps({
            "ok": False, "denied": True, "status_code": 403,
            "detail": payload.get("detail"), "reason": payload.get("reason"),
            "message": "DENIED by the capability gate — this turn lacks C4.",
        })
    if resp.status_code == 409:
        # Evidence/transition rejected by the realization gate — report cleanly.
        return json.dumps({
            "ok": False, "status_code": 409,
            "reason": payload.get("reason") or payload.get("detail"),
            "message": "Transition REJECTED by the realization gate (illegal step or missing/empty evidence).",
            "result": payload,
        })
    return json.dumps({
        "ok": resp.status_code < 400,
        "status_code": resp.status_code,
        "result": payload,
    })


registry.register(
    name="deploy_transition",
    toolset="deploy",
    schema=DEPLOY_TRANSITION_SCHEMA,
    handler=lambda args, **kw: deploy_transition(
        entry_id=args.get("entry_id"),
        to=args.get("to"),
        evidence=args.get("evidence") or {},
        by=args.get("by", "system-a"),
    ),
    emoji="🪜",
)
