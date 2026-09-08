"""Automatic ghost cleanup must not mistake legacy-only history for emptiness."""

import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def history_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("ghost", source="tui")
    db.end_session("ghost", "exit")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = ?",
        (time.time() - 172800, "ghost"),
    ))
    yield db
    db.close()


@pytest.mark.parametrize("filename,payload", [
    ("ghost.jsonl", b'{"role":"user","content":"only surviving history"}\n'),
    ("ghost.json", b'{"messages":[{"role":"user","content":"saved history"}]}'),
    ("ghost.jsonl", b""),
    ("ghost.jsonl", b"partial or corrupt history\xff"),
    ("request_dump_ghost_123.json", b'{"messages":["request evidence"]}'),
])
def test_artifact_preserves_session_and_bytes(history_db, tmp_path, filename, payload):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    artifact = sessions / filename
    artifact.write_bytes(payload)

    assert history_db.prune_empty_ghost_sessions(sessions) == 0
    assert history_db.get_session("ghost") is not None
    assert artifact.read_bytes() == payload


def test_dangling_transcript_link_preserves_session(history_db, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    artifact = sessions / "ghost.jsonl"
    artifact.symlink_to(sessions / "missing-target")

    assert history_db.prune_empty_ghost_sessions(sessions) == 0
    assert history_db.get_session("ghost") is not None
    assert artifact.is_symlink()


def test_unknown_or_unreadable_directory_skips_cleanup(history_db, tmp_path, monkeypatch):
    assert history_db.prune_empty_ghost_sessions() == 0
    with monkeypatch.context() as scoped:
        def inaccessible(self):
            raise PermissionError("synthetic permission failure")
        scoped.setattr(Path, "iterdir", inaccessible)
        assert history_db.prune_empty_ghost_sessions(tmp_path / "sessions") == 0
    assert history_db.get_session("ghost") is not None


def test_only_empty_old_ended_untitled_tui_rows_are_pruned(history_db, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for sid in ("with-messages", "titled", "active", "recent", "telegram"):
        history_db.create_session(sid, source="telegram" if sid == "telegram" else "tui")
        if sid != "active":
            history_db.end_session(sid, "exit")
        if sid != "recent":
            history_db._execute_write(lambda conn, sid=sid: conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 172800, sid),
            ))
    history_db.append_message("with-messages", "user", "preserve")
    history_db.set_session_title("titled", "Keep this")
    unrelated = sessions / "another.jsonl"
    unrelated.write_text("unrelated history")

    assert history_db.prune_empty_ghost_sessions(sessions) == 1
    assert history_db.get_session("ghost") is None
    for sid in ("with-messages", "titled", "active", "recent", "telegram"):
        assert history_db.get_session(sid) is not None
    assert unrelated.read_text() == "unrelated history"
    assert history_db.prune_empty_ghost_sessions(sessions) == 0
