"""Board finding 2026-07-15 (canary ae201016): the deploy submit sent the WTS
link only in the JSON body while the API reads the x-wts-task-id HEADER — the
row persisted with wts_task_id=null, the tool still reported 'linked', and
dedupe reused the linkless row. Contract now: send the header AND verify by
authoritative row readback; a non-persisted link is a typed WTS_LINK_MISMATCH.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import tools.deploy_submit_tool as dst
from gateway import capability_egress as _ce

WTS = "aaaaaaaa-0000-0000-0000-000000000001"


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def _drive(monkeypatch, row_readback):
    captured = {}

    def fake_post(url, json_body=None, extra_headers=None):
        captured["headers"] = extra_headers
        captured["body"] = json_body
        return _Resp(200, {"id": 42, "status": "pending"})

    monkeypatch.setattr(_ce, "post_with_capability", fake_post)

    class _Client:
        def __init__(self, timeout=None): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            captured["readback_url"] = url
            return _Resp(200, row_readback)

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    out = json.loads(dst.deploy_submit(
        service_name="app-starter-kit", wts_task_id=WTS,
        parent_agent=SimpleNamespace()))
    return out, captured


def test_header_sent_and_link_verified(monkeypatch):
    out, cap = _drive(monkeypatch, {"id": 42, "status": "pending", "wts_task_id": WTS})
    assert cap["headers"] == {"x-wts-task-id": WTS}
    assert cap["body"]["wts_task_id"] == WTS  # belt: body still carries it
    assert out["wts_link"] == "verified"
    assert "42" in cap["readback_url"]


def test_null_row_link_is_typed_mismatch(monkeypatch):
    out, cap = _drive(monkeypatch, {"id": 42, "status": "pending", "wts_task_id": None})
    assert out["wts_link"] == "WTS_LINK_MISMATCH"
    assert "WTS_LINK_MISMATCH" in out["message"]
    assert "Do NOT report this deploy as tracker-linked" in out["message"]
    assert out["row_readback"]["wts_task_id"] is None


def test_no_wts_no_header(monkeypatch):
    captured = {}

    def fake_post(url, json_body=None, extra_headers=None):
        captured["headers"] = extra_headers
        return _Resp(200, {"id": 7, "status": "pending"})

    monkeypatch.setattr(_ce, "post_with_capability", fake_post)

    class _Client:
        def __init__(self, timeout=None): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            return _Resp(200, {"id": 7, "status": "pending", "wts_task_id": None})

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    out = json.loads(dst.deploy_submit(service_name="app-starter-kit",
                                       parent_agent=SimpleNamespace()))
    assert captured["headers"] is None
    assert "wts_link" not in out or out.get("wts_link") != "WTS_LINK_MISMATCH"
