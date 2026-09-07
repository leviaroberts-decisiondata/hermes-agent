from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
# Resolve the wrapper from the SHARED ~/.hermes/bin first (where the live runtime
# lives). The legacy ROOT/bin path is a pre-R4 stale copy that the wrapper does
# NOT load from — it just used to satisfy the old static-text tests.
_SHARED_BIN = Path("/Users/openclaw/.hermes/bin")
SCRIPT = (
    _SHARED_BIN / "dd-telegram-visible-lane-run"
    if (_SHARED_BIN / "dd-telegram-visible-lane-run").is_file()
    else ROOT / "bin" / "dd-telegram-visible-lane-run"
)


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


def test_telegram_visible_lane_run_preserves_topic_thread_for_reaper_registration():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'thread = parts[5] if len(parts) > 5 else ""' in text


def test_telegram_visible_lane_run_passes_wts_task_to_lane_run():
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"$LANE_RUN" --lane "$lane" --agent "$agent" --packet "$packet" --wts-task "$wts_task"' in text
    assert '"$LANE_RUN" --lane "$lane" --agent "$agent" --packet "$packet" >"$result_file"' in text


def test_telegram_visible_lane_run_message_labels_are_reviewable():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "WTS: ${tracker_url}" in text
    assert "Handoff file: ${hf_file_id}" in text
    assert "Result file: ${rf_file_id:-<attach-unverified>}" in text
    assert "WTS task: ${tracker_url}" not in text


# ── Phase 3 (canary 72abe1ee 2026-06-17): exact-target verification segment ──
#
# The Telegram and Slack wrappers must extract the verified URL/host:port targets
# and a browser-contradicts flag from the lane result body and append a
# ``| targets=verified=A+B;contradicts=0|1`` segment to the normalized stdout
# status line. route_to_lane reads that segment to downgrade a clean PASS to
# CHANGED-NOT-ACCEPTED when the user named an acceptance target the lane never
# verified, or to block on a browser DOM contradiction even with green deploy
# proof. The result body NEVER leaves the wrapper, so this segment is the only
# channel for the comparison.
def test_telegram_wrapper_carries_targets_segment_block():
    text = SCRIPT.read_text(encoding="utf-8")
    # The wrapper extracts verified targets from $result_file and emits a
    # `targets=verified=…;contradicts=…` segment when present.
    assert "Phase 3 (canary 72abe1ee 2026-06-17)" in text
    assert "targets_segment=" in text
    assert "| targets=${targets_segment}" in text
    # The bash echo line that prints the normalized status line concatenates the
    # targets segment after the wts status; empty segment → omitted.
    assert "${wts_status}${targets_seg}" in text


SLACK_SCRIPT = (
    _SHARED_BIN / "dd-visible-lane-run"
    if (_SHARED_BIN / "dd-visible-lane-run").is_file()
    else ROOT / "bin" / "dd-visible-lane-run"
)


def test_slack_wrapper_also_carries_targets_segment_block():
    text = SLACK_SCRIPT.read_text(encoding="utf-8")
    assert "Phase 3 (canary 72abe1ee 2026-06-17)" in text
    assert "targets_segment=" in text
    assert "${wts_seg}${pending_seg}${targets_seg}" in text


def _bash_extract_targets_segment(tmp_path, result_body):
    """Run JUST the wrapper's Phase-3 python block against a synthetic result
    file and return what it prints. Isolates the extraction logic from all the
    wrapper's Telegram/WTS plumbing."""
    import subprocess
    result_file = tmp_path / "result.md"
    result_file.write_text(result_body, encoding="utf-8")
    # Pull the Phase 3 PY_TARGETS heredoc out of the wrapper and run it.
    text = SCRIPT.read_text(encoding="utf-8")
    # The heredoc opener has trailing shell tokens (``2>/dev/null || true``) on
    # the same line — skip past the newline after that to land on the python.
    open_token = "<<'PY_TARGETS'"
    open_at = text.index(open_token)
    body_start = text.index("\n", open_at) + 1
    body_end = text.index("\nPY_TARGETS\n", body_start) + 1
    py = text[body_start:body_end]
    proc = subprocess.run(
        ["python3", "-c", py, str(result_file)],
        capture_output=True, text=True, timeout=30,
    )
    return proc.stdout.strip()


def test_targets_segment_extracts_verified_urls(tmp_path):
    body = (
        "## Engineering result\n"
        "Gate: PASS\n"
        "Verified: http://100.94.241.120:8641/settings ok\n"
        "Browser: http://100.94.241.120:8641/ai matches new UI\n"
    )
    seg = _bash_extract_targets_segment(tmp_path, body)
    assert "verified=" in seg
    assert "http://100.94.241.120:8641/settings" in seg
    assert "http://100.94.241.120:8641/ai" in seg
    assert "contradicts=0" in seg


def test_targets_segment_flags_browser_contradiction(tmp_path):
    body = (
        "## Engineering result\n"
        "Verified: http://100.94.241.120:8641/settings ok\n"
        "Browser: http://100.94.241.120:8641/settings DOES NOT MATCH — still old Settings\n"
    )
    seg = _bash_extract_targets_segment(tmp_path, body)
    assert "contradicts=1" in seg


def test_targets_segment_empty_when_no_targets(tmp_path):
    body = "## Result\nGate: PASS\nWrote the explainer.\n"
    seg = _bash_extract_targets_segment(tmp_path, body)
    # No verified targets, no contradiction → segment is empty (wrapper omits it).
    assert seg == ""
