"""P18 four source-linked draft reports, independently gated by public_analysis."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from backend.core.contracts import validate_payload
from backend.core.output_switches import PUBLIC_ANALYSIS, OutputSwitches
from backend.modules.intelligence.evidence import Evidence, EvidenceBundle

PUBLIC_SLOTS = {1: ("domestic_digest", "مرور اخبار ایران"), 2: ("foreign_digest", "مرور اخبار جهان"),
                3: ("news_angles", "زاویه‌های پوشش خبری"), 4: ("daily_summary", "جمع‌بندی روز")}


def _sections(items: tuple[Evidence, ...], report_type: str) -> list[dict[str, Any]]:
    refs = [item.artifact_id for item in items]
    sections = [{"heading": "دامنه گزارش",
                 "body": f"{len(items)} سند بازیابی‌شده از {len({item.source_id for item in items})} منبع؛ نمونه محدود است.",
                 "source_refs": refs}]
    for item in sorted(items, key=lambda e: (e.collected_at, e.artifact_id)):
        # Metadata only: no unauthorized full text and no invented interpretation.
        sections.append({"heading": item.stream, "body": "عنوان ثبت‌شده منبع: " + item.title,
                         "source_refs": [item.artifact_id]})
    if report_type == "news_angles":
        sections.append({"heading": "نکته تحریریه",
                         "body": "عنوان‌ها و منابع ثبت‌شده را مقایسه کنید؛ اختلاف پوشش به‌تنهایی اثبات تناقض نیست.",
                         "source_refs": refs})
    return sections


def build_public_report(slot: int, bundle: EvidenceBundle, *, switches: OutputSwitches) -> dict[str, Any] | None:
    if slot not in PUBLIC_SLOTS:
        raise ValueError("invalid public report slot")
    if not switches.enabled(PUBLIC_ANALYSIS):
        return None
    report_type, title = PUBLIC_SLOTS[slot]
    stream = {1: "domestic", 2: "foreign"}.get(slot)
    items = tuple(item for item in bundle.evidence if stream is None or item.stream == stream)
    if not items:
        return None
    refs = [item.artifact_id for item in items]
    sections = _sections(items, report_type)
    identity = hashlib.sha256(json.dumps([slot, bundle.period.as_record(), refs, sections],
                                         ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    record = {"schema_version": "1.0", "report_id": "RPT-P-" + identity[:32], "title": title,
              "sections": sections, "source_refs": refs, "generated_at": datetime.now(timezone.utc).isoformat(),
              "time_range": bundle.period.as_record(), "generation_status": "draft",
              "created_by_project": "P18", "visibility": "public", "report_type": report_type}
    validate_payload("public_report", record)
    return record
