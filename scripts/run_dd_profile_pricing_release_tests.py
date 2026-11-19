#!/usr/bin/env python3
"""Profile artifact proof with mandatory reviewed request-pricing coverage."""
if __package__:
    from scripts.run_dd_profile_release_tests import main as profile_main
else:
    from run_dd_profile_release_tests import main as profile_main


def main():
    return profile_main(pricing_coverage=True, extra_tests=("tests/test_gpt62_component_attribution.py",))


if __name__ == "__main__":
    main()
