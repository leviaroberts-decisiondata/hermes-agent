"""
capability_issuer — mints the per-turn capability credential.

Called at turn construction (api_server.py), BEFORE any model token is generated
and before any shell can run. The turn's System (A/B) is resolved by the SINGLE
authoritative source — api_server._classify_system_principal, i.e. WHICH
authenticated Bearer key validated the request — and passed in as ``system``.
This module does not re-derive identity from any other signal (it no longer
consults ``replace_identity``):

- System "A" (authenticated System-A principal) → credential carrying the full
  System-A capability set.
- System "B" / "unknown" (delivery principal, or any non-System-A caller) →
  producer-only credential carrying NO System-A capabilities.

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
    # INFO-level so capability minting is observable in production (design §7):
    # one structured line per turn shows the System classification without
    # exposing the credential bytes (only the count of caps + the session id).
    logger.info(
        "capability.minted sys=%s caps=%d sid=%s",
        system, len(caps), session_id,
    )
    return credential
