"""Deterministic editorial quality checks with no provider or publication side effects."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,٬،]\d+)*(?!\w)")
_QUOTE_RE = re.compile(r"«([^»]+)»|\u201c([^\u201d]+)\u201d|\"([^\"]+)\"")


@dataclass(frozen=True)
class QualityResult:
    """Trusted service result that can feed publication policy evaluation."""

    passed: bool
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    source_hash: str
    draft_hash: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _numbers(value: str) -> set[str]:
    normalized = value.translate(_DIGIT_TRANSLATION)
    return {
        match.group(0).replace("٬", ",").replace("،", ",")
        for match in _NUMBER_RE.finditer(normalized)
    }


def _quotes(value: str) -> tuple[str, ...]:
    return tuple(next(part for part in match.groups() if part is not None).strip()
                 for match in _QUOTE_RE.finditer(value))


def assess_quality(source_text: str, draft_text: str, *, source_refs: Sequence[str],
                   attributed_refs: Sequence[str], required_names: Sequence[str] = (),
                   claim_evidence: Mapping[str, Sequence[str]] | None = None,
                   prohibited_terms: Sequence[str] = ()) -> QualityResult:
    """Assess a draft against an immutable source snapshot and trusted policy inputs.

    This is deliberately deterministic. It does not claim semantic fact checking: it catches
    missing traceability, changed numbers or required names, fabricated direct quotations, and
    configured prohibited terms. Callers remain responsible for human review of ambiguous claims.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        raise ValueError("source text is required")
    if not isinstance(draft_text, str) or not draft_text.strip():
        raise ValueError("draft text is required")

    source_snapshot = source_text
    source_ref_set = {ref.strip() for ref in source_refs if ref.strip()}
    attributed_ref_set = {ref.strip() for ref in attributed_refs if ref.strip()}
    reasons: set[str] = set()

    if not source_ref_set:
        reasons.add("CITATION_MISSING")
    if source_ref_set - attributed_ref_set:
        reasons.add("ATTRIBUTION_MISSING")
    if _numbers(draft_text) - _numbers(source_snapshot):
        reasons.add("FACT_NUMBER_MISMATCH")
    if any(name.strip() and name.strip() not in draft_text for name in required_names):
        reasons.add("FACT_NAME_MISMATCH")
    if any(quote and quote not in source_snapshot for quote in _quotes(draft_text)):
        reasons.add("FABRICATED_QUOTE")

    for claim, evidence in (claim_evidence or {}).items():
        if not claim.strip():
            raise ValueError("claim text is required")
        if claim in draft_text and not any(item.strip() and item.strip() in source_snapshot
                                           for item in evidence):
            reasons.add("CLAIM_EVIDENCE_MISSING")

    folded_draft = draft_text.casefold()
    if any(term.strip() and term.strip().casefold() in folded_draft for term in prohibited_terms):
        reasons.add("PROHIBITED_DISCRIMINATION")

    source_hash = _digest(source_snapshot)
    draft_hash = _digest(draft_text)
    material = json.dumps(
        [
            source_hash,
            draft_hash,
            sorted(source_ref_set),
            sorted(attributed_ref_set),
            sorted(reasons),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assessment_ref = "quality:" + _digest(material)[:32]
    reason_codes = tuple(sorted(reasons)) or ("QUALITY_CHECKS_PASSED",)
    return QualityResult(
        passed=not reasons,
        reason_codes=reason_codes,
        evidence_refs=tuple(sorted(source_ref_set | {assessment_ref})),
        source_hash=source_hash,
        draft_hash=draft_hash,
    )
