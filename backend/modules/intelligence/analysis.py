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
    claims: list[dict[str, Any]] = [{
        "text": f"در مجموعه بازیابی‌شده، {len(refs)} سند از {len({e.source_id for e in bundle.evidence})} منبع موجود است.",
        "source_refs": refs, "classification": "documented",
    }]
    for e in sorted(bundle.evidence, key=lambda item: (item.collected_at, item.artifact_id)):
        material = excerpt(e, topic)
        # Attribution is explicit. No claim that a source's assertion is independently proven.
        claim = "عنوان ثبت‌شده منبع: " + e.title
        if material:
            claim += "\nبخش عیناً بازیابی‌شده از متن منبع: " + material
        claims.append({"text": claim, "source_refs": [e.artifact_id], "classification": "attributed"})
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
