#!/usr/bin/env python3
"""Five-file CLI DGX release. Stage/verify are read-only; apply requires MC approval."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shlex
import stat
import sys
import tempfile
from uuid import UUID

if __package__:
    from scripts import dd_hermes_ordinary_release as common
    from scripts import dd_hermes_profile_release as component
else:
    import dd_hermes_ordinary_release as common
    import dd_hermes_profile_release as component

SERVICE = "hermes-cli-dgx"
TARGET = "ed06e69a39ebbc8c31d9f9243651a915416c1778"
ROOT = Path("/Users/openclaw/.hermes/hermes-agent")
ARTIFACT = Path("/Users/openclaw/apps/hermes-profile-runtime-dgx-20260908")
PYTHON = ROOT / "venv/bin/python"
CONSOLE = ROOT / "venv/bin/hermes"
PATH_CONSOLE = Path("/Users/openclaw/.local/bin/hermes")
STATE = Path("/Users/openclaw/.openclaw/hermes-cli-release")
PROFILES = ("document-review", "video-review", "security-review")
HOMES = {p: Path("/Users/openclaw/.hermes/profiles") / p for p in PROFILES}
SOURCES = (component.COMPONENT, component.DEFAULT_COMPONENT)
KIND = "hermes_cli_component_v1"


def paths():
    return [ROOT / name for name in SOURCES] + [HOMES[p] / "config.yaml" for p in PROFILES]


def environment_identity():
    return {str(p): common.optional_digest(p) for p in [ROOT / ".env", *[HOMES[n] / ".env" for n in PROFILES]]}


def file_identity(path):
    path = common.safe_path(path)
    if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
        raise common.Refused("file_not_regular_or_symlink")
    info = path.stat()
    if info.st_uid != os.getuid() or getattr(info, "st_flags", 0):
        raise common.Refused("unsupported_file_ownership_or_flags")
    return {"sha256": common.digest(path), "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid, "gid": info.st_gid, "mtime_ns": info.st_mtime_ns}


def helper_identity():
    root = Path(__file__).resolve().parents[1]
    names = ("scripts/dd_hermes_cli_release.py", "scripts/dd_hermes_profile_release.py",
             "scripts/dd_hermes_ordinary_release.py", "scripts/run_dd_cli_release_tests.py",
             "scripts/run_dd_profile_release_tests.py", "scripts/run_dd_release_tests.py",
             "tests/scripts/test_dd_hermes_cli_release.py")
    return {str(root / name): common.digest(root / name) for name in names}


# Resolve the actual console import path from its script directory, and the
# python -m path from cwd. Do not execute main(): it configures logs and may
# start sessions. These are fresh imports of the real transcription consumer.
VERIFY_IMPORTS = common.RESOLVE_PLATFORMS.split('phase = "imports"')[0] + r'''
import importlib.util
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        mode, root, console = sys.argv[1:]
        if mode != "module":
            sys.path[0] = str(pathlib.Path(console).parent)
        main_origin = importlib.util.find_spec("hermes_cli.main").origin
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv(hermes_home=os.environ["HERMES_HOME"], project_env=pathlib.Path(root) / ".env")
        from tools import transcription_tools as t
        from hermes_cli import config
        # load_config initializes directories/permissions/SOUL on every call.
        # Verify those prerequisites without changing them in this inspection
        # process; keep the real config parser/merge/env expansion untouched.
        def existing_home():
            home = config.get_hermes_home()
            if not all((home / p).is_dir() for p in (".", "cron", "sessions", "logs", "memories")):
                raise RuntimeError("inspection_home_not_initialized")
            if not (home / "SOUL.md").is_file():
                raise RuntimeError("inspection_soul_missing")
        existing_home()
        config.ensure_hermes_home = existing_home
        provider = t._load_stt_config().get("provider")
        import hashlib
        result = {"main": main_origin, "transcription": t.__file__, "config": config.__file__,
                  "transcription_sha256": hashlib.sha256(pathlib.Path(t.__file__).read_bytes()).hexdigest(),
                  "config_sha256": hashlib.sha256(pathlib.Path(config.__file__).read_bytes()).hexdigest(),
                  "provider": provider, "home": str(config.get_hermes_home())}
        if blocked: raise RuntimeError("blocked")
    print(json.dumps(result))
except Exception:
    print(json.dumps({"error": "consumer_inspection_refused"}))
    sys.exit(1)
'''


class Host(common.Host):
    def lineage(self, reviewed, deadline):
        value = component.component_identity(self, ARTIFACT, TARGET, reviewed, deadline)
        root = Path(__file__).resolve().parents[1]
        for path, checksum in helper_identity().items():
            relative = str(Path(path).relative_to(root))
            _, raw = self.command(["/usr/bin/git", "-C", str(root), "show", reviewed + ":" + relative], deadline)
            if common.sha(raw) != checksum:
                raise common.Refused("helper_not_reviewed")
        return value

    def unchanged_source(self, deadline):
        # Pin every other tracked byte, including history/session writers.
        _, head = self.command(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"], deadline)
        if head.decode().strip() != component.BASE:
            raise common.Refused("unexpected_cli_baseline_commit")
        _, delta = self.command(["/usr/bin/git", "-C", str(ROOT), "diff", "--binary", component.BASE,
                                 "--", ".", *[":(exclude)" + p for p in SOURCES]], deadline)
        if delta:
            raise common.Refused("other_cli_source_changed")
        return {"base_commit": component.BASE, "unchanged_paths_diff_sha256": common.sha(delta)}

    def gateways(self, deadline):
        labels = {"ai.hermes.gateway", "ai.hermes.gateway-classic"}
        labels.update("ai.hermes.gateway-" + p for p in component.PROFILE_HOMES)
        result = {}
        for label in sorted(labels):
            path = component.PLIST_ROOT / (label + ".plist")
            raw = common.safe_path(path).read_bytes()
            doc = plistlib.loads(raw)
            rc, data = self.command(["/bin/launchctl", "print", "gui/502/" + label], deadline, True)
            match = common.re.search(rb"^\s*pid = (\d+)\s*$", data, common.re.M)
            if rc or not match:
                raise common.Refused("protected_gateway_unobservable")
            pid = int(match.group(1))
            _, data = self.command(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], deadline)
            cwd = [line[1:] for line in data.decode().splitlines() if line.startswith("n")]
            if len(cwd) != 1 or Path(cwd[0]).resolve() != Path(doc["WorkingDirectory"]).resolve():
                raise common.Refused("protected_gateway_cwd_ambiguous")
            if Path(cwd[0]).resolve() == ROOT.resolve():
                raise common.Refused("gateway_still_uses_cli_source")
            result[label] = {"pid": pid, "cwd": cwd[0], "plist_sha256": common.sha(raw)}
        return result

    def consumers(self, deadline):
        environment = environment_identity()
        if PATH_CONSOLE.resolve() != CONSOLE.resolve():
            raise common.Refused("path_console_resolution_changed")
        result = {}
        for profile, home in HOMES.items():
            result[profile] = {}
            for mode in ("console", "path_console", "module"):
                env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
                env.update(HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
                # Both PATH and absolute console launchers use this same pinned
                # entrypoint. No runtime credentials or caller Python overrides.
                env["PATH"] = str(CONSOLE.parent) + os.pathsep + env.get("PATH", "")
                console = PATH_CONSOLE if mode == "path_console" else CONSOLE
                rc, raw = self.command([str(PYTHON), "-B", "-c", VERIFY_IMPORTS, mode, str(ROOT), str(console)],
                                       deadline, True, cwd=Path("/private/tmp"), env=env)
                if rc:
                    raise common.Refused("cli_consumer_inspection_failed")
                value = json.loads(raw)
                expected = {"main": str(ROOT / "hermes_cli/main.py"),
                            "transcription": str(ROOT / SOURCES[0]), "config": str(ROOT / SOURCES[1]),
                            "transcription_sha256": common.digest(ROOT / SOURCES[0]),
                            "config_sha256": common.digest(ROOT / SOURCES[1]),
                            "provider": "dgx", "home": str(home)}
                if value != expected:
                    raise common.Refused("cli_consumer_identity_or_provider_mismatch")
                result[profile][mode] = value
        if environment_identity() != environment:
            raise common.Refused("environment_changed_during_inspection")
        return result


def apply_command(path, checksum):
    return shlex.join([str(PYTHON), "-B", str(Path(__file__).resolve()), "apply", "--manifest", str(path),
                       "--manifest-sha256", checksum, "--queue-id", "auto"])


def approved(host, manifest, path, checksum, operation, deadline, *, verifying=False):
    command = apply_command(path, checksum)
    if operation == "auto":
        rows = host.http(common.QUEUE + "?status=deploying&service=" + SERVICE + "&limit=100", deadline).get("items", [])
        rows = [r for r in rows if r.get("service_name") == SERVICE and r.get("target_commit") == TARGET
                and r.get("restart_command") == command]
        if len(rows) != 1:
            raise common.Refused("unique_deploying_row_required")
        operation = rows[0]["id"]
    try:
        operation = str(UUID(operation))
    except (ValueError, TypeError):
        raise common.Refused("invalid_queue_id") from None
    row = host.http(common.QUEUE + "/" + operation, deadline)
    allowed = {"approved", "deploying", "proven"} if verifying else {"approved", "deploying"}
    if (row.get("id") != operation or row.get("service_name") != SERVICE or row.get("target_commit") != TARGET
            or row.get("restart_command") != command or row.get("status") not in allowed
            or not row.get("decided_by") or not row.get("decided_at")):
        raise common.Refused("matching_approved_queue_required")
    return operation


def replacements():
    # Validate every destination before reading any config, including apply's
    # fresh read after an approval wait. Never follow a replaced symlink.
    for path in paths():
        file_identity(path)
    result = {str(ROOT / name): common.safe_path(ARTIFACT / name).read_bytes() for name in SOURCES}
    result.update({str(HOMES[p] / "config.yaml"): common.dgx_config((HOMES[p] / "config.yaml").read_bytes())
                   for p in PROFILES})
    return result


def stage(reviewed, host=None):
    host = host or Host()
    deadline = host.clock() + 25
    lineage = host.lineage(reviewed, deadline)
    unchanged = host.unchanged_source(deadline)
    baseline = {str(p): file_identity(p) for p in paths()}
    # Both source inputs must be the exact historical base, not another patch.
    for name in SOURCES:
        _, raw = host.command(["/usr/bin/git", "-C", str(ARTIFACT), "show", component.BASE + ":" + name], deadline)
        if common.sha(raw) != baseline[str(ROOT / name)]["sha256"]:
            raise common.Refused("cli_component_baseline_changed")
    host.gateways(deadline)
    candidate = {path: common.sha(raw) for path, raw in replacements().items()}
    if any(file_identity(p) != baseline[str(p)] for p in paths()):
        raise common.Refused("staging_file_drift")
    return {"kind": KIND, "service": SERVICE, "target_commit": TARGET, "component": lineage,
            "reviewed_commit": reviewed, "helper_sha256": helper_identity(), "baseline": baseline,
            "candidate": candidate, "unchanged_source": unchanged,
            "python_sha256": common.digest(PYTHON.resolve()), "console_sha256": common.digest(CONSOLE),
            "apply_seconds": 25, "rollback_seconds": 10}


def read_manifest(path, checksum):
    raw = common.safe_path(path).read_bytes()
    if common.sha(raw) != checksum:
        raise common.Refused("manifest_digest_mismatch")
    m = json.loads(raw)
    expected = {str(p) for p in paths()}
    if (m.get("kind") != KIND or m.get("service") != SERVICE or m.get("target_commit") != TARGET
            or set(m.get("baseline", {})) != expected or set(m.get("candidate", {})) != expected
            or m.get("apply_seconds") != 25 or m.get("rollback_seconds") != 10):
        raise common.Refused("unsupported_manifest")
    return m


def validate(m, host, deadline):
    if m["helper_sha256"] != helper_identity() or host.lineage(m["reviewed_commit"], deadline) != m["component"]:
        raise common.Refused("release_lineage_changed")
    if host.unchanged_source(deadline) != m["unchanged_source"]:
        raise common.Refused("other_cli_source_changed")
    if any(m["candidate"][str(ROOT / name)] != common.digest(ARTIFACT / name) for name in SOURCES):
        raise common.Refused("manifest_component_digest_mismatch")
    if common.digest(PYTHON.resolve()) != m["python_sha256"] or common.digest(CONSOLE) != m["console_sha256"]:
        raise common.Refused("interpreter_or_console_changed")


def verify(path, checksum, operation, host=None, *, challenge="", attempt_id="", _deadline=None):
    host = host or Host()
    deadline = _deadline if _deadline is not None else host.clock() + 25
    m = read_manifest(path, checksum)
    operation = approved(host, m, path, checksum, operation, deadline, verifying=True)
    validate(m, host, deadline)
    for target in paths():
        identity = file_identity(target)
        if identity["sha256"] != m["candidate"][str(target)] or any(
                identity[k] != m["baseline"][str(target)][k] for k in ("mode", "uid", "gid")):
            raise common.Refused("installed_file_or_metadata_changed")
    consumers = host.consumers(deadline)
    if any(common.digest(p) != m["candidate"][str(p)] for p in paths()):
        raise common.Refused("verification_file_drift")
    return {"kind": KIND, "status": "verified", "service": SERVICE, "challenge": challenge,
            "attempt_id": attempt_id, "queue_id": operation, "target_commit": TARGET,
            "manifest_sha256": checksum, "source_files": {str(ROOT / p): m["candidate"][str(ROOT / p)] for p in SOURCES},
            "config_files": {str(HOMES[p] / "config.yaml"): m["candidate"][str(HOMES[p] / "config.yaml")] for p in PROFILES},
            "python_sha256": m["python_sha256"], "console_sha256": m["console_sha256"], "consumer": consumers}


@contextmanager
def locked():
    if STATE.is_symlink():
        raise common.Refused("state_symlink")
    STATE.mkdir(mode=0o700, exist_ok=True)
    if STATE.stat().st_mode & 0o077:
        raise common.Refused("state_not_private")
    fd = os.open(STATE / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise common.Refused("operation_in_progress") from None
        yield


def replace(path, raw, metadata, expected, *, restore=False):
    """Atomic per-file CAS, preserving owner/group/mode; rollback restores mtime."""
    path = common.safe_path(path)
    if file_identity(path) != expected:
        raise common.Refused("file_changed_before_replace")
    fd, name = tempfile.mkstemp(prefix=".dd-cli-release-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fchown(stream.fileno(), metadata["uid"], metadata["gid"])
            os.fchmod(stream.fileno(), metadata["mode"])
            os.fsync(stream.fileno())
        if restore:
            os.utime(name, ns=(metadata["mtime_ns"], metadata["mtime_ns"]))
        if file_identity(path) != expected:
            raise common.Refused("file_changed_before_replace")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply(path, checksum, operation="auto", host=None):
    host = host or Host()
    deadline = host.clock() + 25
    recovery_deadline = deadline + 10
    m = read_manifest(path, checksum)
    operation = approved(host, m, path, checksum, operation, deadline)
    with locked():
        journal = STATE / "state.json"
        old = json.loads(journal.read_bytes()) if journal.exists() else {}
        if operation in old.get("used", []):
            raise common.Refused("operation_already_consumed")
        if old and old.get("status") not in {"succeeded", "rolled_back", "preflight_failed"}:
            raise common.Refused("prior_operation_requires_recovery")
        state = {"operation": operation, "manifest_sha256": checksum, "used": old.get("used", []) + [operation],
                 "status": "prepared", "written": [], "rollback_count": 0}
        def record(status):
            state["status"] = status
            common.write_private(journal, common.canonical(state))
        record("prepared")
        originals, installed = {}, {}
        try:
            validate(m, host, deadline)
            protected = host.gateways(deadline)
            state["protected_gateways"] = protected
            candidate = replacements()
            for target in paths():
                key = str(target)
                if file_identity(target) != m["baseline"][key] or common.sha(candidate[key]) != m["candidate"][key]:
                    raise common.Refused("preflight_file_drift")
                originals[key] = target.read_bytes()
            approved(host, m, path, checksum, operation, deadline)
            backup = STATE / operation
            backup.mkdir(mode=0o700)
            for index, (key, raw) in enumerate(originals.items()):
                common.write_private(backup / str(index), raw)
            record("applying")
            for target in paths():
                host.remaining(deadline)
                key = str(target)
                # Journal intent before rename: death/exception after replace
                # must never be reported as an untouched preflight failure.
                installed[key] = None
                state["written"].append(key)
                record("applying")
                replace(target, candidate[key], m["baseline"][key], m["baseline"][key])
                installed[key] = file_identity(target)
                record("applying")
            result = verify(path, checksum, operation, host, _deadline=deadline)
            if host.gateways(deadline) != protected:
                raise common.Refused("protected_gateway_changed")
            state["verification"] = result
            record("succeeded")
        except Exception as exc:
            state["reason"] = common.safe_reason(exc)
            if not installed:
                record("preflight_failed")
            else:
                state["rollback_count"] = 1
                record("rolling_back")
                try:
                    for key in reversed(installed):
                        host.remaining(recovery_deadline)
                        current = file_identity(key)
                        if current == m["baseline"][key]:
                            continue
                        expected = installed[key]
                        if expected is None:
                            if current["sha256"] != m["candidate"][key] or any(
                                    current[k] != m["baseline"][key][k] for k in ("mode", "uid", "gid")):
                                raise common.Refused("uncertain_replace_contains_foreign_file")
                            expected = current
                        replace(Path(key), originals[key], m["baseline"][key], expected, restore=True)
                    if any(file_identity(p) != m["baseline"][str(p)] for p in paths()):
                        raise common.Refused("rollback_not_exact")
                    if host.gateways(recovery_deadline) != protected:
                        raise common.Refused("protected_gateway_changed")
                    record("rolled_back")
                except Exception as recovery:
                    state["recovery_reason"] = common.safe_reason(recovery)
                    record("failed_recovery")
        return state


def main():
    def forbid_auth(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            common.safe_path(Path(os.fsdecode(args[0])).absolute())
    sys.addaudithook(forbid_auth)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "apply", "verify"))
    parser.add_argument("--reviewed-commit")
    parser.add_argument("--manifest")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--queue-id", default="auto")
    parser.add_argument("--challenge", default="")
    parser.add_argument("--attempt-id", default="")
    args = parser.parse_args()
    try:
        if args.action == "stage":
            result = stage(args.reviewed_commit)
        elif args.action == "apply":
            result = apply(args.manifest, args.manifest_sha256, args.queue_id)
        else:
            result = verify(args.manifest, args.manifest_sha256, args.queue_id,
                            challenge=args.challenge, attempt_id=args.attempt_id)
        print(json.dumps(result, sort_keys=True))
        return 0 if args.action == "stage" or result["status"] in {"succeeded", "verified"} else 1
    except Exception as exc:
        print(json.dumps({"status": "refused", "reason": common.safe_reason(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
