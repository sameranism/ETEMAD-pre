"""P11 deterministic publication eligibility, not a legal opinion or dispatch."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from backend.core.contracts import validate_payload

_SCOPES = {"metadata": 0, "summary": 1, "full_text": 2}


@dataclass(frozen=True)
class SourceRights:
    permission: str
    allowed_scope: str
    requested_scope: str
    evidence_ref: str | None
    attribution_verified: bool = False


@dataclass(frozen=True)
class PolicyContext:
    """Trusted service input, not client-supplied proof of legal approval."""

    policy_version: str
    reviewed_policy_ref: str | None = None
    quality_passed: bool = False
    quality_evidence_refs: tuple[str, ...] = ()
    sensitive: bool = False
    classification: str = "internal"
    prohibited_categories: tuple[str, ...] = ()
    decision_ttl_seconds: int = 900


def evaluate_policy(record: Mapping[str, Any], *, object_type: str,
                    rights: Mapping[str, SourceRights], context: PolicyContext,
                    decided_at: datetime | None = None) -> dict[str, Any]:
    if object_type not in {"article", "public_report"}:
        raise ValueError("unsupported editorial object")
    snapshot = json.loads(json.dumps(dict(record), ensure_ascii=False, allow_nan=False))
    validate_payload(object_type, snapshot)
    if not context.policy_version.strip() or not 60 <= context.decision_ttl_seconds <= 86400:
        raise ValueError("invalid policy configuration")
    now = decided_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("policy decision time requires timezone")
    now = now.astimezone(timezone.utc)
    blocked: set[str] = set()
    review: set[str] = set()
    refs = list(snapshot["source_refs"])
    evidence: set[str] = set(refs)
    if object_type == "public_report":
        for section in snapshot["sections"]:
            if not section["source_refs"]:
                review.add("SECTION_ATTRIBUTION_MISSING")
            refs.extend(section["source_refs"])
        state = snapshot["generation_status"]
        identity = snapshot["report_id"]
    else:
        state = snapshot["status"]
        identity = snapshot["article_id"]
    if state == "held":
        blocked.add("EDITORIAL_HOLD")
    elif state != "ready_for_policy":
        review.add("EDITORIAL_NOT_READY")
    if context.classification not in {"internal", "public"}:
        blocked.add("CLASSIFICATION_NOT_PUBLICABLE")
    if context.prohibited_categories:
        blocked.add("PROHIBITED_CONTENT")
    if context.sensitive:
        review.add("SENSITIVE_CONTENT_REVIEW")
    if context.reviewed_policy_ref and context.reviewed_policy_ref.strip():
        evidence.add(context.reviewed_policy_ref)
    else:
        review.add("LEGAL_POLICY_NOT_REVIEWED")
    if context.quality_passed is not True or not any(ref.strip() for ref in context.quality_evidence_refs):
        review.add("QUALITY_EVIDENCE_INCOMPLETE")
    evidence.update(ref for ref in context.quality_evidence_refs if ref.strip())
    for ref in set(refs):
        if not ref.strip():
            review.add("SOURCE_REFERENCE_INVALID")
            continue
        evidence.add(ref)
        item = rights.get(ref)
        if item is None:
            review.add("SOURCE_RIGHTS_UNKNOWN")
            continue
        if item.evidence_ref and item.evidence_ref.strip():
            evidence.add(item.evidence_ref)
        else:
            review.add("SOURCE_RIGHTS_EVIDENCE_MISSING")
        if item.permission not in {"licensed", "link_only", "unverified", "blocked"}:
            review.add("SOURCE_RIGHTS_UNKNOWN")
        if item.allowed_scope not in _SCOPES or item.requested_scope not in _SCOPES:
            blocked.add("INVALID_CONTENT_SCOPE")
            continue
        if item.permission == "blocked":
            blocked.add("SOURCE_RIGHTS_BLOCKED")
        if _SCOPES[item.requested_scope] > _SCOPES[item.allowed_scope]:
            blocked.add("SOURCE_SCOPE_EXCEEDED")
        if item.permission == "link_only" and item.requested_scope != "metadata":
            blocked.add("LINK_ONLY_SCOPE_EXCEEDED")
        if item.permission != "licensed" and item.requested_scope == "full_text":
            blocked.add("FULL_TEXT_RIGHTS_NOT_LICENSED")
        if item.permission == "unverified":
            review.add("SOURCE_RIGHTS_UNVERIFIED")
        if item.attribution_verified is not True:
            review.add("SOURCE_ATTRIBUTION_UNVERIFIED")
    decision = "blocked" if blocked else "human_review" if review else "auto_allowed"
    reasons = sorted(blocked | review) or ["POLICY_CHECKS_PASSED"]
    material = json.dumps([snapshot, context.policy_version, decision, reasons, sorted(evidence)],
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result = {
        "schema_version": "1.0",
        "decision_id": "POL-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
        "object_id": identity, "object_type": object_type, "decision": decision,
        "reason_codes": reasons, "evidence_refs": sorted(evidence),
        "policy_version": context.policy_version, "decided_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=context.decision_ttl_seconds)).isoformat()
        if decision == "auto_allowed" else None,
    }
    validate_payload("validation_decision", result)
    return result
