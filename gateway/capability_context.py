"""
capability_context — the per-turn control plane for capability credentials.

This is the §5.4 crux: the System-A capability credential is held HERE, in a
``contextvars.ContextVar``, for exactly the same reason ``session_context`` exists
— a ContextVar is task-local, so it is NOT process-global ``os.environ`` and is
NOT a file the child turn can read. A delivery turn's shell never receives the
bytes, so it cannot replay or forge them.

The credential is minted by ``capability_issuer.mint_for_turn`` at turn
construction (where ``replace_identity`` is decided) and set here. Gateway-
mediated outbound calls to protected resources read it via ``current_credential``
and attach it as the control-plane header. The model's tools never see this
module's value placed into their environment.

Secret loading: the HMAC signer secret is read ONCE from a dedicated 0600 file
(``~/.openclaw/capability-signer.key``) and cached in-process. It is deliberately
NOT taken from ``os.environ`` and is never logged — see ``get_signer_secret``.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.capability")

# Per-turn credential. Default _UNSET so "never minted in this context" is
# distinguishable from "explicitly cleared".
_UNSET: Any = object()
_CREDENTIAL: ContextVar = ContextVar("DD_CAPABILITY_CREDENTIAL", default=_UNSET)

# The control-plane header the gateway attaches on mediated outbound calls and
# that protected resources read. It is NOT a header the model can usefully set:
# the only thing that satisfies the gate is a validly-signed credential, which
# the model cannot produce.
CAPABILITY_HEADER = "X-DD-Capability"

# Signer secret path — dedicated, 0600, never in the auto-loaded .env.
_SIGNER_KEY_PATH = Path(os.path.expanduser("~/.openclaw/capability-signer.key"))

_secret_cache: Optional[bytes] = None
_secret_loaded = False


def get_signer_secret() -> Optional[bytes]:
    """Load (once) and return the HMAC signer secret as bytes.

    Returns ``None`` if the key file is absent — callers MUST treat that as
    "cannot mint / cannot verify" (fail closed at the resource), never as
    "auth disabled". The secret is cached in-process; it is never logged and
    never placed into ``os.environ``.
    """
    global _secret_cache, _secret_loaded
    if _secret_loaded:
        return _secret_cache
    _secret_loaded = True
    try:
        if not _SIGNER_KEY_PATH.exists():
            logger.warning(
                "capability signer key absent at %s — capability minting disabled",
                _SIGNER_KEY_PATH,
            )
            _secret_cache = None
            return None
        # Permission sanity: refuse a world/group-readable key.
        mode = _SIGNER_KEY_PATH.stat().st_mode & 0o077
        if mode:
            logger.warning(
                "capability signer key at %s has loose permissions (%o) — refusing to load",
                _SIGNER_KEY_PATH, _SIGNER_KEY_PATH.stat().st_mode & 0o777,
            )
            _secret_cache = None
            return None
        raw = _SIGNER_KEY_PATH.read_text().strip()
        # Stored hex-encoded; decode to the raw key bytes for HMAC.
        _secret_cache = bytes.fromhex(raw)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("failed to load capability signer key: %s", type(e).__name__)
        _secret_cache = None
    return _secret_cache


def set_credential(credential: str):
    """Bind a minted credential to the current turn's context. Returns a token
    for ``clear_credential`` (used in a finally to avoid cross-turn bleed)."""
    return _CREDENTIAL.set(credential)


def clear_credential(token=None) -> None:
    """Explicitly clear the per-turn credential."""
    _CREDENTIAL.set("")


def current_credential() -> Optional[str]:
    """Return the credential bound to the current turn, or ``None``.

    Never falls back to ``os.environ`` — the whole point is that this value
    does not live anywhere the child turn can reach.
    """
    val = _CREDENTIAL.get()
    if val is _UNSET or val == "":
        return None
    return val
