"""Pricing backport fixtures never inspect or modify a running profile."""
import ast
from copy import deepcopy
import subprocess

import pytest

from scripts import dd_hermes_pricing_component as component


OLD = '''from datetime import datetime
unrelated = "original"
def emit():
    _cost = 0
def run():
    canonical_usage = normalize_usage(response.usage)
    cost_result = estimate_usage_cost(model, canonical_usage)
'''
NEW = OLD.replace("import datetime", "import datetime, timezone").replace("_cost = 0", "_cost = None")
NEW = NEW.replace("normalize_usage(response.usage)", "normalize_usage(response.usage, model_name=model)")
NEW = NEW.replace("estimate_usage_cost(model, canonical_usage)", "estimate_usage_cost(model, canonical_usage, requested_at=at)")


def sources():
    parent = {component.PATHS[0]: None, "agent/usage_pricing.py": "price = 1\n",
              "dd_obs.py": "unknown = 0\n", "run_agent.py": OLD}
    reviewed = {component.PATHS[0]: "rate = 8\n", "agent/usage_pricing.py": "price = 8\n",
                "dd_obs.py": "unknown = None\n", "run_agent.py": NEW}
    baseline = deepcopy(parent)
    baseline["run_agent.py"] = OLD.replace('"original"', '"pinned runtime, preserved"')
    return baseline, parent, reviewed


def test_backport_keeps_pinned_runtime_bytes_outside_reviewed_nodes():
    baseline, parent, reviewed = sources()
    result = component.backport(baseline, parent, reviewed)
    assert result["run_agent.py"] == NEW.replace('"original"', '"pinned runtime, preserved"')
    assert set(result) == set(component.PATHS)
    assert baseline["dd_obs.py"] == "unknown = 0\n"


@pytest.mark.parametrize("change", ["seam", "module", "extra_runtime", "existing_new_module", "path_set"])
def test_ambiguous_or_unreviewed_runtime_delta_is_refused(change):
    baseline, parent, reviewed = sources()
    if change == "seam": baseline["run_agent.py"] = baseline["run_agent.py"].replace("_cost = 0", "_cost = 1")
    if change == "module": baseline["dd_obs.py"] = "foreign = True\n"
    if change == "extra_runtime": reviewed["run_agent.py"] += "extra_change = True\n"
    if change == "existing_new_module": baseline[component.PATHS[0]] = "foreign = True\n"
    if change == "path_set": reviewed["foreign.py"] = "foreign = True\n"
    with pytest.raises(component.Refused): component.backport(baseline, parent, reviewed)


def test_duplicate_ast_seam_is_refused():
    with pytest.raises(component.Refused): component.replace_seams(OLD + "_cost = 1\n", OLD, NEW)


def test_utf8_offsets_preserve_unrelated_source():
    raw = '# Ñúñez\n' + OLD
    result = component.replace_seams(raw, OLD, NEW)
    assert result == '# Ñúñez\n' + NEW
    ast.parse(result)


def test_real_owner_snapshots_have_compatible_pricing_seams():
    # Read immutable blobs in this isolated checkout, never a live source tree.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    def show(ref, path):
        p = subprocess.run(["git", "-C", str(root), "show", ref + ":" + path],capture_output=True,text=True)
        return p.stdout if p.returncode == 0 else None
    refs = ("ed06e69a39ebbc8c31d9f9243651a915416c1778", "bb200c4a13e1", "94cf9d01629dab501551026a8fd927b26f75bfca")
    # A shallow executor without the retained snapshots reports the fixture gap.
    if any(show(ref, "run_agent.py") is None for ref in refs):
        pytest.skip("retained owner snapshot objects unavailable")
    base, parent, reviewed = ({p: show(ref, p) for p in component.PATHS} for ref in refs)
    result = component.backport(base, parent, reviewed)
    assert 'model_name=self.model' in result["run_agent.py"]
    assert 'service_tier=getattr(response, "service_tier", None)' in result["run_agent.py"]
    # Large unrelated runtime differences remain pinned, not silently upgraded.
    assert result["run_agent.py"] != reviewed["run_agent.py"]


@pytest.fixture
def git_owner(tmp_path):
    # A local synthetic repository; these refs are not production lineage/proof.
    root = tmp_path / "owner"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args],text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Synthetic fixture")
    git("config", "user.email", "fixture@example.invalid")
    baseline, parent, reviewed = sources()
    def write(values):
        for name, raw in values.items():
            if raw is None: continue
            p = root / name
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(raw)
        git("add", ".")
        git("commit", "-qm", "synthetic")
        return git("rev-parse", "HEAD")
    parent_ref = write(parent)
    reviewed_ref = write(reviewed)
    git("update-ref", "refs/remotes/origin/main", reviewed_ref)
    return root, git, parent_ref, reviewed_ref, baseline


def test_producer_and_verifier_use_real_tree_objects(git_owner):
    root, git, baseline, reviewed, _ = git_owner
    head = git("rev-parse", "HEAD")
    result = component.produce(root, baseline, reviewed)
    assert git("rev-parse", "HEAD") == head
    assert git("rev-parse", result["artifact_commit"] + "^") == baseline
    assert component.verify_tree(root, baseline, reviewed, result["artifact_commit"])["artifact_tree"] == result["artifact_tree"]
    with pytest.raises(component.Refused): component.verify_tree(root, baseline, reviewed, baseline)


def test_unmerged_pricing_cannot_generate_an_artifact(git_owner):
    root, git, baseline, reviewed, _ = git_owner
    git("update-ref", "refs/remotes/origin/main", baseline)
    with pytest.raises(subprocess.CalledProcessError): component.produce(root, baseline, reviewed)
