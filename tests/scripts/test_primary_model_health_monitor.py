import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_monitor():
    path = Path(__file__).resolve().parents[2] / "scripts" / "primary_model_health_monitor.py"
    spec = importlib.util.spec_from_file_location("primary_model_health_monitor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_primary_probe_alerts_when_direct_model_fails(tmp_path, monkeypatch, capsys):
    monitor = _load_monitor()

    def fake_run(cmd, env, capture_output, text, timeout):
        assert env["HERMES_HOME"] == str(tmp_path / "p1")
        return SimpleNamespace(returncode=1, stdout="", stderr="Primary model failed; switching to fallback sk-secret123456789")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    rc = monitor.main([
        "--hermes-bin", "/bin/hermes",
        "--homes", f"P1={tmp_path / 'p1'}",
        "--auth-store", str(tmp_path / "auth.json"),
        "--state", str(tmp_path / "state.json"),
        "--log", str(tmp_path / "missing.log"),
        "--timeout", "1",
    ])

    out = capsys.readouterr().out
    assert rc == 1
    assert "ALERT primary_model_unhealthy home=P1" in out
    assert "switching to fallback" in out


def test_primary_probe_ok_and_log_fallback_alert_is_stateful(tmp_path, monkeypatch, capsys):
    monitor = _load_monitor()
    home = tmp_path / "p1"
    log = home / "logs" / "gateway.log"
    log.parent.mkdir(parents=True)
    log.write_text("INFO boot\nWARN Primary model failed; switching to fallback\n", encoding="utf-8")

    def fake_run(cmd, env, capture_output, text, timeout):
        expected = "PRIMARY_HEALTH_OK_P1"
        return SimpleNamespace(returncode=0, stdout=expected, stderr="")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    args = [
        "--hermes-bin", "/bin/hermes",
        "--homes", f"P1={home}",
        "--auth-store", str(tmp_path / "auth.json"),
        "--state", str(tmp_path / "state.json"),
        "--log", str(log),
    ]
    rc1 = monitor.main(args)
    out1 = capsys.readouterr().out
    assert rc1 == 1
    assert "OK primary_model home=P1" in out1
    assert "ALERT fallback_detected" in out1

    rc2 = monitor.main(args)
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "OK primary_model home=P1" in out2
    assert "ALERT fallback_detected" not in out2
