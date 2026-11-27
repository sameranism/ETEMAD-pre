"""P16 cited extractive response with no unsupported free-text model claims."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.core.contracts import validate_payload
from backend.modules.intelligence.evidence import EvidenceBundle
from backend.modules.intelligence.retrieval import excerpt


def answer_question(question: str, bundle: EvidenceBundle) -> dict[str, Any]:
    if not question.strip() or len(question) > 500:
        raise ValueError("invalid question")
    citations = []
    lines = []
    for index, e in enumerate(bundle.evidence, 1):
        quote = excerpt(e, question)
        citations.append({"source_ref": e.artifact_id, "quote_span": quote})
        line = f"[{index}] عنوان منبع: {e.title}"
        if quote:
            line += "\nبخش عیناً بازیابی‌شده: " + quote
        lines.append(line)
    response = ("اسناد مرتبط بازیابی‌شده (پاسخ استخراجی، نه نتیجه‌گیری مدل):\n" + "\n\n".join(lines)
                if lines else "در اسناد مجاز بررسی‌شده، شاهد مرتبط کافی یافت نشد؛ پاسخ قطعی در دسترس نیست.")
    notes = ["پاسخ فقط بر شواهد آرشیو مجاز استوار است و درستی مستقل ادعای رسانه‌ها را تأیید نمی‌کند."]
    if bundle.bounded:
        notes.append("جست‌وجو محدود است؛ نبود شاهد در این نمونه به معنای نبود خبر در تمام منابع نیست.")
    if any(e.redacted for e in bundle.evidence):
        notes.append("بخشی از منابع فقط در حد فراداده قابل استفاده‌اند.")
    result = {"schema_version": "1.0", "response_id": "CHAT-" + uuid4().hex,
              "question_id": "Q-" + uuid4().hex, "answer": response, "citations": citations,
              "uncertainty_notes": notes, "classification": "internal",
              "created_at": datetime.now(timezone.utc).isoformat()}
    validate_payload("analysis_chat_response", result)
    return result
