#!/usr/bin/env python3
"""DecisionData release regression gate; run with the provisioned test Python.

Preserves the attribution rail and adds the reconciled gateway, execution,
Classic, and audio contracts. Does not install packages into a serving venv.
The full upstream suite remains a separate required pre-push check.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
REQUIRED = (
    "tests/scripts/test_dd_release_gate.py",
    "tests/test_dd_obs_config.py",
    "tests/test_route_to_lane_shared_home.py",
    "tests/test_route_to_lane_mission_parity.py",
    "tests/test_context_budget_reporting.py",
    "tests/test_hermes_logging.py",
    "tests/tools/test_dgx_transcription.py",
    "tests/tools/test_code_execution.py",
    "tests/run_agent/test_concurrent_interrupt.py",
    "tests/run_agent/test_interrupt_propagation.py",
    "tests/run_agent/test_openai_client_lifecycle.py",
    "tests/tools/test_interrupt.py",
    "tests/tools/test_route_to_lane.py",
    "tests/tools/test_route_to_lane_authority.py",
    "tests/tools/test_p1_caller_boundary.py",
    "tests/tools/test_base_environment.py",
    "tests/tools/test_local_env_cwd_recovery.py",
    "tests/hermes_cli/test_oneshot_exit_code.py",
    "tests/agent/test_execution_deadline.py",
    "tests/agent/test_prompt_builder.py",
    "tests/agent/test_auxiliary_main_first.py",
    "tests/agent/test_auxiliary_config_bridge.py",
    "tests/agent/test_auxiliary_transport_autodetect.py",
    "tests/agent/test_context_compressor.py",
    "tests/agent/test_context_compressor_summary_continuity.py",
    "tests/run_agent/test_classic_runtime_integration.py",
    "tests/run_agent/test_tool_call_guardrail_runtime.py",
    "tests/run_agent/test_fallback_model.py",
    "tests/run_agent/test_init_fallback_on_exhausted_pool.py",
    "tests/run_agent/test_413_compression.py",
    "tests/run_agent/test_compress_focus_plugin_fallback.py",
    "tests/run_agent/test_provider_attribution_headers.py",
    "tests/gateway/test_dd_agent_service.py",
    "tests/gateway/test_dd_identity_wiring.py",
    "tests/gateway/test_system_a_messaging_principal.py",
    "tests/gateway/test_session_boundary_security_state.py",
    "tests/gateway/test_reasoning_command.py",
    "tests/gateway/test_lane_wake.py",
    "tests/gateway/test_lane_wake_admission.py",
)
PATTERNS = (
    "tests/test_*attribution*.py",
    "tests/gateway/test_*attribution*.py",
    "tests/tools/test_voice*.py",
    "tests/tools/test_transcription*.py",
    "tests/hermes_cli/test_voice*.py",
    "tests/gateway/test_api_server*.py",
    "tests/gateway/test_restart*.py",
)


def prepare_file_limit():
    """Account for launchd's 256-file soft limit in this test process only."""
    try:
        import resource
    except ImportError:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
    if soft != resource.RLIM_INFINITY and soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    print(f"Test process file limit: {resource.getrlimit(resource.RLIMIT_NOFILE)[0]}", flush=True)


def main():
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit("Required release tests missing: " + ", ".join(missing))
    paths = set(REQUIRED)
    for pattern in PATTERNS:
        paths.update(str(path.relative_to(ROOT)) for path in ROOT.glob(pattern))
    prepare_file_limit()
    # Start from OS essentials instead of a credential-name denylist: collection
    # happens before conftest's per-test cleanup, and auth-store overrides and
    # application-specific key names must never reach that stage.
    env = {name: os.environ[name] for name in (
        "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP",
        "SHELL", "SYSTEMROOT", "COMSPEC",
    ) if name in os.environ}
    env.update(TZ="UTC", LANG="C.UTF-8", LC_ALL="C.UTF-8", PYTHONHASHSEED="0")
    # Unit-test telemetry must not be posted to the running platform.
    for name in (
        "DD_REGISTRY_URL", "DD_OBS_INGEST_URL",
        "HERMES_DECISIONDATA_OBSERVABILITY_URL", "DECISIONDATA_OBSERVABILITY_URL",
    ):
        env[name] = "http://127.0.0.1:1"
    # Also isolate import/collection, before conftest's per-test home fixture.
    with tempfile.TemporaryDirectory(prefix="dd-release-tests-") as test_home:
        env["HERMES_HOME"] = test_home
        print(f"DD release gate: {len(paths)} test files, 4 workers", flush=True)
        return subprocess.call(
            [sys.executable, "-m", "pytest", "-o", "addopts=", "-n", "4",
             "-q", "--tb=short", "-m", "not integration", *sorted(paths), *sys.argv[1:]],
            cwd=ROOT, env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
