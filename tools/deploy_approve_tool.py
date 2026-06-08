"""deploy_approve — P1/System-A deploy-queue approval authority.

This is the sanctioned client for P1 deploy approval. It is intentionally more
than a thin POST wrapper: approval immediately triggers mc-api's deploy pipeline,
so this tool performs a local policy preflight before it sends the C4-gated
request through gateway.capability_egress.

Security model:
- mc-api remains the protected resource and requires a signed C4 capability.
- the credential is attached only in-process via capability_egress, never via
  shell/env/curl.
- specialist / delivery turns do not mint System-A C4 and are denied by mc-api.
- this tool refuses high-risk deploys before it ever calls /approve.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from tools.registry import registry, tool_error

_MC_API_BASE = os.getenv("MC_API_BASE_URL", "http://127.0.0.1:8502")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_DEFAULT_ALLOWLIST = "dd-notification-service,slack-demo-manager,dd-slack-service"
_REQUIRED_CHECKS = (
    "wts_evidence_ready",
    "qa_or_test_evidence_passed",
    "rollback_note_ready",
    "release_note_ready",
    "no_conflict",
    "target_commit_verified",
    "no_secrets_or_env_changes",
    "no_database_migrations",
    "no_auth_or_security_changes",
    "no_external_comms_or_customer_side_effects",
    "blast_radius_low",
)
# Human/C4-reviewed deploys may intentionally carry a known high-risk class
# (for example credential-management UI/storage). P1 may proceed only when the
# human review is explicit, auditable, and scoped to a specific queue item.
_MIN_C4_REVIEW_REASON_CHARS = 24
_HIGH_RISK_FIELDS = {
    "secrets", "secret", "env", "environment", "credentials", "credential",
    "auth", "security", "database", "db", "migration", "finance", "payment",
    "client_sensitive", "customer_comms", "external_comms", "broad_slack",
    "incident", "emergency", "unknown_blast_radius", "destructive",
}


def _deploy_approve_enabled() -> bool:
    """Explicit live gate for P1 approval authority.

    deploy_approve stays registered so P1 can see the sanctioned path, but it
    will not approve anything unless this flag is set in the P1 gateway process.
    This avoids accidental authority on non-P1/specialist surfaces while still
    letting mc-api be the final C4 gate.
    """
    return os.getenv("DD_DEPLOY_APPROVE_ENABLED", "").strip() == "1"


def _allowlist() -> set[str]:
    raw = os.getenv("DD_DEPLOY_APPROVE_ALLOWLIST", _DEFAULT_ALLOWLIST)
    return {p.strip() for p in raw.split(",") if p.strip()}


def _json_response(ok: bool, **fields) -> str:
    fields.setdefault("ok", ok)
    return json.dumps(fields, sort_keys=True)


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "pass", "passed", "ok"}
    return bool(v)


def _normalize_checklist(policy_checklist) -> dict[str, bool]:
    if policy_checklist is None:
        return {}
    if isinstance(policy_checklist, dict):
        return {str(k): _as_bool(v) for k, v in policy_checklist.items()}
    if isinstance(policy_checklist, str):
        txt = policy_checklist.strip()
        if not txt:
            return {}
        try:
            parsed = json.loads(txt)
            if isinstance(parsed, dict):
                return {str(k): _as_bool(v) for k, v in parsed.items()}
        except Exception:
            pass
        # Accept newline/comma separated names as affirmative only when the
        # caller deliberately lists the exact check keys.
        keys = re.split(r"[,\n]+", txt)
        return {k.strip(): True for k in keys if k.strip()}
    return {}


def _normalize_high_risk_flags(high_risk_flags) -> list[str]:
    if high_risk_flags is None:
        return []
    if isinstance(high_risk_flags, str):
        txt = high_risk_flags.strip()
        if not txt:
            return []
        try:
            parsed = json.loads(txt)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in re.split(r"[,\n]+", txt) if x.strip()]
    if isinstance(high_risk_flags, (list, tuple, set)):
        return [str(x).strip() for x in high_risk_flags if str(x).strip()]
    return [str(high_risk_flags).strip()]


def _looks_high_risk(entry: dict) -> list[str]:
    findings: list[str] = []
    files = entry.get("files_changed") or []
    if isinstance(files, str):
        files = [files]
    for f in files if isinstance(files, list) else []:
        low = str(f).lower()
        if any(part in low for part in [".env", "secret", "credential", "auth", "middleware", "migration", "schema", "directus"]):
            findings.append(f"file:{f}")
    text = " ".join(str(entry.get(k) or "") for k in ("diff_summary", "notes", "deploy_summary"))
    low_text = text.lower()
    for token in _HIGH_RISK_FIELDS:
        if token in low_text:
            findings.append(f"text:{token}")
    return findings[:20]


def _fetch_entry(entry_id: str) -> tuple[dict | None, str | None]:
    import httpx
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{_MC_API_BASE}/api/deploy-queue/{entry_id}")
        if resp.status_code >= 400:
            return None, f"entry read failed: HTTP {resp.status_code}: {resp.text[:300]}"
        payload = resp.json()
        if not isinstance(payload, dict):
            return None, "entry read returned non-object payload"
        return payload, None
    except Exception as e:
        return None, f"entry read failed: {type(e).__name__}: {e}"


def _validate_policy(entry: dict, *, expected_service_name: str, expected_target_commit: str,
                     release_note: str, rollback_note: str, policy_checklist,
                     high_risk_flags, force: bool, c4_review_confirmed: bool = False,
                     c4_review_reason: str = "") -> tuple[bool, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    service = str(entry.get("service_name") or "").strip()
    status = str(entry.get("status") or "").strip()
    target_commit = str(entry.get("target_commit") or "").strip()
    wts_task_id = str(entry.get("wts_task_id") or "").strip()

    if force:
        blockers.append("force approval is not allowed for P1 autonomous approval")
    if status != "pending":
        blockers.append(f"queue item status is {status!r}, expected 'pending'")
    allow = _allowlist()
    if service not in allow:
        blockers.append(f"service {service!r} is not in DD_DEPLOY_APPROVE_ALLOWLIST ({sorted(allow)})")
    if expected_service_name and service != expected_service_name.strip():
        blockers.append(f"expected_service_name mismatch: entry has {service!r}")
    if not wts_task_id or not _UUID_RE.match(wts_task_id):
        blockers.append("queue item is missing a valid WTS task id")
    if entry.get("conflict_flag") is True:
        blockers.append("queue item conflict_flag is true")
    if not target_commit or not _SHA_RE.match(target_commit):
        blockers.append("queue item is missing a valid target_commit")
    if expected_target_commit and target_commit.lower() != expected_target_commit.strip().lower():
        blockers.append("expected_target_commit does not match queue item target_commit")
    if not release_note.strip():
        blockers.append("release_note is required")
    if not rollback_note.strip():
        blockers.append("rollback_note is required")

    checks = _normalize_checklist(policy_checklist)
    missing_checks = [k for k in _REQUIRED_CHECKS if checks.get(k) is not True]
    if missing_checks:
        blockers.append("missing/false policy checks: " + ", ".join(missing_checks))

    flags = _normalize_high_risk_flags(high_risk_flags)
    heuristics = _looks_high_risk(entry)
    high_risk_findings = []
    if flags:
        high_risk_findings.extend(f"caller:{f}" for f in flags)
    if heuristics:
        high_risk_findings.extend(heuristics)
    if high_risk_findings:
        if c4_review_confirmed and len(c4_review_reason.strip()) >= _MIN_C4_REVIEW_REASON_CHARS:
            warnings.append("C4-reviewed high-risk deploy allowed: " + ", ".join(high_risk_findings[:20]))
        else:
            blockers.append(
                "entry appears high-risk and needs explicit human/C4 review "
                f"(set c4_review_confirmed with an auditable reason): "
                + ", ".join(high_risk_findings[:20])
            )

    return not blockers, blockers, warnings


DEPLOY_APPROVE_SCHEMA = {
    "name": "deploy_approve",
    "description": (
        "Approve and execute a low-risk deploy queue item as P1/System-A through "
        "the sanctioned C4 capability path. This tool first reads the queue item "
        "and refuses approval unless the P1 policy checklist passes, the service "
        "is allowlisted, WTS evidence exists, there is no conflict, a target commit "
        "is present, and no high-risk class is declared/detected. It then calls "
        "mc-api /approve via gateway capability egress. Specialist lanes lack C4 "
        "and will be denied by mc-api."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entry_id": {"type": "string", "description": "Deploy queue entry id to approve."},
            "expected_service_name": {"type": "string", "description": "Optional service-name readback to prevent approving the wrong item."},
            "expected_target_commit": {"type": "string", "description": "Optional target commit readback to prevent stale/wrong commit approval."},
            "release_note": {"type": "string", "description": "One-line release note to record with the approval."},
            "rollback_note": {"type": "string", "description": "Concrete rollback plan or revert note."},
            "policy_checklist": {
                "type": "object",
                "description": "All required boolean policy checks must be true: " + ", ".join(_REQUIRED_CHECKS),
                "additionalProperties": {"type": "boolean"},
            },
            "high_risk_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Any known high-risk classes. If non-empty, approval is refused and escalated.",
            },
            "decided_by": {"type": "string", "description": "Audit actor. Default dd-p1-system-a."},
            "force": {"type": "boolean", "description": "Must remain false. P1 autonomous approval refuses force=true."},
            "c4_review_confirmed": {"type": "boolean", "description": "Set true only when Levi/C4 explicitly reviewed and approved known high-risk classes for this exact queue item."},
            "c4_review_reason": {"type": "string", "description": "Auditable human/C4 review rationale for high-risk approval; required when c4_review_confirmed is true."},
        },
        "required": ["entry_id", "release_note", "rollback_note", "policy_checklist"],
    },
}


def deploy_approve(entry_id: str, *, expected_service_name: str = "", expected_target_commit: str = "",
                   release_note: str = "", rollback_note: str = "", policy_checklist=None,
                   high_risk_flags=None, decided_by: str = "dd-p1-system-a", force: bool = False,
                   c4_review_confirmed: bool = False, c4_review_reason: str = "") -> str:
    if not entry_id:
        return tool_error("entry_id is required")
    if not _deploy_approve_enabled():
        return _json_response(False, denied=True, reason="approval_gate_disabled",
                              message="DD_DEPLOY_APPROVE_ENABLED is not 1 in this gateway process")

    entry, err = _fetch_entry(entry_id)
    if err:
        return _json_response(False, reason="entry_read_failed", message=err)
    assert entry is not None

    ok, blockers, warnings = _validate_policy(
        entry,
        expected_service_name=expected_service_name,
        expected_target_commit=expected_target_commit,
        release_note=release_note,
        rollback_note=rollback_note,
        policy_checklist=policy_checklist,
        high_risk_flags=high_risk_flags,
        force=force,
        c4_review_confirmed=bool(c4_review_confirmed),
        c4_review_reason=c4_review_reason or "",
    )
    if not ok:
        return _json_response(False, denied=True, reason="policy_block", blockers=blockers,
                              entry={k: entry.get(k) for k in ("id", "service_name", "status", "target_commit", "wts_task_id", "conflict_flag")},
                              message="P1 deploy approval refused by policy; escalate to Levi/C4 or fix evidence.")

    from gateway import capability_egress
    url = f"{_MC_API_BASE}/api/deploy-queue/{entry_id}/approve"
    body = {
        "decided_by": decided_by or "dd-p1-system-a",
        "force": False,
        "release_note": release_note.strip(),
        "rollback_note": rollback_note.strip(),
        "policy_checklist": _normalize_checklist(policy_checklist),
        "c4_review_confirmed": bool(c4_review_confirmed),
        "c4_review_reason": (c4_review_reason or "").strip(),
    }
    try:
        resp = capability_egress.post_with_capability(url, json_body=body, timeout=120.0)
    except Exception as e:
        return tool_error(f"deploy-queue approval request failed: {type(e).__name__}: {e}")

    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text[:1000]}

    if resp.status_code == 403:
        return _json_response(False, denied=True, status_code=403, detail=payload.get("detail"),
                              reason=payload.get("reason"),
                              message="DENIED by capability gate — this turn lacks System-A C4 deploy approval.")
    if resp.status_code >= 400:
        return _json_response(False, status_code=resp.status_code, result=payload,
                              message="mc-api refused or failed deploy approval")
    return _json_response(True, status_code=resp.status_code, result=payload,
                          approved_entry_id=entry_id,
                          service_name=entry.get("service_name"),
                          target_commit=entry.get("target_commit"),
                          wts_task_id=entry.get("wts_task_id"),
                          warnings=warnings,
                          message="Deploy approved through P1 policy gate and C4 capability path; mc-api deploy pipeline was invoked.")



def _fetch_pending_entries_for_service(service_name: str) -> tuple[list[dict], str | None]:
    import httpx
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{_MC_API_BASE}/api/deploy-queue", params={"status": "pending", "service": service_name, "limit": 50})
        if resp.status_code >= 400:
            return [], f"pending read failed: HTTP {resp.status_code}: {resp.text[:300]}"
        payload = resp.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return [], "pending read returned non-list items"
        return [x for x in items if isinstance(x, dict)], None
    except Exception as e:
        return [], f"pending read failed: {type(e).__name__}: {e}"


DEPLOY_RESOLVE_CONFLICT_GROUP_SCHEMA = {
    "name": "deploy_resolve_conflict_group",
    "description": (
        "Resolve a pending deploy conflict group through the existing mc-api C4-gated "
        "group approval rail: approve exactly one target queue item and reject/supersede "
        "older pending items for the same service. This is not a direct DB mutation; it "
        "posts a conflict-resolution object to /api/deploy-queue/group/{service}/approve "
        "via gateway capability egress. High-risk targets still require explicit C4 review fields."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "description": "Deploy service whose pending group is being resolved."},
            "approve_entry_id": {"type": "string", "description": "The single pending queue item to approve/deploy."},
            "expected_target_commit": {"type": "string", "description": "Expected target commit for the approved entry."},
            "reject_entry_ids": {"type": "array", "items": {"type": "string"}, "description": "Pending queue items to reject as superseded."},
            "resolution_reason": {"type": "string", "description": "Auditable reason for approving one item and rejecting the others."},
            "release_note": {"type": "string", "description": "One-line release note for the approved item."},
            "rollback_note": {"type": "string", "description": "Rollback/revert plan for the approved item."},
            "policy_checklist": {"type": "object", "additionalProperties": {"type": "boolean"}, "description": "Required policy checks for the approved item."},
            "high_risk_flags": {"type": "array", "items": {"type": "string"}},
            "c4_review_confirmed": {"type": "boolean", "description": "True only when Levi/C4 explicitly approved the high-risk class for this exact target."},
            "c4_review_reason": {"type": "string", "description": "Auditable C4 review rationale."},
            "decided_by": {"type": "string", "description": "Audit actor. Default dd-p1-system-a."},
        },
        "required": ["service_name", "approve_entry_id", "expected_target_commit", "reject_entry_ids", "resolution_reason", "release_note", "rollback_note", "policy_checklist"],
    },
}


def deploy_resolve_conflict_group(service_name: str, approve_entry_id: str, *, expected_target_commit: str = "",
                                  reject_entry_ids=None, resolution_reason: str = "",
                                  release_note: str = "", rollback_note: str = "", policy_checklist=None,
                                  high_risk_flags=None, c4_review_confirmed: bool = False,
                                  c4_review_reason: str = "", decided_by: str = "dd-p1-system-a") -> str:
    if not _deploy_approve_enabled():
        return _json_response(False, denied=True, reason="approval_gate_disabled",
                              message="DD_DEPLOY_APPROVE_ENABLED is not 1 in this gateway process")
    service_name = (service_name or "").strip()
    approve_entry_id = (approve_entry_id or "").strip()
    reject_entry_ids = [str(x).strip() for x in (reject_entry_ids or []) if str(x).strip()]
    if not service_name or not approve_entry_id:
        return tool_error("service_name and approve_entry_id are required")
    if not reject_entry_ids:
        return tool_error("reject_entry_ids must name at least one superseded pending item")
    if len(resolution_reason.strip()) < _MIN_C4_REVIEW_REASON_CHARS:
        return _json_response(False, denied=True, reason="missing_resolution_reason",
                              message="resolution_reason must be specific/auditable")

    entries, err = _fetch_pending_entries_for_service(service_name)
    if err:
        return _json_response(False, reason="pending_read_failed", message=err)
    entry_map = {str(e.get("id")): e for e in entries}
    target = entry_map.get(approve_entry_id)
    if not target:
        return _json_response(False, denied=True, reason="approve_entry_not_pending",
                              pending_ids=sorted(entry_map), message="approved entry is not pending for this service")
    missing_rejects = [eid for eid in reject_entry_ids if eid not in entry_map]
    if missing_rejects:
        return _json_response(False, denied=True, reason="reject_entry_not_pending",
                              missing_reject_entry_ids=missing_rejects)

    ok, blockers, warnings = _validate_policy(
        target,
        expected_service_name=service_name,
        expected_target_commit=expected_target_commit,
        release_note=release_note,
        rollback_note=rollback_note,
        policy_checklist=policy_checklist,
        high_risk_flags=high_risk_flags,
        force=False,
        c4_review_confirmed=bool(c4_review_confirmed),
        c4_review_reason=c4_review_reason or resolution_reason,
    )
    # The whole point of this tool is resolving a known conflict group, so a
    # conflict_flag on the selected item is allowed when every other pending item
    # for the service is explicitly rejected/superseded in the same C4-gated call.
    conflict_blockers = [b for b in blockers if b == "queue item conflict_flag is true"]
    blockers = [b for b in blockers if b != "queue item conflict_flag is true"]
    if conflict_blockers:
        warnings.append("conflict_flag accepted because conflict group resolution rejects superseded entries atomically")
    unresolved = sorted(set(entry_map) - {approve_entry_id} - set(reject_entry_ids))
    if unresolved:
        blockers.append("unresolved pending entries for service: " + ", ".join(unresolved))
    if blockers:
        return _json_response(False, denied=True, reason="policy_block", blockers=blockers,
                              warnings=warnings,
                              entry={k: target.get(k) for k in ("id", "service_name", "status", "target_commit", "wts_task_id", "conflict_flag")},
                              message="P1 conflict-group approval refused by policy; resolve blockers or escalate.")

    resolution = {
        "strategy": "choose_latest_reject_superseded",
        "summary": resolution_reason.strip(),
        "entry_recommendations": [
            {"entry_id": approve_entry_id, "action": "approve", "reason": release_note.strip(), "order": 1},
            *[{"entry_id": eid, "action": "reject", "reason": f"Superseded by {approve_entry_id}: {resolution_reason.strip()}", "order": 2+i}
              for i, eid in enumerate(reject_entry_ids)],
        ],
        "merge_notes": "No source merge requested; conflict is queue supersession only.",
        "release_note": release_note.strip(),
        "rollback_note": rollback_note.strip(),
        "policy_checklist": _normalize_checklist(policy_checklist),
        "c4_review_confirmed": bool(c4_review_confirmed),
        "c4_review_reason": (c4_review_reason or resolution_reason).strip(),
    }
    from gateway import capability_egress
    url = f"{_MC_API_BASE}/api/deploy-queue/group/{service_name}/approve"
    body = {"decided_by": decided_by or "dd-p1-system-a", "resolution": resolution}
    try:
        resp = capability_egress.post_with_capability(url, json_body=body, timeout=180.0)
    except Exception as e:
        return tool_error(f"deploy conflict-group request failed: {type(e).__name__}: {e}")
    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text[:1000]}
    if resp.status_code == 403:
        return _json_response(False, denied=True, status_code=403, detail=payload.get("detail"),
                              reason=payload.get("reason"),
                              message="DENIED by capability gate — this turn lacks System-A C4 deploy approval.")
    if resp.status_code >= 400:
        return _json_response(False, status_code=resp.status_code, result=payload,
                              message="mc-api refused or failed conflict-group approval")
    return _json_response(True, status_code=resp.status_code, result=payload,
                          approved_entry_id=approve_entry_id,
                          rejected_entry_ids=reject_entry_ids,
                          service_name=service_name,
                          target_commit=target.get("target_commit"),
                          warnings=warnings,
                          message="Deploy conflict group resolved through P1 policy gate and C4 capability path; mc-api group deploy pipeline was invoked.")


registry.register(
    name="deploy_resolve_conflict_group",
    toolset="deploy",
    schema=DEPLOY_RESOLVE_CONFLICT_GROUP_SCHEMA,
    handler=lambda args, **kw: deploy_resolve_conflict_group(
        service_name=args.get("service_name", ""),
        approve_entry_id=args.get("approve_entry_id", ""),
        expected_target_commit=args.get("expected_target_commit", ""),
        reject_entry_ids=args.get("reject_entry_ids") or [],
        resolution_reason=args.get("resolution_reason", ""),
        release_note=args.get("release_note", ""),
        rollback_note=args.get("rollback_note", ""),
        policy_checklist=args.get("policy_checklist"),
        high_risk_flags=args.get("high_risk_flags"),
        c4_review_confirmed=bool(args.get("c4_review_confirmed", False)),
        c4_review_reason=args.get("c4_review_reason", ""),
        decided_by=args.get("decided_by", "dd-p1-system-a"),
    ),
    emoji="🧭",
)


registry.register(
    name="deploy_approve",
    toolset="deploy",
    schema=DEPLOY_APPROVE_SCHEMA,
    handler=lambda args, **kw: deploy_approve(
        entry_id=args.get("entry_id"),
        expected_service_name=args.get("expected_service_name", ""),
        expected_target_commit=args.get("expected_target_commit", ""),
        release_note=args.get("release_note", ""),
        rollback_note=args.get("rollback_note", ""),
        policy_checklist=args.get("policy_checklist"),
        high_risk_flags=args.get("high_risk_flags"),
        decided_by=args.get("decided_by", "dd-p1-system-a"),
        force=bool(args.get("force", False)),
        c4_review_confirmed=bool(args.get("c4_review_confirmed", False)),
        c4_review_reason=args.get("c4_review_reason", ""),
    ),
    emoji="🚀",
)
