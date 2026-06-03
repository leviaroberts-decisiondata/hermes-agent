"""
capability_egress — gateway-mediated outbound calls that carry the per-turn
capability credential as the X-DD-Capability control-plane header.

WHY THIS EXISTS (security review BLOCKER #2): the gate at mc-api :8502 is inert
unless *something* attaches the signed credential on the way out. The credential
lives in a contextvar (capability_context) that is readable ONLY by in-process
gateway code — deliberately NOT in any subprocess environment, so the model's
shell can never read it (which is what stops a delivery turn from stealing it).

Therefore the credential can only be attached by code that runs IN the gateway
process, never by a `curl` the model spawns (a subprocess inherits the gateway
env, so anything we put there to "help" the curl would be world-readable to the
model — see terminal_tool.py spawning with env.env). This module is that
in-process egress: gateway-side code (e.g. a sanctioned deploy-approval tool)
calls ``post_with_capability`` and the credential is read from the contextvar
and attached here, never crossing the shell boundary.

The raw model-driven `curl` to :8502 remains the UNSANCTIONED path — it carries
no credential, so the resource denies it. That asymmetry is the enforcement.

Residual (Phase 4, documented, NOT closed here): a same-uid model shell could
read the signer key file directly and mint its own credential. Closing that
needs uid separation. This module solves "the legit path attaches the
credential without leaking it to the model"; it does not solve "the model's uid
can read the key file."
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from gateway import capability_context as cc

logger = logging.getLogger("hermes.capability")


def post_with_capability(
    url: str,
    *,
    json_body: Optional[dict] = None,
    extra_headers: Optional[dict] = None,
    timeout: float = 30.0,
) -> Any:
    """In-process POST that attaches the current turn's capability credential.

    Reads ``capability_context.current_credential()`` (the contextvar bound at
    turn construction) and sends it as the ``X-DD-Capability`` header. Returns
    the httpx.Response. Raises if httpx is unavailable or the call errors —
    callers decide how to surface failure.

    A turn with no credential (e.g. System B, or no signer key) sends no
    capability header → the resource default-denies. We do NOT fabricate one.
    """
    import httpx

    headers = dict(extra_headers or {})
    credential = cc.current_credential()
    if credential:
        headers[cc.CAPABILITY_HEADER] = credential
    # else: send without it — resource will deny; never mint a fake one here.

    with httpx.Client(timeout=timeout) as client:
        return client.post(url, json=json_body or {}, headers=headers)
