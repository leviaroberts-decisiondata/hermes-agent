#!/usr/bin/env python3
"""Queue-owned, single-profile DGX component cutover for 15 named gateways.

The candidate keeps the d1 runtime/history writer, replaces the reviewed
transcription module and backports its one-token DGX default. Ordinary and Classic
are protected siblings, never targets.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import sys
from uuid import UUID

if __package__:
    from scripts import dd_hermes_ordinary_release as common
else:
    import dd_hermes_ordinary_release as common

BASE = "d1a294da78c45a18c9f8cc89fa992e33b4cc0282"
COMPONENT = "tools/transcription_tools.py"
DEFAULT_COMPONENT = "hermes_cli/config.py"
NAMED = ("architect-standards", "dd-design", "dd-engineer-1", "dd-engineer-2",
         "dd-engineer-3", "dd-pmo", "devops-release", "knowledge-context", "product-os", "qa-review")
SEPARATE = ("azul", "finance", "hardware", "hyperscience", "ptg")
PROFILE_HOMES = {name: Path("/Users/openclaw/.hermes/profiles") / name for name in NAMED}
PROFILE_HOMES.update({name: Path("/Users/openclaw/.hermes-" + name) for name in SEPARATE})
PLIST_ROOT = Path("/Users/openclaw/Library/LaunchAgents")
STATE_ROOT = Path("/Users/openclaw/.openclaw/hermes-profile-release")
PROTECTED = {"ai.hermes.gateway": Path("/Users/openclaw/.hermes"),
             "ai.hermes.gateway-classic": Path("/Users/openclaw/.hermes-classic")}


def helper_identity():
    root = Path(__file__).resolve().parents[1]
    paths = ("scripts/dd_hermes_profile_release.py", "scripts/dd_hermes_ordinary_release.py",
             "scripts/run_dd_profile_release_tests.py", "scripts/run_dd_release_tests.py",
             "tests/scripts/test_dd_hermes_profile_release.py", "tests/scripts/test_dd_hermes_ordinary_release.py",
             "tests/test_route_to_lane_shared_home.py", "tests/test_route_to_lane_mission_parity.py")
    return {str(root / name): common.digest(root / name) for name in paths}


def default_provider_node(raw):
    tree = ast.parse(raw)
    assignments = [node.value for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG" for target in node.targets)]
    def child(node, name):
        if not isinstance(node, ast.Dict): raise common.Refused("default_config_not_literal")
        values = [value for key, value in zip(node.keys, node.values)
                  if isinstance(key, ast.Constant) and key.value == name]
        if len(values) != 1: raise common.Refused("default_config_ambiguous")
        return values[0]
    if len(assignments) != 1: raise common.Refused("default_config_ambiguous")
    value = child(child(assignments[0], "stt"), "provider")
    if not isinstance(value, ast.Constant): raise common.Refused("default_provider_not_literal")
    return value


def backport_default(raw):
    """Only the d1 DEFAULT_CONFIG.stt.provider token changes; preserve all else."""
    node = default_provider_node(raw)
    if node.value != "local": raise common.Refused("unexpected_baseline_default")
    lines = raw.splitlines(keepends=True)
    start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    token = raw[start:end]
    if token not in (b'"local"', b"'local'"):
        raise common.Refused("unsupported_default_token")
    return raw[:start] + token[:1] + b"dgx" + token[-1:] + raw[end:]


def component_identity(host, candidate, target, reviewed, deadline):
    """Actual composite identity, never mislabelled as a full-main runtime."""
    if not re.fullmatch(r"[a-f0-9]{40}", reviewed or ""):
        raise common.Refused("exact_reviewed_commit_required")
    git = ["/usr/bin/git", "-C", str(common.safe_path(candidate))]
    def output(*args):
        return host.command(git + list(args), deadline)[1].decode().strip()
    # The reviewed source must be merged; the artifact is a separate exact tree.
    host.command(git + ["merge-base", "--is-ancestor", reviewed, "origin/main"], deadline)
    changed = output("diff", "--name-only", BASE, target, "--").splitlines()
    if changed != sorted([COMPONENT, DEFAULT_COMPONENT]):
        raise common.Refused("component_candidate_has_unapproved_delta")
    blob = output("rev-parse", target + ":" + COMPONENT)
    if blob != output("rev-parse", reviewed + ":" + COMPONENT):
        raise common.Refused("component_not_exact_reviewed_blob")
    writer = output("rev-parse", target + ":hermes_state.py")
    if writer != output("rev-parse", BASE + ":hermes_state.py"):
        raise common.Refused("history_writer_changed")
    baseline_config = host.command(git + ["show", BASE + ":" + DEFAULT_COMPONENT], deadline)[1]
    actual_config = host.command(git + ["show", target + ":" + DEFAULT_COMPONENT], deadline)[1]
    reviewed_config = host.command(git + ["show", reviewed + ":" + DEFAULT_COMPONENT], deadline)[1]
    if actual_config != backport_default(baseline_config) or default_provider_node(reviewed_config).value != "dgx":
        raise common.Refused("default_backport_not_exact_reviewed_behavior")
    return {"base_commit": BASE, "artifact_commit": target,
            "artifact_tree": output("rev-parse", target + "^{tree}"),
            "reviewed_commit": reviewed, "path": COMPONENT, "blob": blob,
            "unchanged_history_blob": writer,
            "default_backport": {"path": DEFAULT_COMPONENT,
                "baseline_blob": output("rev-parse", BASE + ":" + DEFAULT_COMPONENT),
                "artifact_blob": output("rev-parse", target + ":" + DEFAULT_COMPONENT),
                "reviewed_blob": output("rev-parse", reviewed + ":" + DEFAULT_COMPONENT),
                "change": "DEFAULT_CONFIG.stt.provider local to dgx; all other bytes unchanged"}}


def engine_for(profile):
    """An isolated instance of the tested transaction engine, scoped once.

    Do not mutate the imported ordinary module's globals: tests and other callers
    can hold an ordinary engine in the same process without changing its targets.
    """
    if profile not in PROFILE_HOMES:
        raise common.Refused("profile_not_allowlisted")
    spec = importlib.util.spec_from_file_location("_dd_profile_transaction", common.__file__)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    # Use one refusal class at the public boundary and fixture boundary.
    engine.Refused = common.Refused
    engine.SERVICE = "hermes-profile-" + profile
    engine.LABEL = "gui/502/ai.hermes.gateway-" + profile
    engine.PLIST = PLIST_ROOT / ("ai.hermes.gateway-" + profile + ".plist")
    engine.ORDINARY_HOME = PROFILE_HOMES[profile]
    engine.STATE = STATE_ROOT / profile
    cli_profile = profile if profile in NAMED else "default"

    def read_plist():
        if engine.PLIST.is_symlink():
            raise common.Refused("plist_symlink")
        raw = engine.safe_path(engine.PLIST).read_bytes()
        document = plistlib.loads(raw)
        if (document.get("Label") != engine.LABEL.split("/")[-1]
                or document.get("Program") or not document.get("ProgramArguments")):
            raise common.Refused("profile_plist_identity_mismatch")
        return raw, document

    def candidate_plist(baseline, candidate, python):
        args = list(baseline["ProgramArguments"])
        expected = ["-m", "hermes_cli.main", "--profile", cli_profile, "gateway", "run"]
        if args[1:] not in (expected, expected + ["--replace"]):
            raise common.Refused("unsupported_profile_launcher")
        document = dict(baseline)
        document["ProgramArguments"] = [str(python)] + expected
        document["WorkingDirectory"] = str(candidate)
        return plistlib.dumps(document, sort_keys=True)

    def connected(value, enabled):
        names = sorted(name for name, entry in value.get("platforms", {}).items()
                       if entry.get("state") == "connected")
        required = sorted(set(names).intersection(enabled))
        if "telegram" not in required:
            raise common.Refused("profile_telegram_unavailable")
        return required, sorted(set(names).difference(enabled))

    def command(path, checksum):
        return shlex.join([sys.executable, str(Path(__file__).resolve()), "activate", "--profile", profile,
                           "--manifest", str(engine.safe_path(path)), "--manifest-sha256", checksum,
                           "--queue-id", "auto", "--label", engine.LABEL])

    def approved(host, queue_id, manifest, restart, deadline):
        if queue_id == "auto":
            rows = host.http(engine.QUEUE + "?status=deploying&service=" + engine.SERVICE + "&limit=100", deadline).get("items", [])
            rows = [row for row in rows if row.get("target_commit") == manifest["target_commit"]
                    and row.get("restart_command") == restart]
            if len(rows) != 1:
                raise common.Refused("unique_deploying_row_required")
            queue_id = rows[0]["id"]
        try:
            queue_id = str(UUID(queue_id))
        except (TypeError, ValueError):
            raise common.Refused("invalid_queue_id") from None
        row = host.http(engine.QUEUE + "/" + queue_id, deadline)
        if (row.get("id") != queue_id or row.get("service_name") != engine.SERVICE
                or row.get("target_commit") != manifest["target_commit"]
                or row.get("status") not in {"approved", "deploying"}
                or not row.get("decided_by") or not row.get("decided_at")
                or row.get("restart_command") != restart):
            raise common.Refused("matching_approved_queue_row_required")
        return queue_id

    base_validate = engine.validate
    def validate(manifest, host, deadline, **kwargs):
        base_validate(manifest, host, deadline, **kwargs)
        if manifest.get("profile") != profile or manifest.get("helper_sha256") != helper_identity():
            raise common.Refused("profile_or_helper_changed")
        host.helpers_reviewed(manifest["component"]["reviewed_commit"], deadline)
        actual = host.component_identity(manifest["candidate"], manifest["target_commit"],
                                         manifest["component"]["reviewed_commit"], deadline)
        if actual != manifest["component"]:
            raise common.Refused("component_identity_changed")
        host.check_protected(deadline)

    base_lock = engine.locked_state
    @contextmanager
    def locked_state():
        if STATE_ROOT.is_symlink():
            raise common.Refused("profile_state_root_symlink")
        STATE_ROOT.mkdir(mode=0o700, exist_ok=True)
        if STATE_ROOT.stat().st_mode & 0o077:
            raise common.Refused("profile_state_root_not_private")
        fd = os.open(STATE_ROOT / "fleet.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fleet_lock:
            try:
                fcntl.flock(fleet_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise common.Refused("profile_fleet_operation_in_progress") from None
            with base_lock():
                yield

    base_verify_runtime = engine.verify_runtime
    def verify_runtime(host, root, old_pid, deadline, since, platforms):
        identity = base_verify_runtime(host, root, old_pid, deadline, since, platforms)
        # Recovery also verifies a runtime, but does not call final validate.
        host.check_protected(deadline)
        return identity

    engine.read_plist = read_plist
    engine.candidate_plist = candidate_plist
    engine.connected_platforms = connected
    engine.restart_command = command
    engine.approved_row = approved
    engine.validate = validate
    engine.locked_state = locked_state
    engine.verify_runtime = verify_runtime
    return engine


def host_for(engine):
    class ProfileHost(engine.Host):
        protected = None

        def http(self, url, deadline):
            if url != engine.HEALTH:
                return super().http(url, deadline)
            self.remaining(deadline)
            path = engine.safe_path(engine.ORDINARY_HOME / "gateway_state.json")
            if path.is_symlink():
                raise common.Refused("profile_state_symlink")
            value = json.loads(path.read_bytes())
            # This is portless status, explicitly bound by health() to the
            # launchd/lsof process identity. No invented HTTP proof or timestamp.
            return {**value, "status": "ok"}

        def component_identity(self, candidate, target, reviewed, deadline):
            return component_identity(self, candidate, target, reviewed, deadline)

        def baseline_compatible(self, root, deadline):
            self.command(["/usr/bin/git", "-C", str(root), "diff", "--quiet", BASE, "--"], deadline)

        def helpers_reviewed(self, reviewed, deadline):
            root = Path(__file__).resolve().parents[1]
            for path, expected in helper_identity().items():
                relative = Path(path).relative_to(root).as_posix()
                _, raw = self.command(["/usr/bin/git", "-C", str(root), "show", reviewed + ":" + relative], deadline)
                if engine.sha(raw) != expected:
                    raise common.Refused("helper_not_exact_reviewed_source")

        def protected_snapshot(self, deadline):
            result = {}
            for label, home in PROTECTED.items():
                self.remaining(deadline)
                plist = PLIST_ROOT / (label + ".plist")
                raw = engine.safe_path(plist).read_bytes()
                _, output = self.command(["/bin/launchctl", "print", "gui/502/" + label], deadline)
                match = re.search(rb"^\s*pid = (\d+)\s*$", output, re.M)
                if not match:
                    raise common.Refused("protected_pid_missing")
                pid = int(match.group(1))
                _, cwd_output = self.command(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], deadline)
                paths = [line[1:] for line in cwd_output.decode().splitlines() if line.startswith("n")]
                document = plistlib.loads(raw)
                if len(paths) != 1 or str(Path(paths[0]).resolve()) != str(Path(document["WorkingDirectory"]).resolve()):
                    raise common.Refused("protected_cwd_mismatch")
                result[label] = {"pid": pid, "cwd": paths[0], "plist_sha256": engine.sha(raw),
                                 "config_sha256": engine.digest(home / "config.yaml"),
                                 "source_sha256": self.baseline_source(paths[0], deadline),
                                 "python_sha256": engine.digest(Path(document["ProgramArguments"][0]).resolve())}
            return result

        def check_protected(self, deadline):
            if self.protected is None or self.protected_snapshot(deadline) != self.protected:
                raise common.Refused("protected_sibling_changed")

        def stop(self, pid, deadline):
            self.check_protected(deadline)
            return super().stop(pid, deadline)

        def start(self, deadline):
            self.check_protected(deadline)
            return super().start(deadline)
    return ProfileHost()


def stage(profile, candidate, python, target, reviewed, *, engine=None, host=None):
    engine = engine or engine_for(profile)
    host = host or host_for(engine)
    deadline = host.clock() + 15
    protected = host.protected_snapshot(deadline)
    manifest = engine.stage(candidate, python, target, host)
    host.baseline_compatible(manifest["baseline"]["cwd"], deadline)
    host.helpers_reviewed(reviewed, deadline)
    manifest.update(profile=profile, component=host.component_identity(candidate, target, reviewed, deadline),
                    helper_sha256=helper_identity(), protected=protected,
                    health_contract="portless_launchd_pid_cwd_fresh_gateway_state_telegram")
    host.protected = protected
    host.check_protected(deadline)
    return manifest


def activate(profile, manifest_path, checksum, queue_id="auto", *, engine=None, host=None):
    engine = engine or engine_for(profile)
    host = host or host_for(engine)
    raw = engine.safe_path(manifest_path).read_bytes()
    if engine.sha(raw) != checksum:
        raise common.Refused("manifest_digest_mismatch")
    manifest = json.loads(raw)
    if manifest.get("profile") != profile:
        raise common.Refused("profile_manifest_mismatch")
    # Staged siblings are informational: unrelated approved work may finish
    # while this row awaits approval. The transaction's first validate runs
    # under both locks, inside the journaled preflight, before any mutation.
    host.protected = None
    captured = False
    base_validate = engine.validate

    def validate(current_manifest, current_host, deadline, *, activated=False):
        nonlocal captured
        if not captured:
            if activated:
                raise common.Refused("protected_sibling_baseline_missing")
            snapshot = current_host.protected_snapshot(deadline)
            if not isinstance(snapshot, dict) or set(snapshot) != set(PROTECTED):
                raise common.Refused("protected_sibling_baseline_missing")
            current_host.protected = snapshot
            captured = True
        # Never refresh this baseline at post-validation or during recovery.
        return base_validate(current_manifest, current_host, deadline, activated=activated)

    engine.validate = validate
    try:
        return engine.activate(manifest_path, checksum, queue_id, host)
    finally:
        engine.validate = base_validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "inspect", "activate"))
    parser.add_argument("--profile", required=True, choices=tuple(PROFILE_HOMES))
    parser.add_argument("--candidate")
    parser.add_argument("--python")
    parser.add_argument("--target")
    parser.add_argument("--reviewed-commit")
    parser.add_argument("--manifest")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--queue-id", default="auto")
    parser.add_argument("--label")
    args = parser.parse_args()
    try:
        engine = engine_for(args.profile)
        if args.label is not None and args.label != engine.LABEL:
            raise common.Refused("profile_label_mismatch")
        if args.action == "stage":
            result = stage(args.profile, args.candidate, args.python, args.target, args.reviewed_commit, engine=engine)
        elif args.action == "inspect":
            path = engine.safe_path(engine.STATE / "state.json")
            result = json.loads(path.read_bytes()) if path.exists() else {"status": "absent"}
        else:
            result = activate(args.profile, args.manifest, args.manifest_sha256, args.queue_id, engine=engine)
        print(json.dumps(result, sort_keys=True))
        return 0 if args.action != "activate" or result["status"] == "succeeded" else 1
    except Exception as exc:
        print(json.dumps({"status": "refused", "reason": common.safe_reason(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
