"""P15 descriptive source comparison, not political scoring or prediction."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from backend.core.contracts import validate_payload
from backend.modules.intelligence.evidence import EvidenceBundle
from backend.modules.intelligence.retrieval import excerpt


def analyze(topic: str, bundle: EvidenceBundle) -> dict[str, Any] | None:
    if not topic.strip() or len(topic) > 500:
        raise ValueError("invalid analysis topic")
    if not bundle.evidence:
        return None
    refs = bundle.refs
    ordered = sorted(bundle.evidence, key=lambda item: (item.collected_at, item.artifact_id))
    timeline_refs = [item.artifact_id for item in ordered]
    timeline = "\n".join(
        f"{item.collected_at.astimezone(timezone.utc).isoformat()} | {item.source_id} | "
        f"{item.artifact_id} | {item.title}"
        for item in ordered
    )
    claims: list[dict[str, Any]] = [{
        "text": (
            f"در مجموعه بازیابی‌شده، {len(refs)} سند از "
            f"{len({item.source_id for item in bundle.evidence})} منبع موجود است."
        ),
        "source_refs": refs, "classification": "documented",
    }, {
        "text": "توالی زمانی اسناد بازیابی‌شده (UTC):\n" + timeline,
        "source_refs": timeline_refs,
        "classification": "documented",
    }]
    narrative_groups: dict[str, list[str]] = {}
    for item in ordered:
        material = excerpt(item, topic)
        # Attribution is explicit. No claim that a source's assertion is independently proven.
        claim = (
            f"زمان ثبت منبع: {item.collected_at.astimezone(timezone.utc).isoformat()}\n"
            f"عنوان ثبت‌شده منبع: {item.title}"
        )
        if material:
            claim += "\nبخش عیناً بازیابی‌شده از متن منبع: " + material
        claims.append({
            "text": claim,
            "source_refs": [item.artifact_id],
            "classification": "attributed",
        })
        narrative = " ".join((material or item.title).casefold().split())
        narrative_groups.setdefault(narrative, []).append(item.artifact_id)
    if len(narrative_groups) > 1:
        claims.append({
            "text": (
                f"در متن مجاز بازیابی‌شده {len(narrative_groups)} صورت‌بندی متمایز "
                "از روایت منابع وجود دارد؛ این تفاوت به‌تنهایی تناقض واقعی را اثبات نمی‌کند."
            ),
            "source_refs": timeline_refs,
            "classification": "uncertain",
        })
    uncertainty = ["این تحلیل توصیف اسناد بازیابی‌شده است؛ درستی مستقل ادعاهای منابع احراز نشده است.",
                   "اختلاف عنوان یا روایت به‌تنهایی اثبات تناقض، علت یا نیت نیست."]
    if bundle.bounded:
        uncertainty.append("بازیابی محدود به نمونه مجاز و سقف جست‌وجو است؛ نماینده تمام پوشش خبری نیست.")
    if any(e.redacted for e in bundle.evidence):
        uncertainty.append("متن برخی منابع به دلیل محدوده حقوق استفاده در تحلیل وارد نشده است.")
    identity = hashlib.sha256((topic + str(bundle.period.as_record()) + bundle.fingerprint).encode("utf-8")).hexdigest()
    result = {"schema_version": "1.0", "analysis_id": "AN-" + identity[:32], "topic": topic,
              "time_range": bundle.period.as_record(), "source_refs": refs, "claims": claims,
              "uncertainties": uncertainty, "created_at": datetime.now(timezone.utc).isoformat()}
    validate_payload("analysis_result", result)
    return result
