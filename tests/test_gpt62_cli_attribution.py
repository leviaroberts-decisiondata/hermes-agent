"""Versioned CLI transaction tests: temp files, no profiles or credentials."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import dd_hermes_cli_pricing_release as release

OP = "11111111-2222-4333-8444-555555555555"
REVIEWED = "b" * 40


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    apps = root / "apps"
    artifact = apps / "artifact"
    artifact.mkdir(parents=True)
    monkeypatch.setattr(release.common, "APPS", apps)
    engine = release.engine_for(str(artifact), "a" * 40, "c" * 40)
    engine.ROOT = root / "source"
    engine.ROOT.mkdir()
    engine.STATE = root / "state"
    engine.PYTHON = root / "python"
    engine.CONSOLE = root / "console"
    engine.PYTHON.write_bytes(b"fixture python")
    engine.CONSOLE.write_bytes(b"fixture console")
    engine.PATH_CONSOLE = engine.CONSOLE
    engine.HOMES = {p: root / p for p in engine.PROFILES}
    home_bytes = {}
    for p, home in engine.HOMES.items():
        home.mkdir()
        for name, raw in (("config.yaml", b"model: unchanged\n"), ("history.json", b"retained history\n")):
            path = home / name
            path.write_bytes(raw)
            home_bytes[str(path)] = raw
    original = {}
    for name in engine.SOURCES:
        source, candidate = engine.ROOT / name, artifact / name
        source.parent.mkdir(parents=True, exist_ok=True)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        raw = ("baseline " + name).encode()
        source.write_bytes(raw)
        candidate.write_bytes(raw if name not in release.pricing.PATHS else ("reviewed " + name).encode())
        original[name] = raw

    class Host:
        mode = "normal"
        def clock(self): return 0
        def remaining(self, deadline): return None
        def lineage(self, reviewed, deadline): return {"reviewed_commit": reviewed}
        def unchanged_source(self, deadline): return {"base_commit": engine.component.BASE}
        def gateways(self, deadline): return {"protected": "same"}
        def command(self, args, deadline): return 0, original[args[-1].split(":", 1)[1]]
        def http(self, url, deadline): return {"items": [self.row]} if "?" in url else self.row
        def consumers(self, deadline):
            if self.mode == "consumer_failure": raise release.common.Refused("consumer_failure")
            return {"synthetic": "verified"}
    host = Host()
    manifest = release.stage(engine, REVIEWED, host)
    path = root / "manifest.json"
    path.write_bytes(engine.common.canonical(manifest))
    checksum = engine.common.digest(path)
    host.row = {"id": OP, "service_name": engine.SERVICE, "target_commit": engine.TARGET,
                "status": "deploying", "decided_by": "Independent fixture reviewer",
                "decided_at": "2026-11-19T00:00:00Z", "restart_command": engine.apply_command(path, checksum)}
    return SimpleNamespace(engine=engine, host=host, manifest=manifest, path=path,
                           checksum=checksum, original=original, homes=home_bytes)


def test_success_changes_only_three_pricing_files_with_fresh_verifier_binding(bundle):
    b = bundle
    before = {name: b.engine.file_identity(b.engine.ROOT / name) for name in b.original}
    result = b.engine.apply(b.path, b.checksum, host=b.host)
    assert result["status"] == "succeeded"
    assert set(result["written"]) == {str(b.engine.ROOT / name) for name in release.pricing.PATHS}
    for name in b.engine.SOURCES[:2]:
        assert b.engine.file_identity(b.engine.ROOT / name) == before[name]
    for path, raw in b.homes.items(): assert Path(path).read_bytes() == raw
    observation = b.engine.verify(b.path, b.checksum, OP, b.host, challenge="fresh", attempt_id="fresh-attempt")
    assert observation["kind"] == release.KIND
    assert observation["config_files"] == {}
    assert observation["challenge"] == "fresh" and observation["attempt_id"] == "fresh-attempt"
    assert len(observation["source_files"]) == 5


def test_verification_failure_rolls_back_exact_source_and_metadata(bundle):
    b = bundle
    before = {name: b.engine.file_identity(b.engine.ROOT / name) for name in b.original}
    b.host.mode = "consumer_failure"
    result = b.engine.apply(b.path, b.checksum, host=b.host)
    assert result["status"] == "rolled_back" and result["rollback_count"] == 1
    for name, raw in b.original.items():
        assert (b.engine.ROOT / name).read_bytes() == raw
        assert b.engine.file_identity(b.engine.ROOT / name) == before[name]
    for path, raw in b.homes.items(): assert Path(path).read_bytes() == raw


def test_source_drift_refuses_without_overwriting_it(bundle):
    b = bundle
    path = b.engine.ROOT / "dd_obs.py"
    path.write_bytes(b"foreign edit")
    result = b.engine.apply(b.path, b.checksum, host=b.host)
    assert result["status"] == "preflight_failed" and result["written"] == []
    assert path.read_bytes() == b"foreign edit"


def test_manifest_cannot_redirect_pricing_review_or_artifact(bundle):
    b = bundle
    b.manifest["pricing_reviewed_commit"] = "d" * 40
    b.path.write_bytes(b.engine.common.canonical(b.manifest))
    with pytest.raises(release.common.Refused, match="binding_changed"):
        b.engine.read_manifest(b.path, b.engine.common.digest(b.path))


def test_approval_is_required_before_any_transaction(bundle):
    b = bundle
    b.host.row["decided_by"] = ""
    with pytest.raises(release.common.Refused): b.engine.apply(b.path, b.checksum, host=b.host)
    assert not b.engine.STATE.exists()


def test_all_pricing_helper_dependencies_are_pinned(bundle):
    names = {str(Path(p).relative_to(Path(release.__file__).resolve().parents[1]))
             for p in bundle.engine.helper_identity()}
    assert set(release.EXTRA_HELPERS).issubset(names)
    assert "scripts/dd_hermes_pricing_component.py" in names
    assert bundle.manifest["helper_bundle_version"] == 2


def test_pricing_inspector_compiles_and_includes_expected_semantics(bundle):
    ast.parse(bundle.engine.VERIFY_IMPORTS)
    expected = release.expected_prices(bundle.engine.ROOT, bundle.manifest["candidate"])
    assert (expected["astra_mixed"], expected["sol_mixed"]) == ("0.1456", "0.0364")
    assert expected["unknown_write"] is None
    assert expected["included_status"] == "included" and expected["included_cost"] == "0"


def test_native_inspector_runs_actual_price_module_without_writes_or_network(bundle, tmp_path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "native-inspection-home"
    for name in ("", "cron", "sessions", "logs", "memories"):
        (home / name).mkdir(parents=True, exist_ok=True)
    (home / "SOUL.md").write_text("Synthetic inspection fixture\n")
    (home / "config.yaml").write_text("stt:\n  provider: dgx\n")
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
    run = subprocess.run([sys.executable, "-B", "-c", bundle.engine.VERIFY_IMPORTS,
                          "module", str(root), str(root / "hermes_cli/main.py")],
                         cwd=root, env=env, capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stdout
    result = json.loads(run.stdout)
    hashes = {str(root / name): release.common.digest(root / name) for name in release.pricing.PATHS}
    assert result["pricing"] == release.expected_prices(root, hashes)
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_pricing_lineage_checks_the_actual_clean_checkout(bundle, monkeypatch):
    engine = bundle.engine
    def git(*args):
        return subprocess.check_output(["git", "-C", str(engine.ARTIFACT), *args],text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("add", ".")
    git("commit", "-qm", "synthetic")
    engine.TARGET = git("rev-parse", "HEAD")
    identity = {"artifact_tree": git("rev-parse", "HEAD^{tree}")}
    parent = engine.Host.__mro__[1]
    monkeypatch.setattr(parent, "lineage", lambda *args: identity)
    host = engine.Host()
    assert host.lineage(REVIEWED, host.clock()+5) == identity
    path = engine.ARTIFACT / "dd_obs.py"
    path.write_bytes(b"foreign unreviewed pricing")
    with pytest.raises(release.common.Refused, match="tracked_source_dirty"):
        host.lineage(REVIEWED, host.clock()+5)
