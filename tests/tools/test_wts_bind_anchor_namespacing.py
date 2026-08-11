"""WTS anchor identity is namespaced by Hermes instance (WTS 17cbc96c, Phase C).

The anchor store ``~/.hermes/dd-lanes/telegram-anchors.json`` is written by every
Hermes home, because all five run as the same unix user. The key was
``tg:<chat_id>`` and Levi's Telegram chat id is ``8737984752`` in all five bots —
so one key meant five conversations, and a client's binding and P1's binding were
literally the same entry.

New keys are ``tg:<instance>:<chat_id>``. Old keys are legacy and ambiguous: they
are never resolved on a guess, and never deleted (client evidence must stay
reconstructable).

The binder itself is faked here, so no Directus/WTS call is made — and every
"refused" test additionally asserts the binder was never even invoked, which is
what makes the refusal side-effect-free.
"""

import json
import os
import stat
import textwrap

import pytest

import tools.wts_bind_tool as wb

LEVI_CHAT_ID = "8737984752"
INSTANCES = ("default", "classic", "ptg", "azul", "hyperscience")
P1 = "default"


@pytest.fixture(autouse=True)
def _p1_identity(monkeypatch, tmp_path):
    """Run as P1, with the anchor store redirected away from the live file.

    ``_is_p1_internal`` is patched as well as ``active_caller_id``: since
    d96e0165d the boundary asks ``hermes_cli.profiles.is_p1_internal_home()``,
    which reads the REAL ``HERMES_HOME``, so patching the id alone left every
    test in this module refused by the caller boundary rather than exercised.
    """
    monkeypatch.setattr("tools.p1_caller_boundary.active_caller_id", lambda: P1)
    monkeypatch.setattr("tools.p1_caller_boundary._is_p1_internal", lambda: True)
    monkeypatch.setattr(wb, "active_instance", lambda: P1)
    monkeypatch.setattr(wb, "_SHARED_HOME", tmp_path / "shared-home")


@pytest.fixture()
def fake_binder(tmp_path, monkeypatch):
    binder = tmp_path / "dd-wts-bind"
    binder.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        printf '%s\\n' "$@" > "$ARGV_LOG"
        printf 'WTS_TASK_ID=new-task\\nBOUND=created\\nVERIFY=ok\\n'
    """))
    binder.chmod(binder.stat().st_mode | stat.S_IEXEC)
    argv_log = tmp_path / "argv.log"
    monkeypatch.setattr(wb, "_BINDER", binder)
    monkeypatch.setenv("ARGV_LOG", str(argv_log))
    return argv_log


def _write_anchors(anchors):
    path = wb._anchors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(anchors, indent=2), encoding="utf-8")


def _read_anchors():
    return json.loads(wb._anchors_path().read_text(encoding="utf-8"))


def _argv(argv_log):
    return argv_log.read_text(encoding="utf-8").splitlines()


class _Agent:
    def __init__(self, route_key=None):
        if route_key is not None:
            self._dd_route_key = route_key


# ── key shape ────────────────────────────────────────────────────────────────

class TestAnchorKeys:
    def test_new_keys_are_instance_scoped(self):
        assert wb.anchor_key(P1, LEVI_CHAT_ID) == f"tg:{P1}:{LEVI_CHAT_ID}"
        assert wb.anchor_key("azul", LEVI_CHAT_ID, "77") == f"tg:azul:{LEVI_CHAT_ID}:77"

    def test_one_chat_id_yields_five_distinct_identities(self):
        keys = {wb.anchor_key(i, LEVI_CHAT_ID) for i in INSTANCES}
        assert len(keys) == len(INSTANCES), keys
        assert wb.legacy_anchor_key(LEVI_CHAT_ID) not in keys

    def test_binder_receives_the_namespaced_token(self, fake_binder):
        """dd-wts-bind derives ``tg:<--chat>``; the token it gets must already be
        namespaced, otherwise the anchor it writes is the ambiguous legacy one."""
        out = wb.wts_bind(goal="do a thing", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert "WTS_TASK_ID=new-task" in out
        argv = _argv(fake_binder)
        assert argv[argv.index("--chat") + 1] == f"{P1}:{LEVI_CHAT_ID}"

    def test_derived_chat_is_also_namespaced(self, fake_binder):
        wb.wts_bind(goal="x",
                    parent_agent=_Agent(route_key=f"agent:main:telegram:dm:{LEVI_CHAT_ID}"))
        argv = _argv(fake_binder)
        assert argv[argv.index("--chat") + 1] == f"{P1}:{LEVI_CHAT_ID}"


# ── classification ───────────────────────────────────────────────────────────

class TestClassification:
    def test_absent_when_nothing_bound(self):
        status, key = wb.classify_anchor({}, P1, LEVI_CHAT_ID)
        assert status == "absent"
        assert key == f"tg:{P1}:{LEVI_CHAT_ID}"

    def test_namespaced_anchor_is_used(self):
        anchors = {f"tg:{P1}:{LEVI_CHAT_ID}": {"task_id": "t-p1"}}
        assert wb.classify_anchor(anchors, P1, LEVI_CHAT_ID)[0] == "namespaced"

    def test_legacy_without_provenance_is_ambiguous(self):
        anchors = {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}}
        status, key = wb.classify_anchor(anchors, P1, LEVI_CHAT_ID)
        assert status == "legacy_ambiguous"
        assert key == f"tg:{LEVI_CHAT_ID}"

    def test_legacy_owned_by_this_instance_is_migratable(self):
        anchors = {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-p1", "hermes_instance": P1}}
        assert wb.classify_anchor(anchors, P1, LEVI_CHAT_ID)[0] == "legacy_migratable"

    @pytest.mark.parametrize("owner", [i for i in INSTANCES if i != P1])
    def test_legacy_owned_by_another_instance_is_not_ours(self, owner):
        """Known to belong to a client home → not ambiguous, and not ours. We
        bind fresh under our own key and leave theirs untouched."""
        anchors = {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-client", "hermes_instance": owner}}
        status, key = wb.classify_anchor(anchors, P1, LEVI_CHAT_ID)
        assert status == "absent"
        assert key == f"tg:{P1}:{LEVI_CHAT_ID}"

    def test_legacy_is_not_matched_across_instances(self):
        """The same legacy entry is ambiguous for EVERY home, not just P1."""
        anchors = {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}}
        for instance in INSTANCES:
            assert wb.classify_anchor(anchors, instance, LEVI_CHAT_ID)[0] == "legacy_ambiguous"

    def test_thread_scoped_keys_are_classified_independently(self):
        anchors = {f"tg:{P1}:{LEVI_CHAT_ID}": {"task_id": "t-chat"}}
        assert wb.classify_anchor(anchors, P1, LEVI_CHAT_ID, "99")[0] == "absent"


# ── the gate ─────────────────────────────────────────────────────────────────

class TestLegacyFailsClosed:
    def test_legacy_anchor_refuses_and_never_calls_the_binder(self, fake_binder):
        _write_anchors({f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}})
        out = wb.wts_bind(goal="x", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert "REFUSED" in out
        assert f"tg:{LEVI_CHAT_ID}" in out
        assert not fake_binder.exists(), "binder ran despite an ambiguous anchor"
        # nothing was written to the store either
        assert _read_anchors() == {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}}

    def test_legacy_anchor_refuses_resolve_only(self, fake_binder):
        """resolve_only is the dangerous direction: it would CLAIM the other
        home's task as this turn's."""
        _write_anchors({f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}})
        out = wb.wts_bind(goal="", chat=LEVI_CHAT_ID, resolve_only=True, parent_agent=_Agent())
        assert "REFUSED" in out
        assert not fake_binder.exists()

    def test_legacy_anchor_never_resolves_to_p1s_task(self, fake_binder):
        _write_anchors({f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}})
        out = wb.wts_bind(goal="x", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert "t-unknown" not in out
        assert "WTS_TASK_ID" not in out

    def test_unidentified_instance_refuses(self, fake_binder, monkeypatch):
        monkeypatch.setattr(wb, "active_instance", lambda: "")
        out = wb.wts_bind(goal="x", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert "cannot identify its Hermes instance" in out
        assert not fake_binder.exists()

    def test_force_new_binds_fresh_without_touching_the_legacy_entry(self, fake_binder):
        """The documented non-blocking path: create a NEW namespaced task and
        leave the ambiguous entry exactly as it was."""
        _write_anchors({f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}})
        out = wb.wts_bind(goal="fresh unit", chat=LEVI_CHAT_ID, force_new=True,
                          parent_agent=_Agent())
        assert "WTS_TASK_ID=new-task" in out
        argv = _argv(fake_binder)
        assert argv[argv.index("--chat") + 1] == f"{P1}:{LEVI_CHAT_ID}"
        assert _read_anchors()[f"tg:{LEVI_CHAT_ID}"] == {"task_id": "t-unknown"}

    def test_namespaced_anchor_present_binds_normally(self, fake_binder):
        _write_anchors({
            f"tg:{P1}:{LEVI_CHAT_ID}": {"task_id": "t-p1"},
            f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"},
        })
        out = wb.wts_bind(goal="x", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert "WTS_TASK_ID=new-task" in out
        assert fake_binder.exists()


# ── migration from authoritative provenance ──────────────────────────────────

class TestMigration:
    LEGACY = {"task_id": "t-p1", "goal": "old goal", "owner": "DD P1 / Dispatch",
              "hermes_instance": P1}

    def test_migrates_and_records_the_migration(self, fake_binder):
        _write_anchors({f"tg:{LEVI_CHAT_ID}": dict(self.LEGACY)})
        out = wb.wts_bind(goal="continue", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert "WTS_TASK_ID" in out

        anchors = _read_anchors()
        migrated = anchors[f"tg:{P1}:{LEVI_CHAT_ID}"]
        assert migrated["task_id"] == "t-p1"
        assert migrated["migrated_from"] == f"tg:{LEVI_CHAT_ID}"
        assert migrated["migration_provenance"]
        assert migrated["migration_wts"].startswith("17cbc96c")
        assert migrated["migrated_at"]

    def test_legacy_entry_is_preserved_not_deleted(self, fake_binder):
        """Client-agent evidence must stay reconstructable — supersede, never
        delete."""
        _write_anchors({f"tg:{LEVI_CHAT_ID}": dict(self.LEGACY)})
        wb.wts_bind(goal="continue", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        legacy = _read_anchors()[f"tg:{LEVI_CHAT_ID}"]
        assert legacy["task_id"] == "t-p1"
        assert legacy["owner"] == "DD P1 / Dispatch"
        assert legacy["superseded_by"] == f"tg:{P1}:{LEVI_CHAT_ID}"

    def test_migration_is_idempotent(self, fake_binder):
        _write_anchors({f"tg:{LEVI_CHAT_ID}": dict(self.LEGACY)})
        wb.wts_bind(goal="continue", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        first = _read_anchors()
        wb.wts_bind(goal="continue again", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert _read_anchors() == first

    def test_client_anchors_are_untouched_by_a_p1_migration(self, fake_binder):
        """Preserve legitimate ptg-hermes / azul-hermes / hyperscience-hermes
        evidence."""
        client_rows = {
            f"tg:ptg:{LEVI_CHAT_ID}": {"task_id": "2f1a2e80", "owner": "ptg-hermes"},
            f"tg:azul:{LEVI_CHAT_ID}": {"task_id": "0cd630a6", "owner": "azul-hermes"},
            f"tg:hyperscience:{LEVI_CHAT_ID}": {"task_id": "hs-1", "owner": "hyperscience-hermes"},
        }
        _write_anchors({f"tg:{LEVI_CHAT_ID}": dict(self.LEGACY), **client_rows})
        wb.wts_bind(goal="continue", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        after = _read_anchors()
        for key, row in client_rows.items():
            assert after[key] == row, f"client evidence mutated at {key}"

    def test_migration_helper_refuses_without_provenance(self):
        anchors = {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}}
        assert wb.migrate_legacy_anchor(anchors, P1, LEVI_CHAT_ID) is None
        assert anchors == {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown"}}

    def test_migration_helper_refuses_another_instances_entry(self):
        anchors = {f"tg:{LEVI_CHAT_ID}": {"task_id": "t-azul", "hermes_instance": "azul"}}
        assert wb.migrate_legacy_anchor(anchors, P1, LEVI_CHAT_ID) is None


# ── operator visibility for anchor friction (WTS 17cbc96c, review Fix 4) ─────
# A correct refusal that a human cannot act on is indistinguishable from a broken
# tool. Levi asked for this directly: when wts_bind refuses on legacy_ambiguous,
# the message must name the exact key, the exact instance, and the ONE line that
# repairs it — and there must be a way to SEE the friction before hitting it.

class TestRefusalTellsTheHumanWhatToDo:
    def _refusal(self, fake_binder, chat=LEVI_CHAT_ID, thread=None):
        _write_anchors({f"tg:{chat}": {"task_id": "t-unknown"}})
        return wb.wts_bind(goal="x", chat=chat, thread=thread, parent_agent=_Agent())

    def test_it_names_the_exact_anchor_key(self, fake_binder):
        out = self._refusal(fake_binder)
        assert f"tg:{LEVI_CHAT_ID}" in out

    def test_it_names_the_instance_and_the_key_it_would_have_used(self, fake_binder):
        out = self._refusal(fake_binder)
        assert f"({P1})" in out or f"instance ({P1})" in out
        assert f"tg:{P1}:{LEVI_CHAT_ID}" in out

    def test_it_gives_a_single_runnable_repair_line(self, fake_binder):
        out = self._refusal(fake_binder)
        expected = (f"bin/dd-anchor-repair --stamp tg:{LEVI_CHAT_ID} "
                    f"--instance {P1} --evidence")
        assert expected in out, out

    def test_it_points_at_preflight_so_friction_can_be_seen_first(self, fake_binder):
        out = self._refusal(fake_binder)
        assert "--preflight" in out

    def test_it_states_the_non_blocking_escape_hatch(self, fake_binder):
        out = self._refusal(fake_binder)
        assert "force_new=true" in out

    def test_it_says_nothing_was_mutated(self, fake_binder):
        out = self._refusal(fake_binder)
        assert "Nothing was created, resolved or mutated" in out
        assert not fake_binder.exists()

    def test_the_repair_line_carries_the_thread_scoped_key(self, fake_binder):
        """A thread-scoped anchor must not be repaired with the chat-level key."""
        _write_anchors({f"tg:{LEVI_CHAT_ID}:t-99": {"task_id": "t-unknown"}})
        out = wb.wts_bind(goal="x", chat=LEVI_CHAT_ID, thread="t-99",
                          parent_agent=_Agent())
        assert f"--stamp tg:{LEVI_CHAT_ID}:t-99 --instance {P1}" in out


# ── bin/dd-anchor-repair ─────────────────────────────────────────────────────
# In-repo, NOT installed into ~/.hermes/bin: a tool that rewrites provenance
# should be invoked knowingly. Every test here runs against a temp anchor file;
# the live ~/.hermes/dd-lanes/telegram-anchors.json is never opened.

import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
ANCHOR_REPAIR = REPO_ROOT / "bin" / "dd-anchor-repair"


def _repair(*args, cwd=None):
    return subprocess.run([sys.executable, str(ANCHOR_REPAIR), *args],
                          capture_output=True, text=True,
                          cwd=str(cwd) if cwd else str(REPO_ROOT))


@pytest.fixture()
def anchors_file(tmp_path):
    path = tmp_path / "telegram-anchors.json"
    path.write_text(json.dumps({
        # ambiguous: no provenance
        f"tg:{LEVI_CHAT_ID}": {"task_id": "t-unknown", "goal": "old goal",
                               "owner": "Levi Roberts"},
        f"tg:{LEVI_CHAT_ID}:thread-a": {"task_id": "t-thread", "goal": "threaded"},
        # already owned by P1 -> migratable, not ambiguous
        f"tg:{LEVI_CHAT_ID}:owned": {"task_id": "t-p1", "hermes_instance": P1},
        # already owned by a client -> not ours, not ambiguous
        f"tg:{LEVI_CHAT_ID}:client": {"task_id": "t-ptg", "hermes_instance": "ptg"},
        # already namespaced
        f"tg:{P1}:{LEVI_CHAT_ID}:ns": {"task_id": "t-ns"},
    }, indent=2), encoding="utf-8")
    return path


class TestAnchorRepairIsNotInstalled:
    def test_it_lives_in_the_repo_and_is_executable(self):
        assert ANCHOR_REPAIR.is_file()
        assert os.access(ANCHOR_REPAIR, os.X_OK)

    def test_it_was_not_copied_into_the_live_bin(self):
        """Ambient infrastructure is exactly what this must NOT be."""
        assert not (Path.home() / ".hermes" / "bin" / "dd-anchor-repair").exists()


class TestAnchorRepairPreflight:
    def test_it_lists_every_ambiguous_key(self, anchors_file):
        proc = _repair("--preflight", "--instance", P1, "--anchors", str(anchors_file))
        assert proc.returncode == 0, proc.stderr
        assert "WOULD REFUSE: 2" in proc.stdout
        assert f"tg:{LEVI_CHAT_ID}\n" in proc.stdout
        assert f"tg:{LEVI_CHAT_ID}:thread-a" in proc.stdout

    def test_it_does_not_list_keys_that_would_not_refuse(self, anchors_file):
        proc = _repair("--preflight", "--instance", P1, "--anchors", str(anchors_file))
        body = proc.stdout.split("WOULD REFUSE")[1]
        assert f"tg:{LEVI_CHAT_ID}:owned" not in body, "a migratable key was reported"
        assert f"tg:{LEVI_CHAT_ID}:client" not in body, "a client-owned key was reported"

    def test_it_prints_the_repair_command_for_each(self, anchors_file):
        proc = _repair("--preflight", "--instance", P1, "--anchors", str(anchors_file))
        assert (f"--stamp tg:{LEVI_CHAT_ID} --instance {P1}") in proc.stdout
        assert (f"--stamp tg:{LEVI_CHAT_ID}:thread-a --instance {P1}") in proc.stdout

    def test_it_is_instance_relative(self, anchors_file):
        """The same file reads differently from a different home, and the repair
        line it prints must name THAT home — a stamp is per-instance provenance,
        so a command copied from the wrong preflight would record a falsehood."""
        proc = _repair("--preflight", "--instance", "ptg", "--anchors", str(anchors_file))
        assert proc.returncode == 0, proc.stderr
        body = proc.stdout.split("WOULD REFUSE")[1]
        # `client` is ptg's own (hermes_instance=ptg) → migratable, not ambiguous.
        assert f"tg:{LEVI_CHAT_ID}:client" not in body
        # `owned` names default → not ptg's, not ambiguous either.
        assert f"tg:{LEVI_CHAT_ID}:owned" not in body
        assert "--instance ptg" in body
        assert "--instance default" not in body

    def test_chat_filter_narrows_it(self, anchors_file):
        proc = _repair("--preflight", "--instance", P1, "--chat", "1111111111",
                       "--anchors", str(anchors_file))
        assert proc.returncode == 0
        assert "WOULD REFUSE: 0" in proc.stdout

    def test_it_never_writes(self, anchors_file):
        before = anchors_file.read_bytes()
        listing = sorted(p.name for p in anchors_file.parent.iterdir())
        _repair("--preflight", "--instance", P1, "--anchors", str(anchors_file))
        assert anchors_file.read_bytes() == before
        assert sorted(p.name for p in anchors_file.parent.iterdir()) == listing

    def test_it_agrees_with_the_live_gate(self, anchors_file):
        """Preflight calls classify_anchor — the function wts_bind itself calls —
        so the two can never disagree."""
        anchors = json.loads(anchors_file.read_text(encoding="utf-8"))
        refusing = {k for k in anchors
                    if wb.classify_anchor(anchors, P1, *(k.split(":")[1:] + [None])[:2]
                                          )[0] == "legacy_ambiguous"}
        proc = _repair("--preflight", "--instance", P1, "--anchors", str(anchors_file))
        for key in refusing:
            assert key in proc.stdout


class TestAnchorRepairStamp:
    def test_it_refuses_without_evidence(self, anchors_file):
        proc = _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                       "--anchors", str(anchors_file))
        assert proc.returncode == 2
        assert "evidence" in proc.stderr.lower()
        assert "guess" in proc.stderr.lower()

    def test_it_refuses_without_an_instance(self, anchors_file):
        proc = _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--evidence", "x",
                       "--anchors", str(anchors_file))
        assert proc.returncode == 2
        assert "does not guess" in proc.stderr

    def test_it_refuses_to_create_a_key_that_does_not_exist(self, anchors_file):
        before = anchors_file.read_bytes()
        proc = _repair("--stamp", "tg:9999999999", "--instance", P1,
                       "--evidence", "x", "--anchors", str(anchors_file))
        assert proc.returncode == 2
        assert "never creates one" in proc.stderr
        assert anchors_file.read_bytes() == before

    def test_it_records_provenance_and_preserves_everything_else(self, anchors_file):
        before = json.loads(anchors_file.read_text(encoding="utf-8"))
        proc = _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                       "--evidence", "session 2026-06-13 dd-p1 created this",
                       "--anchors", str(anchors_file))
        assert proc.returncode == 0, proc.stderr
        after = json.loads(anchors_file.read_text(encoding="utf-8"))

        assert set(after) == set(before), "a key appeared or disappeared"
        entry = after[f"tg:{LEVI_CHAT_ID}"]
        for field, value in before[f"tg:{LEVI_CHAT_ID}"].items():
            assert entry[field] == value, f"stamping mutated {field}"
        assert entry["hermes_instance"] == P1
        assert entry["instance_provenance"] == "session 2026-06-13 dd-p1 created this"
        assert entry["instance_stamped_by"] == "dd-anchor-repair"
        assert entry["instance_stamp_wts"].startswith("17cbc96c")
        # every other row untouched
        for key in before:
            if key != f"tg:{LEVI_CHAT_ID}":
                assert after[key] == before[key], f"stamping mutated {key}"

    def test_it_is_idempotent(self, anchors_file):
        """A repeat stamp is a NO-OP: same file, no new backup, no new audit row.

        Note the assertions avoid the word "idempotent" — pytest names the tmp
        directory after the test, so that word appears in every path this command
        prints and a substring check on it passes vacuously.
        """
        args = ("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                "--evidence", "e", "--anchors", str(anchors_file))
        assert _repair(*args).returncode == 0
        first = anchors_file.read_bytes()
        backups_before = sorted(
            p.name for p in anchors_file.parent.glob(f"{anchors_file.name}.bak-*"))
        audit = anchors_file.with_name(f"{anchors_file.name}.audit.jsonl")
        rows_before = len(audit.read_text().splitlines())

        second = _repair(*args)
        assert second.returncode == 0
        assert "already records" in second.stdout, second.stdout
        assert "the file was not rewritten" in second.stdout, second.stdout
        assert anchors_file.read_bytes() == first, "a repeat stamp rewrote the file"
        assert sorted(p.name for p in anchors_file.parent.glob(
            f"{anchors_file.name}.bak-*")) == backups_before, "a repeat stamp re-backed-up"
        assert len(audit.read_text().splitlines()) == rows_before, (
            "a repeat stamp appended a second audit row for work it did not do")

    def test_it_refuses_to_overwrite_another_instances_provenance(self, anchors_file):
        before = anchors_file.read_bytes()
        proc = _repair("--stamp", f"tg:{LEVI_CHAT_ID}:client", "--instance", P1,
                       "--evidence", "e", "--anchors", str(anchors_file))
        assert proc.returncode == 2
        assert "never overwritten" in proc.stderr
        assert anchors_file.read_bytes() == before

    def test_it_writes_a_backup(self, anchors_file):
        before = anchors_file.read_bytes()
        _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                "--evidence", "e", "--anchors", str(anchors_file))
        backups = list(anchors_file.parent.glob(f"{anchors_file.name}.bak-anchor-repair-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == before, "the backup is not the previous file"

    def test_it_writes_an_audit_trail(self, anchors_file):
        _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                "--evidence", "the reason", "--anchors", str(anchors_file))
        audit = anchors_file.with_name(f"{anchors_file.name}.audit.jsonl")
        rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
        assert len(rows) == 1
        assert rows[0]["anchor_key"] == f"tg:{LEVI_CHAT_ID}"
        assert rows[0]["instance"] == P1
        assert rows[0]["evidence"] == "the reason"
        assert rows[0]["anchors_sha256_before"] != rows[0]["anchors_sha256_after"]

    def test_the_audit_trail_is_append_only(self, anchors_file):
        _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                "--evidence", "one", "--anchors", str(anchors_file))
        _repair("--stamp", f"tg:{LEVI_CHAT_ID}:thread-a", "--instance", P1,
                "--evidence", "two", "--anchors", str(anchors_file))
        audit = anchors_file.with_name(f"{anchors_file.name}.audit.jsonl")
        rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
        assert [r["evidence"] for r in rows] == ["one", "two"]

    def test_dry_run_changes_nothing(self, anchors_file):
        before = anchors_file.read_bytes()
        listing = sorted(p.name for p in anchors_file.parent.iterdir())
        proc = _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                       "--evidence", "e", "--dry-run", "--anchors", str(anchors_file))
        assert proc.returncode == 0
        assert "DRY RUN" in proc.stdout
        assert anchors_file.read_bytes() == before
        assert sorted(p.name for p in anchors_file.parent.iterdir()) == listing

    def test_it_rejects_a_nonsense_instance_id(self, anchors_file):
        before = anchors_file.read_bytes()
        proc = _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", "../../etc",
                       "--evidence", "e", "--anchors", str(anchors_file))
        assert proc.returncode == 2
        assert anchors_file.read_bytes() == before

    def test_a_stamp_unblocks_the_real_gate(self, anchors_file, fake_binder, monkeypatch):
        """End to end: the refusal names a command, the command runs, and the very
        next wts_bind migrates instead of refusing."""
        monkeypatch.setattr(wb, "_anchors_path", lambda: anchors_file)
        assert "REFUSED" in wb.wts_bind(goal="x", chat=LEVI_CHAT_ID, parent_agent=_Agent())

        proc = _repair("--stamp", f"tg:{LEVI_CHAT_ID}", "--instance", P1,
                       "--evidence", "confirmed from the 2026-06-13 session",
                       "--anchors", str(anchors_file))
        assert proc.returncode == 0, proc.stderr

        out = wb.wts_bind(goal="continue", chat=LEVI_CHAT_ID, parent_agent=_Agent())
        assert "WTS_TASK_ID" in out and "REFUSED" not in out
        after = json.loads(anchors_file.read_text(encoding="utf-8"))
        assert after[f"tg:{P1}:{LEVI_CHAT_ID}"]["task_id"] == "t-unknown"
        # preserved, never deleted
        assert after[f"tg:{LEVI_CHAT_ID}"]["superseded_by"] == f"tg:{P1}:{LEVI_CHAT_ID}"
