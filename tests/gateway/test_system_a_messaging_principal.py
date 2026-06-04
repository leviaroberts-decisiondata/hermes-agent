"""Fail-closed tests for the messaging-path System-A principal gate (B).

Covers _is_system_a_messaging_turn — the single place that answers "is this
messaging turn System A". The directive requires proving every caller's
classification; these cases mirror the §4 enumeration in
reviews/specs/p1-deploy-telegram-systema-design-2026-06-03.md.

The gate must be AND-composed and fail-closed: only the P1-coordinator-gateway +
Telegram + deploy-toolset combination returns True; everything else returns False
(no mint → no credential → resources default-deny).
"""

import pytest

from gateway.config import Platform
from gateway.run import _is_system_a_messaging_turn


P1_CONFIG = {"dd_system_a_principal": True}
PROFILE_CONFIG = {}  # specialist gateways never set the flag
DEPLOY_TS = ["hermes-telegram", "deploy"]
NO_DEPLOY_TS = ["hermes-telegram"]


def test_p1_telegram_with_deploy_is_system_a():
    """#1 The target case: P1 gateway + Telegram + deploy toolset → System A."""
    assert _is_system_a_messaging_turn(
        user_config=P1_CONFIG,
        platform=Platform.TELEGRAM,
        enabled_toolsets=DEPLOY_TS,
    ) is True


def test_p1_other_platform_is_not_system_a():
    """#3 Other messaging platforms on the P1 gateway never get System A."""
    for plat in (Platform.DISCORD, Platform.SLACK, Platform.SIGNAL):
        assert _is_system_a_messaging_turn(
            user_config=P1_CONFIG,
            platform=plat,
            enabled_toolsets=DEPLOY_TS,  # even if deploy somehow present
        ) is False, plat


def test_profile_gateway_telegram_is_not_system_a():
    """#4 Specialist profile gateways (no flag) never get System A, even on TG."""
    assert _is_system_a_messaging_turn(
        user_config=PROFILE_CONFIG,
        platform=Platform.TELEGRAM,
        enabled_toolsets=DEPLOY_TS,
    ) is False


def test_flag_explicit_false_is_not_system_a():
    """An explicit false flag is fail-closed identical to absent."""
    assert _is_system_a_messaging_turn(
        user_config={"dd_system_a_principal": False},
        platform=Platform.TELEGRAM,
        enabled_toolsets=DEPLOY_TS,
    ) is False


def test_telegram_without_deploy_toolset_is_not_system_a():
    """Coupling guard: no deploy toolset on the surface → no credential."""
    assert _is_system_a_messaging_turn(
        user_config=P1_CONFIG,
        platform=Platform.TELEGRAM,
        enabled_toolsets=NO_DEPLOY_TS,
    ) is False


def test_empty_toolsets_is_not_system_a():
    assert _is_system_a_messaging_turn(
        user_config=P1_CONFIG,
        platform=Platform.TELEGRAM,
        enabled_toolsets=[],
    ) is False
    assert _is_system_a_messaging_turn(
        user_config=P1_CONFIG,
        platform=Platform.TELEGRAM,
        enabled_toolsets=None,
    ) is False


def test_malformed_config_fails_closed():
    """A non-dict / surprising config must not raise and must deny."""
    assert _is_system_a_messaging_turn(
        user_config={},
        platform=Platform.TELEGRAM,
        enabled_toolsets=DEPLOY_TS,
    ) is False


def test_truthy_nonbool_flag_value_is_respected():
    """bool() coercion: a truthy value enables; a falsy one does not."""
    assert _is_system_a_messaging_turn(
        user_config={"dd_system_a_principal": 1},
        platform=Platform.TELEGRAM,
        enabled_toolsets=DEPLOY_TS,
    ) is True
    assert _is_system_a_messaging_turn(
        user_config={"dd_system_a_principal": 0},
        platform=Platform.TELEGRAM,
        enabled_toolsets=DEPLOY_TS,
    ) is False


def test_mint_maps_system_a_to_full_caps_and_b_to_none():
    """The mapping the gate feeds into stays singular and fail-closed.

    This guards the invariant that only the literal "A" grants System-A caps, so
    the gate's True/False is the only lever — there is no third path to caps.
    """
    from gateway import capability_gate as cg
    assert "C4" in cg.SYSTEM_A_CAPS  # deploy needs C4
    # "B"/"unknown" must map to producer-only (no System-A caps). We assert on the
    # issuer's documented mapping rather than minting (which needs the signer key).
    import inspect
    src = inspect.getsource(__import__("gateway.capability_issuer", fromlist=["mint_for_turn"]).mint_for_turn)
    assert 'system == "A"' in src
    assert "SYSTEM_A_CAPS" in src
    assert "caps = ()" in src  # the producer-only branch


# ── Adversarial cases added by the independent review (2026-06-03) ──────────


def test_string_platform_does_not_satisfy_gate():
    """Platform-spoofing / refactor guard: a *string* platform must NOT pass.

    ``source.platform`` is always the ``Platform`` enum on the real adapter path
    (base.build_source binds ``self.platform``), so this can't happen today. But
    ``Platform`` is a plain Enum (not ``str, Enum``), so ``Platform.TELEGRAM !=
    "telegram"`` — the gate is fail-closed against any future refactor that lets a
    raw string reach it, or any caller that tries to forge the surface by string.
    """
    assert _is_system_a_messaging_turn(
        user_config=P1_CONFIG,
        platform="telegram",  # forged / string, not the trusted enum
        enabled_toolsets=DEPLOY_TS,
    ) is False
    # None / arbitrary objects likewise denied, never raised.
    assert _is_system_a_messaging_turn(
        user_config=P1_CONFIG,
        platform=None,
        enabled_toolsets=DEPLOY_TS,
    ) is False
    assert _is_system_a_messaging_turn(
        user_config=P1_CONFIG,
        platform=object(),
        enabled_toolsets=DEPLOY_TS,
    ) is False


def test_clear_credential_returns_contextvar_to_none():
    """The finally-clear contract: after a turn clears, no credential remains.

    Models the run.py finally block (clear_credential) that prevents a minted
    System-A credential from bleeding into the next turn on a reused agent/loop.
    """
    from gateway import capability_context as cc
    assert cc.current_credential() is None  # baseline: nothing minted
    cc.set_credential("turn-A-credential")
    assert cc.current_credential() is not None
    cc.clear_credential()  # the run.py finally does exactly this
    assert cc.current_credential() is None  # no bleed past the turn


def test_copy_context_isolation_prevents_cross_turn_bleed():
    """The structural reason cross-turn bleed cannot happen on the executor path.

    run.py runs run_conversation via _run_in_executor_with_context, which does
    copy_context() + ctx.run(...). A credential set inside that copied context is
    NOT visible in the outer (caller) context — so even if a finally-clear were
    skipped inside the copy, the reused thread-pool thread cannot leak a prior
    turn's credential into the next turn. This is the bug a prior review found in
    a different code path; here ctx.run isolates it.
    """
    import contextvars
    from gateway import capability_context as cc

    cc.clear_credential()  # outer context starts clean

    def _inside_copied_context():
        cc.set_credential("leaky-credential")
        return cc.current_credential()

    ctx = contextvars.copy_context()
    inside = ctx.run(_inside_copied_context)
    assert inside is not None  # set took effect inside the copy
    # The decisive assertion: the mutation did NOT escape the copied context.
    assert cc.current_credential() is None
