"""Ordinary release fixtures: real file transactions, mocked launchd/HTTP only."""

from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib

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
            if self.mode == "feishu_failed" and self.current["cwd"] == self.candidate:
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
