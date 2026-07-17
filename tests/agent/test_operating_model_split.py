"""Operating-model split: per-audience composition + the byte-identical-core guard.

The operating-model node is split into a shared `_core.md` (the inter-layer
boundary, single source of truth) plus per-audience role views. Each wired
loader (gateway coordinator = p1-default; dd-slack-service = slack-project-agent)
composes `core + its own role view`. These tests prove:

  1. Composition is per-audience (each gets ITS role view, not the other's).
  2. The shared-core block is BYTE-IDENTICAL across both wired audiences — the
     enforcement backbone that makes desync structurally impossible. This is the
     guard that must stay green.
  3. No role view leaks another audience's role framing.
  4. The retired "p1-specialists" audience (superseded 2026-07-17, WTS 2911977a)
     composes the shared core only — no caller can silently land on its card.

The shared-core extraction matches what both loaders embed verbatim (the
post-frontmatter body of operating-model/_core.md), so a divergence in either
loader's core handling fails this loudly.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.prompt_builder import (
    compose_operating_model,
    build_context_tree_prompt,
)
from utils import env_var_enabled

REPO_ROOT = Path(__file__).resolve().parents[2]
# The live tree (home-anchored). Tests read it read-only.
LIVE_TREE = Path.home() / ".hermes" / "context"
SLACK_CT_JS = Path.home() / "apps" / "dd-slack-service" / "src" / "context-tree.js"

# The current role-view headings (first line of each card body). If a card is
# re-headed, update these in the same change — they are the leak markers.
P1_HEADING = "Your role — P1 coordinator (System A)"
SLACK_HEADING = "Your role — Slack project agent (System B, direct execution)"

# Explicit opt-in for environments where the operating-model split tree is
# LEGITIMATELY absent (e.g. a stripped container that ships no /context tree).
# This is the ONLY way to skip the boundary guard. Default — and any
# environment that has the tree — runs the guard for real; a missing or
# regressed tree FAILS (red) so a violated System A / System B boundary can
# never disappear behind a green skip. See P0a of the realization plan.
_OPT_IN_ENV = "HERMES_ALLOW_MISSING_OPERATING_MODEL_TREE"
_CORE_PATH = LIVE_TREE / "operating-model" / "_core.md"
_TREE_PRESENT = _CORE_PATH.exists()
_OPT_IN = env_var_enabled(_OPT_IN_ENV)


def test_operating_model_split_tree_present_or_opted_out():
    """Tripwire: the operating-model split tree MUST exist, unless an env
    explicitly opts out. A missing/regressed tree is a real A/B boundary
    regression — it must surface as a RED failure here, never a silent skip.

    To run in an environment that legitimately ships no /context tree, set
    HERMES_ALLOW_MISSING_OPERATING_MODEL_TREE=1 (the guard then skips loudly).
    """
    if _OPT_IN:
        pytest.skip(
            f"{_OPT_IN_ENV} set: operating-model split guard explicitly opted "
            "out for this environment (tree legitimately absent)."
        )
    assert _TREE_PRESENT, (
        f"operating-model split tree MISSING at {_CORE_PATH} — the System A / "
        "System B boundary guard cannot run. This is a boundary regression, not "
        f"a reason to skip. If this environment legitimately has no /context "
        f"tree, set {_OPT_IN_ENV}=1 to opt out explicitly."
    )


# The per-audience / byte-identical-core guards below need the live tree to
# assert against. When the tree is present they run for real (a violated
# boundary fails them red). When it is absent they are skipped ONLY because the
# tripwire above has already failed (no opt-in) or been opted out (opt-in) —
# so a regression is never masked: it is caught by the tripwire instead.
_skip_body = pytest.mark.skipif(
    not _TREE_PRESENT,
    reason=(
        "operating-model split tree absent — boundary failure is reported by "
        "test_operating_model_split_tree_present_or_opted_out (tripwire), not here."
    ),
)


def _core_body(root: Path) -> str:
    """The verbatim post-frontmatter body of _core.md — what both loaders embed."""
    raw = (root / "operating-model" / "_core.md").read_text(encoding="utf-8")
    # strip frontmatter the same way prompt_builder._strip_frontmatter does
    text = raw.strip("﻿")
    if text.startswith("---"):
        closing = text.find("\n---", 3)
        if closing != -1:
            after = text.find("\n", closing + 1)
            if after != -1:
                return text[after + 1:].strip()
    return text.strip()


@_skip_body
class TestPerAudienceComposition:
    def test_p1_gets_coordinator_view_not_slack(self):
        body = compose_operating_model(LIVE_TREE, "p1-default")
        assert body is not None
        assert P1_HEADING in body
        assert SLACK_HEADING not in body

    def test_slack_gets_product_view_not_p1(self):
        body = compose_operating_model(LIVE_TREE, "slack-project-agent")
        assert body is not None
        assert SLACK_HEADING in body
        assert P1_HEADING not in body

    def test_unknown_audience_gets_core_only(self):
        body = compose_operating_model(LIVE_TREE, "no-such-audience")
        assert body is not None
        assert "Your role —" not in body  # no role view composed
        # Core present — pinned to a v1.3 marker (Deploy Queue realization gate).
        assert "Deploy Queue is the realization gate" in body

    def test_retired_p1_specialists_audience_composes_core_only(self):
        """The p1-specialists audience was superseded (2026-07-17, WTS 2911977a):
        the WS4 resolver never returns it and its composer map entry is gone.
        Even while a card file exists on disk, composing the retired audience
        must yield the shared core only — no caller can silently land on it."""
        body = compose_operating_model(LIVE_TREE, "p1-specialists")
        assert body is not None
        assert "Your role —" not in body
        assert "Deploy Queue is the realization gate" in body

    def test_both_audiences_carry_shared_core(self):
        core = _core_body(LIVE_TREE)
        p1 = compose_operating_model(LIVE_TREE, "p1-default")
        sl = compose_operating_model(LIVE_TREE, "slack-project-agent")
        assert core in p1
        assert core in sl


@_skip_body
class TestByteIdenticalCoreGuard:
    """The enforcement backbone: the core block must be byte-identical across
    both wired audiences. Composition is `core + "\\n\\n" + view`, so the core
    is exactly the prefix up to the first role-view heading; we assert the
    extracted prefixes are byte-equal AND equal to _core.md's body."""

    def _core_prefix(self, composed: str) -> str:
        marker = "\n\n# Your role"
        idx = composed.find(marker)
        assert idx != -1, "composed body must contain a role-view heading"
        return composed[:idx]

    def test_gateway_audiences_share_byte_identical_core(self):
        p1 = compose_operating_model(LIVE_TREE, "p1-default")
        sl = compose_operating_model(LIVE_TREE, "slack-project-agent")
        core_from_p1 = self._core_prefix(p1)
        core_from_sl = self._core_prefix(sl)
        assert core_from_p1 == core_from_sl, (
            "SHARED CORE DIVERGED between p1-default and slack-project-agent "
            "composition — the operating-model cores are out of sync."
        )
        # And the shared prefix must equal the _core.md body verbatim.
        assert core_from_p1 == _core_body(LIVE_TREE)

    @pytest.mark.skipif(
        not SLACK_CT_JS.exists() or shutil.which("node") is None,
        reason="dd-slack-service context-tree.js or node not available",
    )
    def test_cross_loader_core_byte_identical(self):
        """Render the Slack loader's operating-model composition via node and
        assert its core block is byte-identical to the gateway's. This is the
        true cross-loader guard — neither loader can ship a divergent core."""
        gw = compose_operating_model(LIVE_TREE, "p1-default")
        gw_core = self._core_prefix(gw)

        node_script = (
            "const ct=require(process.argv[1]);"
            "const b=ct.composeOperatingModel(process.argv[2]);"
            "process.stdout.write(b);"
        )
        out = subprocess.run(
            ["node", "-e", node_script, str(SLACK_CT_JS), str(LIVE_TREE)],
            capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, f"node compose failed: {out.stderr[:300]}"
        slack_composed = out.stdout
        # Slack composes core + slack view; extract its core prefix the same way.
        marker = "\n\n# Your role"
        idx = slack_composed.find(marker)
        assert idx != -1
        slack_core = slack_composed[:idx]
        assert gw_core == slack_core, (
            "CROSS-LOADER CORE DIVERGED: the gateway and dd-slack-service loaders "
            "embed different operating-model cores — alignment-by-construction broken."
        )


@_skip_body
class TestComposedInjectionEndToEnd:
    def test_build_context_tree_prompt_composes_for_audience(self):
        p1 = build_context_tree_prompt(root=LIVE_TREE, audience="p1-default")
        sl = build_context_tree_prompt(root=LIVE_TREE, audience="slack-project-agent")
        # P1 prompt carries its own role view, not Slack's.
        assert P1_HEADING in p1
        assert SLACK_HEADING not in p1
        # Slack prompt carries Slack's view.
        assert SLACK_HEADING in sl
        # Both still carry the tree header + the shared core boundary (v1.3:
        # the Deploy Queue realization-gate invariant lives in the shared core).
        assert "DecisionData /context tree" in p1
        _core_marker = "Deploy Queue is the realization gate"
        assert _core_marker in p1 and _core_marker in sl
