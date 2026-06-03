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


def _service_capability_headers() -> dict:
    """Mint a short-lived SERVICE-tier capability credential for the gateway's
    own control-plane writes to the gated :8510 registry/job-graph endpoints.

    The agent-orchestration (:8510) capability gate (commit 4eeb2eac7) requires a
    signed C6 (R-REG) credential on the P1-ledger writes (/jobs/{id}/p1-claim,
    PATCH /jobs/{id}/p1-status). These writes are GATEWAY INFRASTRUCTURE — the
    gateway recording that a job was claimed/completed — NOT a model effect. They
    must therefore succeed for EVERY turn, delivery turns included, so this
    credential is minted independently of the turn's System classification (it is
    the gateway acting, not the model). It carries only the cap these writes
    need (C6 registry), with a short TTL, signed by the same key the gate
    verifies. If the signer key is unavailable, returns no header and
    the call falls back to its existing non-fatal 403/skip path.

    Mirrors the in-process post_with_capability pattern (capability_egress); the
    credential is never placed in any subprocess env.
    """
    try:
        from gateway import capability_gate as _cg
        from gateway import capability_context as _cc

        secret = _cc.get_signer_secret()
        if not secret:
            return {}
        credential = _cg.mint(
            system="A",
            # Both gated writes this credential authorizes (/jobs/{id}/p1-claim,
            # PATCH /jobs/{id}/p1-status) require C6 only — scope to exactly that.
            capabilities=(_cg.CAP_C6_REG,),
            session_id="gateway-service",
            secret=secret,
            ttl_seconds=120,
        )
        return {_cc.CAPABILITY_HEADER: credential}
    except Exception:  # noqa: BLE001 — never block a turn on credential minting
        return {}


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


def register_session_get_job_id(
    session_id: str,
    *,
    model: str | None = None,
    label: str | None = None,
    session_key: str | None = None,
    initiated_by: str = "hermes-gateway",
) -> str | None:
    """Best-effort POST /gateway/session-created, returning the linked :8510 job_id.

    Same wire call as register_session_created (idempotent on the :8510 side, keyed
    on session_id — returns the EXISTING job on a duplicate), but parses the
    response so the caller can use the job_id to drive the P1 ledger (p1-claim /
    p1-status). Returns None on flag-off, missing session_id, non-2xx, a down/slow
    :8510, or any error. NEVER raises — registration must never block a turn.
    """
    if not ENABLED:
        return None
    if not session_id:
        return None
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
            job_id = (resp.json() or {}).get("job_id") or None
            logger.debug("session-created → :8510 job_id=%s (%s)", job_id, session_id)
            return job_id
        logger.debug("session-created → :8510 returned %d (non-fatal)", resp.status_code)
        return None
    except Exception as exc:  # noqa: BLE001 — registration must never block a turn
        logger.debug("session-created (job_id) skipped (%s) — :8510 unavailable", exc)
        return None


def p1_claim_job(
    job_id: str,
    *,
    owner: str | None = None,
    priority: str | None = None,
    eta: str | None = None,
) -> bool:
    """Best-effort POST /jobs/{job_id}/p1-claim — the A-side ledger write that
    records P1 has begun working this job. Idempotent on the :8510 side (upserts
    the ledger row per job_id). Non-fatal; NEVER raises — a ledger write must
    never block or alter a turn.
    """
    if not ENABLED:
        return False
    if not job_id:
        return False
    body: dict = {}
    if owner is not None:
        body["owner"] = owner
    if priority is not None:
        body["priority"] = priority
    if eta is not None:
        body["eta"] = eta
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                f"{AGENT_SERVICE_URL}/jobs/{job_id}/p1-claim",
                json=body,
                headers=_service_capability_headers(),
            )
        if resp.status_code in (200, 201):
            logger.debug("p1-claim persisted to :8510 (job=%s)", job_id)
            return True
        logger.debug("p1-claim → :8510 returned %d (non-fatal)", resp.status_code)
        return False
    except Exception as exc:  # noqa: BLE001 — ledger write must never block a turn
        logger.debug("p1-claim skipped (%s) — :8510 unavailable", exc)
        return False


def p1_set_status(
    job_id: str,
    *,
    status: str | None = None,
    notes: str | None = None,
) -> bool:
    """Best-effort PATCH /jobs/{job_id}/p1-status — records the P1 ledger status
    transition on turn completion (done) or error. Idempotent on the :8510 side.
    Non-fatal; NEVER raises — a ledger write must never block or alter a turn.
    """
    if not ENABLED:
        return False
    if not job_id:
        return False
    body: dict = {}
    if status is not None:
        body["status"] = status
    if notes is not None:
        body["notes"] = notes
    if not body:
        return False
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.patch(
                f"{AGENT_SERVICE_URL}/jobs/{job_id}/p1-status",
                json=body,
                headers=_service_capability_headers(),
            )
        if resp.status_code in (200, 201):
            logger.debug("p1-status persisted to :8510 (job=%s status=%s)", job_id, status)
            return True
        logger.debug("p1-status → :8510 returned %d (non-fatal)", resp.status_code)
        return False
    except Exception as exc:  # noqa: BLE001 — ledger write must never block a turn
        logger.debug("p1-status skipped (%s) — :8510 unavailable", exc)
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
