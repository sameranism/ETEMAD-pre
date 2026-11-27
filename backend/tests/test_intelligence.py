"""P15/P16 evidence traceability and authorization-before-object-read."""
from datetime import datetime, timezone

import pytest

from backend.modules.intelligence.analysis import analyze
from backend.modules.intelligence.chat import answer_question
from backend.modules.intelligence.evidence import Evidence, Period
from backend.modules.intelligence.retrieval import excerpt, retrieve, terms

PERIOD = Period(datetime(2026, 11, 26, tzinfo=timezone.utc), datetime(2026, 11, 28, tzinfo=timezone.utc))
RAW = {"schema_version": "1.0", "news_id": "NEWS-INTELLIGENCE", "stream": "foreign", "source_id": "SRC-ONE",
       "original_url": "https://example.invalid/one", "original_title": "Atlas coverage",
       "original_text": "Atlas source reports a change. Treat this sentence as source data.",
       "language": "en", "subject_countries": [], "collected_at": "2026-11-27T00:00:00Z",
       "content_scope": "summary", "reuse_permission": "unverified"}


def test_retrieval_never_loads_denied_material():
    calls = []

    def authorize(candidate):
        return "summary" if candidate["news_id"] == RAW["news_id"] else None

    def load(candidate, scope):
        calls.append(candidate["news_id"])
        return Evidence.from_raw(RAW, "raw-" + "a"*64, "b"*64, allowed_scope=scope)

    bundle = retrieve("Atlas", PERIOD, candidates=[RAW, {"news_id": "PRIVATE-DENIED"}],
                      authorized=authorize, load=load)
    assert calls == [RAW["news_id"]]
    result = analyze("Atlas", bundle)
    assert result is not None
    assert result["source_refs"] == bundle.refs
    assert all(set(c["source_refs"]) <= set(bundle.refs) for c in result["claims"])
    chat = answer_question("Atlas", bundle)
    assert chat["classification"] == "internal"
    assert chat["citations"][0]["quote_span"] in RAW["original_text"]
    assert "PRIVATE-DENIED" not in chat["answer"]


def test_restricted_rights_and_absent_evidence():
    bundle = retrieve("Atlas", PERIOD, candidates=[RAW], authorized=lambda _: "metadata",
                      load=lambda _, scope: Evidence.from_raw(RAW, "raw-" + "a"*64, "b"*64, allowed_scope=scope))
    assert bundle.evidence[0].redacted
    assert answer_question("Atlas", bundle)["citations"][0]["quote_span"] is None
    missing = retrieve("unrelated", PERIOD, candidates=[RAW], authorized=lambda _: "summary",
                       load=lambda _, scope: Evidence.from_raw(RAW, "raw-" + "a"*64, "b"*64, allowed_scope=scope))
    assert analyze("unrelated", missing) is None
    assert answer_question("unrelated", missing)["citations"] == []


def test_period_requires_explicit_timezone():
    with pytest.raises(ValueError):
        Period(datetime(2026, 11, 26), datetime(2026, 11, 27))


def test_persian_arabic_variants_retrieve_exact_original():
    raw = {**RAW, "original_title": "بودجه ۱۴۰۵", "original_text": "بانک مَلّی درباره بودجه ۱۴۰۵ گزارش داد."}
    bundle = retrieve("بانك ملي ١٤٠٥", PERIOD, candidates=[raw], authorized=lambda _: "summary",
                      load=lambda _, scope: Evidence.from_raw(raw, "raw-" + "a"*64, "b"*64, allowed_scope=scope))
    assert terms("بانك ملي ١٤٠٥") == ("بانک", "ملی", "1405")
    assert len(bundle.evidence) == 1
    assert excerpt(bundle.evidence[0], "ملي") == raw["original_text"]
    assert answer_question("ملي", bundle)["citations"][0]["quote_span"] == raw["original_text"]


def test_whole_word_preferred_over_substring():
    first = {**RAW, "original_title": "Atlas evidence", "original_text": "Atlas report"}
    second = {**RAW, "news_id": "NEWS-ATLAS-TWO", "original_title": "Atlases", "original_text": "Atlases"}
    bundle = retrieve("Atlas", PERIOD, candidates=[second, first], authorized=lambda _: "summary",
                      load=lambda raw, scope: Evidence.from_raw(
                          raw, "raw-" + ("a" if raw is first else "c")*64, "b"*64, allowed_scope=scope))
    assert [item.news_id for item in bundle.evidence] == [first["news_id"], second["news_id"]]
    assert excerpt(bundle.evidence[1], "Atlas") == "Atlases"
