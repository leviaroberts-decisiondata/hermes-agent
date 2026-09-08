#!/usr/bin/env python3
"""Probe Hermes primary-model health and emit explicit fallback alerts.

Designed for lightweight cron/launchd use. It checks configured Hermes homes by
running a direct primary-provider/model prompt and scans gateway logs for new
fallback lines since the previous run. Output is line-oriented and safe for
alerting hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

FALLBACK_NEEDLES = (
    "switching to fallback",
    "primary model failed",
    "fallback activated",
    "_try_activate_fallback",
)


def _redact(text: str) -> str:
    """Best-effort redaction independent of the global redaction toggle."""
    try:
        os.environ.setdefault("HERMES_REDACT_SECRETS", "1")
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text)
    except Exception:
        return text


@dataclass(frozen=True)
class HomeProbe:
    name: str
    home: Path


def _parse_homes(raw: str) -> list[HomeProbe]:
    homes: list[HomeProbe] = []
    for idx, chunk in enumerate(raw.split(",")):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, path = chunk.split("=", 1)
            homes.append(HomeProbe(name=name.strip() or f"home{idx+1}", home=Path(path).expanduser()))
        else:
            path = Path(chunk).expanduser()
            homes.append(HomeProbe(name=path.name or f"home{idx+1}", home=path))
    return homes


def _run_probe(*, hermes_bin: Path, auth_store: str | None, provider: str, model: str, home: HomeProbe, timeout: int) -> tuple[bool, str]:
    expected = f"PRIMARY_HEALTH_OK_{home.name.upper().replace('-', '_').replace('.', '_')}"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home.home)
    if auth_store:
        env["HERMES_AUTH_STORE_PATH"] = auth_store
    cmd = [str(hermes_bin), "chat", "--provider", provider, "--model", model, "-q", f"Reply with exactly: {expected}", "-Q"]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return False, f"timeout after {timeout}s cmd={shlex.join(cmd[:5])} stdout={_redact(exc.stdout or '')} stderr={_redact(exc.stderr or '')}"
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and expected in output:
        return True, "ok"
    return False, f"exit={proc.returncode} output={_redact(output)[-1000:]}"


def _state_path(default_home: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return default_home / "primary-model-health-state.json"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"logs": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _scan_log(path: Path, state: dict) -> list[str]:
    alerts: list[str] = []
    key = str(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return alerts
    offset = int(state.setdefault("logs", {}).get(key, 0) or 0)
    if offset > size:
        offset = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read(256_000)
            state["logs"][key] = fh.tell()
    except Exception as exc:
        alerts.append(f"WARN log_scan_failed path={path} error={type(exc).__name__}")
        return alerts
    for line in chunk.splitlines():
        low = line.lower()
        if any(needle in low for needle in FALLBACK_NEEDLES):
            alerts.append(f"ALERT fallback_detected log={path} line={_redact(line)[-1200:]}")
    return alerts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-bin", default=os.getenv("HERMES_BIN", "./venv/bin/hermes"))
    parser.add_argument("--provider", default=os.getenv("HERMES_PRIMARY_HEALTH_PROVIDER", "openai-codex"))
    parser.add_argument("--model", default=os.getenv("HERMES_PRIMARY_HEALTH_MODEL", "gpt-5.5"))
    parser.add_argument("--homes", default=os.getenv("HERMES_PRIMARY_HEALTH_HOMES", "P1=/Users/openclaw/.hermes,classic=/Users/openclaw/.hermes-classic"))
    parser.add_argument("--auth-store", default=os.getenv("HERMES_AUTH_STORE_PATH", "/Users/openclaw/.hermes-shared-auth/auth.json"))
    parser.add_argument("--log", action="append", default=[])
    parser.add_argument("--state")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("HERMES_PRIMARY_HEALTH_TIMEOUT", "45")))
    args = parser.parse_args(argv)

    hermes_bin = Path(args.hermes_bin).expanduser()
    homes = _parse_homes(args.homes)
    if not homes:
        print("ALERT primary_model_monitor_misconfigured reason=no_homes")
        return 2

    default_home = homes[0].home
    state_path = _state_path(default_home, args.state)
    state = _load_state(state_path)

    exit_code = 0
    for home in homes:
        ok, detail = _run_probe(
            hermes_bin=hermes_bin,
            auth_store=args.auth_store,
            provider=args.provider,
            model=args.model,
            home=home,
            timeout=args.timeout,
        )
        if ok:
            print(f"OK primary_model home={home.name} provider={args.provider} model={args.model}")
        else:
            exit_code = 1
            print(f"ALERT primary_model_unhealthy home={home.name} provider={args.provider} model={args.model} detail={detail}")

    log_paths = [Path(p).expanduser() for p in args.log]
    if not log_paths:
        for home in homes:
            log_paths.extend([home.home / "logs" / "gateway.log", home.home / "logs" / "gateway.error.log"])
    for alert in _scan_all_logs(log_paths, state):
        exit_code = max(exit_code, 1)
        print(alert)

    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_state(state_path, state)
    return exit_code


def _scan_all_logs(paths: Iterable[Path], state: dict) -> list[str]:
    alerts: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        alerts.extend(_scan_log(path, state))
    return alerts


if __name__ == "__main__":
    raise SystemExit(main())
