#!/usr/bin/env python3
"""Source-level P1 dispatch boundary — WTS 17cbc96c, Phase A.

The 2026-08-10 crossover: PTG and Azul Hermes invoked `route_to_lane`, their work
entered P1's shared lane/reaper infrastructure, the callbacks landed in P1's
Telegram session, and P1 then wrote reconciliation notes and attachments onto
client WTS records.

Config alone cannot hold that line. `p1-dispatch` is a DEFAULT-ON toolset, so a
home is exposed unless it explicitly opts out. These tests pin the source-level
boundary that survives a config regression.

The fixture is the one the scope asks for: the SAME Telegram user (8737984752)
reachable through five different bot/gateway instances, so "same chat id" can
never be mistaken for "same authority".

Run with:  python -m pytest tests/tools/test_p1_caller_boundary.py -v
"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_cli.profiles import get_active_home_id, is_canonical_p1_home
from tools.p1_caller_boundary import (
    P1_DISPATCH_TOOLS,
    active_caller_id,
    p1_authority_error,
    require_p1_caller,
)

# One human, one Telegram user id, five bots. This is the whole point: the chat
# id is identical across every row, so it cannot be the authority.
LEVI_CHAT_ID = "8737984752"
HOMES = {
    "default": "/Users/openclaw/.hermes",              # P1 — the only authorised one
    "classic": "/Users/openclaw/.hermes-classic",      # Personal
    "ptg": "/Users/openclaw/.hermes-ptg",
    "azul": "/Users/openclaw/.hermes-azul",
    "hyperscience": "/Users/openclaw/.hermes-hyperscience",
}
CLIENT_HOMES = {k: v for k, v in HOMES.items() if k != "default"}


class _HomeCtx:
    """Run a block as if the process were started under a given HERMES_HOME."""

    def __init__(self, path):
        self._path = str(path)
        self._patch = None

    def __enter__(self):
        self._patch = patch.dict(os.environ, {"HERMES_HOME": self._path})
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class TestHomeIdentity(unittest.TestCase):
    """Identity must distinguish homes that get_active_profile_name() collapses."""

    def test_each_home_reports_its_own_identity(self):
        for expected, home in HOMES.items():
            with _HomeCtx(home):
                self.assertEqual(get_active_home_id(), expected, f"home {home}")

    def test_sibling_homes_are_distinguishable_from_each_other(self):
        seen = {}
        for name, home in HOMES.items():
            with _HomeCtx(home):
                seen[name] = get_active_home_id()
        self.assertEqual(len(set(seen.values())), len(seen), f"identities collided: {seen}")

    def test_profile_homes_still_resolve_to_the_profile_name(self):
        with _HomeCtx("/Users/openclaw/.hermes/profiles/dd-design"):
            self.assertEqual(get_active_home_id(), "dd-design")

    def test_get_active_profile_name_is_left_alone(self):
        """Pins the EXISTING behaviour so this change provably does not move it.

        Measured 2026-08-10: it returns "default" for every sibling home, not
        the "custom" its docstring promises — get_default_hermes_root() returns
        HERMES_HOME itself for any home outside ~/.hermes, so the "custom"
        branch is unreachable for them. That is its own defect (a sibling home
        asserts it IS the default profile), but it has 17 non-test callers and
        must be corrected deliberately, not as a side effect of this fix.
        """
        from hermes_cli.profiles import get_active_profile_name

        for name in ("azul", "ptg", "classic", "hyperscience"):
            with _HomeCtx(HOMES[name]):
                self.assertEqual(get_active_profile_name(), "default",
                                 f"{name}: existing behaviour moved unexpectedly")
                # The new resolver tells them apart even though the old one cannot.
                self.assertEqual(get_active_home_id(), name)

    def test_unrecognised_home_is_unidentified_not_p1(self):
        for junk in ("/tmp/not-a-hermes-home", "/Users/openclaw/.hermes-UPPER",
                     "/Users/openclaw/.hermes-bad slug"):
            with _HomeCtx(junk):
                self.assertEqual(get_active_home_id(), "", junk)
                self.assertFalse(is_canonical_p1_home(), junk)


class TestBoundaryAdmitsOnlyP1(unittest.TestCase):

    def test_p1_is_permitted_for_every_guarded_tool(self):
        with _HomeCtx(HOMES["default"]):
            self.assertTrue(is_canonical_p1_home())
            for tool in P1_DISPATCH_TOOLS:
                self.assertIsNone(require_p1_caller(tool), tool)

    def test_every_client_home_is_refused_for_every_guarded_tool(self):
        for name, home in CLIENT_HOMES.items():
            with _HomeCtx(home):
                for tool in P1_DISPATCH_TOOLS:
                    denied = require_p1_caller(tool)
                    self.assertIsNotNone(denied, f"{name} was allowed {tool}")
                    self.assertIn(tool, str(denied))

    def test_personal_is_refused_too(self):
        # Personal already disables p1-dispatch by config; the source boundary
        # must agree rather than rely on that config staying put.
        with _HomeCtx(HOMES["classic"]):
            self.assertIsNotNone(require_p1_caller("route_to_lane"))

    def test_refusal_says_it_is_not_retryable(self):
        with _HomeCtx(HOMES["ptg"]):
            msg = str(require_p1_caller("route_to_lane")).lower()
            self.assertIn("not retryable", msg)
            self.assertIn("17cbc96c", msg)

    def test_unidentifiable_caller_fails_closed(self):
        with _HomeCtx("/tmp/some-unknown-home"):
            self.assertIsNotNone(require_p1_caller("route_to_lane"))
        # And if identity resolution raises outright, that is still not authority.
        with patch("tools.p1_caller_boundary.active_caller_id", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                p1_authority_error("route_to_lane")
        with patch("hermes_cli.profiles.get_active_home_id", side_effect=RuntimeError("boom")):
            self.assertEqual(active_caller_id(), "")
            self.assertIsNotNone(require_p1_caller("route_to_lane"))

    def test_identity_cannot_be_supplied_by_the_model(self):
        """Authority comes from HERMES_HOME, never from a tool argument."""
        with _HomeCtx(HOMES["azul"]):
            # No argument of any name can make this return None.
            self.assertIsNotNone(require_p1_caller("route_to_lane"))
            self.assertEqual(active_caller_id(), "azul")


class TestGuardedToolsRefuseWithoutSideEffects(unittest.TestCase):
    """A refused call must not run a lane, bind WTS, or register a callback."""

    def test_route_to_lane_refuses_before_doing_anything(self):
        from tools import route_to_lane_tool

        with _HomeCtx(HOMES["ptg"]):
            with patch.object(route_to_lane_tool, "_write_packet") as packet, \
                 patch.object(route_to_lane_tool, "_register_pending_with_reaper") as reaper, \
                 patch("subprocess.run") as run:
                out = route_to_lane_tool.route_to_lane(
                    lane="qa", goal="should never dispatch", parent_agent=None)

        self.assertIn("refused", str(out).lower())
        packet.assert_not_called()
        reaper.assert_not_called()
        run.assert_not_called()

    def test_wts_bind_refuses_before_binding(self):
        from tools import wts_bind_tool

        with _HomeCtx(HOMES["azul"]):
            with patch.object(wts_bind_tool, "check_wts_bind_requirements") as reqs, \
                 patch("subprocess.run") as run:
                out = wts_bind_tool.wts_bind(goal="should never bind", parent_agent=None)

        self.assertIn("refused", str(out).lower())
        reqs.assert_not_called()   # refused before it even probes for the helper
        run.assert_not_called()

    def test_chain_status_refuses_before_reading_the_spine(self):
        from tools import chain_status_tool

        with _HomeCtx(HOMES["hyperscience"]):
            with patch.object(chain_status_tool, "_api_get") as api:
                out = chain_status_tool.chain_status(chain_id="whatever", parent_agent=None)

        self.assertIn("refused", str(out).lower())
        api.assert_not_called()

    def test_p1_is_not_blocked_by_the_guard(self):
        """Non-regression: the boundary must not break legitimate P1 dispatch."""
        from tools import route_to_lane_tool

        with _HomeCtx(HOMES["default"]):
            out = route_to_lane_tool.route_to_lane(lane="", goal="x", parent_agent=None)
        # Reaches the tool's OWN validation rather than the boundary.
        self.assertNotIn("not authorised", str(out))
        self.assertNotIn("refused", str(out).lower())


class TestSameChatIdIsNotAuthority(unittest.TestCase):
    """The defect in one assertion."""

    def test_identical_chat_id_across_five_bots_yields_five_identities(self):
        identities = {}
        for name, home in HOMES.items():
            with _HomeCtx(home):
                identities[name] = f"{get_active_home_id()}:{LEVI_CHAT_ID}"

        self.assertEqual(len(set(identities.values())), 5, identities)
        # Exactly one of them is P1, no matter that the chat id is shared.
        authorised = [n for n, h in HOMES.items()
                      if _is_p1(h)]
        self.assertEqual(authorised, ["default"])


def _is_p1(home):
    with _HomeCtx(home):
        return is_canonical_p1_home()


if __name__ == "__main__":
    unittest.main()
