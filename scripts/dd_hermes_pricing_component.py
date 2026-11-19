"""Generate a pricing-only backport without migrating a pinned Hermes runtime.

No live paths, process controls, credentials or queue decisions belong here.
The owning profile/CLI release contracts must validate the generated identity.
"""
from __future__ import annotations

import ast
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

PATHS = ("agent/openai_gpt62_pricing.py", "agent/usage_pricing.py", "dd_obs.py", "run_agent.py")
SEAMS = ("datetime", "_cost", "canonical_usage", "cost_result")


class Refused(ValueError):
    pass


def seam(raw, name):
    nodes = []
    for node in ast.walk(ast.parse(raw)):
        if name == "datetime":
            match = (isinstance(node, ast.ImportFrom) and node.module == "datetime"
                     and node.col_offset == 0 and any(a.name == "datetime" and a.asname is None for a in node.names))
        else:
            match = isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        if match:
            nodes.append(node)
    if len(nodes) != 1:
        raise Refused("pricing_seam_ambiguous:" + name)
    return nodes[0]


def replace_seams(raw, old, new):
    """Replace only four reviewed AST nodes; preserve every other source byte."""
    edits = []
    lines = raw.splitlines(keepends=True)
    for name in SEAMS:
        actual, before, after = seam(raw, name), seam(old, name), seam(new, name)
        if ast.dump(actual) != ast.dump(before):
            raise Refused("pricing_baseline_seam_changed:" + name)
        if actual.col_offset != after.col_offset:
            raise Refused("pricing_baseline_indent_changed:" + name)
        # Python AST columns are UTF-8 byte offsets, not character offsets.
        start = sum(len(s.encode()) for s in lines[:actual.lineno - 1]) + actual.col_offset
        end = sum(len(s.encode()) for s in lines[:actual.end_lineno - 1]) + actual.end_col_offset
        edits.append((start, end, ast.get_source_segment(new, after).encode()))
    result = raw.encode()
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    ast.parse(result)
    return result.decode()


def backport(base, parent, reviewed):
    """The pinned runtime may differ elsewhere; never import those differences."""
    if set(parent) != set(PATHS) or set(reviewed) != set(PATHS) or set(base) != set(PATHS):
        raise Refused("pricing_path_set_changed")
    if base[PATHS[0]] is not None or parent[PATHS[0]] is not None:
        raise Refused("pricing_new_module_already_exists")
    result = {PATHS[0]: reviewed[PATHS[0]]}
    for path in PATHS[1:3]:
        if base[path] != parent[path]:
            raise Refused("pricing_baseline_module_changed:" + path)
        result[path] = reviewed[path]
    old, new = parent["run_agent.py"], reviewed["run_agent.py"]
    if replace_seams(old, old, new) != new:
        raise Refused("unapproved_runtime_delta")
    result["run_agent.py"] = replace_seams(base["run_agent.py"], old, new)
    for value in result.values():
        if not isinstance(value, str):
            raise Refused("pricing_source_missing")
        ast.parse(value)
    return result


def git(repo, *args, data=None, env=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=data, env=env, timeout=15)


def generate_tree(repo, baseline, reviewed):
    """Require merged pricing source; use a private index, never a live checkout."""
    for ref in (baseline, reviewed):
        if len(ref) != 40 or any(c not in "0123456789abcdef" for c in ref):
            raise Refused("exact_commit_required")
    subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", reviewed, "origin/main"], check=True)
    parent = git(repo, "rev-parse", reviewed + "^").decode().strip()
    changed = git(repo, "diff", "--name-only", parent, reviewed).decode().splitlines()
    if set(p for p in changed if not p.startswith("tests/")) != set(PATHS):
        raise Refused("reviewed_pricing_scope_changed")
    def sources(ref):
        result = {}
        for path in PATHS:
            exists = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", ref + ":" + path],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            result[path] = git(repo, "show", ref + ":" + path).decode() if exists else None
        return result
    files = backport(sources(baseline), sources(parent), sources(reviewed))
    identities = {}
    with tempfile.TemporaryDirectory(prefix="hermes-pricing-index-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        git(repo, "read-tree", baseline, env=env)
        for path, raw in files.items():
            blob = git(repo, "hash-object", "-w", "--stdin", data=raw.encode()).decode().strip()
            git(repo, "update-index", "--add", "--cacheinfo", "100644," + blob + "," + path, env=env)
            identities[path] = {"blob": blob, "sha256": hashlib.sha256(raw.encode()).hexdigest()}
        tree = git(repo, "write-tree", env=env).decode().strip()
    return {"kind": "hermes_gpt62_pricing_v1", "baseline_commit": baseline,
            "reviewed_commit": reviewed, "review_parent": parent,
            "artifact_tree": tree, "paths": identities}


def verify_tree(repo, baseline, reviewed, target):
    identity = generate_tree(repo, baseline, reviewed)
    actual = git(repo, "rev-parse", target + "^{tree}").decode().strip()
    if actual != identity["artifact_tree"]:
        raise Refused("pricing_artifact_not_exact_generated_tree")
    return identity


def produce(repo, baseline, reviewed):
    """Create an artifact commit, not a deploy proof or an approval."""
    identity = generate_tree(repo, baseline, reviewed)
    message = ("Generated GPT-6.2 pricing-only component\n\n"
               "Reviewed pricing: " + reviewed + "\nPinned component: " + baseline + "\n")
    target = git(repo, "commit-tree", identity["artifact_tree"], "-p", baseline,
                 data=message.encode()).decode().strip()
    # Read the real object back; never trust a caller-supplied artifact identity.
    identity = verify_tree(repo, baseline, reviewed, target)
    return {**identity, "artifact_commit": target}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--pricing-reviewed-commit", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = produce(Path(args.repo).resolve(), args.baseline, args.pricing_reviewed_commit)
    # New metadata only; the producer never overwrites a staged manifest.
    with open(args.out, "x") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({"artifact_commit": result["artifact_commit"], "out": args.out}))


if __name__ == "__main__":
    main()
