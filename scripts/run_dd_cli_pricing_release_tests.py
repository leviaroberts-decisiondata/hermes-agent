#!/usr/bin/env python3
"""Existing artifact contracts plus mandatory reviewed pricing/CLI assertions."""
if __package__:
    from scripts.run_dd_profile_release_tests import main as profile_main
else:
    from run_dd_profile_release_tests import main as profile_main


def main():
    return profile_main(pricing_coverage=True, extra_tests=(
        "tests/scripts/test_dd_hermes_cli_release.py", "tests/test_gpt62_component_attribution.py",
        "tests/test_gpt62_cli_attribution.py"))


if __name__ == "__main__":
    main()
