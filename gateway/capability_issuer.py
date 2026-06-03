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


def mint_for_turn(
    *,
    system_a_authorized: bool,
    replace_identity: bool,
    session_id: str,
) -> Optional[str]:
    """Mint and bind the capability credential for the turn being constructed.

    Returns the credential string (also bound into the contextvar), or ``None``
    if the signer secret is unavailable (in which case the turn proceeds with no
    credential — and any protected resource will default-deny, which is correct:
    a missing issuer must not silently grant System-A power).

    System assignment is FAIL-CLOSED and bound to a TRUSTED server-side signal
    (``system_a_authorized``), NOT to a bare caller-supplied header. The
    ``X-DD-Replace-Identity`` header can only ever *downgrade* a turn to
    System B — it can never *select* System A:

        system A (full caps)  ⟺  system_a_authorized AND NOT replace_identity
        system B (producer)   otherwise — including:
            • caller is not the authenticated internal principal, OR
            • no internal principal key is configured at all, OR
            • the turn asserted replace_identity (delivery).

    Rationale (security review BLOCKER #1): previously absence of the delivery
    header defaulted to System A, so any unauthenticated caller could mint full
    caps simply by omitting a header. The default is now System B; System A
    requires proof.
    """
    secret = cc.get_signer_secret()
    if not secret:
        # Fail closed: no key → no credential → resources deny. Never log the key.
        return None

    if system_a_authorized and not replace_identity:
        system = "A"
        caps = cg.SYSTEM_A_CAPS
    else:
        system = "B"
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
