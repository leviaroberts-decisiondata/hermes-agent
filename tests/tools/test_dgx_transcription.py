"""DGX selection and recording recovery. No recognizer or network runs in these tests."""
import os
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "capture.wav"
    with wave.open(str(p), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(b"\x01\x00" * 16000)
    return p

@pytest.fixture
def dgx(monkeypatch, tmp_path):
    import tools.transcription_tools as stt
    monkeypatch.setattr(stt, "_load_stt_config", lambda: {"provider": "dgx", "enabled": True})
    wrapper = tmp_path / "transcribe.sh"; wrapper.write_text("# test placeholder")
    monkeypatch.setattr(stt, "DGX_WRAPPER_PATH", wrapper)
    for name in ["_transcribe_local", "_transcribe_local_command", "_transcribe_openai", "_transcribe_groq", "_transcribe_mistral", "_transcribe_xai"]:
        monkeypatch.setattr(stt, name, lambda *a, **k: pytest.fail("unexpected recognizer fallback"))
    return stt

@pytest.mark.parametrize("provider", [None, "dgx"])
def test_default_and_explicit_dgx_ignore_installed_recognizers(dgx, monkeypatch, provider):
    monkeypatch.setattr(dgx, "_HAS_FASTER_WHISPER", True)
    monkeypatch.setenv("GROQ_API_KEY", "test-only")
    assert dgx._get_provider({} if provider is None else {"provider": provider}) == "dgx"

def test_dgx_success_uses_fixed_gateway_alias_and_no_shell_interpolation(dgx, monkeypatch, audio):
    def run(argv, **kwargs):
        assert argv == ["/bin/bash", str(dgx.DGX_WRAPPER_PATH), str(audio)]
        assert kwargs.get("shell") is None
        assert kwargs["env"]["TRANSCRIBE_GATEWAY_URL"] == "http://127.0.0.1:8710"
        assert kwargs["env"]["TRANSCRIBE_MODEL"] == "local-transcribe"
        return SimpleNamespace(returncode=0, stdout=" A usable transcript. ")
    monkeypatch.setattr(dgx.subprocess, "run", run)
    result = dgx.transcribe_audio(str(audio), model="base")
    assert result == {"success": True, "transcript": "A usable transcript.", "provider": "dgx", "model": "local-transcribe"}
    assert audio.exists()

@pytest.mark.parametrize("mode", ["missing", "error", "empty", "timeout", "oserror"])
def test_failures_keep_source_and_never_fall_back(dgx, monkeypatch, audio, mode):
    if mode == "missing": dgx.DGX_WRAPPER_PATH.unlink()
    def run(*a, **k):
        if mode == "timeout": raise subprocess.TimeoutExpired(a[0], 1)
        if mode == "oserror": raise OSError("unavailable")
        return SimpleNamespace(returncode=1 if mode == "error" else 0, stdout="partial" if mode == "error" else " ")
    monkeypatch.setattr(dgx.subprocess, "run", run)
    result = dgx.transcribe_audio(str(audio))
    assert result["success"] is False
    assert result["transcript"] == ""
    assert result["provider"] == "dgx"
    assert audio.exists()

def test_failed_capture_retained_privately_outside_temp_cleanup(audio, monkeypatch, tmp_path):
    import tools.voice_mode as voice
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_dir", lambda *a: tmp_path / "profile-audio")
    original = audio.read_bytes()
    retained = Path(voice.finish_recording(str(audio), False))
    assert not audio.exists()
    assert retained.read_bytes() == original
    assert retained.stat().st_mode & 0o777 == 0o600
    assert retained.parent.name == "failed-recordings"
    monkeypatch.setattr(voice, "_TEMP_DIR", str(tmp_path))
    voice.cleanup_temp_recordings(max_age_seconds=-1)
    assert retained.exists()

@pytest.mark.parametrize("success,empty", [(True,False),(True,True),(False,True)])
def test_completed_or_empty_captures_do_not_accumulate(audio, success, empty):
    import tools.voice_mode as voice
    if empty: audio.write_bytes(b"")
    assert voice.finish_recording(str(audio), success) is None
    assert not audio.exists()

def test_storage_failure_leaves_original_untouched(audio, monkeypatch):
    import tools.voice_mode as voice
    monkeypatch.setattr(voice.tempfile, "mkstemp", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert voice.finish_recording(str(audio), False) == str(audio)
    assert audio.exists()

@pytest.mark.parametrize("mode", ["error", "exception", "silence", "text"])
def test_push_to_talk_cleanup_uses_outcome(audio, monkeypatch, tmp_path, mode):
    import hermes_cli.voice as voice
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_dir", lambda *a: tmp_path / "profile-audio")
    monkeypatch.setattr(voice, "_recorder", SimpleNamespace(stop=lambda: str(audio)))
    def transcribe(*a):
        if mode == "exception": raise RuntimeError("unavailable")
        return {"success": mode != "error", "transcript": "The meeting starts at noon." if mode == "text" else ""}
    monkeypatch.setattr(voice, "transcribe_recording", transcribe)
    result = voice.stop_and_transcribe()
    assert bool(result) == (mode == "text")
    retained = list((tmp_path / "profile-audio").glob("failed-recordings/*"))
    assert bool(retained) == (mode in ("error", "exception"))
    assert not audio.exists()
