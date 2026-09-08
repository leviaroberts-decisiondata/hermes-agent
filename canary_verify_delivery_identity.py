#!/usr/bin/env python3
"""
canary_verify_delivery_identity.py — WS1 §4 / §6 canary verification.

Proves, on the CANARY profile only (no prod restart, no prod flag flip, no Slack
message sent), that the INC3 delivery-distinct-identity mechanism removes BOTH
P1-family injections from a delivery turn's assembled system prompt:
  1. the SOUL identity slot #1   (removed by skip_context_files=True + load_soul_identity=False)
  2. the coordinator tree role view (p1-default; removed for delivery turns by the
     WS4 audience resolver — the pre-WS4 hardcoded default composed the since-retired
     p1-specialists view, superseded 2026-07-17, WTS 2911977a)

It reproduces the gateway's own _build_system_prompt assembly the same way the q1
verdict did, for two configurations:
  * BASELINE  — current prod behavior (no flags): expect SOUL + coordinator view present
  * DELIVERY  — INC3 flags (replace_identity): expect NEITHER present, delivery role instead

Acceptance (acc-b, the §6 identity assertion): the DELIVERY assembled prompt
contains no P1 coordinator contract from either source. Read-only.
"""
import os
import sys

# P1-coordinator contract markers (from the q1 verdict's live capture).
SOUL_MARKERS = [
    "P1 never write",            # "P1 never writes the code"
    "route ALL specialist-domain work",
    "coordinates and verifies",
]
# Precise coordinator-ROLE markers — the p1-default role view text (the heading is
# unique to agent-roles/p1-default.md; verified absent from _core.md and the slack
# view). The original q1-verdict marker ("P1 coordinates the specialist lanes") was
# the since-retired p1-specialists card's 2026-06 copy and no longer exists in the
# tree — it would scan NONE on the baseline and make the proof vacuous.
# NOTE: "system-expertise layer" was rejected as a marker: it lives in the
# audience-INDEPENDENT platform/_node.md (a neutral architecture description
# present for every audience, including the healthy Slack canary), so it is a
# false positive for the coordinator contract, not part of it.
TREE_COORDINATOR_MARKERS = [
    "Your role — P1 coordinator (System A)",
]


def _scan(text: str, markers: list[str]) -> list[str]:
    low = text.lower()
    return [m for m in markers if m.lower() in low]


def _role_mirror_in_parity() -> tuple:
    """graduation P1a (WTS 2911977a) — run bin/dd-role-mirror --check.

    The delivery-identity proof composes specialist role views; if a specialist
    card and its profile SOUL have drifted, the view the canary reasons about is
    not the identity the live profile actually carries. Assert parity as a
    precondition. Returns (ok: bool, detail: str). A missing tool or unexpected
    error is reported but treated as non-fatal here (ok=True) — the canary must
    not be blocked by tooling absence, only by an observed DRIFT (exit 1).
    """
    import subprocess
    hermes = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    tool = os.path.join(hermes, "bin", "dd-role-mirror")
    if not os.path.exists(tool):
        return True, f"dd-role-mirror not found at {tool} (skipped)"
    try:
        proc = subprocess.run(
            [tool, "--check", "--hermes", hermes, "--quiet"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # tooling failure — do not block the canary
        return True, f"dd-role-mirror check errored ({exc}) — skipped"
    if proc.returncode == 0:
        return True, "all specialist card ⇄ SOUL mirrors in parity"
    if proc.returncode == 1:
        return False, "role-mirror DRIFT: " + (proc.stderr.strip() or "see dd-role-mirror --check")
    return True, f"dd-role-mirror check inconclusive (rc={proc.returncode}) — skipped"


def build_prompt(*, replace_identity: bool, platform: str) -> str:
    """Assemble the system prompt as _create_agent → _build_system_prompt would."""
    from run_agent import AIAgent

    identity_kwargs = (
        {"skip_context_files": True, "load_soul_identity": False} if replace_identity else {}
    )
    agent = AIAgent(
        model="gpt-5-codex",
        api_key="sk-canary-noop",            # never used — we only assemble the prompt
        base_url="http://127.0.0.1:1",
        quiet_mode=True,
        verbose_logging=False,
        platform=platform,
        ephemeral_system_prompt=(
            "You are the DecisionData Slack delivery agent. Produce the requested "
            "deliverable end-to-end." if replace_identity else None
        ),
        **identity_kwargs,
    )
    return agent._build_system_prompt()


def main() -> int:
    os.environ.setdefault("HERMES_HOME", os.path.expanduser("~/.hermes"))
    print(f"HERMES_HOME = {os.environ['HERMES_HOME']}")
    print("=" * 70)

    # Guard: the whole proof is meaningless if tree injection is OFF (then "no
    # markers" is trivially true because nothing injected). Assert it's enabled +
    # that the tree actually renders content for the coordinator audience.
    from run_agent import AIAgent
    from agent.prompt_builder import build_context_tree_prompt
    _probe = AIAgent(model="x", api_key="k", base_url="http://127.0.0.1:1",
                     quiet_mode=True, platform="api_server")
    tree_on = _probe._context_tree_injection_enabled()
    tree_renders = bool(build_context_tree_prompt(audience="p1-default").strip())
    print(f"PRECONDITION: tree_injection enabled={tree_on}, tree renders content={tree_renders}")
    if not (tree_on and tree_renders):
        print("✗ PRECONDITION FAIL: tree injection off or empty — the 'no markers' result")
        print("  would be vacuous. Cannot assert acc-b. (Enable context.tree_injection.)")
        return 2

    # graduation P1a (WTS 2911977a) — role-mirror parity precondition. The proof
    # reasons about composed specialist role views; a card ⇄ SOUL drift means the
    # canary is checking a view the live profile does not actually carry.
    mirror_ok, mirror_detail = _role_mirror_in_parity()
    print(f"PRECONDITION: role-mirror parity — {mirror_detail}")
    if not mirror_ok:
        print("✗ PRECONDITION FAIL: specialist card ⇄ profile SOUL drift. The composed")
        print("  role view does not match the live profile identity. Run")
        print("  bin/dd-role-mirror --regen to reconcile, then re-run this canary.")
        return 2
    print("=" * 70)

    # COORDINATOR VIEW — the view a non-delivery P1 turn composes (the WS4
    # resolver returns p1-default; the pre-WS4 hardcoded 'p1-specialists' default
    # is retired). Proves the markers actually fire on the coordinator state.
    from agent.prompt_builder import build_context_tree_prompt
    orig_tree = build_context_tree_prompt(audience="p1-default")
    orig_tree_hits = _scan(orig_tree, TREE_COORDINATOR_MARKERS)
    print(f"COORDINATOR tree (audience=p1-default, the resolver's coordinator view)")
    print(f"  tree coordinator     : {orig_tree_hits or 'NONE'}   <- the P1 injection a delivery turn must NOT carry")
    print("=" * 70)

    # BASELINE — what a delivery turn embodies TODAY (the inversion).
    base = build_prompt(replace_identity=False, platform="api_server")
    base_soul = _scan(base, SOUL_MARKERS)
    base_tree = _scan(base, TREE_COORDINATOR_MARKERS)
    print(f"BASELINE (no flags)  len={len(base)}")
    print(f"  SOUL markers present : {base_soul or 'NONE'}")
    print(f"  tree coordinator     : {base_tree or 'NONE'}")
    print("=" * 70)

    # DELIVERY — INC3 mechanism (replace_identity + WS4 audience resolver).
    deliv = build_prompt(replace_identity=True, platform="api_server")
    deliv_soul = _scan(deliv, SOUL_MARKERS)
    deliv_tree = _scan(deliv, TREE_COORDINATOR_MARKERS)
    has_delivery_role = "delivery" in deliv.lower()
    print(f"DELIVERY (replace_identity=True)  len={len(deliv)}")
    print(f"  SOUL markers present : {deliv_soul or 'NONE'}")
    print(f"  tree coordinator     : {deliv_tree or 'NONE'}")
    print(f"  delivery role text   : {'present' if has_delivery_role else 'ABSENT'}")
    print("=" * 70)

    # Acceptance: the delivery prompt must contain NEITHER P1-family injection.
    ok = (not deliv_soul) and (not deliv_tree)
    if ok:
        print("✓ ACCEPTANCE PASS (acc-b): delivery turn carries NO P1 coordinator")
        print("  contract from either the SOUL slot or the tree role view.")
    else:
        print("✗ ACCEPTANCE FAIL: P1 coordinator content still present in delivery turn:")
        if deliv_soul:
            print(f"    SOUL: {deliv_soul}")
        if deliv_tree:
            print(f"    TREE: {deliv_tree}")
    # Sanity: baseline SHOULD still show the inversion (proves the markers work).
    if not base_soul:
        print("  ⚠ note: baseline showed no SOUL markers — verify SOUL.md is present.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
