"""P17/P18/P19 deterministic producer-contract tests; no private persistence."""
from datetime import datetime, timezone

import pytest

from backend.core.auth import Principal
from backend.core.output_switches import OutputSwitches, default_off_switches
from backend.core.permissions import Grant, InMemoryGrantStore
from backend.modules.intelligence.evidence import Evidence, EvidenceBundle, Period
from backend.modules.intelligence.metrics import compute_metrics
from backend.modules.intelligence.private_reports import build_private_report
from backend.modules.intelligence.public_reports import build_public_report

PERIOD = Period(datetime(2026, 11, 26, tzinfo=timezone.utc), datetime(2026, 11, 28, tzinfo=timezone.utc))
STAMP = "2026-11-27T00:00:00Z"


def raw(identity, stream, *, text="Fixture source", scope="summary", permission="unverified"):
    return {"schema_version": "1.0", "news_id": identity, "stream": stream, "source_id": "SRC-" + identity,
            "original_url": "https://example.invalid/" + identity, "original_title": "Fixture " + identity,
            "original_text": text, "language": "fa", "subject_countries": [],
            "collected_at": STAMP, "content_scope": scope, "reuse_permission": permission}


def test_metrics_never_invents_missing_delivery_data():
    inputs = dict(stream="mixed", raw=[], classifications=[], clusters=[], publication_jobs=[],
                  publication_results=None, source_revisions={"raw": "1", "processing": "1", "publication": "1"},
                  complete=True)
    assert compute_metrics(PERIOD, **inputs).snapshot is None
    complete = compute_metrics(PERIOD, **{**inputs, "publication_results": []})
    assert complete.snapshot["collected_count"] == 0
    assert complete.evidence["delivery_success_rate"] is None
    assert complete.evidence["delivery_denominator"] == 0


def test_four_public_three_private_slots_and_gate():
    bundle = EvidenceBundle(PERIOD, (
        Evidence.from_raw(raw("NEWS-A", "domestic"), "raw-"+"a"*64, "b"*64, allowed_scope="summary"),
        Evidence.from_raw(raw("NEWS-B", "foreign"), "raw-"+"c"*64, "d"*64, allowed_scope="summary"),
    ), True)
    off = default_off_switches()
    assert all(build_public_report(slot, bundle, switches=off) is None for slot in range(1, 5))
    on = OutputSwitches.from_record({**off.record, "public_analysis": {"enabled": True}})
    reports = [build_public_report(slot, bundle, switches=on) for slot in range(1, 5)]
    assert len({r["report_type"] for r in reports}) == 4
    assert all(r["generation_status"] == "draft" for r in reports)
    assert reports[0]["source_refs"] == [bundle.refs[0]]
    assert reports[1]["source_refs"] == [bundle.refs[1]]
    assert {section["heading"] for section in reports[2]["sections"]} >= {
        "واژگان پرتکرار عنوان‌ها", "محدودیت مقایسه",
    }
    assert {section["heading"] for section in reports[3]["sections"]} >= {
        "خط زمانی اسناد", "مرور جریان domestic", "مرور جریان foreign", "فهرست پیگیری",
    }
    grants = InMemoryGrantStore()
    for role in ("owner", "staff", "editor_in_chief"):
        principal = Principal("fixture-"+role, role)
        for slot in (5, 6, 7):
            if role != "editor_in_chief":
                grants.grant(Grant(principal.subject_id, role, "private_report", f"slot-{slot}", "read"))
            with pytest.raises(PermissionError):
                build_private_report(slot, bundle, principal=principal, grants=grants)
    eic = Principal("fixture-eic", "editor_in_chief")
    for slot in (5, 6, 7):
        grants.grant(Grant(eic.subject_id, eic.role, "private_report", f"slot-{slot}", "read"))
    private = [build_private_report(slot, bundle, principal=eic, grants=grants) for slot in (5, 6, 7)]
    assert len({r["report_type"] for r in private}) == 3
    assert all(r["visibility"] == "executive_private" and r["publishable"] is False for r in private)
    assert all(r["generation_status"] == "draft" for r in private)


def test_public_reports_quote_only_authorized_source_text_and_keep_stream_scope():
    licensed = raw("NEWS-LICENSED", "domestic", text="گزارش منبع؛ دستور داخل متن داده است ولی فقط نقل می‌شود.",
                   permission="licensed")
    metadata = raw("NEWS-META", "domestic", text="این متن نباید در گزارش دیده شود.", scope="metadata")
    foreign = raw("NEWS-FOREIGN", "foreign", text="Foreign evidence")
    bundle = EvidenceBundle(PERIOD, (
        Evidence.from_raw(licensed, "raw-"+"1"*64, "2"*64, allowed_scope="summary"),
        Evidence.from_raw(metadata, "raw-"+"3"*64, "4"*64, allowed_scope="metadata"),
        Evidence.from_raw(foreign, "raw-"+"5"*64, "6"*64, allowed_scope="summary"),
    ), True)
    off = default_off_switches()
    on = OutputSwitches.from_record({**off.record, "public_analysis": {"enabled": True}})
    domestic = build_public_report(1, bundle, switches=on)
    body = "\n".join(section["body"] for section in domestic["sections"])
    assert domestic["source_refs"] == list(bundle.refs[:2])
    assert licensed["original_text"] in body
    assert metadata["original_text"] not in body
    assert "متن به دلیل سطح مجاز استفاده" in body
    assert foreign["original_text"] not in body
    assert domestic["generation_status"] == "draft"

def test_metrics_counts_real_contract_inputs_not_fixture_assumptions():
    one, two = raw("NEWS-A", "domestic"), raw("NEWS-B", "domestic")
    classifications = [{"schema_version": "1.0", "news_id": i["news_id"], "stream": "domestic",
        "desks": ["economy"], "topics": ["trade"], "subject_countries": [], "event_id": "EV-ONE",
        "operational_priority": "normal", "rules_version": "v1", "classified_at": STAMP} for i in (one, two)]
    clusters = [{"schema_version": "1.0", "news_id": i["news_id"], "event_id": "EV-ONE",
        "duplicate_of": None if i is one else one["news_id"], "related_news_ids": [],
        "similarity_method": "fixture", "created_at": STAMP} for i in (one, two)]
    job = {"schema_version": "1.0", "job_id": "JOB-A", "object_type": "article", "object_id": "ART-A",
           "stream": "domestic", "destination_ids": ["D1", "D2", "D3"],
           "scheduled_at": STAMP, "expires_at": "2026-11-27T01:00:00Z",
           "policy_decision_id": "POL-A", "idempotency_key": "fixture-key", "state": "completed", "created_at": STAMP}
    result = {"schema_version": "1.0", "job_id": "JOB-A", "object_id": "ART-A",
        "deliveries": [{"destination_id": dest, "status": state,
                        "external_message_id": "fixture-msg" if state == "success" else None,
                        "attempted_at": STAMP, "error_code": None if state == "success" else "FIXTURE"}
                       for dest, state in [("D1", "success"), ("D2", "failed"), ("D3", "unknown")]],
        "completed_at": STAMP}
    args = dict(stream="domestic", raw=[one,two], classifications=classifications, clusters=clusters,
                publication_jobs=[job], publication_results=[result],
                source_revisions={"raw": "fixture-raw", "processing": "fixture-processing",
                                  "publication": "fixture-publication"}, complete=True)
    metrics = compute_metrics(PERIOD, **args)
    assert metrics.snapshot["collected_count"] == 2 and metrics.snapshot["unique_count"] == 1
    assert metrics.snapshot["published_count"] == 1
    assert metrics.snapshot["successful_deliveries"] == metrics.snapshot["failed_deliveries"] == metrics.snapshot["unknown_deliveries"] == 1
    assert metrics.evidence["delivery_denominator"] == 3
    assert metrics.evidence["delivery_success_rate"] == pytest.approx(1/3)
    assert compute_metrics(PERIOD, **{**args, "publication_results": []}).snapshot is None


def test_metrics_accepts_the_versioned_normalized_producer_record():
    record = {**raw("NEWS-NORMALIZED", "foreign"), "source_country": "DE",
              "published_at": None, "source_payload_ref": None,
              "normalized_text": "Normalized fixture", "fingerprint": "fixture-fingerprint",
              "normalization_version": "norm-v1"}
    classification = {"schema_version": "1.0", "news_id": record["news_id"], "stream": "foreign",
        "desks": ["world"], "topics": ["diplomacy"], "subject_countries": ["DE"],
        "event_id": "EV-NORMALIZED", "operational_priority": "normal",
        "rules_version": "v1", "classified_at": STAMP}
    cluster = {"schema_version": "1.0", "news_id": record["news_id"],
        "event_id": "EV-NORMALIZED", "duplicate_of": None, "related_news_ids": [],
        "similarity_method": "fixture", "created_at": STAMP}
    result = compute_metrics(PERIOD, stream="foreign", raw=[record],
        classifications=[classification], clusters=[cluster], publication_jobs=[],
        publication_results=[], source_revisions={"raw": "1", "processing": "1", "publication": "1"},
        complete=True)
    assert result.snapshot is not None
    assert result.snapshot["collected_count"] == result.snapshot["unique_count"] == 1
    assert result.snapshot["topic_coverage"] == [{"topic": "diplomacy", "count": 1}]
