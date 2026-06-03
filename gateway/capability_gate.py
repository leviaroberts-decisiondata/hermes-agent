"""
capability_gate — Option-3 System-A/System-B capability enforcement (Phase 1).

A *minimal* shared validator for short-lived, signed capability credentials.
This is NOT a framework: it is the smallest thing a protected resource can call
to answer one question — *may this caller exercise this capability?* — keyed on
an identity the caller cannot forge, steal, or replay.

Design reference: ~/.hermes/reviews/specs/option3-capability-enforcement-design-2026-06-03.md
(§5.2 the capability gate, §5.4 the non-strippable identity mechanism).

Credential shape (compact, stdlib-only — no external deps so it drops cleanly
into both the gateway and mc-api):

    <b64url(payload_json)>.<b64url(hmac_sha256(payload_json))>

payload = {
    "v":   1,                       # format version
    "sys": "A" | "B",               # System A (governs) vs System B (delivers)
    "cap": ["C1", ... ],            # capabilities this credential bears
    "sid": "<session_id>",          # turn / session binding
    "iat": <unix_seconds>,          # issued-at
    "exp": <unix_seconds>,          # hard expiry (short TTL)
    "jti": "<hex nonce>",           # per-mint nonce (replay distinctness)
}

NON-FORGEABLE: the HMAC is keyed on a secret held ONLY by the issuer/verifier
processes (loaded from a dedicated 0600 file, never placed in os.environ, never
logged). A delivery turn's shell has no key, so it cannot produce a valid MAC.
NON-STEALABLE (Phase 1 scope): System-A credentials are minted on the gateway
side and held in a contextvar (capability_context), never exported into the
child turn's environment, so a delivery shell never receives one to replay.
NON-REPLAYABLE: short TTL + turn binding (sid) + per-mint jti.

DEFAULT-DENY: every failure mode (absent, malformed, bad signature, expired,
missing capability) returns a deny. Absence of a credential is denial, not
permission — stripping the credential only fails closed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Iterable, Optional

# Capability vocabulary (design §2). Kept as plain strings so a route can
# declare `requires="C4"` without importing an enum.
CAP_C1_LANE = "C1"          # specialist / lane invocation
CAP_C2_GOV = "C2"           # governance mutation
CAP_C3_LIFECYCLE = "C3"     # context-lifecycle promotion (B->A)  [human-tier]
CAP_C4_DEPLOY = "C4"        # deploy / release authorization      [human-tier for prod]
CAP_C5_SVC = "C5"           # service lifecycle / env mutation
CAP_C6_REG = "C6"           # work/knowledge registry mutation
CAP_C7_JOB = "C7"           # job-graph / orchestration mutation

# The full System-A capability set. A P1/System-A turn bears all of these.
SYSTEM_A_CAPS = (
    CAP_C1_LANE, CAP_C2_GOV, CAP_C3_LIFECYCLE, CAP_C4_DEPLOY,
    CAP_C5_SVC, CAP_C6_REG, CAP_C7_JOB,
)

# Default short TTL. Credentials are turn-scoped; a turn that runs longer than
# this re-mints (the issuer is cheap). Kept generous enough to cover a long
# turn but short enough that a leaked credential is useless within minutes.
DEFAULT_TTL_SECONDS = 900

_ALG = hashlib.sha256


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass(frozen=True)
class Verdict:
    """Result of a capability check. ``allowed`` is the only thing a resource
    must branch on; ``reason``/``system`` feed the observability event."""
    allowed: bool
    reason: str
    system: str = "unknown"        # "A" | "B" | "unknown"
    capability: str = ""
    session_id: str = ""
    credential_id: str = ""        # jti, for the audit row (never the secret)


def sign(payload: dict, secret: bytes) -> str:
    """Serialize + HMAC a payload into a credential string.

    Canonical JSON (sorted keys, no whitespace) so the MAC is stable across
    issuer/verifier. The secret is bytes held only in-process.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    mac = hmac.new(secret, body, _ALG).digest()
    return f"{_b64u_encode(body)}.{_b64u_encode(mac)}"


def mint(
    *,
    system: str,
    capabilities: Iterable[str],
    session_id: str,
    secret: bytes,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: Optional[int] = None,
    nonce: Optional[str] = None,
) -> str:
    """Mint a signed capability credential. Issuer-side only (needs the secret).

    Caller is responsible for choosing ``capabilities`` from the turn's nature:
    System A → SYSTEM_A_CAPS; System B (delivery) → () (producer-only).
    """
    iat = int(now if now is not None else time.time())
    if nonce is None:
        # Per-mint distinctness without Math.random/Date restrictions: derive
        # from a fresh urandom-backed token. (os.urandom is allowed here; this
        # is issuer-side code, not the restricted workflow sandbox.)
        import os as _os
        nonce = _os.urandom(8).hex()
    payload = {
        "v": 1,
        "sys": system,
        "cap": sorted(set(capabilities)),
        "sid": session_id or "",
        "iat": iat,
        "exp": iat + int(ttl_seconds),
        "jti": nonce,
    }
    return sign(payload, secret)


def verify(
    credential: Optional[str],
    required_capability: str,
    secret: bytes,
    *,
    now: Optional[int] = None,
) -> Verdict:
    """The gate. Pure function over a credential + the capability a resource needs.

    Returns a deny Verdict for every failure mode (default-deny). The order is:
    present → well-formed → signature valid → not expired → capability present.
    """
    if not credential:
        return Verdict(False, "no_credential")

    try:
        body_b64, mac_b64 = credential.split(".", 1)
        body = _b64u_decode(body_b64)
        got_mac = _b64u_decode(mac_b64)
    except Exception:
        return Verdict(False, "malformed")

    # Constant-time MAC comparison — recompute over the exact bytes received.
    want_mac = hmac.new(secret, body, _ALG).digest()
    if not hmac.compare_digest(got_mac, want_mac):
        return Verdict(False, "bad_signature")

    try:
        payload = json.loads(body)
    except Exception:
        return Verdict(False, "malformed_payload")

    if payload.get("v") != 1:
        return Verdict(False, "bad_version")

    system = payload.get("sys", "unknown")
    sid = payload.get("sid", "")
    jti = payload.get("jti", "")
    caps = payload.get("cap", [])

    ts = int(now if now is not None else time.time())
    exp = payload.get("exp")
    if not isinstance(exp, int) or ts >= exp:
        return Verdict(False, "expired", system=system, session_id=sid, credential_id=jti)

    # `cap` MUST be a JSON array. If it is a string, `x in caps` is a SUBSTRING
    # test — `"C4" in "xxC4xx"` is True — which would grant a capability the
    # credential never carried. Require a list before the membership check.
    if not isinstance(caps, list):
        return Verdict(False, "malformed_caps", system=system, session_id=sid, credential_id=jti)

    if required_capability not in caps:
        return Verdict(
            False, "missing_capability",
            system=system, capability=required_capability,
            session_id=sid, credential_id=jti,
        )

    return Verdict(
        True, "ok",
        system=system, capability=required_capability,
        session_id=sid, credential_id=jti,
    )
