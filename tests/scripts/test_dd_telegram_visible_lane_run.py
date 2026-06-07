from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "bin" / "dd-telegram-visible-lane-run"


def test_telegram_visible_lane_run_has_valid_bash_syntax():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


def test_telegram_visible_lane_run_does_not_fallback_to_stale_lane_log_task():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'wts_task="$wts_log_task"' not in text
    assert 'wts_source="standing-lane-log"' not in text
    assert 'wts_task="$DD_WTS_TASK_ID"; wts_source="env-bound-task"' in text
    assert "Do NOT fall back to the standing per-lane log task" in text


def test_telegram_visible_lane_run_registers_pending_runs_with_reaper():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'REAPER="$HERMES_BIN/dd-lane-reaper"' in text
    assert "register_pending_with_reaper" in text
    assert '"$REAPER" --register "$rd" "$session_key"' in text
    assert 'register_pending_with_reaper "$result_file" "$run_code"' in text
