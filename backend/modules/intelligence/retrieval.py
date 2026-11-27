"""P16 bounded lexical retrieval from already-authorized P26 raw originals."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable, Mapping, Sequence

from backend.modules.intelligence.evidence import Evidence, EvidenceBundle, Period

_MAX_CANDIDATES = 100
# Include Arabic combining marks in original word spans so excerpts can
# preserve the exact source even when the query omits vowel marks.
_TERMS = re.compile(r"(?:[^\W_]|[\u064b-\u065f\u0670\u06d6-\u06ed])+", re.UNICODE)
_ARABIC_MARKS = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
_VARIANTS = str.maketrans("يىك٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "ییک01234567890123456789")
_STOP = frozenset({"از", "به", "در", "و", "که", "را", "این", "چه", "است", "برای", "با",
                   "the", "a", "an", "is", "of", "in", "to", "and", "what", "about"})


def normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().translate(_VARIANTS)
    return _ARABIC_MARKS.sub("", value)


def terms(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t for t in _TERMS.findall(normalized(query)) if len(t) > 1 and t not in _STOP))[:20]


def retrieve(
    query: str, period: Period, *,
    candidates: Sequence[Mapping[str, Any]],
    authorized: Callable[[Mapping[str, Any]], str | None],
    load: Callable[[Mapping[str, Any], str], Evidence | None],
    limit: int = 12, bounded: bool = True,
) -> EvidenceBundle:
    if not query.strip() or len(query) > 500 or not 1 <= limit <= 20 or len(candidates) > _MAX_CANDIDATES:
        raise ValueError("invalid retrieval bounds")
    tokens = terms(query)
    if not tokens:
        return EvidenceBundle(period, (), bounded)
    ranked: list[tuple[int, Evidence]] = []
    seen: set[str] = set()
    for candidate in candidates:
        # Rights/ACL filtering happens BEFORE loading any object bytes.
        allowed_scope = authorized(candidate)
        if allowed_scope is None:
            continue
        evidence = load(candidate, allowed_scope)
        if evidence is None or evidence.artifact_id in seen:
            continue
        if not period.start <= evidence.collected_at < period.end:
            continue
        seen.add(evidence.artifact_id)
        title = normalized(evidence.title)
        body = normalized((evidence.text or "")[:60_000])
        title_words = set(_TERMS.findall(title))
        body_words = set(_TERMS.findall(body))
        # Keep substring recall for inflected words; rank whole-word evidence
        # first. Never modify the source used for citations.
        score = sum(6 * (token in title_words) + 2 * (token in body_words)
                    + 3 * (token in title) + (token in body) for token in tokens)
        if score > 0:
            ranked.append((score, evidence))
    ranked.sort(key=lambda pair: (-pair[0], -pair[1].collected_at.timestamp(), pair[1].artifact_id))
    return EvidenceBundle(period, tuple(e for _, e in ranked[:limit]), bounded or len(ranked) > limit)


def excerpt(evidence: Evidence, query: str, *, maximum: int = 240) -> str | None:
    if evidence.redacted or not evidence.text:
        return None
    text = evidence.text
    # Return an exact contiguous excerpt, not a rewritten quotation.
    words = terms(query)
    matches = [m.start() for m in _TERMS.finditer(text) if normalized(m.group()) in words]
    if not matches:
        matches = [m.start() for m in _TERMS.finditer(text)
                   if any(word in normalized(m.group()) for word in words)]
    if not matches:
        return None
    start = max(0, matches[0] - 60)
    return text[start:start + maximum]
