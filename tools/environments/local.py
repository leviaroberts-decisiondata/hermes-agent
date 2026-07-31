"""Local execution environment — spawn-per-call with session snapshot."""

import os
import platform
import shutil
import signal
import subprocess
import tempfile

from tools.environments.base import BaseEnvironment, _pipe_stdin

_IS_WINDOWS = platform.system() == "Windows"


# ── Delivery-side OS-account separation (V1.1 Track B / V2 P0) ───────────────
# When enabled, the per-turn delivery shell is dropped to an unprivileged OS
# account (dd-delivery) instead of inheriting the gateway's uid. That account
# cannot traverse ~/.openclaw (0700 openclaw) to read the capability signing
# key, and has no sudo grant, so the System A/B boundary becomes an OS-level
# boundary rather than an in-process-only one.
#
# Mechanism: prefix the bash invocation with `sudo -n -u dd-delivery` (the
# sanctioned cross-user runner shape; backed by the existing openclaw sudo
# grant — no new sudoers entry). The child env is rebuilt from scratch with
# `env -i` so the curated, sanitized variables are passed deterministically and
# HOME points at the delivery account's own scratch dir (it cannot write into
# the openclaw-owned profile HOME).
#
# Default OFF. Flip on via DD_DELIVERY_UID_SEPARATION=1 (per-process env) for a
# code-free rollback path.
_DELIVERY_ACCOUNT = os.getenv("DD_DELIVERY_ACCOUNT", "dd-delivery")
_DELIVERY_HOME = os.getenv("DD_DELIVERY_HOME", "/Users/dd-delivery")


def _delivery_uid_separation_enabled() -> bool:
    """True when delivery turns should drop to the unprivileged OS account.

    Read live (not cached) so the flag can be flipped via launchctl setenv +
    gateway restart without a code change, and so tests can toggle it.
    """
    if _IS_WINDOWS:
        return False
    return os.getenv("DD_DELIVERY_UID_SEPARATION", "") in ("1", "true", "True", "yes")


_DELIVERY_SESSION_TMP = "/tmp/dd-delivery-sessions"


def _ensure_delivery_session_tmp() -> str | None:
    """Create (idempotently) a sticky temp dir shared by gateway + delivery.

    Session snapshot / cwd-marker files are written by the gateway (openclaw)
    and re-read/re-dumped by the delivery account. A sticky 1777 dir (like /tmp
    itself) lets both write while each owns its own files. Returns the path, or
    None on failure (caller then falls back to the normal TMPDIR resolution).
    """
    try:
        os.makedirs(_DELIVERY_SESSION_TMP, exist_ok=True)
        # 1777: world read/write/execute + sticky, so dd-delivery can traverse
        # and create files alongside openclaw-written ones.
        os.chmod(_DELIVERY_SESSION_TMP, 0o1777)
        return _DELIVERY_SESSION_TMP
    except Exception:
        return None


def _wrap_delivery_account(args: list[str], run_env: dict) -> tuple[list[str], dict, str]:
    """Wrap a bash argv to execute under the unprivileged delivery account.

    Returns (new_args, new_env, cwd_override). The new argv is
        sudo -n -u <acct> /usr/bin/env -i K=V ... <original argv>
    so the child sees exactly the curated env (HOME redirected to the delivery
    scratch dir). The Popen env is irrelevant to the child once `env -i` runs,
    but we still hand the sanitized env to the sudo process itself.

    ``cwd_override`` is always /tmp: Popen performs its chdir while still running
    as the gateway uid (so it cannot enter the 0700 delivery HOME), then sudo
    drops to dd-delivery (whose bash cannot getcwd in a 0700 openclaw tree). /tmp
    is enterable by both. The real landing dir is set by the in-command
    ``builtin cd <cwd> || cd $HOME`` that base._wrap_command emits under
    delivery separation. Callers operating on openclaw-owned 0700 trees must
    arrange delivery-readable paths (a known limitation, see REPORT).
    """
    # Curated env for the delivery child. Start from run_env (already
    # provider-stripped by _make_run_env), then force HOME/USER/LOGNAME to the
    # delivery account so tools (git, gh, npm) write into a dir it owns.
    child_env = dict(run_env)
    child_env["HOME"] = _DELIVERY_HOME
    child_env["USER"] = _DELIVERY_ACCOUNT
    child_env["LOGNAME"] = _DELIVERY_ACCOUNT
    # TMPDIR under the delivery HOME avoids cross-account /tmp ownership noise.
    child_env.setdefault("TMPDIR", os.path.join(_DELIVERY_HOME, "tmp"))

    # Popen performs its chdir while still running as the *gateway* uid, then
    # sudo drops to dd-delivery. The chdir target must therefore be enterable by
    # the gateway uid (rules out the 0700 delivery HOME) AND let the dropped
    # bash getcwd at startup (rules out 0700 openclaw trees). /tmp satisfies
    # both unconditionally. The real landing dir is set by the in-command
    # `builtin cd <cwd> || cd $HOME` that base._wrap_command emits.
    cwd_override = "/tmp"

    env_assignments = [f"{k}={v}" for k, v in child_env.items()]
    new_args = [
        "sudo", "-n", "-u", _DELIVERY_ACCOUNT,
        "/usr/bin/env", "-i", *env_assignments,
        *args,
    ]
    # The sudo process itself inherits run_env; the child gets child_env via env -i.
    return new_args, run_env, cwd_override


# Hermes-internal env vars that should NOT leak into terminal subprocesses.
_HERMES_PROVIDER_ENV_FORCE_PREFIX = "_HERMES_FORCE_"


def _build_provider_env_blocklist() -> frozenset:
    """Derive the blocklist from provider, tool, and gateway config."""
    blocked: set[str] = set()

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        for pconfig in PROVIDER_REGISTRY.values():
            blocked.update(pconfig.api_key_env_vars)
            if pconfig.base_url_env_var:
                blocked.add(pconfig.base_url_env_var)
    except ImportError:
        pass

    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS
        for name, metadata in OPTIONAL_ENV_VARS.items():
            category = metadata.get("category")
            if category in {"tool", "messaging"}:
                blocked.add(name)
            elif category == "setting" and metadata.get("password"):
                blocked.add(name)
    except ImportError:
        pass

    blocked.update({
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "LLM_MODEL",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "PERPLEXITY_API_KEY",
        "COHERE_API_KEY",
        "FIREWORKS_API_KEY",
        "XAI_API_KEY",
        "HELICONE_API_KEY",
        "PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_NAME",
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_NAME",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_AUTO_THREAD",
        "SLACK_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL_NAME",
        "SLACK_ALLOWED_USERS",
        "WHATSAPP_ENABLED",
        "WHATSAPP_MODE",
        "WHATSAPP_ALLOWED_USERS",
        "SIGNAL_HTTP_URL",
        "SIGNAL_ACCOUNT",
        "SIGNAL_ALLOWED_USERS",
        "SIGNAL_GROUP_ALLOWED_USERS",
        "SIGNAL_HOME_CHANNEL",
        "SIGNAL_HOME_CHANNEL_NAME",
        "SIGNAL_IGNORE_STORIES",
        "HASS_TOKEN",
        "HASS_URL",
        "EMAIL_ADDRESS",
        "EMAIL_PASSWORD",
        "EMAIL_IMAP_HOST",
        "EMAIL_SMTP_HOST",
        "EMAIL_HOME_ADDRESS",
        "EMAIL_HOME_ADDRESS_NAME",
        "GATEWAY_ALLOWED_USERS",
        "GH_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DAYTONA_API_KEY",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
    })
    return frozenset(blocked)


_HERMES_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()


def _sanitize_subprocess_env(base_env: dict | None, extra_env: dict | None = None) -> dict:
    """Filter Hermes-managed secrets from a subprocess environment."""
    try:
        from tools.env_passthrough import is_env_passthrough as _is_passthrough
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731

    sanitized: dict[str, str] = {}

    for key, value in (base_env or {}).items():
        if key.startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            continue
        if key not in _HERMES_PROVIDER_ENV_BLOCKLIST or _is_passthrough(key):
            sanitized[key] = value

    for key, value in (extra_env or {}).items():
        if key.startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            real_key = key[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            sanitized[real_key] = value
        elif key not in _HERMES_PROVIDER_ENV_BLOCKLIST or _is_passthrough(key):
            sanitized[key] = value

    # Per-profile HOME isolation for background processes (same as _make_run_env).
    from hermes_constants import get_subprocess_home
    _profile_home = get_subprocess_home()
    if _profile_home:
        sanitized["HOME"] = _profile_home

    return sanitized


def _find_bash() -> str:
    """Find bash for command execution."""
    if not _IS_WINDOWS:
        return (
            shutil.which("bash")
            or ("/usr/bin/bash" if os.path.isfile("/usr/bin/bash") else None)
            or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
            or os.environ.get("SHELL")
            or "/bin/sh"
        )

    custom = os.environ.get("HERMES_GIT_BASH_PATH")
    if custom and os.path.isfile(custom):
        return custom

    found = shutil.which("bash")
    if found:
        return found

    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate

    raise RuntimeError(
        "Git Bash not found. Hermes Agent requires Git for Windows on Windows.\n"
        "Install it from: https://git-scm.com/download/win\n"
        "Or set HERMES_GIT_BASH_PATH to your bash.exe location."
    )


# Backward compat — process_registry.py imports this name
_find_shell = _find_bash


# Standard PATH entries for environments with minimal PATH.
_SANE_PATH = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def _make_run_env(env: dict) -> dict:
    """Build a run environment with a sane PATH and provider-var stripping."""
    try:
        from tools.env_passthrough import is_env_passthrough as _is_passthrough
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731

    merged = dict(os.environ | env)
    run_env = {}
    for k, v in merged.items():
        if k.startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            real_key = k[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            run_env[real_key] = v
        elif k not in _HERMES_PROVIDER_ENV_BLOCKLIST or _is_passthrough(k):
            run_env[k] = v
    existing_path = run_env.get("PATH", "")
    if "/usr/bin" not in existing_path.split(":"):
        run_env["PATH"] = f"{existing_path}:{_SANE_PATH}" if existing_path else _SANE_PATH

    # Per-profile HOME isolation: redirect system tool configs (git, ssh, gh,
    # npm …) into {HERMES_HOME}/home/ when that directory exists.  Only the
    # subprocess sees the override — the Python process keeps the real HOME.
    from hermes_constants import get_subprocess_home
    _profile_home = get_subprocess_home()
    if _profile_home:
        run_env["HOME"] = _profile_home

    # Per-turn canonical WTS task -> DD_TURN_WTS_TASK for `dd-delivery ship`. Sourced
    # from the task-local session contextvar (per-turn isolated), injected into THIS
    # subprocess env only — never into the shared gateway os.environ, so concurrent
    # turns cannot cross-contaminate. Absent/empty on non-turn (CLI/cron) contexts.
    try:
        from gateway.session_context import get_session_env
        _turn_task = get_session_env("HERMES_SESSION_WTS_TASK_ID", "")
        if _turn_task and "DD_TURN_WTS_TASK" not in run_env:
            run_env["DD_TURN_WTS_TASK"] = _turn_task
    except Exception:
        pass

    return run_env


def _read_terminal_shell_init_config() -> tuple[list[str], bool]:
    """Return (shell_init_files, auto_source_bashrc) from config.yaml.

    Best-effort — returns sensible defaults on any failure so terminal
    execution never breaks because the config file is unreadable.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        terminal_cfg = cfg.get("terminal") or {}
        files = terminal_cfg.get("shell_init_files") or []
        if not isinstance(files, list):
            files = []
        auto_bashrc = bool(terminal_cfg.get("auto_source_bashrc", True))
        return [str(f) for f in files if f], auto_bashrc
    except Exception:
        return [], True


def _resolve_shell_init_files() -> list[str]:
    """Resolve the list of files to source before the login-shell snapshot.

    Expands ``~`` and ``${VAR}`` references and drops anything that doesn't
    exist on disk, so a missing ``~/.bashrc`` never breaks the snapshot.
    The ``auto_source_bashrc`` path runs only when the user hasn't supplied
    an explicit list — once they have, Hermes trusts them.
    """
    explicit, auto_bashrc = _read_terminal_shell_init_config()

    candidates: list[str] = []
    if explicit:
        candidates.extend(explicit)
    elif auto_bashrc and not _IS_WINDOWS:
        # Build a login-shell-ish source list so tools like n / nvm / asdf /
        # pyenv that self-install into the user's shell rc land on PATH in
        # the captured snapshot.
        #
        # ~/.profile and ~/.bash_profile run first because they have no
        # interactivity guard — installers like ``n`` and ``nvm`` append
        # their PATH export there on most distros, and a non-interactive
        # ``. ~/.profile`` picks that up.
        #
        # ~/.bashrc runs last. On Debian/Ubuntu the default bashrc starts
        # with ``case $- in *i*) ;; *) return;; esac`` and exits early
        # when sourced non-interactively, which is why sourcing bashrc
        # alone misses nvm/n PATH additions placed below that guard. We
        # still include it so users who put PATH logic in bashrc (and
        # stripped the guard, or never had one) keep working.
        candidates.extend(["~/.profile", "~/.bash_profile", "~/.bashrc"])

    resolved: list[str] = []
    for raw in candidates:
        try:
            path = os.path.expandvars(os.path.expanduser(raw))
        except Exception:
            continue
        if path and os.path.isfile(path):
            resolved.append(path)
    return resolved


def _prepend_shell_init(cmd_string: str, files: list[str]) -> str:
    """Prepend ``source <file>`` lines (guarded + silent) to a bash script.

    Each file is wrapped so a failing rc file doesn't abort the whole
    bootstrap: ``set +e`` keeps going on errors, ``2>/dev/null`` hides
    noisy prompts, and ``|| true`` neutralises the exit status.
    """
    if not files:
        return cmd_string

    prelude_parts = ["set +e"]
    for path in files:
        # shlex.quote isn't available here without an import; the files list
        # comes from os.path.expanduser output so it's a concrete absolute
        # path.  Escape single quotes defensively anyway.
        safe = path.replace("'", "'\\''")
        prelude_parts.append(f"[ -r '{safe}' ] && . '{safe}' 2>/dev/null || true")
    prelude = "\n".join(prelude_parts) + "\n"
    return prelude + cmd_string


class LocalEnvironment(BaseEnvironment):
    """Run commands directly on the host machine.

    Spawn-per-call: every execute() spawns a fresh bash process.
    Session snapshot preserves env vars across calls.
    CWD persists via file-based read after each command.
    """

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict = None):
        if cwd:
            cwd = os.path.expanduser(cwd)
        super().__init__(cwd=cwd or os.getcwd(), timeout=timeout, env=env)
        self.init_session()

    def get_temp_dir(self) -> str:
        """Return a shell-safe writable temp dir for local execution.

        Termux does not provide /tmp by default, but exposes a POSIX TMPDIR.
        Prefer POSIX-style env vars when available, keep using /tmp on regular
        Unix systems, and only fall back to tempfile.gettempdir() when it also
        resolves to a POSIX path.

        Check the environment configured for this backend first so callers can
        override the temp root explicitly (for example via terminal.env or a
        custom TMPDIR), then fall back to the host process environment.
        """
        # Under delivery OS-account separation the per-session snapshot/cwd
        # files are written by the gateway (openclaw) but re-read and re-dumped
        # by the dropped delivery account (dd-delivery). The usual TMPDIR
        # (/tmp/claude-502, 0700 openclaw) is not traversable by dd-delivery, so
        # the snapshot can't be sourced and env/cwd persistence silently breaks.
        # Route session temp files to a shared sticky dir both accounts can use.
        if _delivery_uid_separation_enabled():
            shared = _ensure_delivery_session_tmp()
            if shared:
                return shared

        for env_var in ("TMPDIR", "TMP", "TEMP"):
            candidate = self.env.get(env_var) or os.environ.get(env_var)
            if candidate and candidate.startswith("/"):
                return candidate.rstrip("/") or "/"

        if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK | os.X_OK):
            return "/tmp"

        candidate = tempfile.gettempdir()
        if candidate.startswith("/"):
            return candidate.rstrip("/") or "/"

        return "/tmp"

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        bash = _find_bash()
        # For login-shell invocations (used by init_session to build the
        # environment snapshot), prepend sources for the user's bashrc /
        # custom init files so tools registered outside bash_profile
        # (nvm, asdf, pyenv, …) end up on PATH in the captured snapshot.
        # Non-login invocations are already sourcing the snapshot and
        # don't need this.
        if login:
            init_files = _resolve_shell_init_files()
            if init_files:
                cmd_string = _prepend_shell_init(cmd_string, init_files)
        args = [bash, "-l", "-c", cmd_string] if login else [bash, "-c", cmd_string]
        run_env = _make_run_env(self.env)
        run_cwd = self.cwd

        # Delivery-side OS-account separation: drop non-login (delivery) turns to
        # the unprivileged dd-delivery account when the flag is on. Login-shell
        # invocations (init_session env snapshot) stay on the host account — the
        # snapshot defines the base env and is not delivery work.
        if not login and _delivery_uid_separation_enabled():
            args, run_env, run_cwd = _wrap_delivery_account(args, run_env)

        proc = subprocess.Popen(
            args,
            text=True,
            env=run_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
            cwd=run_cwd,
        )

        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)

        return proc

    def _kill_process(self, proc):
        """Kill the entire process group (all children)."""
        try:
            if _IS_WINDOWS:
                proc.terminate()
            else:
                pgid = os.getpgid(proc.pid)
                # Under delivery uid-separation the leaf processes run as
                # dd-delivery; our (gateway-uid) killpg "succeeds" at the
                # syscall but cannot actually deliver to a different uid, and
                # the sudo leader dies on the first SIGTERM — so the wait()
                # below returns and the TimeoutExpired escalation never fires,
                # leaking the delivery children. Always escalate via sudo to
                # the delivery account (which the gateway is permitted to do)
                # so the whole group is reaped regardless.
                delivery = _delivery_uid_separation_enabled()
                os.killpg(pgid, signal.SIGTERM)
                if delivery:
                    self._kill_delivery_group(pgid, "-TERM")
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
                    if delivery:
                        self._kill_delivery_group(pgid, "-KILL")
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass

    def _delivery_cd_fallback(self) -> bool:
        """Soft-cd to $HOME (instead of exit 126) when delivery separation is on.

        Delivery turns may target openclaw-owned 0700 trees the dd-delivery
        account cannot enter; base._wrap_command uses this to fall back to the
        delivery scratch HOME rather than aborting the whole shell.
        """
        return _delivery_uid_separation_enabled()

    @staticmethod
    def _kill_delivery_group(pgid: int, sig: str = "-KILL"):
        """Signal a process group owned by the delivery account via sudo.

        ``kill <sig> -<pgid>`` targets the whole group; run as dd-delivery so it
        can signal its own processes. Best-effort; never raises.
        """
        try:
            subprocess.run(
                ["sudo", "-n", "-u", _DELIVERY_ACCOUNT, "kill", sig, f"-{pgid}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except Exception:
            pass

    def _update_cwd(self, result: dict):
        """Read CWD from temp file (local-only, no round-trip needed)."""
        try:
            with open(self._cwd_file) as f:
                cwd_path = f.read().strip()
            if cwd_path:
                self.cwd = cwd_path
        except (OSError, FileNotFoundError):
            pass

        # Still strip the marker from output so it's not visible
        self._extract_cwd_from_output(result)

    def cleanup(self):
        """Clean up temp files."""
        for f in (self._snapshot_path, self._cwd_file):
            try:
                os.unlink(f)
            except OSError:
                pass
