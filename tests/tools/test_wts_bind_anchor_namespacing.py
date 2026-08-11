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
import stat
import textwrap

import pytest

import tools.wts_bind_tool as wb

LEVI_CHAT_ID = "8737984752"
INSTANCES = ("default", "classic", "ptg", "azul", "hyperscience")
P1 = "default"


@pytest.fixture(autouse=True)
def _p1_identity(monkeypatch, tmp_path):
    """Run as P1, with the anchor store redirected away from the live file."""
    monkeypatch.setattr("tools.p1_caller_boundary.active_caller_id", lambda: P1)
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
