"""Tests for P1 deploy_approve policy gate."""
import json
from types import SimpleNamespace
from unittest.mock import patch as mock_patch

import tools.deploy_approve_tool as dat
from tools.deploy_approve_tool import deploy_approve, _REQUIRED_CHECKS


def _resp(status_code=200, payload=None, text=""):
    return SimpleNamespace(status_code=status_code, json=lambda: payload if payload is not None else {}, text=text)


def _entry(**overrides):
    base = {
        "id": "queue-1",
        "service_name": "dd-notification-service",
        "status": "pending",
        "target_commit": "95023c14436baa0df202eb8ac801f1ac2d1d0ece",
        "wts_task_id": "97520d5f-7df1-40cc-af29-726417ab7f69",
        "conflict_flag": False,
        "files_changed": ["app/routes/notify.py"],
        "diff_summary": "low risk service-to-service notification routing",
        "notes": "QA passed",
    }
    base.update(overrides)
    return base


def _checklist():
    return {k: True for k in _REQUIRED_CHECKS}


class TestDarkGate:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("DD_DEPLOY_APPROVE_ENABLED", raising=False)
        out = json.loads(deploy_approve("q1", release_note="r", rollback_note="rb", policy_checklist=_checklist()))
        assert out["ok"] is False
        assert out["reason"] == "approval_gate_disabled"


class TestPolicyPreflight:
    def test_blocks_non_allowlisted_service_before_post(self, monkeypatch):
        monkeypatch.setenv("DD_DEPLOY_APPROVE_ENABLED", "1")
        monkeypatch.setenv("DD_DEPLOY_APPROVE_ALLOWLIST", "dd-notification-service")
        calls = {"post": 0}
        with mock_patch.object(dat, "_fetch_entry", return_value=(_entry(service_name="mc-api"), None)):
            with mock_patch("gateway.capability_egress.post_with_capability", side_effect=lambda *a, **k: calls.__setitem__("post", calls["post"]+1)):
                out = json.loads(deploy_approve("q1", release_note="r", rollback_note="rb", policy_checklist=_checklist()))
        assert out["ok"] is False
        assert out["reason"] == "policy_block"
        assert calls["post"] == 0

    def test_blocks_missing_required_check(self, monkeypatch):
        monkeypatch.setenv("DD_DEPLOY_APPROVE_ENABLED", "1")
        cl = _checklist(); cl["target_commit_verified"] = False
        with mock_patch.object(dat, "_fetch_entry", return_value=(_entry(), None)):
            out = json.loads(deploy_approve("q1", release_note="r", rollback_note="rb", policy_checklist=cl))
        assert out["ok"] is False
        assert "target_commit_verified" in " ".join(out["blockers"])

    def test_blocks_high_risk_heuristic(self, monkeypatch):
        monkeypatch.setenv("DD_DEPLOY_APPROVE_ENABLED", "1")
        with mock_patch.object(dat, "_fetch_entry", return_value=(_entry(files_changed=["app/middleware.ts"]), None)):
            out = json.loads(deploy_approve("q1", release_note="r", rollback_note="rb", policy_checklist=_checklist()))
        assert out["ok"] is False
        assert "high-risk" in out["blockers"][-1]


class TestApprovalPath:
    def test_posts_to_approve_with_capability_after_policy_pass(self, monkeypatch):
        monkeypatch.setenv("DD_DEPLOY_APPROVE_ENABLED", "1")
        captured = {}
        def fake_post(url, *, json_body=None, **kw):
            captured["url"] = url
            captured["body"] = json_body
            return _resp(200, {"status": "deployed", "id": "q1"})
        with mock_patch.object(dat, "_fetch_entry", return_value=(_entry(), None)):
            with mock_patch("gateway.capability_egress.post_with_capability", side_effect=fake_post):
                out = json.loads(deploy_approve(
                    "q1",
                    expected_service_name="dd-notification-service",
                    expected_target_commit="95023c14436baa0df202eb8ac801f1ac2d1d0ece",
                    release_note="ship notification routing",
                    rollback_note="revert commit",
                    policy_checklist=_checklist(),
                ))
        assert out["ok"] is True
        assert captured["url"].endswith("/api/deploy-queue/q1/approve")
        assert captured["body"]["force"] is False
        assert captured["body"]["release_note"] == "ship notification routing"

    def test_403_is_reported_honestly(self, monkeypatch):
        monkeypatch.setenv("DD_DEPLOY_APPROVE_ENABLED", "1")
        with mock_patch.object(dat, "_fetch_entry", return_value=(_entry(), None)):
            with mock_patch("gateway.capability_egress.post_with_capability", return_value=_resp(403, {"detail": "Forbidden", "reason": "no_credential"})):
                out = json.loads(deploy_approve("q1", release_note="r", rollback_note="rb", policy_checklist=_checklist()))
        assert out["ok"] is False
        assert out["denied"] is True
        assert out["status_code"] == 403
        assert out["reason"] == "no_credential"
