"""Operating-model split: per-audience composition + the byte-identical-core guard.

The operating-model node is split into a shared `_core.md` (the inter-layer
boundary, single source of truth) plus per-audience role views. Each wired
loader (gateway = p1-specialists; dd-slack-service = slack-project-agent)
composes `core + its own role view`. These tests prove:

  1. Composition is per-audience (each gets ITS role view, not the other's).
  2. The shared-core block is BYTE-IDENTICAL across both wired audiences — the
     enforcement backbone that makes desync structurally impossible. This is the
     guard that must stay green.
  3. No role view leaks another audience's role framing.

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

REPO_ROOT = Path(__file__).resolve().parents[2]
# The live tree (home-anchored). Tests read it read-only; if absent, skip.
LIVE_TREE = Path.home() / ".hermes" / "context"
SLACK_CT_JS = Path.home() / "apps" / "dd-slack-service" / "src" / "context-tree.js"


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


pytestmark = pytest.mark.skipif(
    not (LIVE_TREE / "operating-model" / "_core.md").exists(),
    reason="operating-model split tree not present",
)


class TestPerAudienceComposition:
    def test_p1_gets_stewardship_view_not_slack(self):
        body = compose_operating_model(LIVE_TREE, "p1-specialists")
        assert body is not None
        assert "Your role — System expertise & maintenance (Layer 3)" in body
        assert "Your role — Product execution (Layer 2)" not in body

    def test_slack_gets_product_view_not_p1(self):
        body = compose_operating_model(LIVE_TREE, "slack-project-agent")
        assert body is not None
        assert "Your role — Product execution (Layer 2)" in body
        assert "Your role — System expertise & maintenance (Layer 3)" not in body

    def test_unknown_audience_gets_core_only(self):
        body = compose_operating_model(LIVE_TREE, "no-such-audience")
        assert body is not None
        assert "Your role —" not in body  # no role view composed
        assert "never via live agent-to-agent comms" in body  # core present

    def test_both_audiences_carry_shared_core(self):
        core = _core_body(LIVE_TREE)
        p1 = compose_operating_model(LIVE_TREE, "p1-specialists")
        sl = compose_operating_model(LIVE_TREE, "slack-project-agent")
        assert core in p1
        assert core in sl


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
        p1 = compose_operating_model(LIVE_TREE, "p1-specialists")
        sl = compose_operating_model(LIVE_TREE, "slack-project-agent")
        core_from_p1 = self._core_prefix(p1)
        core_from_sl = self._core_prefix(sl)
        assert core_from_p1 == core_from_sl, (
            "SHARED CORE DIVERGED between p1-specialists and slack-project-agent "
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
        gw = compose_operating_model(LIVE_TREE, "p1-specialists")
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


class TestComposedInjectionEndToEnd:
    def test_build_context_tree_prompt_composes_for_audience(self):
        p1 = build_context_tree_prompt(root=LIVE_TREE, audience="p1-specialists")
        sl = build_context_tree_prompt(root=LIVE_TREE, audience="slack-project-agent")
        # P1 prompt carries its own role view, not Slack's.
        assert "System expertise & maintenance (Layer 3)" in p1
        assert "Your role — Product execution (Layer 2)" not in p1
        # Slack prompt carries Slack's view.
        assert "Your role — Product execution (Layer 2)" in sl
        # Both still carry the tree header + the shared core boundary.
        assert "DecisionData /context tree" in p1
        assert "never via live agent-to-agent comms" in p1 and "never via live agent-to-agent comms" in sl
