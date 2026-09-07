"""Classic opt-ins retain logs and avoid unnecessary automatic session splits."""
import io
import logging
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.model_metadata import estimate_messages_tokens_rough
from run_agent import AIAgent, _set_console_quiet_logging


@pytest.fixture
def compression_case():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(model="test/model", protect_first_n=2,
                                       protect_last_n=2, quiet_mode=True)
    compressor.tail_token_budget = 40
    messages = [{"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"message {i} " + "detail " * 20} for i in range(20)]
    # The pruning algorithm has separate tests. Here its output contains the
    # same turns and reasoning, with an old result shortened by the cheap pass.
    messages[3]["reasoning"] = "keep this reasoning"
    pruned = [dict(m) for m in messages]
    pruned[3]["content"] = "[old result pruned]"
    compressor._prune_old_tool_results = MagicMock(return_value=(pruned, 1))
    compressor._generate_summary = MagicMock(return_value="summary of earlier turns")
    return compressor, messages, pruned


def test_pruning_that_fits_skips_summary_and_retains_reasoning(compression_case):
    compressor, messages, pruned = compression_case
    result = compressor.compress(messages, request_overhead_tokens=100,
                                 prune_only_if_fits=True)
    assert result == pruned
    assert result[3]["reasoning"] == messages[3]["reasoning"]
    assert len(result) == len(messages)
    assert compressor._last_compression_skipped is True
    assert compressor.compression_count == 0
    compressor._generate_summary.assert_not_called()


@pytest.mark.parametrize("mode", ["default", "focus", "overhead", "oversized"])
def test_summary_still_runs_when_required(compression_case, mode):
    compressor, messages, pruned = compression_case
    kwargs = {"prune_only_if_fits": True}
    if mode == "default":
        kwargs = {}  # Default behavior remains summary-based.
    elif mode == "focus":
        kwargs["focus_topic"] = "database decisions"
    elif mode == "overhead":
        kwargs["request_overhead_tokens"] = (
            compressor.threshold_tokens - estimate_messages_tokens_rough(pruned)
        )  # Equality is still pressure: skip requires strictly below threshold.
    else:
        compressor.threshold_tokens = estimate_messages_tokens_rough(pruned) - 1
    result = compressor.compress(messages, **kwargs)
    assert compressor._last_compression_skipped is False
    compressor._generate_summary.assert_called_once()
    assert len(result) < len(messages)


def test_skip_state_resets_before_short_transcript_return(compression_case):
    compressor, messages, _ = compression_case
    compressor.compress(messages, prune_only_if_fits=True)
    assert compressor._last_compression_skipped is True
    compressor.compress(messages[:2], prune_only_if_fits=True)
    assert compressor._last_compression_skipped is False
    compressor._last_compression_skipped = True
    compressor.on_session_reset()
    assert compressor._last_compression_skipped is False


def _bare_agent(compressor):
    agent = object.__new__(AIAgent)
    agent.context_compressor = compressor
    agent._compression_prune_before_summary = True
    agent.session_id = "original-session"
    agent.model = "test/model"
    agent.log_prefix = ""
    agent._memory_manager = None
    agent._cached_system_prompt = "cached system prompt"
    agent.tools = []
    agent._todo_store = MagicMock()
    agent._todo_store.format_for_injection.return_value = ""
    agent._session_db = MagicMock()
    agent._last_flushed_db_idx = 10
    agent._invalidate_system_prompt = MagicMock()
    agent._build_system_prompt = MagicMock(return_value="rebuilt prompt")
    agent._vprint = MagicMock()
    return agent


def test_automatic_prune_preserves_session_prompt_and_flush_cursor(compression_case):
    compressor, messages, pruned = compression_case
    agent = _bare_agent(compressor)
    result, prompt = agent._compress_context(messages, None, allow_prune_only=True)
    assert result == pruned
    assert prompt == agent._cached_system_prompt == "cached system prompt"
    assert agent.session_id == "original-session"
    assert agent._last_flushed_db_idx == 10
    agent._session_db.end_session.assert_not_called()
    agent._session_db.create_session.assert_not_called()
    agent._invalidate_system_prompt.assert_not_called()
    agent._build_system_prompt.assert_not_called()


@pytest.mark.parametrize("mode", ["disabled", "manual_or_recovery", "focus", "large_tools"])
def test_agent_requires_config_and_automatic_caller_to_skip(compression_case, mode):
    compressor, messages, _ = compression_case
    agent = _bare_agent(compressor)
    agent._session_db = None
    kwargs = {"allow_prune_only": True}
    if mode == "disabled":
        agent._compression_prune_before_summary = False
    elif mode == "manual_or_recovery":
        kwargs = {}  # All existing manual and provider-error callers use this.
    elif mode == "focus":
        kwargs["focus_topic"] = "keep the API contract"
    else:
        agent.tools = [{"description": "x" * (compressor.threshold_tokens * 4)}]
    _, prompt = agent._compress_context(messages, None, **kwargs)
    compressor._generate_summary.assert_called_once()
    agent._invalidate_system_prompt.assert_called_once()
    assert prompt == "rebuilt prompt"


@pytest.mark.parametrize("setting, expected", [(None, False), (False, False), (True, True)])
def test_prune_config_is_explicit_opt_in(setting, expected):
    config = {} if setting is None else {"compression": {"prune_before_summary": setting}}
    with (patch("hermes_cli.config.load_config", return_value=config),
          patch("run_agent.get_tool_definitions", return_value=[]),
          patch("run_agent.check_toolset_requirements", return_value={}),
          patch("run_agent.OpenAI")):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        model="test/model", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    assert agent._compression_prune_before_summary is expected


def test_quiet_console_keeps_file_records_and_verbose_removes_filter(tmp_path):
    root = logging.getLogger()
    console_output = io.StringIO()
    console = logging.StreamHandler(console_output)
    log_path = tmp_path / "agent.log"
    file_handler = logging.FileHandler(log_path)
    test_logger = logging.getLogger("run_agent.classic_logging_test")
    prior_level, prior_root_level = test_logger.level, root.level
    prior_handlers = root.handlers[:]
    root.handlers = [console, file_handler]
    test_logger.setLevel(logging.INFO)
    root.setLevel(logging.INFO)
    try:
        _set_console_quiet_logging(True)
        _set_console_quiet_logging(True)  # Agent initialization is per turn.
        assert len(console.filters) == 1
        test_logger.info("retained info")
        test_logger.warning("retained warning")
        test_logger.error("visible error")
        file_handler.flush()
        assert "retained info" in log_path.read_text()
        assert "retained warning" in log_path.read_text()
        assert console_output.getvalue().strip() == "visible error"
        _set_console_quiet_logging(False)
        test_logger.info("verbose info")
        assert "verbose info" in console_output.getvalue()
        assert not console.filters
        assert not file_handler.filters
    finally:
        root.handlers = prior_handlers
        root.setLevel(prior_root_level)
        test_logger.setLevel(prior_level)
        console.close()
        file_handler.close()


def test_cli_verbose_cycle_updates_console_filter():
    from cli import HermesCLI

    shell = object.__new__(HermesCLI)
    shell.tool_progress_mode = "all"
    shell.agent = None
    with patch("run_agent._set_console_quiet_logging") as quiet, patch("cli._cprint"):
        root = logging.getLogger()
        prior_level = root.level
        try:
            shell._toggle_verbose()
            assert shell.verbose is True
            quiet.assert_called_with(False)
            shell._toggle_verbose()
            assert shell.verbose is False
            quiet.assert_called_with(True)
        finally:
            root.setLevel(prior_level)


@pytest.mark.parametrize("supports_focus", [False, True])
def test_strict_plugin_signature_survives_opt_in(supports_focus):
    class OldPlugin:
        compression_count = 1

        def compress(self, messages, current_tokens=None):
            return messages[-2:]

    class FocusPlugin(OldPlugin):
        def compress(self, messages, current_tokens=None, focus_topic=None):
            return messages[-2:]

    engine = FocusPlugin() if supports_focus else OldPlugin()
    agent = _bare_agent(engine)
    agent._session_db = None
    messages = [{"role": "user", "content": str(i)} for i in range(5)]
    result, prompt = agent._compress_context(messages, None, allow_prune_only=True)
    assert result == messages[-2:]
    assert prompt == "rebuilt prompt"
