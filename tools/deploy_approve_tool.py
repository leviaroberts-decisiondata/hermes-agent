"""deploy_approve — sanctioned System-A deploy-queue approval via the capability gate.

Why this exists (Option-3 Phase 2A-a): mc-api :8502 deploy-queue mutations are
now gated on a C4 capability credential. A System-A turn must approve through a
GATEWAY-MEDIATED path that attaches the per-turn credential from the contextvar —
NOT a raw shell `curl`, which (a) carries no credential and (b) cannot be handed
one without leaking it into the model's shell env. This tool is that mediated
path: it calls capability_egress.post_with_capability, which reads the turn's
credential from capability_context and attaches X-DD-Capability in-process.

A System-B (delivery) turn calling this tool mints no System-A credential (its
contextvar holds a producer-only / no credential), so the resource denies it —
the gate, not this tool, is the enforcement point. This tool is just the
sanctioned client.

Read-only nothing here; the effect (approve) happens at the resource behind the
gate. The tool reports the resource's verdict honestly (including a 403 deny).
"""
from __future__ import annotations

import os

from tools.registry import registry, tool_error

# mc-api deploy-queue base — localhost only.
_MC_API_BASE = os.getenv("MC_API_BASE_URL", "http://127.0.0.1:8502")

DEPLOY_APPROVE_SCHEMA = {
    "name": "deploy_approve",
    "description": (
        "Approve a deploy-queue entry through the SANCTIONED System-A capability "
        "path. Use this instead of curling the deploy-queue API by hand — it "
        "attaches your turn's signed capability credential so the resource "
        "authorizes the approval. If your turn is not System-A (e.g. a delivery "
        "turn), the resource will DENY with 403 and this tool reports that "
        "honestly. Returns the resource's status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "The deploy-queue entry id to approve.",
            },
            "decided_by": {
                "type": "string",
                "description": "Who is recording the approval (for the audit trail).",
            },
            "force": {
                "type": "boolean",
                "description": "Skip the merge-check gate. Default false.",
            },
        },
        "required": ["entry_id"],
    },
}


def deploy_approve(entry_id: str, decided_by: str = "system-a", force: bool = False) -> dict:
    if not entry_id:
        return tool_error("entry_id is required")
    from gateway import capability_egress

    url = f"{_MC_API_BASE}/api/deploy-queue/{entry_id}/approve"
    try:
        resp = capability_egress.post_with_capability(
            url, json_body={"decided_by": decided_by, "force": bool(force)}
        )
    except Exception as e:
        return tool_error(f"deploy-queue request failed: {type(e).__name__}: {e}")

    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text[:500]}

    if resp.status_code == 403:
        # The gate denied — surface it honestly; do not pretend success.
        return {
            "ok": False,
            "denied": True,
            "status_code": 403,
            "detail": payload.get("detail"),
            "reason": payload.get("reason"),
            "message": (
                "DENIED by the capability gate — this turn lacks the System-A "
                "(C4) capability to approve a deploy."
            ),
        }
    return {
        "ok": resp.status_code < 400,
        "status_code": resp.status_code,
        "result": payload,
    }


registry.register(
    name="deploy_approve",
    toolset="deploy",
    schema=DEPLOY_APPROVE_SCHEMA,
    handler=lambda args, **kw: deploy_approve(
        entry_id=args.get("entry_id"),
        decided_by=args.get("decided_by", "system-a"),
        force=bool(args.get("force", False)),
    ),
    emoji="🚀",
)
