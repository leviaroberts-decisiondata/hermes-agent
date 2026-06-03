"""
capability_issuer — mints the per-turn capability credential.

This is the single place that knows the truth about a turn's nature BEFORE any
model token is generated and before any shell can run: it is called at turn
construction, right where ``replace_identity`` is decided (api_server.py).

- A delivery turn (``replace_identity=True``) is System B → minted a
  producer-only credential carrying NO System-A capabilities.
- A P1 / System-A turn (``replace_identity=False``) → minted a credential
  carrying the full System-A capability set.

The minted credential is stored in the per-turn contextvar (capability_context),
never in os.environ. The signer secret is held only here/at the verifier.
"""

from __future__ import annotations

import logging
from typing import Optional

from gateway import capability_gate as cg
from gateway import capability_context as cc

logger = logging.getLogger("hermes.capability")


def mint_for_turn(*, system: str, session_id: str) -> Optional[str]:
    """Mint and bind the capability credential for the turn being constructed.

    ``system`` is the SINGLE authoritative System designation, resolved by the
    caller from exactly one source — the authenticated principal (which Bearer
    key validated the request); see api_server._classify_system_principal. This
    function does NOT re-derive identity from any other signal; it simply maps
    the authoritative System to its capability set.

    Returns the credential string (also bound into the contextvar), or ``None``
    if the signer secret is unavailable (in which case the turn proceeds with no
    credential — and any protected resource will default-deny, which is correct:
    a missing issuer must not silently grant System-A power).

    Mapping (fail-closed — only "A" grants System-A capabilities):
        system == "A"               → caps = SYSTEM_A_CAPS
        system in ("B", "unknown")  → caps = ()  (producer-only)
    """
    secret = cc.get_signer_secret()
    if not secret:
        # Fail closed: no key → no credential → resources deny. Never log the key.
        return None

    if system == "A":
        caps = cg.SYSTEM_A_CAPS
    else:
        # Normalize anything that is not an explicit System-A principal to "B".
        system = "B" if system not in ("A", "B") else system
        caps = ()

    credential = cg.mint(
        system=system,
        capabilities=caps,
        session_id=session_id or "",
        secret=secret,
    )
    cc.set_credential(credential)
    logger.debug(
        "minted capability credential sys=%s caps=%d sid=%s",
        system, len(caps), session_id,
    )
    return credential
