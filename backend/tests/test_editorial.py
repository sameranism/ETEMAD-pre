"""S04-009 initial deterministic editorial fixtures. No provider or publication."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from backend.modules.editorial.glossary import EditorialGlossary
from backend.modules.editorial.policy import PolicyContext, SourceRights, evaluate_policy

NOW = datetime(2026, 11, 26, 20, tzinfo=timezone.utc)


def rule(rule_id="R-1", source="عبارت نمونه", replacement="معادل نمونه", **updates):
    return {"rule_id": rule_id, "from_text": source, "to_text": replacement,
            "mode": "auto_replace", "applies_to": ["body", "title"],
            "exempt_quotes": False, "requires_fact_check": False, **updates}


def rules(*items):
    return {"schema_version": "1.0", "ruleset_id": "RULE-FIXTURE", "revision": 2,
            "stream": "foreign", "desk": "international", "replacement_rules": list(items),
            "prohibited_content_categories": [], "updated_at": NOW.isoformat()}


def preview(glossary, text, **updates):
    return glossary.preview(text, **{"stream": "foreign", "desk": "international", "field": "body", **updates})


def test_glossary_protects_original_and_quotes_and_records_revision():
    config = rules(rule())
    glossary = EditorialGlossary(config)
    text = "عبارت نمونه و «عبارت نمونه»"
    result = preview(glossary, text)
    assert result.original == text
    assert result.proposed == "معادل نمونه و «عبارت نمونه»"
    assert [e.action for e in result.replacements] == ["applied", "blocked"]
    assert all(e.revision == 2 and e.ruleset_id == "RULE-FIXTURE" for e in result.replacements)
    assert preview(glossary, text, original_source=True).proposed == text
    assert preview(glossary, "عبارت نمونه", field="title").replacements[0].action == "review"
    assert preview(glossary, "عبارت نمونه", context_sensitive=True).replacements[0].action == "review"
    assert preview(glossary, "'عبارت نمونه'").proposed == "'عبارت نمونه'"
    assert preview(glossary, "عبارت نمونه", protected_spans=[(0, len("عبارت نمونه"))]).proposed == "عبارت نمونه"
    config["replacement_rules"][0]["to_text"] = "دست‌کاری"
    assert preview(glossary, text).proposed == result.proposed


def test_overlapping_and_fact_check_rules_are_not_automatic():
    glossary = EditorialGlossary(rules(rule(), rule("R-2", "نمونه", "دیگر")))
    result = preview(glossary, "عبارت نمونه")
    assert result.proposed == result.original and all(e.action == "review" for e in result.replacements)
    review = EditorialGlossary(rules(rule(requires_fact_check=True)))
    assert preview(review, "عبارت نمونه").replacements[0].reason == "CONTEXT_REVIEW_REQUIRED"
    with pytest.raises(ValueError, match="scope"):
        preview(review, "عبارت نمونه", stream="domestic")


def article(**updates):
    return {"schema_version": "1.0", "article_id": "ART-FIXTURE", "news_id": "NEWS-FIXTURE",
            "stream": "foreign", "desk": "international", "title": "Fixture",
            "lead": "Synthetic lead", "body": "Synthetic body", "short_summary": "Synthetic summary",
            "source_refs": ["NEWS-FIXTURE"], "framework_version": "FW-FIXTURE:1",
            "editorial_rules_version": "RULE-FIXTURE:2", "status": "ready_for_policy",
            "created_at": NOW.isoformat(), **updates}


def context(**updates):
    return PolicyContext(**{"policy_version": "fixture-policy-v1",
        "reviewed_policy_ref": "fixture-policy-review", "quality_passed": True,
        "quality_evidence_refs": ("fixture-quality",), **updates})


def test_policy_only_allows_complete_trusted_evidence():
    obj = article()
    original = deepcopy(obj)
    rights = {"NEWS-FIXTURE": SourceRights("licensed", "summary", "summary", "fixture-rights", True)}
    result = evaluate_policy(obj, object_type="article", rights=rights, context=context(), decided_at=NOW)
    assert result["decision"] == "auto_allowed" and result["expires_at"] is not None
    assert obj == original
    held = evaluate_policy(obj, object_type="article", rights=rights,
                           context=context(reviewed_policy_ref=None), decided_at=NOW)
    assert held["decision"] == "human_review" and "LEGAL_POLICY_NOT_REVIEWED" in held["reason_codes"]


@pytest.mark.parametrize("permission,allowed,requested,expected", [
    ("unverified", "full_text", "full_text", "blocked"),
    ("unverified", "summary", "summary", "human_review"),
    ("link_only", "metadata", "summary", "blocked"),
    ("blocked", "summary", "summary", "blocked"),
    ("licensed", "metadata", "summary", "blocked"),
])
def test_policy_denies_unverified_or_excess_content(permission, allowed, requested, expected):
    rights = {"NEWS-FIXTURE": SourceRights(permission, allowed, requested, "fixture-rights", True)}
    decision = evaluate_policy(article(), object_type="article", rights=rights,
                               context=context(), decided_at=NOW)
    assert decision["decision"] == expected
    assert decision["expires_at"] is None


def test_private_sensitive_and_unknown_rights_fail_closed():
    result = evaluate_policy(article(), object_type="article", rights={},
                             context=context(classification="executive_private"), decided_at=NOW)
    assert result["decision"] == "blocked"
    assert "CLASSIFICATION_NOT_PUBLICABLE" in result["reason_codes"]
    result = evaluate_policy(article(), object_type="article", rights={},
                             context=context(sensitive=True, quality_passed=False), decided_at=NOW)
    assert result["decision"] == "human_review"
    assert "SOURCE_RIGHTS_UNKNOWN" in result["reason_codes"]
