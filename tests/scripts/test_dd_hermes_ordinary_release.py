"""Ordinary release fixtures: real file transactions, mocked launchd/HTTP only."""

from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

from scripts import dd_hermes_ordinary_release as release


QUEUE_ID = "11111111-2222-4333-8444-555555555555"
TARGET = "a" * 40


class FakeHost(release.Host):
    def __init__(self, baseline, candidate):
        self.elapsed = 0
        self.current = {"pid": 100, "cwd": str(baseline)}
        self.baseline = str(baseline)
        self.candidate = str(candidate)
        self.next_pid = 101
        self.detached_old_alive = False
        self.mutations = []
        self.mode = "normal"
        self.row = {}
        self.enabled = ["api_server", "feishu", "telegram"]

    def enabled_platforms(self, root, python, plist, deadline):
        self.remaining(deadline)
        if self.mode == "enabled_drift" and str(root) == self.candidate:
            return ["api_server", "telegram"]
        return self.enabled

    def clock(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds

    def source(self, root, deadline):
        self.remaining(deadline)
        return {"commit": "b" * 40 if self.mode == "source_drift" else TARGET, "tree": "c" * 40}

    def baseline_source(self, root, deadline):
        self.remaining(deadline)
        return "d" * 64

    def python_origin(self, root, python, deadline):
        self.remaining(deadline)
        if self.mode == "bad_import":
            raise release.Refused("candidate_import_origin_mismatch")

    def identity(self, deadline):
        self.remaining(deadline)
        if self.current is None:
            raise release.Refused("ordinary_job_not_observable")
        return dict(self.current)

    def pid_alive(self, pid):
        return ((self.current is not None and self.current["pid"] == pid)
                or (self.detached_old_alive and pid == 100))

    def http(self, url, deadline):
        self.remaining(deadline)
        if url == release.HEALTH:
            now = datetime.now(timezone.utc).isoformat()
            # Existing healthy connections may have old transition timestamps.
            stamp = "2020-01-01T00:00:00+00:00" if self.current["pid"] == 100 else now
            platforms = {name: {"state": "connected", "updated_at": stamp}
                         for name in ("telegram", "api_server", "feishu")}
            platforms["slack"] = {"state": "retrying", "updated_at": stamp}
            if self.mode == "feishu_always_failed" or (self.mode == "feishu_failed" and self.current["cwd"] == self.candidate):
                platforms["feishu"]["state"] = "retrying"
            return {"status": "ok", "gateway_state": "running", "pid": self.current["pid"],
                    "active_agents": 1 if self.mode == "busy" else 0,
                    "updated_at": stamp, "platforms": platforms}
        return {"items": [self.row]} if "?" in url else dict(self.row)

    def stop(self, pid, deadline):
        self.remaining(deadline)
        self.mutations.append("stop")
        if self.mode == "stop_timeout_unloaded":
            self.current = None
            self.detached_old_alive = True
            raise release.Refused("bootout_accepted_but_old_pid_alive")
        if self.mode == "stop_timeout_alive":
            raise release.Refused("stop_timed_out_old_pid_alive")
        self.current = None
        if self.mode.startswith("drift_during_rollback_") and self.mutations.count("stop") == 2:
            path = release.PLIST if self.mode.endswith("plist") else release.ORDINARY_HOME / "config.yaml"
            path.write_bytes(b"third-party-definition")
        if self.mode == "plist_drift_during_stop":
            release.PLIST.write_bytes(b"third-party-definition")
        if self.mode == "stop_failure":
            self.mode = "normal"
            raise release.Refused("stop_failed_after_effect")

    def start(self, deadline):
        self.remaining(deadline)
        self.mutations.append("start")
        plist = plistlib.loads(release.PLIST.read_bytes())
        cwd = plist["WorkingDirectory"]
        self.current = {"pid": self.next_pid, "cwd": cwd}
        self.next_pid += 1
        if self.mode == "plist_drift_after_start":
            release.PLIST.write_bytes(b"third-party-definition")
            raise release.Refused("startup_failed")
        if self.mode in {"start_failure", "rollback_failure"} and cwd == self.candidate:
            raise release.Refused("candidate_start_failed_after_effect")
        if self.mode.startswith("drift_during_rollback_") and cwd == self.candidate:
            raise release.Refused("candidate_start_failed_after_effect")
        if self.mode == "rollback_failure" and cwd == self.baseline:
            raise release.Refused("rollback_start_failed")


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    apps = root / "apps"
    apps.mkdir()
    candidate = apps / "candidate"
    candidate.mkdir()
    baseline = root / "baseline"
    baseline.mkdir()
    home = root / "ordinary-home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("# preserve this comment\nstt:\n  provider: local # approved edit\n  language: en\nmodel: unchanged\n")
    python = root / "shared-python"
    python.write_text("synthetic executable")
    python.chmod(0o755)
    plist = root / "ordinary.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "ai.hermes.gateway", "WorkingDirectory": str(baseline),
        "ProgramArguments": [str(python), "-m", "hermes_cli.main", "--profile", "default", "gateway", "run", "--replace"],
        "EnvironmentVariables": {"HERMES_HOME": str(home), "API_KEY": "fixture-private-value"},
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
    }))
    monkeypatch.setattr(release, "APPS", apps)
    monkeypatch.setattr(release, "PLIST", plist)
    monkeypatch.setattr(release, "STATE", root / "state")
    monkeypatch.setattr(release, "ORDINARY_HOME", home)
    host = FakeHost(baseline, candidate)
    manifest = release.stage(candidate, python, TARGET, host)
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(release.canonical(manifest))
    checksum = release.digest(manifest_path)
    host.row = {"id": QUEUE_ID, "service_name": "hermes-agent", "target_commit": TARGET,
                "status": "deploying", "decided_by": "Levi", "decided_at": "2026-09-07T00:00:00Z",
                "restart_command": release.restart_command(manifest_path, checksum)}
    return {"host": host, "manifest": manifest, "path": manifest_path, "hash": checksum,
            "plist": plist, "config": config, "baseline_plist": plist.read_bytes(),
            "baseline_config": config.read_bytes(), "root": root}


def activate(bundle):
    return release.activate(bundle["path"], bundle["hash"], "auto", bundle["host"])


def test_stage_is_read_only_and_does_not_expose_private_values(bundle):
    assert not release.STATE.exists()
    assert bundle["host"].mutations == []
    assert bundle["plist"].read_bytes() == bundle["baseline_plist"]
    assert bundle["config"].read_bytes() == bundle["baseline_config"]
    assert "fixture-private-value" not in json.dumps(bundle["manifest"])
    assert bundle["manifest"]["connected_platforms"] == ["api_server", "feishu", "telegram"]


def restage(bundle):
    host, old = bundle["host"], bundle["manifest"]
    manifest = release.stage(old["candidate"], old["python"], TARGET, host)
    bundle["manifest"] = manifest
    bundle["path"].write_bytes(release.canonical(manifest))
    bundle["hash"] = release.digest(bundle["path"])
    host.row["restart_command"] = release.restart_command(bundle["path"], bundle["hash"])
    return manifest


def test_disabled_persisted_transport_does_not_block_candidate_or_rollback(bundle):
    bundle["host"].enabled = ["api_server", "telegram"]
    manifest = restage(bundle)
    assert manifest["connected_platforms"] == ["api_server", "telegram"]
    assert manifest["ignored_disabled_platforms"] == ["feishu"]
    bundle["host"].mode = "feishu_failed"
    assert activate(bundle)["status"] == "succeeded"


def test_disabled_persisted_transport_does_not_block_restored_baseline(bundle):
    bundle["host"].enabled = ["api_server", "telegram"]
    restage(bundle)
    bundle["host"].mode = "start_failure"
    result = activate(bundle)
    assert result["status"] == "rolled_back"
    assert result["failure_reason"] == "candidate_start_failed_after_effect"


def test_enabled_extra_transport_remains_required_with_diagnostic(bundle):
    bundle["host"].mode = "feishu_failed"
    result = activate(bundle)
    assert result["status"] == "rolled_back"
    assert result["failure_reason"] == "verification_timeout_transport_not_connected_feishu"


def test_candidate_must_preserve_enabled_platforms(bundle):
    bundle["host"].mode = "enabled_drift"
    with pytest.raises(release.Refused, match="candidate_enabled_platforms_changed"):
        restage(bundle)
    assert activate(bundle)["status"] == "preflight_failed"
    assert not bundle["host"].mutations


def test_new_optional_resolver_input_refuses_before_mutation(bundle):
    (release.ORDINARY_HOME / "gateway.json").write_text("{}")
    assert activate(bundle)["status"] == "preflight_failed"
    assert not bundle["host"].mutations


def failed_state(bundle):
    host = bundle["host"]
    host.mode = "rollback_failure"
    assert activate(bundle)["status"] == "failed_recovery"
    host.mode = "normal"
    host.enabled = ["api_server", "telegram"]
    host.row["status"] = "failed"
    assert host.current["cwd"] == host.baseline
    return (release.STATE / "state.json").read_bytes()


def acknowledge(bundle, raw):
    return release.acknowledge_recovery(bundle["path"], bundle["hash"], QUEUE_ID,
                                        release.sha(raw), bundle["host"])


def test_acknowledgment_preserves_failure_archive_and_replay_fence(bundle):
    raw = failed_state(bundle)
    prior = json.loads(raw)
    mutations = list(bundle["host"].mutations)
    result = acknowledge(bundle, raw)
    assert result["status"] == "operator_recovered"
    state = json.loads((release.STATE / "state.json").read_bytes())
    assert state["used"] == prior["used"]
    assert state["events"][:-1] == prior["events"]
    assert state["events"][-1]["ignored_disabled_platforms"] == ["feishu"]
    assert state["recovery_failure_reason"] == prior["recovery_failure_reason"]
    archive = release.STATE / (QUEUE_ID + "." + release.sha(raw) + ".failed-state.json")
    assert archive.read_bytes() == raw
    assert archive.stat().st_mode & 0o777 == 0o600
    receipt = release.STATE / (QUEUE_ID + "." + release.sha(raw) + ".recovery.json")
    assert json.loads(receipt.read_bytes()) == state["events"][-1]
    assert bundle["host"].row["status"] == "failed"
    assert bundle["host"].mutations == mutations
    bundle["host"].row["status"] = "deploying"
    with pytest.raises(release.Refused, match="already_consumed"):
        activate(bundle)


@pytest.mark.parametrize("drift", ["state", "operation", "queue", "candidate", "config", "plist", "python", "source"])
def test_acknowledgment_refuses_unproven_baseline(bundle, drift):
    raw = failed_state(bundle)
    host = bundle["host"]
    if drift == "state":
        (release.STATE / "state.json").write_bytes(raw + b" ")
    elif drift == "operation":
        state = json.loads(raw)
        state["operation"] = "22222222-2222-4333-8444-555555555555"
        raw = release.canonical(state)
        (release.STATE / "state.json").write_bytes(raw)
    elif drift == "queue":
        host.row["status"] = "deploying"
    elif drift == "candidate":
        host.current["cwd"] = host.candidate
    elif drift == "config":
        bundle["config"].write_bytes(b"foreign")
    elif drift == "plist":
        document = plistlib.loads(bundle["plist"].read_bytes())
        document["RunAtLoad"] = False
        bundle["plist"].write_bytes(plistlib.dumps(document))
    elif drift == "python":
        Path(bundle["manifest"]["python"]).write_bytes(b"foreign")
    elif drift == "source":
        host.baseline_source = lambda *args: "e" * 64
    before = (release.STATE / "state.json").read_bytes()
    with pytest.raises(release.Refused):
        acknowledge(bundle, raw)
    assert (release.STATE / "state.json").read_bytes() == before
    assert not list(release.STATE.glob("*.failed-state.json"))


def test_acknowledgment_accepts_old_manifest_without_rebuilding_old_command(bundle):
    # Acknowledgment can run from the new author checkout while the failed row
    # remains bound to the old immutable candidate helper and manifest.
    manifest = bundle["manifest"]
    manifest.pop("enabled_platforms")
    manifest.pop("ignored_disabled_platforms")
    manifest["config_sha256"] = {str(bundle["config"]): release.digest(bundle["config"])}
    bundle["path"].write_bytes(release.canonical(manifest))
    bundle["hash"] = release.digest(bundle["path"])
    release.STATE.mkdir(mode=0o700)
    state = {"operation": QUEUE_ID, "manifest_sha256": bundle["hash"], "phase": "failed_recovery",
             "used": [QUEUE_ID], "rollback_count": 1, "events": [{"phase": "failed_recovery"}]}
    raw = release.canonical(state)
    (release.STATE / "state.json").write_bytes(raw)
    bundle["host"].row.update(status="failed", restart_command="/old/python /old/helper activate --manifest-sha256 " + bundle["hash"])
    assert acknowledge(bundle, raw)["status"] == "operator_recovered"


def test_acknowledgment_still_requires_original_enabled_transport(bundle):
    raw = failed_state(bundle)
    bundle["host"].enabled = ["api_server", "feishu", "telegram"]
    bundle["host"].mode = "feishu_always_failed"
    with pytest.raises(release.Refused, match="transport_not_connected_feishu"):
        acknowledge(bundle, raw)
    assert (release.STATE / "state.json").read_bytes() == raw


@pytest.mark.parametrize("attempt", ["none", "write", "auth", "network", "subprocess",
                                     "old_baseline", "disabled_plugin", "enabled_plugin",
                                     "existing_directory", "missing_directory", "same_mode", "changed_mode",
                                     "socket_construction", "bind", "ipv6_probe"])
def test_real_resolver_guard_is_read_only_and_redacts_output(tmp_path, monkeypatch, attempt):
    root = tmp_path.resolve()
    for package in ("gateway", "hermes_cli"):
        (root / package).mkdir()
        (root / package / "__init__.py").write_text("")
    marker = root / "forbidden-write"
    # This auth path is deliberately absent; neither fixture nor helper creates
    # or reads a credential store. The audit hook must reject before OS access.
    attempts = {"none": "pass", "write": "open('forbidden-write', 'w')",
                "auth": "open('auth.json')", "network": "__import__('socket').socket().connect(('127.0.0.1', 1))",
                "socket_construction": "__import__('socket').socket().close()",
                "bind": "__import__('socket').socket().bind(('127.0.0.1', 0))",
                "ipv6_probe": "__import__('socket').has_ipv6 and __import__('socket').socket().bind(('127.0.0.1', 0))",
                "subprocess": "__import__('subprocess').run(['/usr/bin/true'])",
                "existing_directory": "__import__('pathlib').Path('gateway').mkdir(exist_ok=True)",
                "missing_directory": "__import__('pathlib').Path('forbidden-write').mkdir(exist_ok=True)",
                "same_mode": "__import__('os').chmod('gateway', 0o700)",
                "changed_mode": "__import__('os').chmod('gateway', 0o777)"}
    (root / "gateway").chmod(0o700)
    (root / "hermes_cli/env_loader.py").write_text(
        "def load_hermes_dotenv(**kwargs):\n"
        "    print('fixture-secret-must-not-escape')\n"
        "    try:\n        " + attempts.get(attempt, "pass") + "\n"
        "    except Exception:\n        pass\n")
    (root / "gateway/config.py").write_text(
        "from types import SimpleNamespace as S\nfrom enum import Enum\n"
        "class Platform(Enum):\n    API='api_server'\n    TELEGRAM='telegram'\n"
        "def load_gateway_config():\n"
        "    return S(platforms={p:S(enabled=True) for p in Platform})\n")
    if attempt != "old_baseline":
        entries = ("[S(name='telegram')]" if attempt == "enabled_plugin" else
                   "[S(name='disabled_fixture')]" if attempt == "disabled_plugin" else "[]")
        (root / "gateway/platform_registry.py").write_text(
            "from types import SimpleNamespace as S\nclass Registry:\n"
            "    def plugin_entries(self): return " + entries + "\nplatform_registry=Registry()\n")
    monkeypatch.setattr(release, "ORDINARY_HOME", root)
    host = release.Host()
    if attempt in {"none", "old_baseline", "disabled_plugin", "existing_directory", "same_mode", "socket_construction", "ipv6_probe"}:
        assert host.enabled_platforms(root, sys.executable, {}, host.clock() + 5) == ["api_server", "telegram"]
    else:
        reason = "^resolver_plugins_failed$" if attempt == "enabled_plugin" else "^resolver_plugins_blocked_"
        with pytest.raises(release.Refused, match=reason):
            host.enabled_platforms(root, sys.executable, {}, host.clock() + 5)
    assert not marker.exists()
    assert (root / "gateway").stat().st_mode & 0o777 == 0o700
    assert not list(root.rglob("__pycache__"))


def test_real_resolver_excludes_agent_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "agent-private-fixture")
    def run(args, **kwargs):
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["SERVICE_FIXTURE"] == "service-private-fixture"
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        return subprocess.CompletedProcess(args, 0, b'["api_server", "telegram"]', b"")
    monkeypatch.setattr(subprocess, "run", run)
    host = release.Host()
    assert host.enabled_platforms(tmp_path, sys.executable, {"EnvironmentVariables": {
        "SERVICE_FIXTURE": "service-private-fixture"}}, host.clock() + 5) == ["api_server", "telegram"]


def test_success_changes_only_ordinary_plist_and_stt_with_private_backups(bundle):
    sibling = bundle["root"] / "classic-config.yaml"
    sibling.write_text("stt:\n  provider: local\n")
    assert activate(bundle)["status"] == "succeeded"
    assert bundle["host"].mutations == ["stop", "start"]
    assert b"provider: dgx # approved edit" in bundle["config"].read_bytes()
    assert sibling.read_text() == "stt:\n  provider: local\n"
    current = plistlib.loads(bundle["plist"].read_bytes())
    assert current["WorkingDirectory"] == bundle["manifest"]["candidate"]
    assert "--replace" not in current["ProgramArguments"]
    for name, raw in [(".baseline.plist", bundle["baseline_plist"]),
                      (".baseline-config.yaml", bundle["baseline_config"])]:
        backup = release.STATE / (QUEUE_ID + name)
        assert backup.read_bytes() == raw
        assert backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", ["source_drift", "bad_import", "busy"])
def test_preflight_refusal_never_mutates_service(bundle, mode):
    bundle["host"].mode = mode
    assert activate(bundle)["status"] == "preflight_failed"
    assert bundle["host"].mutations == []
    assert bundle["config"].read_bytes() == bundle["baseline_config"]


@pytest.mark.parametrize("field,value", [("status", "pending"), ("target_commit", "b" * 40),
                                         ("restart_command", "unrelated"), ("decided_by", "")])
def test_unapproved_or_mismatched_queue_refuses_before_state_write(bundle, field, value):
    bundle["host"].row[field] = value
    with pytest.raises(release.Refused):
        activate(bundle)
    assert bundle["host"].mutations == []
    assert not release.STATE.exists()


@pytest.mark.parametrize("mode", ["stop_failure", "start_failure", "feishu_failed"])
def test_one_rollback_restores_exact_baseline_bytes(bundle, mode):
    bundle["host"].mode = mode
    result = activate(bundle)
    assert result["status"] == "rolled_back"
    assert result["rollback_count"] == 1
    assert bundle["plist"].read_bytes() == bundle["baseline_plist"]
    assert bundle["config"].read_bytes() == bundle["baseline_config"]
    assert bundle["host"].clock() <= 25


def test_replay_and_failed_recovery_are_fenced(bundle):
    bundle["host"].mode = "rollback_failure"
    assert activate(bundle)["status"] == "failed_recovery"
    before = list(bundle["host"].mutations)
    with pytest.raises(release.Refused, match="operation_already_consumed"):
        activate(bundle)
    assert bundle["host"].mutations == before


def test_manifest_tampering_refuses_before_os_mutation(bundle):
    bundle["path"].write_text("{}")
    with pytest.raises(release.Refused, match="manifest_digest_mismatch"):
        activate(bundle)
    assert bundle["host"].mutations == []


def test_lock_refuses_concurrent_execution(bundle):
    with release.locked_state():
        with pytest.raises(release.Refused, match="operation_in_progress"):
            activate(bundle)
    assert bundle["host"].mutations == []


@pytest.mark.parametrize("mode", ["plist_drift_during_stop", "plist_drift_after_start"])
def test_foreign_plist_bytes_are_never_overwritten(bundle, mode):
    bundle["host"].mode = mode
    assert activate(bundle)["status"] == "failed_recovery"
    assert release.PLIST.read_bytes() == b"third-party-definition"


@pytest.mark.parametrize("mode", ["stop_timeout_alive", "stop_timeout_unloaded"])
def test_timed_out_stop_with_old_pid_alive_never_bootstraps(bundle, mode):
    bundle["host"].mode = mode
    assert activate(bundle)["status"] == "failed_recovery"
    assert "start" not in bundle["host"].mutations
    assert bundle["host"].pid_alive(100)


@pytest.mark.parametrize("surface", ["plist", "config"])
def test_drift_during_rollback_stop_is_not_overwritten(bundle, surface):
    bundle["host"].mode = "drift_during_rollback_" + surface
    assert activate(bundle)["status"] == "failed_recovery"
    assert bundle[surface].read_bytes() == b"third-party-definition"


def test_dgx_edit_preserves_comments_and_rejects_ambiguous_yaml():
    raw = b"stt:\n  provider: 'local' # keep\n  model: tiny\nother: unchanged\n"
    assert release.dgx_config(raw) == raw.replace(b"'local'", b"dgx")
    with pytest.raises(release.Refused):
        release.dgx_config(b"stt:\n  provider: local\n  provider: remote\n")


def test_real_os_adapter_uses_only_named_label_without_shell(monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(release.subprocess, "run", command)
    host = release.Host()
    monkeypatch.setattr(host, "pid_alive", lambda pid: False)
    deadline = host.clock() + 5
    host.stop(42, deadline)
    host.start(deadline)
    assert calls[0][0] == ["/bin/launchctl", "bootout", release.LABEL]
    assert calls[1][0] == ["/bin/launchctl", "bootstrap", "gui/502", str(release.PLIST)]
    assert all("shell" not in kwargs and 0 < kwargs["timeout"] <= 2 for _, kwargs in calls)
