"""Five-file release fixtures; no live files, credential stores or network."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import dd_hermes_cli_release as release

OP = "11111111-2222-4333-8444-555555555555"
REVIEWED = "b" * 40


class FakeHost:
    def __init__(self):
        self.elapsed = 0
        self.mode = "normal"
        self.siblings = {"fixture_gateway": {"pid": 123, "cwd": "/separate-runtime"}}
        self.row = {}
        self.consumer_calls = 0
    def clock(self): return self.elapsed
    def remaining(self, deadline):
        if self.elapsed >= deadline: raise release.common.Refused("deadline_expired")
    def lineage(self, reviewed, deadline):
        if self.mode == "lineage_drift": raise release.common.Refused("lineage_drift")
        return {"reviewed_commit": reviewed, "artifact_commit": release.TARGET}
    def unchanged_source(self, deadline):
        if self.mode == "source_drift": raise release.common.Refused("source_drift")
        return {"base_commit": release.component.BASE}
    def gateways(self, deadline):
        self.remaining(deadline)
        return json.loads(json.dumps(self.siblings))
    def command(self, args, deadline):
        relative = args[-1].split(":", 1)[1]
        return 0, self.original_sources[relative]
    def http(self, url, deadline):
        return {"items": [self.row]} if "?" in url else dict(self.row)
    def consumers(self, deadline):
        self.consumer_calls += 1
        self.remaining(deadline)
        if self.mode == "consumer_failure": raise release.common.Refused("consumer_failure")
        return {p: {"provider": "dgx"} for p in release.PROFILES}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    source = root / "source"
    artifact = root / "artifact"
    for directory in (source, artifact):
        (directory / "tools").mkdir(parents=True)
        (directory / "hermes_cli").mkdir()
    homes = {p: root / p for p in release.PROFILES}
    for home in homes.values():
        home.mkdir()
        (home / "config.yaml").write_text("# retained\nstt:\n  provider: local # retained\nmodel: unchanged\n")
        (home / "sessions").mkdir()
        (home / "sessions/synthetic.json").write_text('{"message":"synthetic unchanged"}')
    for relative in release.SOURCES:
        (source / relative).write_text("baseline " + relative)
        (artifact / relative).write_text("candidate " + relative)
    python = root / "python"
    python.write_bytes(b"fixture interpreter")
    console = root / "hermes"
    console.write_bytes(b"fixture console")
    path_console = root / "path-hermes"
    path_console.symlink_to(console)
    monkeypatch.setattr(release, "ROOT", source)
    monkeypatch.setattr(release, "ARTIFACT", artifact)
    monkeypatch.setattr(release, "HOMES", homes)
    monkeypatch.setattr(release, "PYTHON", python)
    monkeypatch.setattr(release, "CONSOLE", console)
    monkeypatch.setattr(release, "PATH_CONSOLE", path_console)
    monkeypatch.setattr(release, "STATE", root / "state")
    host = FakeHost()
    host.original_sources = {p: (source / p).read_bytes() for p in release.SOURCES}
    manifest = release.stage(REVIEWED, host)
    path = root / "manifest.json"
    path.write_bytes(release.common.canonical(manifest))
    checksum = release.common.digest(path)
    host.row = {"id": OP, "service_name": release.SERVICE, "target_commit": release.TARGET,
                "status": "deploying", "decided_by": "Levi", "decided_at": "2026-09-08T00:00:00Z",
                "restart_command": release.apply_command(path, checksum)}
    return SimpleNamespace(host=host, manifest=manifest, path=path, checksum=checksum,
                           original={str(p): p.read_bytes() for p in release.paths()})


def apply(b): return release.apply(b.path, b.checksum, host=b.host)


def test_success_exact_five_files_and_fresh_verifier_contract(bundle):
    b = bundle
    assert not release.STATE.exists()
    result = apply(b)
    assert result["status"] == "succeeded" and len(result["written"]) == 5
    assert result["rollback_count"] == 0 and b.host.consumer_calls == 1
    out = release.verify(b.path, b.checksum, OP, b.host, challenge="fresh-challenge", attempt_id="attempt1")
    assert out["service"] == release.SERVICE and out["queue_id"] == OP
    assert out["challenge"] == "fresh-challenge" and out["attempt_id"] == "attempt1"
    assert out["manifest_sha256"] == b.checksum and out["target_commit"] == release.TARGET
    assert len(out["source_files"]) == 2 and len(out["config_files"]) == 3
    for path in release.paths():
        assert release.common.digest(path) == b.manifest["candidate"][str(path)]
        assert path.stat().st_mode & 0o777 == b.manifest["baseline"][str(path)]["mode"]
    for home in release.HOMES.values():
        assert (home / "sessions/synthetic.json").read_text() == '{"message":"synthetic unchanged"}'
        assert (home / "config.yaml").read_text().startswith("# retained\nstt:\n  provider: dgx # retained")
    with pytest.raises(release.common.Refused, match="already_consumed"):
        apply(b)


@pytest.mark.parametrize("drift", ["source", "config", "metadata", "lineage_drift", "source_drift", "python", "console"])
def test_preflight_drift_has_no_service_mutation(bundle, drift):
    b = bundle
    if drift in ("lineage_drift", "source_drift"): b.host.mode = drift
    elif drift == "source": release.paths()[0].write_text("foreign")
    elif drift == "config": release.paths()[-1].write_text("foreign")
    elif drift == "metadata": release.paths()[0].chmod(0o600)
    elif drift == "python": release.PYTHON.write_bytes(b"different interpreter")
    else: release.CONSOLE.write_bytes(b"different console")
    before = {str(p): p.read_bytes() for p in release.paths()}
    result = apply(b)
    assert result["status"] == "preflight_failed" and result["written"] == []
    assert before == {str(p): p.read_bytes() for p in release.paths()}


@pytest.mark.parametrize("field,value", [("service_name", "other"), ("target_commit", "f"*40),
                                        ("status", "pending"), ("restart_command", "echo not-approved"),
                                        ("decided_by", "")])
def test_unapproved_queue_refuses_before_journal(bundle, field, value):
    b = bundle; b.host.row[field] = value
    with pytest.raises(release.common.Refused): apply(b)
    assert not release.STATE.exists()


@pytest.mark.parametrize("after", range(1, 6))
def test_failure_after_each_atomic_write_restores_exact_bytes_and_metadata(bundle, monkeypatch, after):
    b = bundle
    original = release.replace
    count = 0
    def fail_after_write(*args, **kwargs):
        nonlocal count
        original(*args, **kwargs)
        if not kwargs.get("restore"):
            count += 1
            if count == after: raise release.common.Refused("failure_after_rename")
    monkeypatch.setattr(release, "replace", fail_after_write)
    result = apply(b)
    assert result["status"] == "rolled_back" and result["rollback_count"] == 1
    assert all(release.file_identity(p) == b.manifest["baseline"][str(p)] for p in release.paths())
    assert all(p.read_bytes() == b.original[str(p)] for p in release.paths())


def test_foreign_edit_is_preserved_and_replay_fenced(bundle, monkeypatch):
    b = bundle
    def fail(deadline):
        release.paths()[0].write_bytes(b"foreign after apply")
        raise release.common.Refused("consumer_failure")
    monkeypatch.setattr(b.host, "consumers", fail)
    result = apply(b)
    assert result["status"] == "failed_recovery" and result["rollback_count"] == 1
    assert release.paths()[0].read_bytes() == b"foreign after apply"
    b.host.row["id"] = "22222222-2222-4333-8444-555555555555"
    with pytest.raises(release.common.Refused, match="prior_operation_requires_recovery"): apply(b)


def test_sibling_capture_at_apply_not_approval_wait_and_drift_during_apply_refuses(bundle, monkeypatch):
    b = bundle
    b.host.siblings["fixture_gateway"]["pid"] = 456
    original = b.host.consumers
    def change(deadline):
        value = original(deadline)
        b.host.siblings["fixture_gateway"]["pid"] += 1
        return value
    monkeypatch.setattr(b.host, "consumers", change)
    result = apply(b)
    assert result["protected_gateways"]["fixture_gateway"]["pid"] == 456
    assert result["status"] == "failed_recovery"
    assert all(p.read_bytes() == b.original[str(p)] for p in release.paths())


def test_process_death_after_mutation_leaves_fence_not_false_success(bundle, monkeypatch):
    b = bundle
    original = release.replace
    def die(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt
    monkeypatch.setattr(release, "replace", die)
    with pytest.raises(KeyboardInterrupt): apply(b)
    state = json.loads((release.STATE / "state.json").read_bytes())
    assert state["status"] == "applying" and len(state["written"]) == 1
    with pytest.raises(release.common.Refused, match="already_consumed"): apply(b)


def test_lock_and_manifest_path_allowlist(bundle):
    b = bundle
    with release.locked():
        with pytest.raises(release.common.Refused, match="operation_in_progress"): apply(b)
    b.manifest["candidate"][str(release.ROOT / "other.py")] = "a"*64
    b.path.write_bytes(release.common.canonical(b.manifest))
    with pytest.raises(release.common.Refused, match="unsupported_manifest"):
        release.read_manifest(b.path, release.common.digest(b.path))


def test_manifest_cannot_substitute_unreviewed_component_hash(bundle):
    b = bundle
    b.manifest["candidate"][str(release.ROOT / release.SOURCES[0])] = "a"*64
    with pytest.raises(release.common.Refused, match="manifest_component_digest_mismatch"):
        release.validate(b.manifest, b.host, 25)


def test_unrelated_env_rotation_while_awaiting_approval_does_not_block(bundle):
    b = bundle
    assert "environment_sha256" not in b.manifest
    (release.ROOT / ".env").write_text("SYNTHETIC_ROTATION=changed\n")
    assert apply(b)["status"] == "succeeded"


def test_replaced_config_symlink_refuses_without_reading_target(bundle):
    b = bundle
    target = release.paths()[-1]
    target.unlink()
    target.symlink_to("/nonexistent/auth.json")
    assert apply(b)["status"] == "preflight_failed"
    assert release.paths()[0].read_bytes() == b.original[str(release.paths()[0])]


def test_cli_driver_retains_existing_component_harness_and_requires_cli_tests(monkeypatch):
    from scripts import run_dd_cli_release_tests as driver
    calls = []
    monkeypatch.setattr(driver, "profile_main", lambda **kwargs: calls.append(kwargs) or 7)
    assert driver.main() == 7
    assert calls == [{"extra_tests": ("tests/scripts/test_dd_hermes_cli_release.py",)}]


@pytest.fixture
def launch_fixture(tmp_path):
    """Actual d1 profile pre-parser and profile resolver, with a report-only CLI."""
    repo = Path(__file__).resolve().parents[2]
    def old(path):
        return subprocess.run(["/usr/bin/git", "-C", str(repo), "show", release.component.BASE+":"+path],
                              capture_output=True, text=True, check=True, timeout=5).stdout
    root = tmp_path.resolve() / "editable-source"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "hermes_cli/__init__.py").write_text("")
    (root / "tools/__init__.py").write_text("")
    (root / "hermes_cli/env_loader.py").write_text("def load_hermes_dotenv(**kwargs): pass\n")
    (root / "hermes_constants.py").write_text(old("hermes_constants.py"))
    (root / "hermes_cli/profiles.py").write_text(old("hermes_cli/profiles.py"))
    tree = ast.parse(old("hermes_cli/main.py"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_apply_profile_override")
    prefix = "import os,sys,json\nfrom pathlib import Path\n"
    (root / "hermes_cli/main.py").write_text(prefix + ast.unparse(node) + '''
_apply_profile_override()
def main():
 from tools import transcription_tools as t
 print(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd(),"home":os.environ["HERMES_HOME"],"module":t.__file__,"provider":t._load_stt_config()["provider"]}))
if __name__ == "__main__": main()
''')
    (root / "hermes_cli/config.py").write_text('import os,json\nfrom pathlib import Path\ndef get_hermes_home(): return Path(os.environ["HERMES_HOME"])\ndef load_config(): return json.loads((get_hermes_home()/"config.yaml").read_text())\n')
    (root / "tools/transcription_tools.py").write_text('from hermes_cli.config import load_config\ndef _load_stt_config(): return load_config()["stt"]\n')
    home = tmp_path / "home"
    for p in release.PROFILES:
        target = home / ".hermes/profiles" / p
        target.mkdir(parents=True)
        (target / "config.yaml").write_text('{"stt":{"provider":"dgx"}}')
        for directory in ("cron", "sessions", "logs", "memories"):
            (target / directory).mkdir()
        (target / "SOUL.md").write_text("synthetic fixture")
    bindir = tmp_path / "bin"; bindir.mkdir()
    console = bindir / "hermes"
    console.write_text("#!"+sys.executable+"\nfrom hermes_cli.main import main\nif __name__=='__main__': main()\n")
    console.chmod(0o700)
    aliasdir = tmp_path / "aliases"; aliasdir.mkdir()
    (aliasdir / "hermes").symlink_to(console)
    neutral = tmp_path / "neutral"; neutral.mkdir()
    env = {"PATH": str(aliasdir)+os.pathsep+os.environ.get("PATH", ""), "HOME": str(home),
           "HERMES_HOME": str(home/".hermes"), "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    return SimpleNamespace(root=root, home=home, console=console, neutral=neutral, env=env)


@pytest.mark.parametrize("mode", ["console", "path_console", "module"])
@pytest.mark.parametrize("profile", release.PROFILES)
def test_fresh_real_console_and_background_launch_preserves_profile_cwd_args(launch_fixture, mode, profile):
    f = launch_fixture
    prefix = {"console": [str(f.console)], "path_console": ["hermes"],
              "module": [sys.executable, "-B", "-m", "hermes_cli.main"]}[mode]
    r = subprocess.run(prefix+["-p",profile,"-z","synthetic background body"], cwd=f.neutral,
                       env=f.env, capture_output=True, text=True, check=True, timeout=5)
    value = json.loads(r.stdout)
    assert value == {"argv":["-z","synthetic background body"],"cwd":str(f.neutral),
                     "home":str(f.home/".hermes/profiles"/profile),
                     "module":str(f.root/"tools/transcription_tools.py"),"provider":"dgx"}


@pytest.mark.parametrize("attack", ["auth", "write", "network"])
def test_consumer_inspection_guard_refuses_even_caught_side_effect(launch_fixture, attack):
    f = launch_fixture
    code = {"auth": "open('/nonexistent/auth.json')", "write": "open('forbidden-output','w')",
            "network": "__import__('socket').socket().connect(('127.0.0.1',1))"}[attack]
    with (f.root / "tools/transcription_tools.py").open("a") as stream:
        stream.write("\ntry:\n "+code+"\nexcept Exception: pass\n")
    env = {**f.env, "HERMES_HOME":str(f.home/".hermes/profiles/document-review")}
    r = subprocess.run([sys.executable,"-B","-c",release.VERIFY_IMPORTS,"module",str(f.root),str(f.console)],
                       cwd=f.neutral,env=env,capture_output=True,text=True,timeout=5)
    assert r.returncode == 1 and json.loads(r.stdout)["error"] == "consumer_inspection_refused"
    assert not (f.neutral/"forbidden-output").exists()


def test_inspection_does_not_create_missing_home_prerequisites(launch_fixture):
    f = launch_fixture
    home = f.home / ".hermes/profiles/document-review"
    (home / "SOUL.md").unlink()
    env = {**f.env, "HERMES_HOME": str(home)}
    r = subprocess.run([sys.executable,"-B","-c",release.VERIFY_IMPORTS,"module",str(f.root),str(f.console)],
                       cwd=f.neutral,env=env,capture_output=True,text=True,timeout=5)
    assert r.returncode == 1
    assert not (home / "SOUL.md").exists()
