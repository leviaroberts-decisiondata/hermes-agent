"""Portless profile transactions, with all OS mutation mocked or temp-only."""
import ast
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import dd_hermes_profile_release as release

TARGET = "a" * 40
REVIEWED = "b" * 40
OP = "11111111-2222-4333-8444-555555555555"


class FakeHost:
    def __init__(self, engine, baseline, candidate):
        self.engine, self.baseline, self.candidate = engine, str(baseline), str(candidate)
        self.elapsed, self.next_pid = 0, 101
        self.current = {"pid": 100, "cwd": str(baseline)}
        self.mode, self.mutations, self.protected = "normal", [], None
        self.row = {}

    def clock(self): return self.elapsed
    def sleep(self, value): self.elapsed += value
    def remaining(self, deadline, cap=2):
        if deadline <= self.elapsed: raise release.common.Refused("deadline_expired")
        return min(cap, deadline - self.elapsed)
    def source(self, root, deadline):
        self.remaining(deadline)
        return {"commit": TARGET, "tree": "c" * 40}
    def baseline_source(self, root, deadline): return "d" * 64
    def baseline_compatible(self, root, deadline):
        if self.mode == "baseline_changed": raise release.common.Refused("baseline_changed")
    def helpers_reviewed(self, reviewed, deadline):
        if self.mode == "helper_unreviewed": raise release.common.Refused("helper_not_exact_reviewed_source")
    def python_origin(self, *args): pass
    def enabled_platforms(self, *args): return ["telegram"]
    def component_identity(self, candidate, target, reviewed, deadline):
        if self.mode == "component_changed": raise release.common.Refused("component_changed")
        return {"reviewed_commit": reviewed, "base_commit": release.BASE, "artifact_commit": target}
    def protected_snapshot(self, deadline):
        return {"ordinary": "changed" if self.mode == "protected_changed" else "untouched", "classic": "untouched"}
    def check_protected(self, deadline):
        if self.protected != self.protected_snapshot(deadline):
            raise release.common.Refused("protected_sibling_changed")
    def identity(self, deadline):
        self.remaining(deadline)
        if self.current is None: raise release.common.Refused("job_absent")
        return dict(self.current)
    def pid_alive(self, pid): return self.current is not None and self.current["pid"] == pid
    def http(self, url, deadline):
        self.remaining(deadline)
        if url != self.engine.HEALTH:
            return {"items": [self.row]} if "?" in url else dict(self.row)
        stamp = datetime.now(timezone.utc).isoformat()
        if self.current["pid"] == 100 or self.mode == "stale": stamp = "2020-01-01T00:00:00Z"
        return {"pid": self.current["pid"] + (1 if self.mode == "wrong_pid" else 0),
                "status": "ok", "gateway_state": "running", "updated_at": stamp,
                "active_agents": 1 if self.mode == "busy" else 0,
                "platforms": {"telegram": {"state": "connected", "updated_at": stamp},
                              "feishu": {"state": "connected", "updated_at": "2020-01-01T00:00:00Z"}}}
    def stop(self, pid, deadline):
        self.check_protected(deadline)
        self.mutations.append("stop")
        if self.mode == "stop_hung": raise release.common.Refused("stop_hung")
        self.current = None
        if self.mode == "foreign_plist": self.engine.PLIST.write_bytes(b"foreign")
    def start(self, deadline):
        self.check_protected(deadline)
        self.mutations.append("start")
        cwd = plistlib.loads(self.engine.PLIST.read_bytes())["WorkingDirectory"]
        self.current = {"pid": self.next_pid, "cwd": cwd}
        self.next_pid += 1
        if self.mode in {"start_failed", "rollback_failed"} and cwd == self.candidate:
            raise release.common.Refused("candidate_start_failed")
        if self.mode == "rollback_failed" and cwd == self.baseline:
            raise release.common.Refused("rollback_start_failed")


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    homes = {name: root / name for name in release.PROFILE_HOMES}
    for home in homes.values(): home.mkdir()
    monkeypatch.setattr(release, "PROFILE_HOMES", homes)
    plists = root / "plists"
    plists.mkdir()
    monkeypatch.setattr(release, "PLIST_ROOT", plists)
    monkeypatch.setattr(release, "STATE_ROOT", root / "state")
    candidate = root / "apps/candidate"
    candidate.mkdir(parents=True)
    baseline = root / "baseline"
    baseline.mkdir()
    python = root / "python"
    python.write_bytes(b"synthetic")
    python.chmod(0o700)
    def create(profile="qa-review"):
        engine = release.engine_for(profile)
        engine.APPS = root / "apps"
        config = engine.ORDINARY_HOME / "config.yaml"
        config.write_text("# keep\nstt:\n  enabled: true\n  provider: local # keep\nmodel: unchanged\n")
        cli_profile = profile if profile in release.NAMED else "default"
        engine.PLIST.write_bytes(plistlib.dumps({"Label": engine.LABEL.split("/")[-1],
            "WorkingDirectory": str(baseline), "ProgramArguments": [str(python), "-m", "hermes_cli.main",
                "--profile", cli_profile, "gateway", "run", "--replace"],
            "EnvironmentVariables": {"HERMES_HOME": str(engine.ORDINARY_HOME), "FIXTURE_KEY": "never-output"}}))
        host = FakeHost(engine, baseline, candidate)
        manifest = release.stage(profile, candidate, python, TARGET, REVIEWED, engine=engine, host=host)
        path = root / (profile + ".manifest.json")
        path.write_bytes(engine.canonical(manifest))
        checksum = engine.digest(path)
        host.row = {"id": OP, "service_name": engine.SERVICE, "target_commit": TARGET,
                    "status": "deploying", "decided_by": "Levi", "decided_at": "2026-09-08T00:00:00Z",
                    "restart_command": engine.restart_command(path, checksum)}
        return SimpleNamespace(profile=profile, engine=engine, host=host, manifest=manifest, path=path,
                               checksum=checksum, config=config, plist=engine.PLIST,
                               config_bytes=config.read_bytes(), plist_bytes=engine.PLIST.read_bytes())
    return create


def activate(b):
    return release.activate(b.profile, b.path, b.checksum, engine=b.engine, host=b.host)


@pytest.mark.parametrize("profile", release.NAMED + release.SEPARATE)
def test_every_allowed_profile_preserves_home_and_cli_profile(bundle, profile):
    b = bundle(profile)
    assert not release.STATE_ROOT.exists()
    assert b.manifest["connected_platforms"] == ["telegram"]
    assert b.manifest["ignored_disabled_platforms"] == ["feishu"]
    assert "never-output" not in json.dumps(b.manifest)
    result = activate(b)
    assert result["status"] == "succeeded"
    plist = plistlib.loads(b.plist.read_bytes())
    old = plistlib.loads(b.plist_bytes)
    assert plist["EnvironmentVariables"] == old["EnvironmentVariables"]
    assert plist["ProgramArguments"][4] == old["ProgramArguments"][4]
    assert "--replace" not in plist["ProgramArguments"]
    assert b.config.read_bytes() == b.config_bytes.replace(b"provider: local", b"provider: dgx")
    assert b.host.mutations == ["stop", "start"]


@pytest.mark.parametrize("profile", ["ordinary", "classic", "default", "../classic", "unknown"])
def test_protected_or_unknown_profiles_are_never_targets(profile):
    with pytest.raises(release.common.Refused, match="profile_not_allowlisted"):
        release.engine_for(profile)


@pytest.mark.parametrize("mode", ["busy", "wrong_pid", "component_changed", "protected_changed", "helper_unreviewed"])
def test_preflight_failure_has_no_mutation(bundle, mode):
    b = bundle(); b.host.mode = mode
    assert activate(b)["status"] == "preflight_failed"
    assert not b.host.mutations
    assert b.config.read_bytes() == b.config_bytes


@pytest.mark.parametrize("mode,expected", [("start_failed", "rolled_back"), ("rollback_failed", "failed_recovery"),
                                           ("stale", "failed_recovery"), ("stop_hung", "failed_recovery")])
def test_failure_rolls_back_once_and_fences_replay(bundle, mode, expected):
    b = bundle(); b.host.mode = mode
    result = activate(b)
    assert result["status"] == expected
    assert result["rollback_count"] == 1
    assert b.host.elapsed <= 25
    with pytest.raises(release.common.Refused, match="already_consumed"):
        activate(b)
    if mode != "stop_hung":
        assert b.config.read_bytes() == b.config_bytes
        assert b.plist.read_bytes() == b.plist_bytes


def test_foreign_plist_is_not_overwritten(bundle):
    b = bundle(); b.host.mode = "foreign_plist"
    assert activate(b)["status"] == "failed_recovery"
    assert b.plist.read_bytes() == b"foreign"


def test_fleet_lock_serializes_different_profiles(bundle):
    a, b = bundle("qa-review"), bundle("azul")
    with a.engine.locked_state():
        with pytest.raises(release.common.Refused, match="fleet_operation_in_progress"):
            activate(b)
    assert not b.host.mutations


def test_wrong_queue_service_refuses_without_state(bundle):
    b = bundle(); b.host.row["service_name"] = "hermes-agent"
    with pytest.raises(release.common.Refused, match="matching_approved"):
        activate(b)
    assert not release.STATE_ROOT.exists()


def test_engines_do_not_change_ordinary_or_each_other(bundle):
    ordinary_label = release.common.LABEL
    a, b = bundle("azul"), bundle("qa-review")
    assert a.engine.LABEL == "gui/502/ai.hermes.gateway-azul"
    assert b.engine.LABEL == "gui/502/ai.hermes.gateway-qa-review"
    assert release.common.LABEL == ordinary_label == "gui/502/ai.hermes.gateway"


def test_portless_os_adapter_reads_only_selected_status(bundle):
    b = bundle()
    path = b.engine.ORDINARY_HOME / "gateway_state.json"
    path.write_text(json.dumps({"pid": 100, "gateway_state": "running", "active_agents": 0}))
    host = release.host_for(b.engine)
    value = host.http(b.engine.HEALTH, host.clock() + 2)
    assert value["pid"] == 100 and value["status"] == "ok"


def test_d1_gateway_keeps_failed_voice_recording(tmp_path, monkeypatch):
    # The actual preserved d1 method, not a reconstruction or live gateway import.
    repo = Path(__file__).resolve().parents[2]
    source = subprocess.run(["/usr/bin/git", "-C", str(repo), "show", release.BASE + ":gateway/run.py"],
                            capture_output=True, text=True, check=True, timeout=5).stdout
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "_enrich_message_with_transcription")
    namespace = {"List": list, "asyncio": asyncio, "logger": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "d1-gateway-fixture", "exec"), namespace)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", SimpleNamespace(
        transcribe_audio=lambda path: {"success": False, "error": "synthetic DGX failure"}))
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"synthetic recording")
    gateway = SimpleNamespace(config=SimpleNamespace(stt_enabled=True), _has_setup_skill=lambda: False)
    result = asyncio.run(namespace[node.name](gateway, "", [str(audio)]))
    assert "synthetic DGX failure" in result
    assert audio.read_bytes() == b"synthetic recording"


@pytest.fixture
def component_repo(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    def git(*args):
        return subprocess.run(["/usr/bin/git", "-C", str(root), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    git("init", "-q")
    (root / "tools").mkdir()
    (root / "hermes_cli").mkdir()
    module = root / release.COMPONENT
    config = root / release.DEFAULT_COMPONENT
    config.write_text('DEFAULT_CONFIG = {"stt": {"provider": "local"}, "other": "untouched"}\n')
    module.write_text("legacy\n")
    (root / "hermes_state.py").write_text("unchanged writer\n")
    git("add", "."); git("commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD")
    monkeypatch.setattr(release, "BASE", base)
    module.write_text("reviewed dgx\n")
    config.write_text('DEFAULT_CONFIG = {"stt": {"provider": "dgx"}, "other": "new main feature"}\n')
    (root / "unrelated-main-feature.py").write_text("main only\n")
    git("add", "."); git("commit", "-qm", "reviewed main")
    reviewed = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", reviewed)
    git("checkout", "-q", "--detach", base)
    module.write_text("reviewed dgx\n")
    config.write_bytes(release.backport_default(config.read_bytes()))
    git("add", "."); git("commit", "-qm", "immutable component artifact")
    target = git("rev-parse", "HEAD")
    return root, git, reviewed, target


def test_real_composite_identity_does_not_claim_full_main(component_repo):
    root, git, reviewed, target = component_repo
    host = release.common.Host()
    result = release.component_identity(host, root, target, reviewed, host.clock() + 5)
    assert result["artifact_commit"] == target != reviewed
    assert result["artifact_tree"] == git("rev-parse", "HEAD^{tree}")
    assert result["blob"] == git("rev-parse", reviewed + ":" + release.COMPONENT)
    assert not (root / "unrelated-main-feature.py").exists()


@pytest.mark.parametrize("drift", ["extra", "writer", "blob", "unmerged_review", "default_other_bytes", "default_not_changed"])
def test_real_component_identity_refuses_unapproved_delta(component_repo, drift):
    root, git, reviewed, target = component_repo
    if drift == "extra": (root / "extra.py").write_text("unapproved\n")
    elif drift == "writer": (root / "hermes_state.py").write_text("new writer\n")
    elif drift == "blob": (root / release.COMPONENT).write_text("unreviewed module\n")
    elif drift == "default_other_bytes":
        (root / release.DEFAULT_COMPONENT).write_text('DEFAULT_CONFIG = {"stt": {"provider": "dgx"}, "other": "changed"}\n')
    elif drift == "default_not_changed":
        (root / release.DEFAULT_COMPONENT).write_text('DEFAULT_CONFIG = {"stt": {"provider": "local"}, "other": "untouched"}\n')
    else: reviewed = target
    if drift != "unmerged_review":
        git("add", "."); git("commit", "-qm", "unapproved change")
        target = git("rev-parse", "HEAD")
    host = release.common.Host()
    with pytest.raises(release.common.Refused):
        release.component_identity(host, root, target, reviewed, host.clock() + 5)


def test_actual_candidate_transcription_module_routes_dgx_without_fallback(tmp_path):
    # The component proof runner sets this to the actual immutable d1 artifact.
    # Ordinary author tests use the identical reviewed module in this checkout.
    candidate = Path(os.environ.get("DD_PROFILE_COMPONENT_ROOT", Path(__file__).resolve().parents[2])).resolve()
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    code = '''
import json, pathlib, tempfile
from types import SimpleNamespace
from tools import transcription_tools as module
assert pathlib.Path(module.__file__).resolve() == pathlib.Path.cwd() / 'tools/transcription_tools.py'
# Exercise the actual candidate's canonical config merge in an empty home.
from hermes_cli.config import load_config
assert load_config()['stt']['provider'] == 'dgx'
module._validate_audio_file = lambda path: None
module.DGX_WRAPPER_PATH = pathlib.Path(__file__ if '__file__' in globals() else 'tools/transcription_tools.py')
calls = []
def run(args, **kwargs):
    calls.append((args, kwargs['env']['TRANSCRIBE_MODEL']))
    return SimpleNamespace(returncode=1, stdout='', stderr='synthetic failure')
module.subprocess.run = run
module._transcribe_local = lambda *args: (_ for _ in ()).throw(AssertionError('fallback forbidden'))
result = module.transcribe_audio('synthetic.ogg')
assert result['success'] is False and result['provider'] == 'dgx'
assert len(calls) == 1 and calls[0][1] == 'local-transcribe'
print(json.dumps({'provider': result['provider'], 'fallback': False}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=candidate, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, "isolated candidate module probe failed"
    assert json.loads(result.stdout) == {"provider": "dgx", "fallback": False}


@pytest.mark.parametrize("first_rc", [0, 7])
def test_component_proof_runner_isolates_collection_and_preserves_failure(tmp_path, monkeypatch, first_rc):
    from scripts import run_dd_profile_release_tests as runner
    candidate = tmp_path / "candidate"
    for name in (*runner.MANDATORY, runner.BOUNDARY, "tests/conftest.py"):
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    monkeypatch.setattr(sys, "argv", ["proof", "--candidate", str(candidate)])
    monkeypatch.setattr(runner, "prepare_file_limit", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-collection")
    monkeypatch.setenv("PYTHONPATH", "must-not-reach-collection")
    calls, homes = [], []
    def call(command, *, cwd, env):
        assert "ANTHROPIC_API_KEY" not in env
        home = Path(env["HERMES_HOME"])
        assert home.is_dir()
        assert env["DD_OBS_INGEST_URL"] == "http://127.0.0.1:1"
        homes.append(home); calls.append((command, cwd))
        if len(calls) == 1:
            assert cwd == candidate.resolve()
            assert env["PYTHONPATH"] == str(candidate.resolve())
            harness = Path(command[command.index("--rootdir") + 1])
            for name in (*runner.MANDATORY, runner.BOUNDARY):
                assert str(harness / name) in command
            for name in runner.REVIEWED_FIXTURES:
                assert (harness / name).read_bytes() == (runner.HELPER_ROOT / name).read_bytes()
            assert (harness / runner.BOUNDARY).read_bytes() == (candidate / runner.BOUNDARY).read_bytes()
            return first_rc
        assert cwd == runner.HELPER_ROOT
        assert "PYTHONPATH" not in env
        assert env["DD_PROFILE_COMPONENT_ROOT"] == str(candidate.resolve())
        return 0
    monkeypatch.setattr(subprocess, "call", call)
    assert runner.main() == first_rc
    assert len(calls) == (2 if first_rc == 0 else 1)
    assert all(not home.exists() for home in homes)


def test_component_proof_runner_missing_mandatory_refuses(tmp_path, monkeypatch):
    from scripts import run_dd_profile_release_tests as runner
    monkeypatch.setattr(sys, "argv", ["proof", "--candidate", str(tmp_path)])
    with pytest.raises(SystemExit, match="Mandatory candidate attribution tests missing"):
        runner.main()
