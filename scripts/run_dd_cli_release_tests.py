#!/usr/bin/env python3
"""Use the established artifact proof harness with mandatory CLI release tests."""
if __package__:
    from scripts.run_dd_profile_release_tests import main as profile_main
else:
    from run_dd_profile_release_tests import main as profile_main


def main():
    return profile_main(extra_tests=("tests/scripts/test_dd_hermes_cli_release.py",))


if __name__ == "__main__":
    raise SystemExit(main())
