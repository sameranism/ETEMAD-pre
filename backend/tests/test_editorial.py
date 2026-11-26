"""S04-009 initial deterministic editorial fixtures. No provider or publication."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from backend.modules.editorial.glossary import EditorialGlossary
from backend.modules.editorial.policy import PolicyContext, SourceRights, evaluate_policy
from backend.modules.editorial.quality import assess_quality

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



def test_quality_passes_traceable_copy_without_mutation():
    source = (
        "خبرگزاری نمونه گفت: «بودجه ۱۲ میلیارد است». "
        "علی رضایی توضیح داد."
    )
    original = deepcopy(source)
    result = assess_quality(
        source,
        "علی رضایی گفت «بودجه ۱۲ میلیارد است».",
        source_refs=("NEWS-FIXTURE",),
        attributed_refs=("NEWS-FIXTURE",),
        required_names=("علی رضایی",),
        claim_evidence={
            "بودجه ۱۲ میلیارد است": ("بودجه ۱۲ میلیارد است",),
        },
    )
    assert result.passed
    assert result.reason_codes == ("QUALITY_CHECKS_PASSED",)
    assert result.evidence_refs[0] == "NEWS-FIXTURE"
    assert source == original


def test_quality_fails_closed_on_missing_attribution_and_changed_facts():
    result = assess_quality(
        "بودجه ۱۲ میلیارد است و علی رضایی توضیح داد.",
        "بودجه ۱۵ میلیارد است.",
        source_refs=("NEWS-FIXTURE",),
        attributed_refs=(),
        required_names=("علی رضایی",),
        claim_evidence={"بودجه ۱۵ میلیارد است": ()},
    )
    assert not result.passed
    assert set(result.reason_codes) == {
        "ATTRIBUTION_MISSING",
        "CLAIM_EVIDENCE_MISSING",
        "FACT_NAME_MISMATCH",
        "FACT_NUMBER_MISMATCH",
    }


def test_quality_rejects_untraceable_quotes_and_prohibited_terms():
    result = assess_quality(
        "متن منبع مجاز است.",
        "او گفت «نقل قول ساختگی» و از عبارت ممنوع استفاده کرد.",
        source_refs=(),
        attributed_refs=(),
        prohibited_terms=("عبارت ممنوع",),
    )
    assert set(result.reason_codes) == {
        "CITATION_MISSING",
        "FABRICATED_QUOTE",
        "PROHIBITED_DISCRIMINATION",
    }


@pytest.mark.parametrize("source,draft", [("", "draft"), ("source", "")])
def test_quality_requires_non_empty_text(source, draft):
    with pytest.raises(ValueError):
        assess_quality(source, draft, source_refs=(), attributed_refs=())


def _editorial_source(**updates):
    return {"schema_version": "1.0", "news_id": "NEWS-FIXTURE", "stream": "foreign",
            "source_id": "SRC-FIXTURE", "source_country": "DE", "language": "en",
            "original_url": "https://example.invalid/news", "original_title": "Fixture",
            "original_text": 'Alice said “12 units”.', "subject_countries": [],
            "collected_at": NOW.isoformat(), "content_scope": "summary",
            "reuse_permission": "unverified", **updates}


def _editorial_gateway(task, reply):
    from backend.core.ai_gateway import Gateway, ModelBinding

    class FixtureProvider:
        name = "fixture"
        def complete(self, prompt, *, model, timeout_s):
            assert prompt and timeout_s > 0
            return reply, 0.0

    return Gateway({task: ModelBinding(task, "fixture", "fixture-model")},
                   {"fixture": FixtureProvider()}, enabled=True)


def _framework():
    return {"schema_version": "1.0", "framework_id": "FW-FIXTURE", "desk": "international",
            "stream": "foreign", "headline_max_chars": 110, "summary_max_chars": 300,
            "required_sections": ["title", "lead", "body", "sources"],
            "attribution_required": True, "updated_at": NOW.isoformat()}


def test_translation_gateway_preserves_source_and_marks_semantic_review():
    from backend.modules.editorial.translator import translate

    raw = _editorial_source()
    before = deepcopy(raw)
    draft = translate(raw, gateway=_editorial_gateway("translation", 'Alice گفت «12 units».'),
                      ruleset=rules(), desk="international", required_names=("Alice",))
    assert raw == before
    assert draft.record["news_id"] == raw["news_id"]
    assert draft.record["target_language"] == "fa"
    assert draft.record["model_id"] == "fixture/fixture-model"
    assert draft.needs_review and draft.record["warnings"] == ["TRANSLATION_SEMANTIC_REVIEW_REQUIRED"]
    assert draft.provenance["cost_usd"] == 0.0


def test_translation_flags_omission_and_invented_quote():
    from backend.modules.editorial.translator import translate

    draft = translate(_editorial_source(), gateway=_editorial_gateway("translation", "او گفت «ساختگی»."),
                      ruleset=rules(), desk="international", required_names=("Alice",))
    assert {"FACT_NUMBER_MISMATCH", "FACT_NAME_MISMATCH", "DIRECT_QUOTE_MISMATCH",
            "FABRICATED_QUOTE"} <= set(draft.record["warnings"])


def test_translation_rejects_metadata_and_disabled_gateway():
    from backend.core.ai_gateway import Gateway, GatewayDisabled
    from backend.modules.editorial.translator import translate

    with pytest.raises(ValueError, match="scope"):
        translate(_editorial_source(content_scope="metadata"), gateway=Gateway({}, {}),
                  ruleset=rules(), desk="international")
    with pytest.raises(GatewayDisabled):
        translate(_editorial_source(), gateway=Gateway({}, {}), ruleset=rules(), desk="international")


def test_article_draft_uses_translation_and_never_grants_publication():
    import json

    from backend.modules.editorial.editor import create_draft
    from backend.modules.editorial.translator import translate

    raw = _editorial_source()
    tr = translate(raw, gateway=_editorial_gateway("translation", 'Alice گفت «12 units».'),
                   ruleset=rules(), desk="international")
    reply = json.dumps({k: tr.record["translated_text"] for k in ("title", "lead", "body", "short_summary")})
    draft = create_draft(raw, gateway=_editorial_gateway("editorial", reply),
                         ruleset=rules(), framework=_framework(), translation=tr,
                         required_names=("Alice",))
    assert draft.record["status"] == "draft"
    assert draft.record["source_refs"] == [raw["news_id"]]
    assert "TRANSLATION_REVIEW_REQUIRED" in draft.warnings
    assert evaluate_policy(draft.record, object_type="article", rights={}, context=context())["decision"] == "human_review"
    with pytest.raises(ValueError, match="translation/source mismatch"):
        create_draft({**raw, "original_text": "changed"}, gateway=_editorial_gateway("editorial", reply),
                     ruleset=rules(), framework=_framework(), translation=tr)


def test_article_holds_fabricated_facts_and_rejects_malformed_output():
    import json

    from backend.modules.editorial.editor import create_draft
    from backend.modules.editorial.translator import translate

    raw = _editorial_source()
    tr = translate(raw, gateway=_editorial_gateway("translation", 'Alice گفت «12 units».'),
                   ruleset=rules(), desk="international")
    bad = json.dumps({k: "Alice گفت «99 میلیارد»" for k in ("title", "lead", "body", "short_summary")})
    draft = create_draft(raw, gateway=_editorial_gateway("editorial", bad), ruleset=rules(),
                         framework=_framework(), translation=tr)
    assert draft.record["status"] == "held"
    assert {"FACT_NUMBER_MISMATCH", "FABRICATED_QUOTE"} <= set(draft.warnings)
    with pytest.raises(ValueError, match="article response"):
        create_draft(raw, gateway=_editorial_gateway("editorial", "not-json"),
                     ruleset=rules(), framework=_framework(), translation=tr)
