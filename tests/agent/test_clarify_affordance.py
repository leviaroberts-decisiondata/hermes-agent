"""Fleet-repair 1.1 (WTS 7e1d32e9): never advertise a dead clarify.

Background: `clarify` sat in _HERMES_CORE_TOOLS and was serialized into every
gateway turn, but no gateway callback was ever wired (`grep -rn clarify
gateway/` = zero hits), so every call returned "Clarify tool is not available
in this execution context" and the agent guessed instead of asking. The rule
now: no clarify_callback at construction ⇒ no clarify in the tool list — an
agent that cannot ask must know it cannot, and puts the question in its answer.
"""


def _mk_agent(**overrides):
    from run_agent import AIAgent
    kwargs = dict(
        model="openai/gpt-4o-mini",
        provider="openrouter",
        api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
        enabled_toolsets=["hermes-telegram"],
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


def test_clarify_stripped_without_callback():
    agent = _mk_agent()
    assert "clarify" not in agent.valid_tool_names, (
        "clarify advertised with no callback wired — the dead affordance is back"
    )


def test_clarify_kept_with_callback():
    agent = _mk_agent(clarify_callback=lambda question, choices=None: "answer")
    assert "clarify" in agent.valid_tool_names, (
        "clarify missing even though a real callback was wired"
    )
