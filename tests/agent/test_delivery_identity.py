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


def test_lane_profile_set_has_twelve_lanes():
    # graduation P1a (WTS 2911977a): security-review + video-review added to the
    # lane audience set (both set tree_injection:true and previously fell back
    # SILENTLY to the p1-default coordinator view — a deploy-authority leak).
    assert len(_LANE_PROFILE_AUDIENCES) == 12
    assert "dd-design" in _LANE_PROFILE_AUDIENCES
    assert "security-review" in _LANE_PROFILE_AUDIENCES
    assert "video-review" in _LANE_PROFILE_AUDIENCES
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


# --- P2: System A/B boundary (route_to_lane removal) ----------------------------

# The direct System-A dispatch tool a System B (delivery) turn must not carry.
# terminal / execute_code are *indirect* A-vectors (the gateway terminal tool can
# exec ~/.hermes/bin/dd-lane-run) but are intentionally LEFT in the delivery
# toolset for now — the delivery persona relies on shell to build deliverables;
# closing that vector is a separate decision. These tests pin route_to_lane only.
_SYSTEM_A_DISPATCH_TOOL = "route_to_lane"
# Tools deliberately retained in delivery despite being indirect vectors.
_RETAINED_PENDING_DECISION = ("terminal", "execute_code")


def _build_boundary_agent(skip_ctx, load_soul):
    """A toolset-bearing agent on the api_server platform with all toolsets."""
    from run_agent import AIAgent
    from toolsets import get_all_toolsets

    return AIAgent(
        model="x", api_key="k", base_url="http://127.0.0.1:1", quiet_mode=True,
        platform="api_server", enabled_toolsets=sorted(get_all_toolsets()),
        skip_context_files=skip_ctx, load_soul_identity=load_soul,
    )


def test_delivery_turn_has_no_route_to_lane():
    """A delivery build (skip_context_files + not load_soul_identity) must expose
    NO route_to_lane — neither to the model (self.tools) nor to dispatch
    validation (valid_tool_names)."""
    agent = _build_boundary_agent(skip_ctx=True, load_soul=False)
    schema_names = {t["function"]["name"] for t in agent.tools}
    assert _SYSTEM_A_DISPATCH_TOOL not in schema_names, (
        f"delivery model schema leaks {_SYSTEM_A_DISPATCH_TOOL}")
    assert _SYSTEM_A_DISPATCH_TOOL not in agent.valid_tool_names, (
        f"delivery valid_tool_names leaks {_SYSTEM_A_DISPATCH_TOOL}")
    # schema and validation set must agree — no tool the model sees but the
    # validator rejects, and none the validator allows but the model can't see.
    assert schema_names == agent.valid_tool_names


def test_non_delivery_turn_keeps_route_to_lane():
    """A normal System A turn must STILL carry route_to_lane (no over-broad strip)."""
    agent = _build_boundary_agent(skip_ctx=False, load_soul=False)
    assert _SYSTEM_A_DISPATCH_TOOL in agent.valid_tool_names, (
        f"System A lost {_SYSTEM_A_DISPATCH_TOOL}")


def test_delivery_turn_retains_shell_and_delivery_capabilities():
    """The strip is surgical: route_to_lane only. A delivery turn keeps the tools
    it needs to actually deliver (read/write docs, delegate), AND — pending the
    terminal/execute_code decision — still keeps its shell tools."""
    agent = _build_boundary_agent(skip_ctx=True, load_soul=False)
    for needed in ("read_file", "write_file", "delegate_task", "todo"):
        assert needed in agent.valid_tool_names, f"delivery lost {needed!r}"
    for retained in _RETAINED_PENDING_DECISION:
        assert retained in agent.valid_tool_names, (
            f"delivery unexpectedly lost {retained!r} — strip widened beyond "
            f"route_to_lane without the gating decision")
