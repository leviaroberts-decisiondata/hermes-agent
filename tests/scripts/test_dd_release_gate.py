"""Release checks cannot inherit live credentials before pytest fixtures run."""

from pathlib import Path
import runpy
import subprocess
import sys

import pytest


RUNNER = Path(__file__).resolve().parents[2] / "scripts" / "run_dd_release_tests.py"


def test_collection_environment_is_private_and_failure_propagates(monkeypatch):
    module = runpy.run_path(str(RUNNER))
    inherited = (
        "HERMES_AUTH_STORE_PATH", "API_SERVER_KEY", "HERMES_DELIVERY_KEY",
        "AWS_ACCESS_KEY_ID", "MODAL_TOKEN_ID", "FEISHU_ENCRYPT_KEY",
        "VOICE_TOOLS_OPENAI_KEY", "OPENAI_BASE_URL", "PYTHONPATH",
    )
    for name in inherited:
        monkeypatch.setenv(name, "synthetic-value")
    monkeypatch.setenv("DD_OBS_INGEST_URL", "http://example.invalid")
    monkeypatch.setattr(sys, "argv", [str(RUNNER)])
    seen_home = []

    def fake_pytest(args, *, cwd, env):
        assert all(name not in env for name in inherited)
        assert env["DD_OBS_INGEST_URL"] == "http://127.0.0.1:1"
        home = Path(env["HERMES_HOME"])
        assert home.is_dir() and not list(home.iterdir())
        seen_home.append(home)
        assert Path(cwd) == RUNNER.parent.parent
        assert args[0] == sys.executable
        assert all(name in args for name in module["REQUIRED"])
        return 7

    monkeypatch.setattr(subprocess, "call", fake_pytest)
    assert module["main"]() == 7
    assert seen_home and not seen_home[0].exists()


def test_missing_required_gate_fails_before_pytest(monkeypatch, tmp_path):
    module = runpy.run_path(str(RUNNER))
    module["main"].__globals__["ROOT"] = tmp_path
    monkeypatch.setattr(subprocess, "call", lambda *a, **kw: pytest.fail("must not run"))
    with pytest.raises(SystemExit, match="Required release tests missing"):
        module["main"]()


@pytest.mark.parametrize("soft,hard,expected", [(256, 8192, 4096), (256, 1024, 1024), (8192, 8192, None)])
def test_test_process_limit_respects_existing_hard_limit(monkeypatch, soft, hard, expected):
    resource = pytest.importorskip("resource")
    module = runpy.run_path(str(RUNNER))
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda key: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda key, value: calls.append((key, value)))
    module["prepare_file_limit"]()
    assert calls == ([] if expected is None else [(resource.RLIMIT_NOFILE, (expected, hard))])
