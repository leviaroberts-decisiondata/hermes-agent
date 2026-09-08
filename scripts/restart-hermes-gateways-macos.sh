#!/usr/bin/env bash
# Restart Hermes launchd gateways on macOS and verify model availability.
#
# Why bootout/bootstrap instead of kickstart -k:
# launchctl kickstart restarts the process but does not reload plist
# EnvironmentVariables. P1/classic shared-auth changes require a full unload/load.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTEST_BIN="${ROOT_DIR}/venv/bin/hermes"
if [[ ! -x "${PYTEST_BIN}" && -x "${ROOT_DIR}/.venv/bin/hermes" ]]; then
  PYTEST_BIN="${ROOT_DIR}/.venv/bin/hermes"
fi

USER_ID="$(id -u)"
CLASSIC_LABEL="ai.hermes.gateway-classic"
P1_LABEL="ai.hermes.gateway"
CLASSIC_PLIST="${HOME}/Library/LaunchAgents/${CLASSIC_LABEL}.plist"
P1_PLIST="${HOME}/Library/LaunchAgents/${P1_LABEL}.plist"
SHARED_AUTH="${HERMES_AUTH_STORE_PATH:-/Users/openclaw/.hermes-shared-auth/auth.json}"
P1_HEALTH_URL="${P1_HEALTH_URL:-http://127.0.0.1:8642/health}"
SKIP_PROBES="${SKIP_PROBES:-0}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

usage() {
  cat <<'EOF'
Usage: scripts/restart-hermes-gateways-macos.sh [--classic-only|--p1-only|--both]

Default is --both. Restart order for --both is classic first, then P1.
Set SKIP_PROBES=1 to skip Hermes model probes.
EOF
}

mode="both"
case "${1:-}" in
  ""|--both) mode="both" ;;
  --classic-only) mode="classic" ;;
  --p1-only) mode="p1" ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

wait_unloaded() {
  local label="$1"
  local deadline=$((SECONDS + 20))
  while launchctl print "gui/${USER_ID}/${label}" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      return 1
    fi
    sleep 0.5
  done
}

restart_label() {
  local label="$1"
  local plist="$2"
  if [[ ! -f "${plist}" ]]; then
    log "ERROR: plist not found for ${label}: ${plist}"
    return 1
  fi

  log "Restarting ${label} with bootout/bootstrap"
  launchctl bootout "gui/${USER_ID}" "${plist}" >/dev/null 2>&1 || true
  if ! wait_unloaded "${label}"; then
    log "${label} still visible after bootout; retrying once"
    launchctl bootout "gui/${USER_ID}" "${plist}" >/dev/null 2>&1 || true
    wait_unloaded "${label}" || {
      log "ERROR: ${label} did not unload cleanly"
      return 1
    }
  fi
  launchctl bootstrap "gui/${USER_ID}" "${plist}"
  launchctl kickstart -k "gui/${USER_ID}/${label}" >/dev/null 2>&1 || true
  launchctl print "gui/${USER_ID}/${label}" >/dev/null
  log "${label} loaded"
}

probe_model() {
  local hermes_home="$1"
  local expected="$2"
  if [[ "${SKIP_PROBES}" == "1" ]]; then
    log "Skipping model probe for ${hermes_home}"
    return 0
  fi
  if [[ ! -x "${PYTEST_BIN}" ]]; then
    log "ERROR: hermes binary not found under ${ROOT_DIR}/{venv,.venv}"
    return 1
  fi
  local out
  out="$(HERMES_HOME="${hermes_home}" HERMES_AUTH_STORE_PATH="${SHARED_AUTH}" \
    "${PYTEST_BIN}" chat --provider openai-codex --model gpt-5.5 -q "Reply with exactly: ${expected}" -Q 2>&1 || true)"
  if [[ "${out}" != *"${expected}"* ]]; then
    log "ERROR: model probe failed for ${hermes_home}: ${out}"
    return 1
  fi
  log "model probe OK for ${hermes_home}"
}

probe_p1_health() {
  if command -v curl >/dev/null 2>&1; then
    local health
    health="$(curl -fsS -m 5 "${P1_HEALTH_URL}" 2>&1 || true)"
    if [[ "${health}" != *'"status":"ok"'* ]]; then
      log "ERROR: P1 health failed: ${health}"
      return 1
    fi
    log "P1 health OK: ${health}"
  fi
}

if [[ "${mode}" == "both" || "${mode}" == "classic" ]]; then
  restart_label "${CLASSIC_LABEL}" "${CLASSIC_PLIST}"
  probe_model "/Users/openclaw/.hermes-classic" "CLASSIC_PRIMARY_OK"
fi

if [[ "${mode}" == "both" || "${mode}" == "p1" ]]; then
  restart_label "${P1_LABEL}" "${P1_PLIST}"
  probe_p1_health
  probe_model "/Users/openclaw/.hermes" "P1_PRIMARY_OK"
fi

log "restart sequence complete (${mode})"
