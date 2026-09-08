#!/usr/bin/env python3
"""Prove the actual d1 component artifact plus its independently pinned helper."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

if __package__:
    from scripts.run_dd_release_tests import prepare_file_limit
else:
    from run_dd_release_tests import prepare_file_limit

HELPER_ROOT = Path(__file__).resolve().parents[1]
MANDATORY = ("tests/test_dd_obs_config.py", "tests/test_route_to_lane_shared_home.py",
             "tests/test_route_to_lane_mission_parity.py")
REVIEWED_FIXTURES = ("tests/test_route_to_lane_shared_home.py", "tests/test_route_to_lane_mission_parity.py")
BOUNDARY = "tests/tools/test_p1_caller_boundary.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    candidate = Path(args.candidate).resolve()
    if not candidate.is_dir(): raise SystemExit("Candidate missing")
    paths = set(MANDATORY) | {BOUNDARY}
    for pattern in ("tests/test_*attribution*.py", "tests/gateway/test_*attribution*.py"):
        paths.update(str(path.relative_to(candidate)) for path in candidate.glob(pattern))
    if any(not (candidate / name).is_file() for name in (*MANDATORY, BOUNDARY, "tests/conftest.py")):
        raise SystemExit("Mandatory candidate attribution tests missing")
    if any(not (HELPER_ROOT / name).is_file() for name in REVIEWED_FIXTURES):
        raise SystemExit("Reviewed authorization fixtures missing")
    prepare_file_limit()
    env = {name: os.environ[name] for name in (
        "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "SHELL", "SYSTEMROOT", "COMSPEC"
    ) if name in os.environ}
    env.update(TZ="UTC", LANG="C.UTF-8", LC_ALL="C.UTF-8", PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    for name in ("DD_REGISTRY_URL", "DD_OBS_INGEST_URL", "HERMES_DECISIONDATA_OBSERVABILITY_URL",
                 "DECISIONDATA_OBSERVABILITY_URL"):
        env[name] = "http://127.0.0.1:1"
    command = [sys.executable, "-B", "-m", "pytest", "-o", "addopts=", "-n", "4", "-q", "--tb=short",
               "-m", "not integration"]
    # Two separate pytest processes avoid importing helper-main runtime modules
    # into the artifact's attribution suite. Neither installs dependencies.
    with tempfile.TemporaryDirectory(prefix="dd-profile-proof-") as home:
        env["HERMES_HOME"] = home
        # d1's two transport tests omitted their authorized caller fixture. Use
        # exact reviewed test blobs in a temporary harness, never edit candidate
        # tests or weaken their assertions. Negative boundary tests stay d1's.
        harness = Path(home) / "harness"
        for name in sorted(paths):
            source = HELPER_ROOT if name in REVIEWED_FIXTURES else candidate
            destination = harness / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((source / name).read_bytes())
        conftest = (candidate / "tests/conftest.py").read_text()
        conftest += '''
\n@pytest.fixture(autouse=True)
def _component_import_origins():
    import importlib
    import os
    from pathlib import Path
    root = Path(os.environ["DD_PROFILE_COMPONENT_ROOT"]).resolve()
    for name in ("dd_obs", "tools.route_to_lane_tool", "tools.p1_caller_boundary"):
        module = importlib.import_module(name)
        assert Path(module.__file__).resolve().is_relative_to(root), "runtime import escaped candidate"
'''
        (harness / "tests/conftest.py").write_text(conftest)
        env["PYTHONPATH"] = str(candidate)
        env["DD_PROFILE_COMPONENT_ROOT"] = str(candidate)
        rc = subprocess.call(command + ["--rootdir", str(harness), *[str(harness / name) for name in sorted(paths)]],
                             cwd=candidate, env=env)
        if rc: return rc
    with tempfile.TemporaryDirectory(prefix="dd-profile-helper-proof-") as home:
        env["HERMES_HOME"] = home
        env.pop("PYTHONPATH", None)
        env["DD_PROFILE_COMPONENT_ROOT"] = str(candidate)
        return subprocess.call(command + ["tests/scripts/test_dd_hermes_ordinary_release.py",
                                          "tests/scripts/test_dd_hermes_profile_release.py"],
                               cwd=HELPER_ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
