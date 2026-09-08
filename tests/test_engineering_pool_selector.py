import json
import os
from pathlib import Path

from tools.engineering_pool_selector import (
    classify_run_dir,
    select_engineering_lane,
    recovery_reconciliation_gate,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path, lane: str, run_id: str, files: dict[str, str]) -> Path:
    run_dir = root / lane / "runs" / run_id
    for name, text in files.items():
        _write(run_dir / name, text)
    return run_dir


def test_classify_prefers_actual_success_over_synthetic_stall(tmp_path):
    run_dir = _run(tmp_path, "engineering", "20260608-124031-29737", {
        "exit_code": "0\n",
        "reaped": "mode=stalled exit_code=124\n",
        "STALLED": "soft budget exceeded\n",
        "stdout.log": "Gate: PASS\nPUBLISH-READY: branch=build-leg/x commit=abc cwd=/repo\n",
    })

    state = classify_run_dir(run_dir, now=1_717_000_000)

    assert state.status == "idle"
    assert state.exit_code == 0
    assert state.gate == "PASS"
    assert state.has_publish_ready is True
    assert state.has_conflict is True
    assert state.canonical == "actual_exit_stdout"


def test_selects_engineer_2_when_engineer_1_busy(tmp_path):
    _run(tmp_path, "engineering", "20260608-110034-24027", {
        "pid": "999999\n",
        "stdout.log": "still working\n",
    })

    selection = select_engineering_lane(lanes_root=tmp_path, now=1_717_000_000)

    assert selection.selected_lane == "engineering-2"
    assert selection.selected_agent == "dd-engineer-2"
    assert selection.status == "selected"
    assert "Engineer 1" in selection.reason
    assert selection.lanes[0].status in {"busy", "busy_or_suspect", "over_budget"}


def test_selects_engineer_3_when_first_two_busy(tmp_path):
    _run(tmp_path, "engineering", "20260608-110034-24027", {"pid": "999999\n"})
    _run(tmp_path, "engineering-2", "20260608-110044-24028", {"pid": "999998\n"})

    selection = select_engineering_lane(lanes_root=tmp_path, now=1_717_000_000)

    assert selection.selected_lane == "engineering-3"
    assert selection.selected_agent == "dd-engineer-3"
    assert selection.status == "selected"


def test_all_busy_returns_no_capacity_without_affinity(tmp_path):
    _run(tmp_path, "engineering", "20260608-110034-24027", {"pid": "999999\n"})
    _run(tmp_path, "engineering-2", "20260608-110044-24028", {"pid": "999998\n"})
    _run(tmp_path, "engineering-3", "20260608-110054-24029", {"pid": "999997\n"})

    selection = select_engineering_lane(lanes_root=tmp_path, now=1_717_000_000)

    assert selection.status == "no_capacity"
    assert selection.selected_lane is None
    assert "queue" in selection.reason.lower()


def test_affinity_reason_allows_reusing_active_engineer(tmp_path):
    _run(tmp_path, "engineering", "20260608-110034-24027", {"pid": "999999\n"})

    selection = select_engineering_lane(
        lanes_root=tmp_path,
        preferred_lane="engineering",
        affinity_reason="same SDM settings surface and branch continuity",
        now=1_717_000_000,
    )

    assert selection.status == "selected_with_affinity"
    assert selection.selected_lane == "engineering"
    assert "same SDM settings surface" in selection.reason


def test_live_run_age_uses_meta_started_at_not_marker_mtime(tmp_path):
    old_started_at = 1_716_998_000
    run_dir = _run(tmp_path, "engineering", "20260608-110034-24027", {
        "pid": f"{os.getpid()}\n",
        "meta.json": json.dumps({"started_at": old_started_at}),
        "OVER_BUDGET": "marker written recently by reaper\n",
    })
    os.utime(run_dir, (1_716_999_990, 1_716_999_990))

    state = classify_run_dir(run_dir, now=1_717_000_000, soft_budget_seconds=20 * 60)

    assert state.age_seconds == 2000
    assert state.status == "over_budget"
    assert "over soft budget" in "; ".join(state.reasons)


def test_recovery_gate_recommends_reconcile_for_newer_pass_same_wts_surface(tmp_path):
    _run(tmp_path, "engineering", "20260608-121941-67661", {
        "packet.md": "WTS: 93b11f2b-a8ac-4524-bfc7-8b1be14f6bac\nSurface: SDM approvals\n",
        "exit_code": "124\n",
        "reaped": "mode=stalled exit_code=124\n",
    })
    _run(tmp_path, "engineering-2", "20260608-130121-89973", {
        "packet.md": "WTS: 93b11f2b-a8ac-4524-bfc7-8b1be14f6bac\nSurface: SDM approvals\n",
        "exit_code": "0\n",
        "stdout.log": "Gate: PASS\nPUBLISH-READY: branch=build-leg/x commit=abc cwd=/repo\n",
    })

    decision = recovery_reconciliation_gate(
        wts_task="93b11f2b-a8ac-4524-bfc7-8b1be14f6bac",
        surface="SDM approvals",
        lanes_root=tmp_path,
        now=1_717_000_000,
    )

    assert decision.recommendation == "reconcile_before_recovery"
    assert any(r.lane == "engineering-2" and r.gate == "PASS" for r in decision.matches)
    assert "PASS" in decision.reason
