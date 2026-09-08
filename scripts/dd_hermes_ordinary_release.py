#!/usr/bin/env python3
"""Queue-controlled ordinary Hermes maintenance cutover; never manages Classic.

stage and inspect are read-only. activate requires an approved matching MC row.
Recovery is in-process, bounded, once-only; a crashed helper requires an operator.
"""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from uuid import UUID

import yaml


LABEL = "gui/502/ai.hermes.gateway"
SERVICE = "hermes-agent"
APPS = Path("/Users/openclaw/apps")
PLIST = Path("/Users/openclaw/Library/LaunchAgents/ai.hermes.gateway.plist")
STATE = Path("/Users/openclaw/.openclaw/hermes-ordinary-release")
ORDINARY_HOME = Path("/Users/openclaw/.hermes")
HEALTH = "http://127.0.0.1:8642/health/detailed"
QUEUE = "http://127.0.0.1:8502/api/deploy-queue"
MAINTENANCE = "Brief maintenance interruption; a turn may arrive after the idle check."


class Refused(RuntimeError):
    """Public, non-secret refusal code only."""


def safe_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise Refused("unsafe_path")
    if "auth.json" in path.parts or "auth.json" in path.resolve().parts:
        raise Refused("credential_store_forbidden")
    return path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(path):
    return sha(safe_path(path).read_bytes())


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


# Runs before gateway imports. A blocked operation remains a failure even when
# optional plugin discovery or dotenv sanitation catches the exception internally.
RESOLVE_PLATFORMS = r'''
import contextlib, importlib.util, io, json, os, pathlib, socket, stat, sys
blocked = []
def guard(event, args):
    forbidden = event in {"os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod",
                          "os.chown", "os.truncate", "os.link", "os.symlink", "os.utime",
                          "os.system", "subprocess.Popen", "os.posix_spawn", "os.exec", "os.fork"}
    # asyncio imports may construct sockets; construction alone communicates
    # nothing. Resolution, connect, bind, send and every other socket event refuse.
    forbidden = forbidden or (event.startswith("socket.") and event != "socket.__new__")
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes)):
            path = pathlib.Path(os.fsdecode(path))
            forbidden = forbidden or "auth.json" in path.parts or "auth.json" in path.resolve().parts
        forbidden = forbidden or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
    if forbidden:
        blocked.append(event)
        raise PermissionError("release_resolver_read_only")
sys.addaudithook(guard)
# Canonical discovery uses mkdir(exist_ok=True) for existing directories.
# Reproduce the existing-directory result without making a mutating syscall;
# missing directories still reach the audit refusal and poison the result.
original_mkdir = os.mkdir
def read_only_mkdir(path, mode=0o777, *, dir_fd=None):
    if dir_fd is None and pathlib.Path(path).is_dir():
        raise FileExistsError("release_resolver_existing_directory")
    return original_mkdir(path, mode, dir_fd=dir_fd)
os.mkdir = read_only_mkdir
original_chmod = os.chmod
def read_only_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
    if dir_fd is None and isinstance(path, (str, bytes, os.PathLike)):
        try:
            current = os.stat(path, follow_symlinks=follow_symlinks)
            if stat.S_IMODE(current.st_mode) == mode:
                return
        except OSError:
            pass
    return original_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
os.chmod = read_only_chmod
# urllib3 otherwise binds ::1:0 during import to detect IPv6 support. This
# inspection-only capability hint skips that probe; actual bind stays forbidden.
# It does not change service environment, platform enablement or runtime sockets.
socket.has_ipv6 = False
phase = "imports"
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from hermes_cli.env_loader import load_hermes_dotenv
        phase = "dotenv"
        load_hermes_dotenv(hermes_home=os.environ["HERMES_HOME"], project_env=pathlib.Path.cwd() / ".env")
        phase = "config"
        from gateway.config import load_gateway_config
        cfg = load_gateway_config()
        names = sorted(p.value for p, c in cfg.platforms.items() if c.enabled)
        phase = "plugins"
        # Older baseline sources predate this optional registry. Disabled
        # discovered entries do not participate in adapter startup.
        if importlib.util.find_spec("gateway.platform_registry") is not None:
            from gateway.platform_registry import platform_registry
            if any(entry.name in names for entry in platform_registry.plugin_entries()):
                raise RuntimeError("enabled_plugin_unsupported")
    if blocked:
        raise PermissionError("blocked_operation")
except Exception:
    print(json.dumps({"error": "resolver_" + phase + ("_blocked_" + blocked[0].replace(".", "_").lower() if blocked else "_failed")}))
    sys.exit(1)
print(json.dumps(names))
'''


def optional_digest(path):
    path = safe_path(path)
    return digest(path) if path.exists() else None


def safe_reason(exc):
    value = str(exc) if isinstance(exc, Refused) else "unexpected_exception"
    return value if re.fullmatch(r"[a-z0-9_]{1,160}", value) else "redacted_exception"


class Host:
    """Actual bounded OS/HTTP adapter. Tests replace this entire boundary."""

    def clock(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def remaining(self, deadline, cap=2):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise Refused("deadline_expired")
        return min(cap, remaining)

    def command(self, args, deadline, allow_failure=False, **kwargs):
        try:
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=self.remaining(deadline), check=False, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refused("os_command_failed_or_timed_out") from exc
        if result.returncode and not allow_failure:
            raise Refused("os_command_failed")
        return result.returncode, result.stdout

    def http(self, url, deadline):
        try:
            with urlopen(url, timeout=self.remaining(deadline)) as response:
                # Bound diagnostic payloads and never emit raw queue/config bodies.
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise Refused("http_response_too_large")
                return json.loads(body)
        except Exception as exc:
            raise Refused("http_probe_failed") from exc

    def identity(self, deadline):
        rc, output = self.command(["/bin/launchctl", "print", LABEL], deadline, True)
        if rc:
            raise Refused("ordinary_job_not_observable")
        match = re.search(rb"^\s*pid = (\d+)\s*$", output, re.M)
        if not match:
            raise Refused("ordinary_pid_missing")
        pid = int(match.group(1))
        _, output = self.command(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], deadline)
        paths = [line[1:] for line in output.decode().splitlines() if line.startswith("n")]
        if len(paths) != 1:
            raise Refused("ordinary_cwd_missing")
        return {"pid": pid, "cwd": str(Path(paths[0]).resolve())}

    def pid_alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def source(self, root, deadline):
        root = safe_path(root).resolve()
        git = ["/usr/bin/git", "-C", str(root)]
        _, head = self.command(git + ["rev-parse", "HEAD"], deadline)
        _, tree = self.command(git + ["rev-parse", "HEAD^{tree}"], deadline)
        rc, _ = self.command(git + ["diff", "--quiet", "HEAD", "--"], deadline, True)
        if rc:
            raise Refused("tracked_source_dirty")
        _, untracked = self.command(git + ["ls-files", "--others", "--exclude-standard", "-z"], deadline)
        if untracked:
            raise Refused("untracked_candidate_files")
        return {"commit": head.decode().strip(), "tree": tree.decode().strip()}

    def baseline_source(self, root, deadline):
        git = ["/usr/bin/git", "-C", str(safe_path(root))]
        _, tree = self.command(git + ["rev-parse", "HEAD^{tree}"], deadline)
        _, delta = self.command(git + ["diff", "--binary", "HEAD", "--"], deadline)
        return sha(tree + delta)

    def python_origin(self, root, python, deadline):
        code = ("import importlib.util,json;print(json.dumps({n:importlib.util.find_spec(n).origin "
                "for n in ('run_agent','hermes_cli')}))")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        _, output = self.command([str(python), "-c", code], deadline, cwd=root, env=env)
        origins = json.loads(output)
        if (Path(origins["run_agent"]).resolve() != root / "run_agent.py"
                or Path(origins["hermes_cli"]).resolve() != root / "hermes_cli/__init__.py"):
            raise Refused("candidate_import_origin_mismatch")

    def enabled_platforms(self, root, python, plist, deadline):
        env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG")
               if k in os.environ}
        env.update(plist.get("EnvironmentVariables", {}))
        env.update(HERMES_HOME=str(ORDINARY_HOME), PYTHONDONTWRITEBYTECODE="1")
        # The resolver receives the service environment, never the agent's keys.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        rc, output = self.command([str(python), "-B", "-c", RESOLVE_PLATFORMS], deadline,
                                  True, cwd=root, env=env)
        if rc:
            try:
                error = json.loads(output).get("error", "")
            except (ValueError, AttributeError):
                error = ""
            if isinstance(error, str) and re.fullmatch(r"resolver_[a-z_]{1,100}", error):
                raise Refused(error)
            raise Refused("enabled_platform_resolver_failed")
        try:
            names = json.loads(output)
            if (not isinstance(names, list) or len(names) != len(set(names))
                    or not all(isinstance(n, str) and re.fullmatch(r"[a-z0-9_]+", n) for n in names)):
                raise ValueError()
        except (TypeError, ValueError):
            raise Refused("enabled_platform_resolver_invalid") from None
        return sorted(names)

    def stop(self, pid, deadline):
        # Exactly one stop request. Never a repeated kill loop or --replace takeover.
        self.command(["/bin/launchctl", "bootout", LABEL], deadline)
        while self.pid_alive(pid):
            self.sleep(min(.1, self.remaining(deadline)))

    def start(self, deadline):
        self.command(["/bin/launchctl", "bootstrap", "gui/502", str(PLIST)], deadline)


def read_plist():
    if PLIST.is_symlink():
        raise Refused("plist_symlink")
    raw = safe_path(PLIST).read_bytes()
    document = plistlib.loads(raw)
    if document.get("Label") != "ai.hermes.gateway":
        raise Refused("wrong_plist_label")
    if not document.get("ProgramArguments") or document.get("Program"):
        raise Refused("missing_program")
    return raw, document


def candidate_plist(baseline, candidate, python):
    document = dict(baseline)
    args = list(document["ProgramArguments"])
    # Restrict the allowed launcher shape; no custom shell or sibling profile.
    if args[1:] != ["-m", "hermes_cli.main", "--profile", "default", "gateway", "run", "--replace"]:
        raise Refused("unsupported_ordinary_launcher")
    # The old PID must have exited before bootstrap, so takeover is unnecessary.
    args[0] = str(python)
    args.remove("--replace")
    document["ProgramArguments"] = args
    document["WorkingDirectory"] = str(candidate)
    return plistlib.dumps(document, sort_keys=True)


def dgx_config(raw):
    """Replace only the existing ordinary stt.provider scalar, preserving comments."""
    text = raw.decode("utf-8")
    before = yaml.safe_load(text)
    if not isinstance(before, dict) or not isinstance(before.get("stt"), dict):
        raise Refused("ordinary_stt_config_missing")
    if before["stt"].get("provider") not in {"local", "dgx"}:
        raise Refused("unexpected_ordinary_stt_provider")
    node = yaml.compose(text)
    sections = [value for key, value in node.value if key.value == "stt"]
    if len(sections) != 1 or not isinstance(sections[0], yaml.MappingNode):
        raise Refused("ambiguous_stt_section")
    values = [value for key, value in sections[0].value if key.value == "provider"]
    if len(values) != 1 or not isinstance(values[0], yaml.ScalarNode):
        raise Refused("ambiguous_stt_provider")
    value = values[0]
    after = text[:value.start_mark.index] + "dgx" + text[value.end_mark.index:]
    expected = copy.deepcopy(before)
    expected["stt"] = {**expected["stt"], "provider": "dgx"}
    if yaml.safe_load(after) != expected:
        raise Refused("stt_edit_changes_other_configuration")
    return after.encode("utf-8")


def health(host, identity, deadline, *, idle=False, since=None, platforms=()):
    value = host.http(HEALTH, deadline)
    if (value.get("status") != "ok" or value.get("gateway_state") != "running"
            or value.get("pid") != identity["pid"]):
        raise Refused("health_identity_mismatch")
    if idle and value.get("active_agents") != 0:
        raise Refused("ordinary_not_idle")
    try:
        if since:
            updated = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00")).timestamp()
            if updated > time.time() + 2 or updated < since - 1:
                raise ValueError()
        for platform in platforms:
            item = value["platforms"][platform]
            if item["state"] != "connected":
                raise Refused("transport_not_connected_" + platform)
            if since:
                connected = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp()
                if connected < since - 1 or connected > time.time() + 2:
                    raise Refused("transport_not_fresh_" + platform)
    except (KeyError, ValueError, TypeError):
        raise Refused("health_or_transport_not_fresh") from None
    return value


def connected_platforms(value, enabled):
    connected = sorted(name for name, entry in value.get("platforms", {}).items()
                       if entry.get("state") == "connected")
    required = sorted(set(connected).intersection(enabled))
    if not {"telegram", "api_server"}.issubset(required):
        raise Refused("ordinary_required_transports_unavailable")
    return required, sorted(set(connected).difference(enabled))


def resolver_inputs(baseline, candidate):
    return sorted({str(ORDINARY_HOME / name) for name in ("config.yaml", ".env", "gateway.json")}
                  | {str(safe_path(root) / ".env") for root in (baseline, candidate)})


def stage(candidate, python, target, host=None):
    """Read-only manifest generation; caller saves its non-secret output."""
    host = host or Host()
    deadline = host.clock() + 15
    candidate = safe_path(candidate).resolve()
    python = safe_path(python)
    if not candidate.is_relative_to(APPS.resolve()) or candidate == APPS.resolve():
        raise Refused("candidate_outside_apps")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise Refused("interpreter_unavailable")
    if not re.fullmatch(r"[a-f0-9]{40}", target):
        raise Refused("exact_target_required")
    source = host.source(candidate, deadline)
    if source["commit"] != target:
        raise Refused("candidate_target_mismatch")
    host.python_origin(candidate, python, deadline)
    raw, plist = read_plist()
    baseline = host.identity(deadline)
    if baseline["cwd"] != str(Path(plist["WorkingDirectory"]).resolve()):
        raise Refused("loaded_plist_cwd_drift")
    if baseline["cwd"] == str(candidate):
        raise Refused("candidate_already_live")
    baseline_health = health(host, baseline, deadline, idle=True)
    new_plist = candidate_plist(plist, candidate, python)
    # Pin the original config/environment; activation changes only stt.provider.
    home = Path(plist.get("EnvironmentVariables", {}).get("HERMES_HOME", str(ORDINARY_HOME)))
    if home.resolve() != ORDINARY_HOME.resolve():
        raise Refused("ordinary_home_mismatch")
    config = {p: optional_digest(p) for p in resolver_inputs(baseline["cwd"], candidate)}
    enabled = host.enabled_platforms(baseline["cwd"], plist["ProgramArguments"][0], plist, deadline)
    if host.enabled_platforms(candidate, python, plist, deadline) != enabled:
        raise Refused("candidate_enabled_platforms_changed")
    platforms, ignored = connected_platforms(baseline_health, enabled)
    if host.identity(deadline) != baseline:
        raise Refused("baseline_process_changed")
    if any(optional_digest(p) != expected for p, expected in config.items()):
        raise Refused("resolver_inputs_changed")
    new_config = dgx_config((home / "config.yaml").read_bytes())
    return {"schema": 1, "service": SERVICE, "label": LABEL, "target_commit": target,
            "candidate": str(candidate), "python": str(python), "source": source,
            "python_sha256": digest(python.resolve()), "baseline": baseline,
            "baseline_source_sha256": host.baseline_source(baseline["cwd"], deadline),
            "baseline_python_sha256": digest(Path(plist["ProgramArguments"][0]).resolve()),
            "baseline_plist_sha256": sha(raw), "candidate_plist_sha256": sha(new_plist),
            "config_sha256": config, "maintenance": MAINTENANCE,
            "connected_platforms": platforms,
            "enabled_platforms": enabled, "ignored_disabled_platforms": ignored,
            "candidate_config_sha256": sha(new_config),
            "activation_seconds": 15, "rollback_seconds": 10,
            "crash_recovery": "operator-required; no external-supervisor guarantee"}


def validate(manifest, host, deadline, *, activated=False):
    if (manifest.get("schema") != 1 or manifest.get("service") != SERVICE
            or manifest.get("label") != LABEL or manifest.get("maintenance") != MAINTENANCE
            or manifest.get("activation_seconds") != 15 or manifest.get("rollback_seconds") != 10):
        raise Refused("unsupported_manifest")
    root = safe_path(manifest["candidate"]).resolve()
    python = safe_path(manifest["python"])
    if not root.is_relative_to(APPS.resolve()) or root == APPS.resolve():
        raise Refused("candidate_path_invalid")
    if not re.fullmatch(r"[a-f0-9]{40}", manifest["target_commit"]):
        raise Refused("exact_target_required")
    source = host.source(root, deadline)
    if source != manifest["source"] or source["commit"] != manifest["target_commit"]:
        raise Refused("candidate_source_changed")
    if digest(python.resolve()) != manifest["python_sha256"]:
        raise Refused("interpreter_changed")
    host.python_origin(root, python, deadline)
    for path, expected in manifest["config_sha256"].items():
        if activated and path == str(ORDINARY_HOME / "config.yaml"):
            expected = manifest["candidate_config_sha256"]
        if optional_digest(path) != expected:
            raise Refused("configuration_changed")


def restart_command(manifest_path, manifest_digest):
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "activate", "--manifest",
                       str(safe_path(manifest_path)), "--manifest-sha256", manifest_digest,
                       "--queue-id", "auto", "--label", LABEL])


def approved_row(host, queue_id, manifest, command, deadline):
    if queue_id == "auto":
        rows = host.http(QUEUE + "?status=deploying&service=hermes-agent&limit=100", deadline).get("items", [])
        rows = [row for row in rows if row.get("target_commit") == manifest["target_commit"]
                and row.get("restart_command") == command]
        if len(rows) != 1:
            raise Refused("unique_deploying_row_required")
        queue_id = rows[0]["id"]
    try:
        queue_id = str(UUID(queue_id))
    except (ValueError, TypeError):
        raise Refused("invalid_queue_id") from None
    row = host.http(QUEUE + "/" + queue_id, deadline)
    if (row.get("id") != queue_id or row.get("service_name") != SERVICE
            or row.get("target_commit") != manifest["target_commit"]
            or row.get("status") not in {"approved", "deploying"}
            or not row.get("decided_by") or not row.get("decided_at")
            or row.get("restart_command") != command):
        raise Refused("matching_approved_queue_row_required")
    return queue_id


def write_private(path, data):
    path = safe_path(path)
    if path.is_symlink():
        raise Refused("output_symlink")
    fd, temp = tempfile.mkstemp(prefix=".ordinary-release-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def locked_state():
    if STATE.is_symlink():
        raise Refused("state_symlink")
    STATE.mkdir(mode=0o700, parents=False, exist_ok=True)
    if STATE.stat().st_mode & 0o077:
        raise Refused("state_permissions_not_private")
    fd = os.open(STATE / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("operation_in_progress") from None
        yield


def verify_runtime(host, root, old_pid, deadline, since, platforms):
    stable_since = None
    last_reason = "runtime_not_observed"
    while True:
        try:
            host.remaining(deadline)
            identity = host.identity(deadline)
            if identity["pid"] == old_pid or identity["cwd"] != root:
                raise Refused("runtime_identity_wrong")
            health(host, identity, deadline, since=since, platforms=platforms)
            if stable_since is None:
                stable_since = host.clock()
            if host.clock() - stable_since >= 1:
                return identity
        except Refused as exc:
            if host.clock() >= deadline:
                raise Refused("verification_timeout_" + last_reason) from None
            last_reason = safe_reason(exc)
            stable_since = None
        host.sleep(min(.2, max(0, deadline - host.clock())))


def activate(manifest_path, manifest_digest, queue_id, host=None):
    host = host or Host()
    started = host.clock()
    deadline, recovery_deadline = started + 15, started + 25
    raw_manifest = safe_path(manifest_path).read_bytes()
    if sha(raw_manifest) != manifest_digest:
        raise Refused("manifest_digest_mismatch")
    manifest = json.loads(raw_manifest)
    command = restart_command(manifest_path, manifest_digest)
    operation = approved_row(host, queue_id, manifest, command, deadline)
    with locked_state():
        state_path = STATE / "state.json"
        old = json.loads(state_path.read_text()) if state_path.exists() else {}
        if operation in old.get("used", []):
            raise Refused("operation_already_consumed")
        if old and old.get("phase") not in {"succeeded", "rolled_back", "preflight_failed", "operator_recovered"}:
            raise Refused("prior_operation_requires_operator_recovery")
        state = {"operation": operation, "manifest_sha256": manifest_digest,
                 "used": old.get("used", []) + [operation], "events": [], "rollback_count": 0}

        def record(phase):
            state["phase"] = phase
            state["events"].append({"phase": phase, "at": datetime.now(timezone.utc).isoformat()})
            write_private(state_path, canonical(state))

        record("prepared")
        mutated = False
        baseline_raw = None
        baseline_config = None
        baseline_pid = manifest["baseline"]["pid"]
        try:
            validate(manifest, host, deadline)
            baseline_raw, plist = read_plist()
            if sha(baseline_raw) != manifest["baseline_plist_sha256"]:
                raise Refused("baseline_plist_changed")
            if (host.baseline_source(manifest["baseline"]["cwd"], deadline) != manifest["baseline_source_sha256"]
                    or digest(Path(plist["ProgramArguments"][0]).resolve()) != manifest["baseline_python_sha256"]):
                raise Refused("baseline_runtime_changed")
            if host.identity(deadline) != manifest["baseline"]:
                raise Refused("baseline_process_changed")
            enabled = host.enabled_platforms(manifest["baseline"]["cwd"], plist["ProgramArguments"][0], plist, deadline)
            if (enabled != manifest.get("enabled_platforms")
                    or host.enabled_platforms(manifest["candidate"], manifest["python"], plist, deadline) != enabled):
                raise Refused("enabled_platforms_changed")
            new_plist = candidate_plist(plist, manifest["candidate"], manifest["python"])
            if sha(new_plist) != manifest["candidate_plist_sha256"]:
                raise Refused("candidate_plist_changed")
            health(host, manifest["baseline"], deadline, idle=True, platforms=manifest["connected_platforms"])
            approved_row(host, operation, manifest, command, deadline)
            config_path = safe_path(ORDINARY_HOME / "config.yaml")
            baseline_config = config_path.read_bytes()
            new_config = dgx_config(baseline_config)
            if (sha(baseline_config) != manifest["config_sha256"].get(str(config_path))
                    or sha(new_config) != manifest["candidate_config_sha256"]):
                raise Refused("ordinary_config_changed")
            backup = STATE / (operation + ".baseline.plist")
            if backup.exists():
                raise Refused("baseline_backup_already_exists")
            write_private(backup, baseline_raw)
            write_private(STATE / (operation + ".baseline-config.yaml"), baseline_config)
            record("stop_intent")
            mutated = True
            host.stop(baseline_pid, deadline)
            record("replace_intent")
            if digest(PLIST) != manifest["baseline_plist_sha256"]:
                raise Refused("ordinary_plist_changed_during_stop")
            if digest(config_path) != sha(baseline_config):
                raise Refused("ordinary_config_changed_during_stop")
            write_private(config_path, new_config)
            write_private(PLIST, new_plist)
            issued = time.time()
            record("start_intent")
            host.start(deadline)
            identity = verify_runtime(host, manifest["candidate"], baseline_pid, deadline, issued,
                                      manifest["connected_platforms"])
            validate(manifest, host, deadline, activated=True)
            state["running_identity"] = identity
            record("succeeded")
        except Exception as exc:
            state["failure_reason"] = safe_reason(exc)
            if not mutated:
                record("preflight_failed")
            else:
                state["rollback_count"] = 1
                record("rollback_intent")
                try:
                    # Preserve exact approved baseline, never reconstruct from current config.
                    if baseline_raw is None or sha(baseline_raw) != manifest["baseline_plist_sha256"]:
                        raise Refused("rollback_baseline_unavailable")
                    if host.baseline_source(manifest["baseline"]["cwd"], recovery_deadline) != manifest["baseline_source_sha256"]:
                        raise Refused("rollback_source_changed")
                    if digest(Path(plist["ProgramArguments"][0]).resolve()) != manifest["baseline_python_sha256"]:
                        raise Refused("rollback_interpreter_changed")
                    for path, expected in manifest["config_sha256"].items():
                        if path != str(config_path) and optional_digest(path) != expected:
                            raise Refused("rollback_environment_changed")
                    if (baseline_config is None or digest(config_path) not in
                            {sha(baseline_config), manifest["candidate_config_sha256"]}):
                        raise Refused("rollback_configuration_changed")
                    if digest(PLIST) not in {manifest["baseline_plist_sha256"], manifest["candidate_plist_sha256"]}:
                        raise Refused("rollback_plist_changed")
                    try:
                        current = host.identity(recovery_deadline)
                    except Refused:
                        current = None
                    if current:
                        if current["cwd"] not in {manifest["baseline"]["cwd"], manifest["candidate"]}:
                            raise Refused("foreign_runtime_refuses_rollback")
                        host.stop(current["pid"], recovery_deadline)
                    elif host.pid_alive(baseline_pid):
                        raise Refused("baseline_still_stopping")
                    # Stop may block; recheck ownership immediately before writes.
                    if digest(config_path) not in {sha(baseline_config), manifest["candidate_config_sha256"]}:
                        raise Refused("rollback_configuration_changed_during_stop")
                    if digest(PLIST) not in {manifest["baseline_plist_sha256"], manifest["candidate_plist_sha256"]}:
                        raise Refused("rollback_plist_changed_during_stop")
                    write_private(config_path, baseline_config)
                    write_private(PLIST, baseline_raw)
                    issued = time.time()
                    host.start(recovery_deadline)
                    state["restored_identity"] = verify_runtime(
                        host, manifest["baseline"]["cwd"], baseline_pid, recovery_deadline, issued,
                        manifest["connected_platforms"])
                    record("rolled_back")
                except Exception as exc:
                    state["recovery_failure_reason"] = safe_reason(exc)
                    record("failed_recovery")
        return {"operation": operation, "status": state["phase"], "rollback_count": state["rollback_count"],
                **{key: state[key] for key in ("failure_reason", "recovery_failure_reason") if key in state}}


def acknowledge_recovery(manifest_path, manifest_digest, expected_operation, expected_state_digest, host=None):
    """Verify a restored baseline and release its fence; never restart or alter MC."""
    host = host or Host()
    deadline = host.clock() + 15
    raw_manifest = safe_path(manifest_path).read_bytes()
    if sha(raw_manifest) != manifest_digest:
        raise Refused("manifest_digest_mismatch")
    manifest = json.loads(raw_manifest)
    operation = str(UUID(expected_operation))
    if not re.fullmatch(r"[a-f0-9]{64}", expected_state_digest or ""):
        raise Refused("exact_prior_state_digest_required")
    with locked_state():
        state_path = safe_path(STATE / "state.json")
        if state_path.is_symlink():
            raise Refused("state_symlink")
        prior_raw = state_path.read_bytes()
        prior = json.loads(prior_raw)
        if (sha(prior_raw) != expected_state_digest or prior.get("operation") != operation
                or prior.get("manifest_sha256") != manifest_digest
                or prior.get("phase") != "failed_recovery" or operation not in prior.get("used", [])):
            raise Refused("matching_failed_state_required")
        row = host.http(QUEUE + "/" + operation, deadline)
        command = shlex.split(row.get("restart_command", ""))
        binding = [i for i, item in enumerate(command) if item == "--manifest-sha256"]
        if (row.get("id") != operation or row.get("service_name") != SERVICE
                or row.get("target_commit") != manifest.get("target_commit")
                or row.get("status") != "failed" or len(binding) != 1
                or command[binding[0] + 1:binding[0] + 2] != [manifest_digest]):
            raise Refused("matching_terminal_failed_queue_row_required")
        # The old candidate must remain immutable until this acknowledgment.
        validate(manifest, host, deadline)
        raw, plist = read_plist()
        baseline_root = manifest["baseline"]["cwd"]
        if (sha(raw) != manifest["baseline_plist_sha256"]
                or str(Path(plist["WorkingDirectory"]).resolve()) != baseline_root
                or host.baseline_source(baseline_root, deadline) != manifest["baseline_source_sha256"]
                or digest(Path(plist["ProgramArguments"][0]).resolve()) != manifest["baseline_python_sha256"]):
            raise Refused("original_baseline_not_restored")
        # Old manifests predate optional-input pinning. Capture those inputs for
        # this check and receipt without pretending they were in the old proof.
        inputs = {p: optional_digest(p) for p in resolver_inputs(baseline_root, manifest["candidate"])}
        identity = host.identity(deadline)
        if identity["cwd"] != baseline_root or identity["cwd"] == manifest["candidate"]:
            raise Refused("baseline_process_not_restored")
        enabled = host.enabled_platforms(baseline_root, plist["ProgramArguments"][0], plist, deadline)
        platforms, ignored = connected_platforms(health(host, identity, deadline), enabled)
        platforms = sorted(set(platforms) | set(manifest["connected_platforms"]).intersection(enabled))
        health(host, identity, deadline, platforms=platforms)
        host.sleep(min(1, host.remaining(deadline)))
        health(host, identity, deadline, platforms=platforms)
        if (host.identity(deadline) != identity or digest(PLIST) != manifest["baseline_plist_sha256"]
                or host.baseline_source(baseline_root, deadline) != manifest["baseline_source_sha256"]
                or digest(Path(plist["ProgramArguments"][0]).resolve()) != manifest["baseline_python_sha256"]
                or any(optional_digest(p) != expected for p, expected in inputs.items())
                or sha(state_path.read_bytes()) != expected_state_digest):
            raise Refused("baseline_changed_during_acknowledgment")
        archive = safe_path(STATE / (operation + "." + expected_state_digest + ".failed-state.json"))
        if archive.is_symlink():
            raise Refused("prior_state_archive_symlink")
        if archive.exists() and archive.read_bytes() != prior_raw:
            raise Refused("prior_state_archive_conflict")
        if not archive.exists():
            write_private(archive, prior_raw)
        receipt = {"phase": "operator_recovered", "at": datetime.now(timezone.utc).isoformat(),
                   "prior_state_sha256": expected_state_digest, "failed_queue_id": operation,
                   "running_identity": identity, "enabled_platforms": enabled,
                   "connected_platforms": platforms, "ignored_disabled_platforms": ignored,
                   "resolver_inputs_sha256": inputs}
        # Retain acknowledgment evidence after a future operation replaces the
        # current journal. The state hash gives each acknowledgment one identity.
        receipt_path = safe_path(STATE / (operation + "." + expected_state_digest + ".recovery.json"))
        if receipt_path.is_symlink():
            raise Refused("recovery_receipt_symlink")
        if receipt_path.exists():
            existing = json.loads(receipt_path.read_bytes())
            if existing.get("prior_state_sha256") != expected_state_digest or existing.get("failed_queue_id") != operation:
                raise Refused("recovery_receipt_conflict")
            receipt = existing
        else:
            write_private(receipt_path, canonical(receipt))
        recovered = copy.deepcopy(prior)
        recovered["phase"] = "operator_recovered"
        recovered["events"].append(receipt)
        write_private(state_path, canonical(recovered))
        return {"status": "operator_recovered", "operation": operation,
                "prior_state_sha256": expected_state_digest, "running_identity": identity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "inspect", "activate", "acknowledge-recovery"])
    parser.add_argument("--candidate")
    parser.add_argument("--python")
    parser.add_argument("--target")
    parser.add_argument("--manifest")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--expected-operation")
    parser.add_argument("--expected-state-sha256")
    parser.add_argument("--queue-id", default="auto")
    parser.add_argument("--label", choices=[LABEL], default=LABEL)
    args = parser.parse_args()
    try:
        if args.action == "stage":
            result = stage(args.candidate, args.python, args.target)
        elif args.action == "inspect":
            path = safe_path(STATE / "state.json")
            result = json.loads(path.read_text()) if path.exists() else {"status": "absent"}
        elif args.action == "acknowledge-recovery":
            result = acknowledge_recovery(args.manifest, args.manifest_sha256,
                                          args.expected_operation, args.expected_state_sha256)
        else:
            result = activate(args.manifest, args.manifest_sha256, args.queue_id)
        print(json.dumps(result, sort_keys=True))
        return 0 if args.action != "activate" or result["status"] == "succeeded" else 1
    except Exception as exc:
        print(json.dumps({"status": "refused", "reason": safe_reason(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
