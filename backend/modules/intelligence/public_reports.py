"""P18 four source-linked draft reports, independently gated by public_analysis."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from backend.core.contracts import validate_payload
from backend.core.output_switches import PUBLIC_ANALYSIS, OutputSwitches
from backend.modules.intelligence.evidence import Evidence, EvidenceBundle

PUBLIC_SLOTS = {1: ("domestic_digest", "مرور اخبار ایران"), 2: ("foreign_digest", "مرور اخبار جهان"),
                3: ("news_angles", "زاویه‌های پوشش خبری"), 4: ("daily_summary", "جمع‌بندی روز")}
_WORDS = re.compile(r"[^\W_]{2,}", re.UNICODE)
_STOP = frozenset({"این", "برای", "است", "شد", "های", "در", "از", "به", "با", "که", "را", "و",
                   "the", "and", "for", "from", "with", "that", "this"})


def _scope(items: tuple[Evidence, ...]) -> dict[str, Any]:
    refs = [item.artifact_id for item in items]
    streams = Counter(item.stream for item in items)
    stream_text = "، ".join(f"{key}: {streams[key]}" for key in sorted(streams))
    return {"heading": "دامنه و محدودیت گزارش",
            "body": (f"{len(items)} سند مجاز از {len({item.source_id for item in items})} منبع؛ "
                     f"توزیع جریان‌ها: {stream_text}. این گزارش فقط اسناد همین بازه و نمونه محدود را توصیف "
                     "می‌کند و صحت مستقل ادعاهای منابع یا پوشش کامل رویدادها را تأیید نمی‌کند."),
            "source_refs": refs}


def _source_body(item: Evidence) -> str:
    stamp = item.collected_at.astimezone(timezone.utc).isoformat()
    base = f"منبع {item.source_id} در {stamp}؛ عنوان ثبت‌شده: {item.title}"
    if item.redacted or not item.text:
        return base + "؛ متن به دلیل سطح مجاز استفاده در گزارش وارد نشده است."
    # This is a contiguous source excerpt, not a rewrite or an instruction for
    # the system. React escapes it on display and the source reference remains attached.
    excerpt = item.text[:320]
    return base + (f"\nگزیده مستقیم منبع: «{excerpt}»" if excerpt else "")


def _digest(items: tuple[Evidence, ...]) -> list[dict[str, Any]]:
    sections = [_scope(items)]
    for index, item in enumerate(sorted(items, key=lambda e: (-e.collected_at.timestamp(), e.artifact_id)), 1):
        sections.append({"heading": f"خبر {index}", "body": _source_body(item),
                         "source_refs": [item.artifact_id]})
    sections.append({"heading": "اقدام تحریریه",
                     "body": "ادعاهای مهم، عددها و نقل‌قول‌ها پیش از تصمیم P11 با منبع اصلی و شاهد مستقل بررسی شوند.",
                     "source_refs": [item.artifact_id for item in items]})
    return sections


def _angles(items: tuple[Evidence, ...]) -> list[dict[str, Any]]:
    refs = [item.artifact_id for item in items]
    by_source: dict[str, list[Evidence]] = defaultdict(list)
    title_counts: Counter[str] = Counter()
    for item in items:
        by_source[item.source_id].append(item)
        title_counts.update(word.casefold() for word in _WORDS.findall(item.title) if word.casefold() not in _STOP)
    sections = [_scope(items)]
    for source_id in sorted(by_source):
        source_items = sorted(by_source[source_id], key=lambda e: (e.collected_at, e.artifact_id))
        titles = " | ".join(item.title for item in source_items)
        sections.append({"heading": f"پوشش ثبت‌شده منبع {source_id}",
                         "body": f"{len(source_items)} عنوان در نمونه: {titles}",
                         "source_refs": [item.artifact_id for item in source_items]})
    repeated = [(word, count) for word, count in title_counts.most_common(12) if count >= 2]
    sections.append({"heading": "واژگان پرتکرار عنوان‌ها",
                     "body": ("، ".join(f"{word} ({count})" for word, count in repeated)
                              if repeated else "در این نمونه واژه عنوانی مشترک با تکرار حداقل دو بار ثبت نشد."),
                     "source_refs": refs})
    sections.append({"heading": "محدودیت مقایسه",
                     "body": "تفاوت عنوان، تعداد یا واژگان پوشش به‌تنهایی اثبات تناقض، اهمیت، علت یا نیت رسانه نیست.",
                     "source_refs": refs})
    return sections


def _summary(items: tuple[Evidence, ...]) -> list[dict[str, Any]]:
    refs = [item.artifact_id for item in items]
    sections = [_scope(items)]
    ordered = sorted(items, key=lambda e: (e.collected_at, e.artifact_id))
    timeline = "\n".join(
        f"{item.collected_at.astimezone(timezone.utc).isoformat()} | {item.stream} | {item.source_id} | {item.title}"
        for item in ordered
    )
    sections.append({"heading": "خط زمانی اسناد", "body": timeline, "source_refs": refs})
    for stream in sorted({item.stream for item in items}):
        stream_items = tuple(item for item in items if item.stream == stream)
        sections.append({"heading": f"مرور جریان {stream}",
                         "body": "\n\n".join(_source_body(item) for item in stream_items),
                         "source_refs": [item.artifact_id for item in stream_items]})
    sections.append({"heading": "فهرست پیگیری",
                     "body": "هر مورد این نمونه پیش از استفاده تحریریه به بررسی منشأ، حق استفاده و شاهد تکمیلی نیاز دارد.",
                     "source_refs": refs})
    return sections


def _sections(items: tuple[Evidence, ...], report_type: str) -> list[dict[str, Any]]:
    if report_type in {"domestic_digest", "foreign_digest"}:
        return _digest(items)
    if report_type == "news_angles":
        return _angles(items)
    if report_type == "daily_summary":
        return _summary(items)
    raise ValueError("unknown public report type")


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
