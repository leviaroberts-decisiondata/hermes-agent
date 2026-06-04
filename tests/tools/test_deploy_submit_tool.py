"""Tests for the deploy_submit tool (P-E / B-4a + B-4c).

Proves the SUBMIT-ONLY contract and the dark-ship gate WITHOUT going live:
  - the tool is withheld from every surface until DD_DEPLOY_SUBMIT_ENABLED=1
    (and that gate does not corrupt the proven deploy_approve/transition path);
  - it POSTs ONLY to /api/deploy-queue/submit (never /approve, /transition, …);
  - it assembles the minimal B-4c package: service_name + diff fields + the
    turn's bound WTS task id (inherited from parent_agent._dd_wts_task_id),
    one-lines diff_stat, and leaves pre_deploy_test empty;
  - it returns a STRING (json) on every path (the tool-result pipeline slices
    the result — a dict would crash);
  - it reports a 403 capability denial honestly instead of faking success.

The live A-or-B-credential gate behavior (signed cap → 200, same cap → 403 at
/approve, no cap → 403) is proven end-to-end against mc-api :8502 in the P-E
live contract test; these are the in-process unit-level guarantees.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

import tools.deploy_submit_tool as dst
from tools.deploy_submit_tool import deploy_submit, _deploy_submit_enabled


def _resp(status_code=200, payload=None, text=""):
    """A minimal httpx.Response stand-in."""
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: (payload if payload is not None else {}),
        text=text,
    )


class TestDarkShipGate:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("DD_DEPLOY_SUBMIT_ENABLED", raising=False)
        assert _deploy_submit_enabled() is False

    def test_only_literal_one_enables(self, monkeypatch):
        for val in ("", "0", "true", "yes", "2", " 1 x"):
            monkeypatch.setenv("DD_DEPLOY_SUBMIT_ENABLED", val)
            assert _deploy_submit_enabled() is False, val
        monkeypatch.setenv("DD_DEPLOY_SUBMIT_ENABLED", "1")
        assert _deploy_submit_enabled() is True
        # whitespace-padded "1" still enables (we .strip())
        monkeypatch.setenv("DD_DEPLOY_SUBMIT_ENABLED", "  1  ")
        assert _deploy_submit_enabled() is True

    def test_registered_but_hidden_when_dark(self, monkeypatch):
        """The tool is registered (wired) but excluded from definitions when dark,
        and the proven deploy_approve/transition path stays visible + available."""
        from tools.registry import registry, discover_builtin_tools, invalidate_check_fn_cache
        discover_builtin_tools()
        # registered (wired)
        assert registry._tools.get("deploy_submit") is not None
        # our per-tool gate did NOT become the toolset-level check
        assert registry._toolset_checks.get("deploy") is None
        assert registry.is_toolset_available("deploy") is True

        monkeypatch.delenv("DD_DEPLOY_SUBMIT_ENABLED", raising=False)
        invalidate_check_fn_cache()
        names = {
            (d.get("function", {}).get("name") or d.get("name"))
            for d in registry.get_definitions(
                {"deploy_submit", "deploy_approve", "deploy_transition"}, quiet=True
            )
        }
        assert "deploy_submit" not in names          # dark
        assert "deploy_approve" in names             # proven path unaffected
        assert "deploy_transition" in names

        monkeypatch.setenv("DD_DEPLOY_SUBMIT_ENABLED", "1")
        invalidate_check_fn_cache()
        names_on = {
            (d.get("function", {}).get("name") or d.get("name"))
            for d in registry.get_definitions({"deploy_submit"}, quiet=True)
        }
        assert "deploy_submit" in names_on           # enabled by the one flag
        invalidate_check_fn_cache()


class TestRequiredFields:
    def test_missing_service_name_errors(self):
        out = deploy_submit("")
        assert isinstance(out, str)
        assert json.loads(out)["error"]

    def test_whitespace_service_name_errors(self):
        assert json.loads(deploy_submit("   "))["error"]


class TestPackageAssembly:
    def _capture(self):
        """Patch the egress and capture the posted url + body."""
        captured = {}

        def fake_post(url, *, json_body=None, **kw):
            captured["url"] = url
            captured["body"] = json_body
            return _resp(200, {"id": 7, "status": "pending", "conflict_flag": False})

        return captured, fake_post

    def test_posts_only_to_submit_endpoint(self):
        captured, fake = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api")
        assert captured["url"].endswith("/api/deploy-queue/submit")
        # SUBMIT-ONLY: the tool must never construct an approve/transition/execute URL
        for forbidden in ("/approve", "/transition", "/execute", "/reject"):
            assert forbidden not in captured["url"]

    def test_inherits_bound_wts_task_from_parent_agent(self):
        captured, fake = self._capture()
        bound = "11111111-2222-3333-4444-555555555555"
        agent = SimpleNamespace(_dd_wts_task_id=bound, session_id="sid-9")
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api", parent_agent=agent)
        assert captured["body"]["wts_task_id"] == bound
        assert captured["body"]["agent_session_id"] == "sid-9"

    def test_explicit_wts_overrides_bound(self):
        captured, fake = self._capture()
        bound = "11111111-2222-3333-4444-555555555555"
        explicit = "99999999-8888-7777-6666-555555555555"
        agent = SimpleNamespace(_dd_wts_task_id=bound)
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api", wts_task_id=explicit, parent_agent=agent)
        assert captured["body"]["wts_task_id"] == explicit

    def test_no_wts_when_neither_present(self):
        captured, fake = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api")  # no parent_agent, no explicit id
        assert "wts_task_id" not in captured["body"]

    def test_diff_stat_is_one_lined(self):
        captured, fake = self._capture()
        multiline = "3 files changed, 40 insertions(+), 5 deletions(-)\nEXTRA PROSE LINE\nmore"
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api", diff_stat=multiline)
        assert "\n" not in captured["body"]["diff_stat"]
        assert captured["body"]["diff_stat"] == "3 files changed, 40 insertions(+), 5 deletions(-)"

    def test_pre_deploy_test_always_empty(self):
        captured, fake = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api", test_results="42 passed", notes="ran full suite")
        # prose chokes the shell runner; mc-api auto-resolves the real test cmd
        assert captured["body"]["pre_deploy_test"] == ""
        # test_results is a JSON column → wrapped as an object, never a bare
        # string (a bare string 500s Directus).
        assert captured["body"]["test_results"] == {"summary": "42 passed"}
        assert captured["body"]["notes"] == "ran full suite"

    def test_test_results_omitted_when_empty(self):
        captured, fake = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api")  # no test_results
        # left NULL (not an empty object/string) when the agent supplies none
        assert "test_results" not in captured["body"]

    def test_files_changed_normalized_from_list(self):
        captured, fake = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api", files_changed=["a.py", "  b.py ", "", "c.py"])
        assert captured["body"]["files_changed"] == ["a.py", "b.py", "c.py"]

    def test_files_changed_normalized_from_string(self):
        captured, fake = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api", files_changed="a.py, b.py\nc.py")
        assert captured["body"]["files_changed"] == ["a.py", "b.py", "c.py"]

    def test_non_uuid_wts_dropped_not_500(self):
        """A non-UUID bound id must be DROPPED (fail-soft), not sent — the
        deploy_queue.wts_task_id column is a UUID and a bad value 500s the whole
        submit."""
        captured, fake = self._capture()
        agent = SimpleNamespace(_dd_wts_task_id="not-a-uuid-slug-123")
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            parsed = json.loads(deploy_submit("mc-api", parent_agent=agent))
        assert "wts_task_id" not in captured["body"]   # not sent
        assert parsed["wts_link_dropped"] is True
        assert "WTS" in parsed["message"]

    def test_valid_uuid_wts_is_sent(self):
        captured, fake = self._capture()
        good = "726d6afd-a429-4d50-940b-d60c4bed3b85"
        agent = SimpleNamespace(_dd_wts_task_id=good)
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            parsed = json.loads(deploy_submit("mc-api", parent_agent=agent))
        assert captured["body"]["wts_task_id"] == good
        assert parsed["wts_link_dropped"] is False

    def test_target_commit_only_sent_when_present(self):
        captured, fake = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api")
        assert "target_commit" not in captured["body"]
        captured2, fake2 = self._capture()
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake2):
            deploy_submit("mc-api", target_commit="deadbeef")
        assert captured2["body"]["target_commit"] == "deadbeef"


class TestReturnContract:
    def test_success_returns_id_status_string(self):
        def fake(url, *, json_body=None, **kw):
            return _resp(200, {"id": 42, "status": "pending", "conflict_flag": False})
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            out = deploy_submit("mc-api")
        assert isinstance(out, str)
        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["id"] == 42
        assert parsed["status"] == "pending"
        assert "id=42" in parsed["message"]

    def test_conflict_flag_surfaced(self):
        def fake(url, *, json_body=None, **kw):
            return _resp(200, {
                "id": 5, "status": "pending", "conflict_flag": True,
                "conflict_details": {"type": "hard", "overlapping_files": []},
            })
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            parsed = json.loads(deploy_submit("mc-api"))
        assert parsed["conflict_flag"] is True
        assert "conflict" in parsed["message"].lower()
        assert parsed["conflict_details"]["type"] == "hard"

    def test_403_reports_denial_honestly(self):
        def fake(url, *, json_body=None, **kw):
            return _resp(403, {"detail": "Forbidden", "reason": "no_credential"})
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            parsed = json.loads(deploy_submit("mc-api"))
        assert parsed["ok"] is False
        assert parsed["denied"] is True
        assert parsed["status_code"] == 403
        assert parsed["reason"] == "no_credential"
        # must NOT pretend it queued
        assert "queued" not in json.dumps(parsed).lower()

    def test_other_4xx_not_faked_as_success(self):
        def fake(url, *, json_body=None, **kw):
            return _resp(400, {"detail": "service_name is required"})
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            parsed = json.loads(deploy_submit("mc-api"))
        assert parsed["ok"] is False
        assert parsed["status_code"] == 400

    def test_transport_error_returns_error_string(self):
        def fake(url, *, json_body=None, **kw):
            raise RuntimeError("connection refused")
        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            out = deploy_submit("mc-api")
        assert isinstance(out, str)
        assert "connection refused" in json.loads(out)["error"]


class TestEgressCredentialContract:
    """The tool must go through capability_egress (which attaches the contextvar
    credential in-process), never a raw shell curl. Asserting it calls
    post_with_capability is the in-process proof of the credential path."""

    def test_uses_post_with_capability(self):
        calls = {"n": 0}

        def fake(url, *, json_body=None, **kw):
            calls["n"] += 1
            return _resp(200, {"id": 1, "status": "pending"})

        with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake):
            deploy_submit("mc-api")
        assert calls["n"] == 1
