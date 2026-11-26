"""P10 versioned editorial rule preview. Originals and direct quotes are immutable."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from backend.core.contracts import validate_payload

MAX_TEXT = 100_000
MAX_RULES = 200
MAX_MATCHES = 2000
_QUOTE = re.compile(r'«[^»]*(?:»|$)|“[^”]*(?:”|$)|‘[^’]*(?:’|$)|"[^"]*(?:"|$)')
_WORD = r"[\w\u200c]"


@dataclass(frozen=True)
class Replacement:
    ruleset_id: str
    revision: int
    rule_id: str
    start: int
    end: int
    original: str
    proposed: str
    action: str
    reason: str


@dataclass(frozen=True)
class GlossaryPreview:
    original: str
    proposed: str
    replacements: tuple[Replacement, ...]


class EditorialGlossary:
    def __init__(self, rules: Mapping[str, Any]) -> None:
        snapshot = json.loads(json.dumps(dict(rules), ensure_ascii=False, allow_nan=False))
        validate_payload("editorial_rules", snapshot)
        replacements = snapshot["replacement_rules"]
        if len(replacements) > MAX_RULES or len({r["rule_id"] for r in replacements}) != len(replacements):
            raise ValueError("duplicate or excessive editorial rules")
        if any(len(r["from_text"]) > 512 or len(r["to_text"]) > 512 for r in replacements):
            raise ValueError("editorial replacement exceeds limit")
        self._rules = snapshot

    @property
    def version(self) -> str:
        return f"{self._rules['ruleset_id']}:{self._rules['revision']}"

    def preview(self, text: str, *, stream: str, desk: str, field: str,
                original_source: bool = False, context_sensitive: bool = False,
                protected_spans: Sequence[tuple[int, int]] = ()) -> GlossaryPreview:
        if len(text) > MAX_TEXT:
            raise ValueError("editorial text exceeds limit")
        if stream != self._rules["stream"] or self._rules["desk"] not in (desk, "all"):
            raise ValueError("editorial rules scope mismatch")
        spans = [(m.start(), m.end()) for m in _QUOTE.finditer(text)]
        spans.extend((m.start(), m.end()) for m in re.finditer(r"(?<!\w)'[^']*(?:'|$)", text))
        for start, end in protected_spans:
            if not 0 <= start < end <= len(text):
                raise ValueError("invalid protected quote span")
            spans.append((start, end))
        matches: list[tuple[int, int, dict[str, Any]]] = []
        for rule in self._rules["replacement_rules"]:
            if field not in rule["applies_to"] or rule["from_text"] == rule["to_text"]:
                continue
            expression = re.compile(r"(?<!" + _WORD + ")" + re.escape(rule["from_text"]) + r"(?!" + _WORD + ")")
            for match in expression.finditer(text):
                matches.append((match.start(), match.end(), rule))
                if len(matches) > MAX_MATCHES:
                    raise ValueError("too many editorial replacement matches")
        matches.sort(key=lambda m: (m[0], m[1], m[2]["rule_id"]))
        # All matches use original offsets. Overlapping rules cannot auto-apply.
        ambiguous: set[int] = set()
        for i, (start, end, _) in enumerate(matches):
            for j in range(i + 1, len(matches)):
                if matches[j][0] >= end:
                    break
                ambiguous.update((i, j))
        edits: list[Replacement] = []
        for i, (start, end, rule) in enumerate(matches):
            if original_source or field in {"original_text", "original_title", "source"}:
                action, reason = "blocked", "ORIGINAL_IMMUTABLE"
            elif any(start < b and end > a for a, b in spans):
                action, reason = "blocked", "DIRECT_QUOTE_IMMUTABLE"
            elif rule["mode"] == "never_replace":
                action, reason = "blocked", "RULE_FORBIDS_REPLACEMENT"
            elif i in ambiguous:
                action, reason = "review", "AMBIGUOUS_RULE_MATCH"
            elif (context_sensitive or field in {"title", "headline"} or rule["requires_fact_check"]
                  or rule["mode"] == "suggest_review"):
                # Conservative: title context cannot be proven apolitical by string matching.
                action, reason = "review", "CONTEXT_REVIEW_REQUIRED"
            else:
                action, reason = "applied", "UNAMBIGUOUS_DRAFT_RULE"
            edits.append(Replacement(
                self._rules["ruleset_id"], self._rules["revision"], rule["rule_id"],
                start, end, text[start:end], rule["to_text"], action, reason,
            ))
        proposed = text
        for edit in reversed(edits):
            if edit.action == "applied":
                proposed = proposed[:edit.start] + edit.proposed + proposed[edit.end:]
        return GlossaryPreview(text, proposed, tuple(edits))
