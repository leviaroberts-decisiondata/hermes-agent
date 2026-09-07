"""Regression test for #17929: AIAgent.__init__ should try fallback_model
when primary provider credentials are exhausted."""
import pytest
from unittest.mock import patch, MagicMock
from run_agent import AIAgent


def _make_tool_defs():
    return [{"type": "function", "function": {"name": "web_search",
             "description": "search", "parameters": {"type": "object", "properties": {}}}}]


def _mock_client(api_key="fb-key-1234567890", base_url="https://fb.example.com/v1"):
    c = MagicMock()
    c.api_key = api_key
    c.base_url = base_url
    c._default_headers = None
    return c


def test_init_tries_fallback_when_primary_returns_none():
    """When resolve_provider_client returns None for primary but succeeds for
    a fallback entry, __init__ should NOT raise RuntimeError."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "tencent-token-plan":
            return fb, "kimi2.5"
        return None, None  # primary exhausted

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        agent = AIAgent(
            provider="alibaba-coding-plan",
            model="qwen3.6-plus",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "tencent-token-plan", "model": "kimi2.5"}],
        )
        assert agent.provider == "tencent-token-plan"
        assert agent.model == "kimi2.5"
        assert agent._fallback_activated is True
        assert agent._fallback_events[0]["reason"] == "primary_credentials_unavailable"


def test_init_raises_when_no_fallback_configured():
    """When primary returns None and no fallback is set, should raise."""
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        with pytest.raises(RuntimeError, match="no API key was found"):
            AIAgent(
                provider="alibaba-coding-plan",
                model="qwen3.6-plus",
                api_key=None,
                base_url=None,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=None,
            )


@pytest.mark.parametrize("provider,enabled,model", [
    ("copilot-acp", False, "supported"),
    ("copilot", False, "unsupported"),
    ("copilot-acp", True, "unsupported"),
])
def test_init_fallback_rejects_disallowed_copilot_before_resolution(monkeypatch, provider, enabled, model):
    monkeypatch.setenv("HERMES_ENABLE_COPILOT_ACP_FALLBACK", "1" if enabled else "0")
    calls = []
    def resolve(name, **kwargs):
        calls.append(name)
        return (_mock_client(), "kimi2.5") if name == "tencent-token-plan" else (None, None)
    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("hermes_cli.models.provider_model_ids", return_value=["supported"]),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(provider="alibaba-coding-plan", model="qwen3.6-plus",
                        api_key=None, base_url=None, quiet_mode=True,
                        skip_context_files=True, skip_memory=True,
                        fallback_model=[{"provider": provider, "model": model},
                                        {"provider": "tencent-token-plan", "model": "kimi2.5"}])
    assert provider not in calls
    assert agent.provider == "tencent-token-plan"


def test_enabled_supported_acp_is_admitted(monkeypatch):
    from run_agent import _fallback_candidate_allowed
    monkeypatch.setenv("HERMES_ENABLE_COPILOT_ACP_FALLBACK", "1")
    with (
        patch("hermes_cli.models.provider_model_ids", return_value=["supported"]),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
    ):
        assert _fallback_candidate_allowed("copilot-acp", "supported")
