"""Source-level authority boundary for P1-only dispatch tools (WTS 17cbc96c).

The 2026-08-10 crossover: PTG and Azul Hermes invoked `route_to_lane`, their
work entered P1's shared lane/reaper infrastructure, and the callbacks came back
into P1's Telegram session — after which P1 wrote reconciliation notes and
attachments onto *client* WTS records.

Configuration alone cannot prevent that. `p1-dispatch` is a **default-on**
toolset (`toolsets.py`), so a home is exposed unless it explicitly opts out via
`agent.disabled_toolsets`. A new home, a restored backup, a hand-edited config
or a bad merge silently re-opens the hole. This module is the seam that holds
when configuration regresses.

Design rules, from the scope:

* Caller identity comes from **trusted runtime configuration** (HERMES_HOME via
  `get_active_home_id`), never from a model-supplied argument. A model cannot
  argue its way past this by passing a different instance name.
* Only the canonical P1 instance is permitted.
* Missing, malformed, ambiguous or non-P1 identity is **rejected**, not guessed.
  An unidentified caller is not P1.
* Rejection is non-retryable and produces **no** side effect: no lane run, no
  callback registration, no WTS mutation, no attachment, no chain advance.

Deliberately dependency-free and cheap so it can be the first statement in each
guarded tool, before any argument parsing or I/O.
"""

from __future__ import annotations

# Tools this boundary protects. Mirrors the `p1-dispatch` toolset membership in
# toolsets.py; kept here so the source guard does not depend on config state.
P1_DISPATCH_TOOLS = ("route_to_lane", "wts_bind", "chain_status")


def active_caller_id() -> str:
    """Identity of the Hermes instance in this process, or "" if unidentifiable."""
    try:
        from hermes_cli.profiles import get_active_home_id
        return get_active_home_id() or ""
    except Exception:
        # Never let an identity failure read as authority.
        return ""


def _is_p1_internal() -> bool:
    """True for P1 and for P1's own ``~/.hermes/profiles/<name>`` specialists.

    The trust boundary is the ~/.hermes TREE. Testing ``caller == "default"``
    refused all 12 specialist profiles — a capability they use across 500+
    recorded sessions — while the crossover this guard exists to stop came from
    SIBLING homes. Any resolution failure is False, so this still fails closed.
    """
    try:
        from hermes_cli.profiles import is_p1_internal_home
        return bool(is_p1_internal_home())
    except Exception:
        return False


def _describe(caller: str) -> str:
    return caller if caller else "<unidentified>"


def p1_authority_error(tool_name: str) -> "str | None":
    """None if this process may use `tool_name`; an error message if it may not.

    The message is deliberately explicit that the refusal is final — a retry
    from the same home will always fail — so an agent reports the boundary
    instead of looping against it.
    """
    caller = active_caller_id()
    if _is_p1_internal():
        return None
    return (
        f"{tool_name}: refused — this Hermes instance ({_describe(caller)}) is not "
        f"authorised to use P1 dispatch capabilities. "
        f"{', '.join(P1_DISPATCH_TOOLS)} route work through P1's shared specialist-lane "
        f"and callback infrastructure, so running them from another home sends that "
        f"home's work into P1's session and can write P1-authored records onto client "
        f"WTS tasks (WTS 17cbc96c). "
        f"This is a source-level boundary and is NOT retryable: it does not depend on "
        f"toolset configuration, and no run, callback, WTS change or chain advance was "
        f"created. Use this home's own WTS workflow tools, or ask P1 to dispatch."
    )


def require_p1_caller(tool_name: str) -> "str | None":
    """Guard for the top of every P1-only tool. Returns a `tool_error` or None.

    Usage — must be the first statement, before any argument handling or I/O::

        denied = require_p1_caller("route_to_lane")
        if denied:
            return denied
    """
    message = p1_authority_error(tool_name)
    if message is None:
        return None
    try:
        from tools.registry import tool_error
        return tool_error(message)
    except Exception:
        return message
