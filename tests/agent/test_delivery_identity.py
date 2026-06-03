"""
test_delivery_identity.py — WS1 §4 + WS4 audience resolution (FAIL-not-skip).

Asserts the resolver maps identities to the right audience, and that a delivery
turn (replace_identity) resolves to the delivery role — not the coordinator.
Uses a fixture tree so it does not depend on the live ~/.hermes tree.
"""
import os

import pytest

from agent.prompt_builder import compose_operating_model, _LANE_PROFILE_AUDIENCES


@pytest.fixture
def tree(tmp_path):
    """Minimal operating-model tree: core + a coordinator view + a specialist view."""
    om = tmp_path / "operating-model"
    (om / "agent-roles" / "specialists").mkdir(parents=True)
    (om / "_core.md").write_text("# Core\nShared inter-layer boundary.\n")
    (om / "agent-roles" / "p1-specialists.md").write_text(
        "# Role\nP1 coordinates the specialist lanes. System-expertise layer.\n"
    )
    (om / "agent-roles" / "p1-default.md").write_text("# Role\nYou are P1, the coordinator.\n")
    (om / "agent-roles" / "slack-project-agent.md").write_text(
        "# Role\nYou are the Slack delivery agent. You produce deliverables end-to-end.\n"
    )
    (om / "agent-roles" / "specialists" / "dd-design.md").write_text(
        "# Role\nYou are the design specialist. You produce design artifacts.\n"
    )
    return tmp_path


def test_compose_lane_profile_gets_own_specialist_view(tree):
    body = compose_operating_model(tree, "dd-design")
    assert "design specialist" in body
    assert "P1 coordinates the specialist lanes" not in body  # NOT the coordinator view


def test_compose_delivery_audience_gets_delivery_view_not_coordinator(tree):
    body = compose_operating_model(tree, "slack-project-agent")
    assert "Slack delivery agent" in body
    assert "P1 coordinates the specialist lanes" not in body


def test_compose_default_gets_coordinator_view(tree):
    body = compose_operating_model(tree, "p1-default")
    assert "coordinator" in body.lower()


def test_compose_falls_soft_to_core_when_view_missing(tree):
    body = compose_operating_model(tree, "no-such-audience")
    assert "Shared inter-layer boundary" in body  # core alone
    assert "P1 coordinates" not in body


def test_lane_profile_set_has_ten_lanes():
    assert len(_LANE_PROFILE_AUDIENCES) == 10
    assert "dd-design" in _LANE_PROFILE_AUDIENCES
    assert "default" not in _LANE_PROFILE_AUDIENCES  # default → p1-default, not a lane


def test_resolver_delivery_turn_resolves_slack_role():
    """A delivery turn (skip_context_files + not load_soul_identity) → slack-project-agent."""
    from run_agent import AIAgent

    agent = AIAgent(
        model="x", api_key="k", base_url="http://127.0.0.1:1", quiet_mode=True,
        platform="api_server", skip_context_files=True, load_soul_identity=False,
    )
    assert agent._resolve_context_audience() == "slack-project-agent"


def test_resolver_non_delivery_default_is_coordinator():
    """A normal default-profile turn resolves to p1-default (not the generic p1-specialists)."""
    from run_agent import AIAgent

    agent = AIAgent(model="x", api_key="k", base_url="http://127.0.0.1:1", quiet_mode=True,
                    platform="api_server")
    aud = agent._resolve_context_audience()
    assert aud in ("p1-default",), f"expected p1-default, got {aud}"
    assert aud != "p1-specialists"


def test_resolver_specialist_survives_profile_helper_failure(monkeypatch, tmp_path):
    """If get_active_profile_name() raises, a specialist must NOT silently demote.

    The resolver derives the profile from HERMES_HOME instead, so a specialist
    whose profile-helper throws still gets its own lane view — never the
    coordinator view, and never the generic p1-specialists.
    """
    import hermes_cli.profiles as profiles_mod
    from run_agent import AIAgent

    def _boom():
        raise RuntimeError("profile helper down")

    monkeypatch.setattr(profiles_mod, "get_active_profile_name", _boom)
    home = tmp_path / ".hermes" / "profiles" / "dd-design"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = AIAgent(model="x", api_key="k", base_url="http://127.0.0.1:1", quiet_mode=True,
                    platform="api_server")
    aud = agent._resolve_context_audience()
    assert aud == "dd-design", f"specialist demoted on helper failure: got {aud}"
    assert aud != "p1-specialists"


def test_resolver_delivery_never_returns_generic_p1_specialists(tmp_path, monkeypatch):
    """A delivery turn must NEVER return the generic coordinator-of-specialists view."""
    from run_agent import AIAgent

    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    agent = AIAgent(model="x", api_key="k", base_url="http://127.0.0.1:1", quiet_mode=True,
                    platform="api_server", skip_context_files=True, load_soul_identity=False)
    aud = agent._resolve_context_audience()
    assert aud == "slack-project-agent"
    assert aud != "p1-specialists"
