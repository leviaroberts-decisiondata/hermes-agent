#!/usr/bin/env python3
"""
register_fleet.py — make the agent fleet enumerable in the Service Layer (:8510).

WS1 §2 step 2: register P1 + the 10 specialist lanes (+ optional Slack agents)
into dd-agent-service via POST /agents/register, flipping ``registered_agents``
from 0 to the live fleet so the backbone the contract names as the Service Layer
actually knows who exists (C6 Work Registry must enumerate the command layer too,
so P1/``default`` is registered as agent #11 — §9-Q7).

ADDITIVE + IDEMPOTENT + NON-FATAL:
  * Gated on ENABLE_AGENT_SERVICE_REGISTRATION (default OFF) — inert unless flipped.
  * /agents/register re-activates on duplicate name, so re-runs are safe.
  * A down :8510 is logged and skipped; this never mutates dispatch or routing.

Fleet source: the lane registry (~/.hermes/dd-lanes/telegram-targets.json), the
single 10-lane source of record (WS4 §4.2), + P1 (``default``). This is the same
list the onboarding orchestrators (WS7-2) call register_agent() for per-agent;
this script is the batch/manual entrypoint and the activation-gate fixture.

Usage:
  ENABLE_AGENT_SERVICE_REGISTRATION=1 python -m gateway.register_fleet
  ENABLE_AGENT_SERVICE_REGISTRATION=1 python -m gateway.register_fleet --dry-run
"""
import argparse
import json
import logging
import os
import sys

logger = logging.getLogger("hermes.register_fleet")

LANE_REGISTRY = os.path.expanduser(
    os.getenv("DD_LANE_REGISTRY", "~/.hermes/dd-lanes/telegram-targets.json")
)


def load_fleet() -> list[dict]:
    """Return [{name, webhook_url, role}] for P1 + every specialist lane profile."""
    fleet: list[dict] = [
        # P1 command layer — agent #11; enumerated so the Work Registry view is complete.
        {"name": "default", "webhook_url": "", "role": "p1-coordinator"},
    ]
    try:
        with open(LANE_REGISTRY) as f:
            lanes = json.load(f).get("lanes", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read lane registry %s (%s); registering P1 only", LANE_REGISTRY, exc)
        return fleet
    seen = {"default"}
    for lane, info in lanes.items():
        profile = (info or {}).get("profile") or (info or {}).get("default_agent")
        if not profile or profile in seen:
            continue
        seen.add(profile)
        fleet.append({"name": profile, "webhook_url": "", "role": f"specialist:{lane}"})
    return fleet


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Register the agent fleet into :8510")
    ap.add_argument("--dry-run", action="store_true", help="List the fleet without POSTing")
    args = ap.parse_args(argv)

    fleet = load_fleet()
    print(f"Fleet to register ({len(fleet)} agents):")
    for a in fleet:
        print(f"  - {a['name']:22s} [{a['role']}]")

    if args.dry_run:
        print("\n--dry-run: no registration POSTed.")
        return 0

    from gateway import dd_agent_service

    if not dd_agent_service.is_enabled():
        print(
            "\nENABLE_AGENT_SERVICE_REGISTRATION is OFF — refusing to register.\n"
            "Set ENABLE_AGENT_SERVICE_REGISTRATION=1 to flip the fleet live.",
            file=sys.stderr,
        )
        return 2

    ok = 0
    for a in fleet:
        if dd_agent_service.register_agent(a["name"], a["webhook_url"]):
            ok += 1
            print(f"  ✓ registered {a['name']}")
        else:
            print(f"  ✗ skipped {a['name']} (:8510 unavailable or non-2xx)")
    print(f"\nRegistered {ok}/{len(fleet)} agents into the Service Layer (:8510).")
    return 0 if ok == len(fleet) else 1


if __name__ == "__main__":
    raise SystemExit(main())
