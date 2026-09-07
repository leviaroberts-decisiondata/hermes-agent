"""The reaper's re-inject authority gate, executed as bash (WTS 17cbc96c, Fix 1/3).

``~/.hermes/bin/dd-lane-reaper`` is a live 0755 bash script OUTSIDE this repo, so
the change is delivered as a reviewable unified diff at
``bin/patches/dd-lane-reaper--authority-gate.patch`` (the same convention as
``dd-lane-run--wake-target-v2.patch``). A patch nobody can run is a promise, not a
fix — so these tests apply it to a COPY of the live file and then execute the
patched bash.

What the patch changes, and why each part is here:

* ``reinject_caller_session()`` appends a lane's closeout to a gateway transcript
  resolved by ``gateway.mirror._find_session_id(platform, chat_id)``. Levi's
  Telegram chat id ``8737984752`` is identical across all five bots, so that
  lookup names a TRANSPORT, not an owner — on 2026-08-10 it put PTG's and Azul's
  closeouts onto P1's transcript. It now consults the run's dispatch-authority
  sidecar first and re-injects ONLY when the recorded ``destination_instance`` is
  the instance that owns this reaper's ``HERMES_HOME``. It fires on exactly the
  paths the wake does not own — ``skipped(disabled)``, ``skipped(no-routing)``,
  ``error(exc)``, ``CONTINUATION_BLOCKED``, a refused wake, or
  ``DD_LANE_EXACTLY_ONCE=0``.

* The gateway's drain writes a processed marker when it QUARANTINES a callback.
  The reaper reads that marker back and used to wrap ANY non-empty outcome as
  ``wake=gateway-accepted(...)``, log it as the canonical injection proof, and
  grade the run ``orch=DONE``. A refusal was therefore reported as the strongest
  success signal the rail has. It now grades ``BLOCKED``.

Nothing here writes to the live script, the live anchors, or any live home: the
harness copies the reaper into tmp and runs with ``HOME`` pointed at tmp, so the
instance identity ``get_active_home_id()`` derives is fully synthetic.
"""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH = REPO_ROOT / "bin" / "patches" / "dd-lane-reaper--authority-gate.patch"
LIVE_REAPER = Path.home() / ".hermes" / "bin" / "dd-lane-reaper"

LEVI_CHAT_ID = "8737984752"
P1 = "default"


# ── applying the patch ───────────────────────────────────────────────────────

def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture(scope="module")
def patched(tmp_path_factory):
    """A COPY of the live reaper with the patch applied. Never touches the original."""
    if not PATCH.is_file():
        pytest.skip(f"patch not found: {PATCH}")
    if not LIVE_REAPER.is_file():
        pytest.skip(f"live reaper not present on this machine: {LIVE_REAPER}")

    work = tmp_path_factory.mktemp("reaper-patch")
    (work / "bin").mkdir()
    shutil.copy(str(LIVE_REAPER), str(work / "bin" / "dd-lane-reaper"))

    assert _git(["init", "-q"], work).returncode == 0
    _git(["add", "-A"], work)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], work)

    check = _git(["apply", "--check", "-p1", str(PATCH)], work)
    assert check.returncode == 0, (
        "the patch no longer applies to the LIVE reaper — it has drifted since the "
        f"patch was written:\n{check.stderr}"
    )
    applied = _git(["apply", "-p1", str(PATCH)], work)
    assert applied.returncode == 0, applied.stderr

    script = work / "bin" / "dd-lane-reaper"
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, f"patched reaper is not valid bash:\n{syntax.stderr}"
    return script


class TestThePatchItself:
    def test_it_applies_cleanly_to_the_live_reaper_and_is_valid_bash(self, patched):
        """Both assertions live in the fixture; this names them as a test."""
        assert patched.is_file()

    def test_the_live_script_is_never_modified(self, patched):
        assert patched.resolve() != LIVE_REAPER.resolve()
        assert os.access(LIVE_REAPER, os.R_OK)


# ── extracting and running individual bash functions ─────────────────────────

def _extract(script: Path, *names: str) -> str:
    """Pull named shell functions out of the patched script, verbatim.

    Function bodies in dd-lane-reaper end with a ``}`` in column 0, which is what
    delimits them here. Verbatim extraction is the point: the harness must run the
    SAME text that will be live, not a re-typed approximation of it.
    """
    lines = script.read_text(encoding="utf-8").splitlines()
    chunks = []
    for name in names:
        starts = [i for i, line in enumerate(lines) if line.startswith(f"{name}() {{")]
        assert len(starts) == 1, f"expected exactly one definition of {name}, got {starts}"
        start = starts[0]
        ends = [j for j in range(start + 1, len(lines)) if lines[j] == "}"]
        assert ends, f"no closing brace for {name}"
        chunks.append("\n".join(lines[start:ends[0] + 1]))
    return "\n\n".join(chunks)


def _run_bash(body: str, *, env=None, cwd=None):
    proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, cwd=str(cwd) if cwd else None)
    return proc


def _harness(patched: Path, *names: str) -> str:
    return textwrap.dedent("""\
        set -uo pipefail
        log() { printf 'LOG %s\\n' "$*" >> "${HARNESS_LOG:-/dev/null}"; }
    """) + _extract(patched, *names) + "\n"


# ── the three pure classifiers ───────────────────────────────────────────────

class TestWakeProofClassification:
    """`wake_injection_proof` reads the gateway's processed marker. Whatever it
    says, a REFUSAL must never be dressed up as the canonical acceptance token."""

    @pytest.mark.parametrize("outcome,expected", [
        ("injected", "wake=gateway-accepted(injected,KEY)"),
        ("claimed", "wake=gateway-accepted(claimed,KEY)"),
        ("already-processed", "wake=gateway-accepted(already-processed,KEY)"),
        # the token this change introduces
        ("refused-quarantine:destination_mismatch",
         "wake=gateway-refused(refused-quarantine:destination_mismatch,KEY)"),
        ("refused-quarantine:session_mismatch",
         "wake=gateway-refused(refused-quarantine:session_mismatch,KEY)"),
        # the token the LIVE gateway wrote before this change, which may still be
        # sitting in .processed markers on disk when the patch lands
        ("quarantined:destination_mismatch",
         "wake=gateway-refused(quarantined:destination_mismatch,KEY)"),
        ("quarantined", "wake=gateway-refused(quarantined,KEY)"),
    ])
    def test_classification(self, patched, outcome, expected):
        proc = _run_bash(_harness(patched, "classify_wake_proof")
                         + f'classify_wake_proof "{outcome}" "KEY"')
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == expected

    @pytest.mark.parametrize("token,refused", [
        ("wake=gateway-refused(refused-quarantine:destination_mismatch,KEY)", True),
        ("wake=gateway-refused(quarantined:stale,KEY)", True),
        ("wake=gateway-accepted(injected,KEY)", False),
        ("wake=emitted(KEY)", False),
        ("wake=skipped(no-routing)", False),
        ("wake=CONTINUATION_BLOCKED(no sidecar)", False),
    ])
    def test_wake_was_refused(self, patched, token, refused):
        proc = _run_bash(_harness(patched, "wake_was_refused")
                         + f'if wake_was_refused "{token}"; then echo REFUSED; '
                           f'else echo NOT-REFUSED; fi')
        assert proc.stdout.strip() == ("REFUSED" if refused else "NOT-REFUSED")

    @pytest.mark.parametrize("token,owns", [
        ("wake=emitted(KEY)", True),
        ("wake=gateway-accepted(injected,KEY)", True),
        ("wake=skipped(dup:KEY)", True),
        ("wake=skipped(processed:KEY)", True),
        # THE POINT: a refused wake delivered nothing, so it cannot own delivery.
        ("wake=gateway-refused(refused-quarantine:destination_mismatch,KEY)", False),
        ("wake=skipped(no-routing)", False),
        ("wake=skipped(disabled)", False),
        ("wake=error(RuntimeError)", False),
        ("wake=CONTINUATION_BLOCKED(no sidecar)", False),
    ])
    def test_wake_owns_delivery(self, patched, token, owns):
        proc = _run_bash(_harness(patched, "wake_owns_delivery")
                         + f'if wake_owns_delivery "{token}"; then echo OWNS; '
                           f'else echo NOT-OWNS; fi')
        assert proc.stdout.strip() == ("OWNS" if owns else "NOT-OWNS")


# ── orchestration grading: a quarantine is BLOCKED, never DONE ───────────────

def _orch_chain(patched: Path) -> str:
    """The elif chain that decides `orch`, lifted verbatim from the patched file."""
    text = patched.read_text(encoding="utf-8")
    start = text.index('      if [[ "$wake_outcome" == wake=CONTINUATION_BLOCKED* ]]; then')
    end = text.index("      fi ;;", start)
    return text[start:end] + "      fi\n"


class TestOrchestrationGrading:
    @pytest.mark.parametrize("wake_outcome,expected", [
        ("wake=gateway-refused(refused-quarantine:destination_mismatch,KEY)", "BLOCKED"),
        ("wake=gateway-refused(quarantined:stale,KEY)", "BLOCKED"),
        ("wake=gateway-accepted(injected,KEY)", "DONE"),
        ("wake=emitted(KEY)", "DONE"),
        ("wake=CONTINUATION_BLOCKED(no sidecar)", "CONTINUATION-BLOCKED"),
    ])
    def test_grading(self, patched, wake_outcome, expected):
        body = (_harness(patched, "wake_was_refused")
                + textwrap.dedent(f"""\
                    wake_outcome='{wake_outcome}'
                    wts_outcome='wts_attach=OK'
                    _had_caller=true
                    mirror_degraded=0
                    orch=UNSET
                    orch_reason=''
                """)
                + _orch_chain(patched)
                + '\nprintf "%s\\n" "$orch"\n')
        proc = _run_bash(body)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == expected

    def test_a_quarantine_beats_the_other_warn_arms(self, patched):
        """Even with a degraded mirror in play, a refusal is not a WARN."""
        body = (_harness(patched, "wake_was_refused")
                + textwrap.dedent("""\
                    wake_outcome='wake=gateway-refused(refused-quarantine:stale,KEY)'
                    wts_outcome='wts_attach=OK'
                    _had_caller=true
                    mirror_degraded=1
                    orch=UNSET
                    orch_reason=''
                """)
                + _orch_chain(patched)
                + '\nprintf "%s|%s\\n" "$orch" "$orch_reason"\n')
        proc = _run_bash(body)
        out = proc.stdout.strip()
        assert out.startswith("BLOCKED|"), out
        assert "QUARANTINED" in out


# ── the real authority gate, executed as bash ────────────────────────────────

def _fake_home(tmp_path, instance):
    """A synthetic Hermes home whose get_active_home_id() is `instance`.

    HOME is redirected into tmp, so ``Path.home()`` — the anchor the whole
    identity derivation uses — is synthetic too. Nothing in the real ~/.hermes is
    read or written.
    """
    home = tmp_path / "home"
    hermes = home / ".hermes" if instance == P1 else home / f".hermes-{instance}"
    hermes.mkdir(parents=True, exist_ok=True)
    return home, hermes


def _run_with_sidecar(patched, tmp_path, *, reaper_instance, record_destination,
                      write_sidecar=True, sidecar_body=None):
    from tools import dispatch_authority as da

    home, hermes = _fake_home(tmp_path, reaper_instance)
    run_dir = hermes / "dd-lanes" / "engineering" / "runs" / "20260810-173216-69340"
    run_dir.mkdir(parents=True, exist_ok=True)
    if sidecar_body is not None:
        (run_dir / da.SIDECAR_NAME).write_text(sidecar_body, encoding="utf-8")
    elif write_sidecar:
        da.write_sidecar(run_dir, da.build_authority(
            caller_instance=record_destination,
            destination_instance=record_destination,
            originating_session_id=f"{record_destination}-sess-1",
            run_id=run_dir.name, run_dir=str(run_dir), lane="engineering",
            platform="telegram", chat_type="dm", chat_id=LEVI_CHAT_ID,
        ))

    body = _harness(patched, "reinject_authorised") + f'reinject_authorised "{run_dir}"'
    proc = _run_bash(body, env={
        "HOME": str(home),
        "HERMES_HOME": str(hermes),
        "AGENT_DIR": str(REPO_ROOT),
        "VENV_PY": os.environ.get("DD_TEST_PYTHON") or _venv_python(),
        "LOG_FILE": str(tmp_path / "reaper.log"),
        "HARNESS_LOG": str(tmp_path / "harness.log"),
        "PYTHONPATH": "",
    }, cwd=tmp_path)  # cwd is NOT the repo: only AGENT_DIR may supply the import
    return proc, run_dir


def _venv_python() -> str:
    import sys
    return sys.executable


class TestReinjectAuthorisedInBash:
    """`reinject_authorised` is a four-line shim onto
    tools.dispatch_authority.reinject_authorisation (unit-tested in
    tests/tools/test_dispatch_authority.py). These prove the SHIM: that the bash
    really reaches that decision, really reports it, and fails closed."""

    def test_p1s_own_run_is_authorised(self, patched, tmp_path):
        proc, _ = _run_with_sidecar(patched, tmp_path,
                                    reaper_instance=P1, record_destination=P1)
        assert proc.stdout.strip() == "ok", (proc.stdout, proc.stderr)

    @pytest.mark.parametrize("client", ["ptg", "azul", "hyperscience", "classic"])
    def test_a_client_run_in_p1s_reaper_is_refused(self, patched, tmp_path, client):
        """The incident on the passive path: the reaper holds a run dispatched by
        a sibling home, and the chat id would resolve P1's session."""
        proc, _ = _run_with_sidecar(patched, tmp_path,
                                    reaper_instance=P1, record_destination=client)
        out = proc.stdout.strip()
        assert out.startswith("skip:destination-"), (out, proc.stderr)
        assert client in out

    def test_the_gate_is_symmetric(self, patched, tmp_path):
        """A PTG reaper must equally not take P1's run."""
        proc, _ = _run_with_sidecar(patched, tmp_path,
                                    reaper_instance="ptg", record_destination=P1)
        assert proc.stdout.strip().startswith("skip:destination-default")

    def test_no_dispatch_record_is_not_authorisation(self, patched, tmp_path):
        proc, _ = _run_with_sidecar(patched, tmp_path, reaper_instance=P1,
                                    record_destination=P1, write_sidecar=False)
        assert proc.stdout.strip() == "skip:no-dispatch-record"

    def test_a_malformed_sidecar_is_not_authorisation(self, patched, tmp_path):
        proc, _ = _run_with_sidecar(patched, tmp_path, reaper_instance=P1,
                                    record_destination=P1, sidecar_body="{not json")
        assert proc.stdout.strip() == "skip:no-dispatch-record"

    def test_a_record_with_no_destination_is_not_authorisation(self, patched, tmp_path):
        body = json.dumps({"schema": "dispatch-authority/1", "caller_instance": "",
                           "destination_instance": "", "originating_session_id": "s"})
        proc, _ = _run_with_sidecar(patched, tmp_path, reaper_instance=P1,
                                    record_destination=P1, sidecar_body=body)
        assert proc.stdout.strip() == "skip:record-incomplete"

    def test_an_unidentifiable_reaper_authorises_nothing(self, patched, tmp_path):
        """HERMES_HOME in an unrecognised layout resolves to no instance at all."""
        from tools import dispatch_authority as da

        home = tmp_path / "home"
        hermes = tmp_path / "somewhere-else"
        run_dir = hermes / "dd-lanes" / "engineering" / "runs" / "20260810-173216-69340"
        run_dir.mkdir(parents=True)
        da.write_sidecar(run_dir, da.build_authority(
            caller_instance=P1, destination_instance=P1,
            originating_session_id="p1-1", run_id=run_dir.name, run_dir=str(run_dir)))
        (home / ".hermes").mkdir(parents=True)

        proc = _run_bash(
            _harness(patched, "reinject_authorised") + f'reinject_authorised "{run_dir}"',
            env={"HOME": str(home), "HERMES_HOME": str(hermes),
                 "AGENT_DIR": str(REPO_ROOT), "VENV_PY": _venv_python(),
                 "LOG_FILE": str(tmp_path / "reaper.log"), "PYTHONPATH": ""},
            cwd=tmp_path)
        assert proc.stdout.strip() == "skip:reaper-instance-unidentified"

    def test_it_fails_closed_when_the_check_cannot_run_at_all(self, patched, tmp_path):
        """A broken AGENT_DIR (no importable tools package) must answer skip, not
        fall through to authorisation."""
        broken = tmp_path / "empty-agent-dir"
        broken.mkdir()
        proc = _run_bash(
            _harness(patched, "reinject_authorised") + 'reinject_authorised "/tmp/x"',
            env={"HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / ".hermes"),
                 "AGENT_DIR": str(broken), "VENV_PY": _venv_python(),
                 "LOG_FILE": str(tmp_path / "reaper.log"), "PYTHONPATH": ""},
            cwd=broken)
        assert proc.stdout.strip() == "skip:authority-check-unavailable"

    def test_it_never_prints_a_chat_id_or_a_session(self, patched, tmp_path):
        proc, _ = _run_with_sidecar(patched, tmp_path,
                                    reaper_instance=P1, record_destination="azul")
        assert LEVI_CHAT_ID not in proc.stdout
        assert "azul-sess-1" not in proc.stdout


# ── the caller: reinject_caller_session must obey the gate ───────────────────

class TestReinjectCallerSessionObeysTheGate:
    """These stub `reinject_authorised` deliberately — the decision itself is
    tested above and in the unit tests; what is under test HERE is the control
    flow of the function that performs the transcript write."""

    def _run(self, patched, tmp_path, verdict):
        # VENV_PY is the ONLY way this function can reach gateway.mirror. Point it
        # at a tripwire: if it runs, the mirror leg was reached.
        tripwire = tmp_path / "mirror-was-called"
        stub_py = tmp_path / "stub-python"
        stub_py.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            cat > /dev/null
            printf 'CALLED\\n' >> "{tripwire}"
            printf 'reinject_ok=True\\n'
        """), encoding="utf-8")
        stub_py.chmod(0o755)

        body = (_harness(patched, "reinject_caller_session")
                + textwrap.dedent(f"""\
                    reinject_authorised() {{ printf '%s\\n' '{verdict}'; }}
                    reinject_caller_session telegram {LEVI_CHAT_ID} "" "CLOSEOUT BODY" \\
                        "/tmp/dd-lanes/engineering/runs/20260810-173216-69340"
                """))
        proc = _run_bash(body, env={
            "VENV_PY": str(stub_py), "AGENT_DIR": str(REPO_ROOT),
            "HERMES_HOME": str(tmp_path / ".hermes"),
            "LOG_FILE": str(tmp_path / "reaper.log"),
            "HARNESS_LOG": str(tmp_path / "harness.log"),
        })
        return proc, tripwire, tmp_path / "harness.log"

    def test_an_unauthorised_run_never_reaches_the_mirror(self, patched, tmp_path):
        proc, tripwire, harness_log = self._run(
            patched, tmp_path, "skip:destination-ptg-not-default")
        assert not tripwire.exists(), (
            "the closeout was appended to this gateway's transcript despite the "
            "run being addressed to another instance")
        assert "reinject_skipped=destination-ptg-not-default" in proc.stdout
        # The log line is distinct and NOT alarming — a skip is correct behaviour.
        log = harness_log.read_text(encoding="utf-8")
        assert "REINJECT-SKIPPED" in log
        assert "reason=destination-ptg-not-default" in log
        assert "durable on WTS" in log
        for alarming in ("ERROR", "FAIL", "CRITICAL"):
            assert alarming not in log, f"a correct skip logged {alarming!r}"

    def test_the_skip_token_is_machine_readable_by_the_call_site(self, patched, tmp_path):
        """The call site greps `reinject_(ok|err|skipped)=` out of this output; the
        reason has to survive that grep so the reap marker carries it."""
        proc, _, _ = self._run(patched, tmp_path, "skip:no-dispatch-record")
        grep = _run_bash(
            "printf '%s' \"$RJ\" | grep -oE 'reinject_(ok|err|skipped)=[^ ]*' | head -1",
            env={"RJ": proc.stdout})
        assert grep.stdout.strip() == "reinject_skipped=no-dispatch-record"

    def test_an_authorised_run_still_reaches_the_mirror(self, patched, tmp_path):
        """NON-REGRESSION: the gate must not close on P1's own results."""
        proc, tripwire, _ = self._run(patched, tmp_path, "ok")
        assert tripwire.exists(), "the gate blocked a run it had authorised"
        assert "reinject_ok=True" in proc.stdout

    @pytest.mark.parametrize("verdict", [
        "skip:no-dispatch-record",
        "skip:record-incomplete",
        "skip:reaper-instance-unidentified",
        "skip:authority-check-unavailable",
        "",                       # the check produced nothing at all
        "unexpected garbage",     # anything that is not exactly "ok"
    ])
    def test_only_the_word_ok_opens_the_gate(self, patched, tmp_path, verdict):
        _, tripwire, _ = self._run(patched, tmp_path, verdict)
        assert not tripwire.exists(), f"verdict {verdict!r} was treated as authorisation"


# ── the call site wiring ─────────────────────────────────────────────────────

class TestCallSiteWiring:
    def test_the_run_dir_is_actually_passed_to_the_gate(self, patched):
        """A gate that never receives the run dir cannot gate anything."""
        text = patched.read_text(encoding="utf-8")
        assert ('reinject_caller_session "$platform" "$chat_id" "$thread_id" '
                '"$closeout" "$rd"') in text

    def test_the_exactly_once_arm_uses_the_shared_classifier(self, patched):
        """The old inline `case` listed `wake=gateway-accepted(*`, which a
        `gateway-*` glob would silently re-widen to include refusals."""
        text = patched.read_text(encoding="utf-8")
        assert 'if wake_owns_delivery "$wake_outcome"; then _wake_owns=true; fi' in text
        assert "wake=gateway-refused" not in _extract(patched, "wake_owns_delivery")
