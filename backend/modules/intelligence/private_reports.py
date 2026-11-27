"""P19 three executive-only report builders.

No public repository, API, publisher, object store or outbound model is
connected here. The future secure private service must own persistence and ACL.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from backend.core.auth import Principal
from backend.core.contracts import validate_payload
from backend.core.permissions import GrantLookup, decide_access
from backend.modules.intelligence.evidence import EvidenceBundle

PRIVATE_SLOTS = {5: ("media_narratives", "روایت‌های رسانه‌ای"),
                 6: ("political_public_affairs", "مرور امور عمومی و سیاسی"),
                 7: ("editorial_decision_support", "پشتیبانی تصمیم تحریریه")}


def build_private_report(slot: int, bundle: EvidenceBundle, *,
                         principal: Principal, grants: GrantLookup) -> dict[str, Any] | None:
    if slot not in PRIVATE_SLOTS:
        raise ValueError("invalid private report slot")
    # Only an explicitly granted EIC may receive this result; owner/staff are denied.
    decision = decide_access(principal.subject_id, principal.role, "private_report",
                             f"slot-{slot}", "read", grants, classification="executive_private")
    if decision["allowed"] is not True:
        raise PermissionError("private report unavailable")
    if not bundle.evidence:
        return None
    report_type, title = PRIVATE_SLOTS[slot]
    refs = bundle.refs
    sources = len({item.source_id for item in bundle.evidence})
    sections = [{"heading": "دامنه شواهد",
                 "body": f"{len(refs)} سند مجاز از {sources} منبع؛ پوشش کامل یا صحت مستقل ادعاها احراز نشده است.",
                 "source_refs": refs}]
    for item in sorted(bundle.evidence, key=lambda e: (e.collected_at, e.artifact_id)):
        sections.append({"heading": "روایت منتسب به منبع", "body": item.title,
                         "source_refs": [item.artifact_id]})
    notes = {5: "شباهت و تفاوت روایت‌ها نیازمند مقایسه متن و زمینه مستقل است.",
             6: "این مرور صرفاً توصیفی است؛ پیش‌بینی یا ارزیابی سیاسی افراد انجام نشده است.",
             7: "پیشنهاد تحریریه: شواهد تکمیلی و تأیید مستقل ادعاهای مهم را بررسی کنید."}
    sections.append({"heading": "محدودیت و اقدام تحریریه", "body": notes[slot], "source_refs": refs})
    identity = hashlib.sha256(json.dumps([slot, principal.subject_id, bundle.period.as_record(), refs, sections],
                                         sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    report = {"schema_version": "1.0", "report_id": "RPT-X-" + identity[:32], "title": title,
              "sections": sections, "source_refs": refs, "generated_at": datetime.now(timezone.utc).isoformat(),
              "time_range": bundle.period.as_record(), "generation_status": "draft",
              "created_by_project": "P19", "visibility": "executive_private",
              "report_type": report_type, "owner_role": "editor_in_chief", "publishable": False}
    validate_payload("private_report", report)
    return report
