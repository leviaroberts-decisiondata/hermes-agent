"""
dd_agent_service.py — best-effort registration into the Service Layer backbone (:8510).

WS1 §2: adopt the idle dd-agent-service (:8510) by routing real fleet/session
traffic into it so agents become enumerable in the backbone
(``registered_agents`` flips from 0 to the live fleet).

DESIGN INVARIANTS (do not violate):
  * ADDITIVE & NON-FATAL. A down/slow :8510 must NEVER block or alter a turn.
    Every call here is wrapped, short-timeout, and swallows all errors (mirrors
    ``agent-orchestration/mc_registration.py``'s graceful-skip).
  * DEFAULT-OFF. Gated on ENABLE_AGENT_SERVICE_REGISTRATION; when unset/false this
    module is inert and the dispatch/routing path is byte-identical to today.
  * NO DISPATCH/ROUTING CHANGE. This only POSTs registration/session metadata;
    it never selects an agent, forwards a turn, or influences prompt assembly.

Endpoints used (both idempotent on the :8510 side):
  POST /agents/register          {name, webhook_url}      — re-activates on dup name
  POST /gateway/session-created  {session_id, model, ...} — returns existing on dup session
"""
import logging
import os

logger = logging.getLogger("hermes.dd_agent_service")

# Default-OFF master flag. Unset/"0"/"false" → this module is inert.
ENABLED = os.getenv("ENABLE_AGENT_SERVICE_REGISTRATION", "").strip().lower() in ("1", "true", "yes", "on")

AGENT_SERVICE_URL = os.getenv("AGENT_SERVICE_URL", "http://127.0.0.1:8510").rstrip("/")


def _safe_float(env_val: str | None, default: float) -> float:
    """Parse a float env var, falling back to default on any bad value.

    Import-time float() would crash module load (and thus gateway startup) on a
    malformed AGENT_SERVICE_TIMEOUT. This wiring must never affect startup.
    """
    try:
        return float(env_val) if env_val not in (None, "") else default
    except (TypeError, ValueError):
        logger.debug("Bad AGENT_SERVICE_TIMEOUT=%r; using default %.1fs", env_val, default)
        return default


_TIMEOUT = _safe_float(os.getenv("AGENT_SERVICE_TIMEOUT"), 3.0)


def is_enabled() -> bool:
    """True only when the default-off flag is explicitly turned on."""
    return ENABLED


def register_session_created(
    session_id: str,
    *,
    model: str | None = None,
    label: str | None = None,
    session_key: str | None = None,
    initiated_by: str = "hermes-gateway",
) -> bool:
    """Best-effort POST /gateway/session-created. Returns True on a 2xx, else False.

    Non-fatal: any failure (flag off, :8510 down, timeout, bad status) is logged
    at debug and swallowed. NEVER raises to the caller.
    """
    if not ENABLED:
        return False
    if not session_id:
        return False
    payload = {
        "session_id": session_id,
        "model": model,
        "label": label,
        "session_key": session_key,
        "initiated_by": initiated_by,
    }
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(f"{AGENT_SERVICE_URL}/gateway/session-created", json=payload)
        if resp.status_code in (200, 201):
            logger.debug("session-created registered with :8510 (%s)", session_id)
            return True
        logger.debug("session-created → :8510 returned %d (non-fatal)", resp.status_code)
        return False
    except Exception as exc:  # noqa: BLE001 — registration must never block a turn
        logger.debug("session-created registration skipped (%s) — :8510 unavailable", exc)
        return False


def register_agent(name: str, webhook_url: str = "") -> bool:
    """Best-effort POST /agents/register. Idempotent on the :8510 side (re-activates).

    Used by onboarding orchestrators (WS7-2) and the fleet-registration entrypoint
    to make P1 + specialists + Slack agents enumerable. Non-fatal; never raises.
    """
    if not ENABLED:
        return False
    if not name:
        return False
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                f"{AGENT_SERVICE_URL}/agents/register",
                json={"name": name, "webhook_url": webhook_url},
            )
        if resp.status_code in (200, 201):
            logger.debug("agent registered with :8510 (%s)", name)
            return True
        logger.debug("agent register → :8510 returned %d (non-fatal)", resp.status_code)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent registration skipped (%s) — :8510 unavailable", exc)
        return False
