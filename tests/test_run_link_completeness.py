#!/usr/bin/env python3
"""WS-5 run-link completeness — unit proof for the slack-surface run derivation.

Reviewer residual 1 (verbatim): "Status/stage parity is fixed. But run evidence parity
still has runs 1 < expected 2. If PG becomes authority, run/evidence completeness
matters."

Root cause: _derive_runs gated a lane_run on `h.get("run_dir")` being truthy. A
SLACK-surface run (Model A progress-follow path) produces NO local run dir — it replies
in-thread — so a slack-only chain (e.g. no-task:d1d1a618) derived 0 lane_runs while its
ledger genuinely recorded 2. Same class as the status-head sync gap (5c3e113): a sync
condition silently excluding a category of real records.

Fix: when run_dir is empty, derive a lane_run keyed on the Slack thread ts embedded in
`detail` (stable + unique per dispatch), falling back to stage:dispatched_at so two
empty-run_dir hops never collide on an empty key.

Pure unit test — no PG, no network. The external helper is not shipped by this
repository. Set DD_CHAIN_PG_MODULE to an explicit reviewed dd_chain_pg.py path
before running this proof. Missing opt-in skips pytest collection; an explicitly
configured missing module is an error.
"""
import importlib.util
import os
import sys

# This deployment-spine helper is maintained outside the Hermes repository.
# Never discover/import the live ~/.hermes/bin copy implicitly during repo tests.
MODULE_PATH = os.environ.get("DD_CHAIN_PG_MODULE")
if not MODULE_PATH:
    reason = "external dd_chain_pg.py proof requires explicit DD_CHAIN_PG_MODULE"
    if __name__ == "__main__":
        raise SystemExit(reason)
    import pytest
    pytest.skip(reason, allow_module_level=True)

# An explicitly configured missing module is an error, not an optional skip.
spec = importlib.util.spec_from_file_location("dd_chain_pg", MODULE_PATH)
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


def _lane_runs(rec):
    return [r for r in pg._derive_runs(rec) if r["run_kind"] == "lane_run"]


def main():
    fails = []

    def check(cond, msg):
        print(("  ✅ " if cond else "  ❌ ") + msg)
        if not cond:
            fails.append(msg)

    print("== WS-5 run-link completeness proof ==")

    # 1. The exact d1d1a618 shape: 2 slack runs, empty run_dir, ts in detail.
    rec = {
        "chain_id": "no-task:UNITTEST", "history": [
            {"kind": "run", "stage": "engineering", "run_dir": "", "gate": "PASS",
             "dispatched_at": 1780627425, "closed_at": 1780627425,
             "detail": "[engineering] PASS | ... ts=1780627396.281489 reply_ts=1780627424.4"},
            {"kind": "run", "stage": "qa", "run_dir": "", "gate": "PASS",
             "dispatched_at": 1780627483, "closed_at": 1780627483,
             "detail": "[qa] PASS | WARN | ... ts=1780627426.025139 reply_ts=1780627482.5"},
        ]}
    runs = _lane_runs(rec)
    check(len(runs) == 2, f"two slack runs derived (got {len(runs)}) — the 1<2 gap closed")
    ids = [r["source_record_id"] for r in runs]
    check(len(set(ids)) == 2, f"source_record_ids are UNIQUE (got {ids}) — no empty-key collision")
    check(ids[0] == "slack:engineering:1780627396.281489", f"engineering keyed on its thread ts (got {ids[0]})")
    check(ids[1] == "slack:qa:1780627426.025139", f"qa keyed on its thread ts (got {ids[1]})")
    check(all(r["status"] == "PASS" for r in runs), "gate PASS carried into run status")
    check(all(r["metadata"].get("surface") == "slack" for r in runs), "metadata marks surface=slack")

    # 2. A run with NO ts in detail falls back to stage:dispatched_at (still unique).
    rec2 = {"chain_id": "x", "history": [
        {"kind": "run", "stage": "engineering", "run_dir": "", "gate": "PASS",
         "dispatched_at": 111, "closed_at": 111, "detail": "no thread ts here"},
        {"kind": "run", "stage": "engineering", "run_dir": "", "gate": "PASS",
         "dispatched_at": 222, "closed_at": 222, "detail": "also none"},
    ]}
    r2 = _lane_runs(rec2)
    ids2 = [r["source_record_id"] for r in r2]
    check(len(set(ids2)) == 2, f"fallback key stays unique per dispatch (got {ids2})")
    check(ids2 == ["slack:engineering:111", "slack:engineering:222"],
          f"fallback = slack:stage:dispatched_at (got {ids2})")

    # 3. A local-run-dir hop is UNCHANGED (basename key, no slack: prefix).
    rec3 = {"chain_id": "x", "history": [
        {"kind": "run", "stage": "engineering",
         "run_dir": "/Users/openclaw/.hermes/dd-lanes/engineering/runs/20260604-202353-64365",
         "gate": "PASS", "dispatched_at": 1, "closed_at": 2}]}
    r3 = _lane_runs(rec3)
    check(len(r3) == 1 and r3[0]["source_record_id"] == "20260604-202353-64365",
          f"local run_dir still keyed on basename (got {[r['source_record_id'] for r in r3]})")
    check(r3[0]["metadata"].get("run_dir", "").endswith("20260604-202353-64365"),
          "local run carries its run_dir in metadata")

    # 4. A chain with NO run history derives 0 lane_runs (e.g. a parked/scoping chain).
    check(len(_lane_runs({"chain_id": "x", "history": []})) == 0,
          "no run history -> 0 lane_runs")

    print()
    if fails:
        print(f"RESULT: {len(fails)} CHECK(S) FAILED")
        return 1
    print("RESULT: ALL CHECKS PASSED — slack-surface run-link completeness closed.")
    return 0


def test_run_link_completeness():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
