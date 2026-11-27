"""P22-P25 contract tests; no actual outbound API."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.adapters.base import FixtureAdapter
from backend.adapters.registry import AdapterRegistry
from backend.core.output_switches import OutputSwitches, default_off_switches
from backend.modules.publishing.destinations import Destination
from backend.modules.publishing.dispatcher import FixtureLedger, dispatch_fixture
from backend.modules.publishing.scheduler import ApprovedContent, content_hash, plan_job

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)
ARTICLE = {"schema_version": "1.0", "article_id": "ART-FIXTURE", "news_id": "NEWS-FIXTURE",
           "stream": "domestic", "desk": "economy", "title": "عنوان نمونه", "lead": "نمونه",
           "body": "نمونه", "short_summary": "خلاصه", "source_refs": ["NEWS-FIXTURE"],
           "framework_version": "fixture:1", "editorial_rules_version": "fixture:1",
           "status": "ready_for_policy", "created_at": NOW.isoformat()}
DECISION = {"schema_version": "1.0", "decision_id": "POL-FIXTURE", "object_id": "ART-FIXTURE",
            "object_type": "article", "decision": "auto_allowed", "reason_codes": ["FIXTURE"],
            "evidence_refs": ["NEWS-FIXTURE"], "policy_version": "fixture:1",
            "decided_at": NOW.isoformat(), "expires_at": (NOW + timedelta(minutes=10)).isoformat()}


def destination(identity, platform):
    return Destination({"schema_version": "1.0", "destination_id": identity, "platform": platform,
        "name": identity, "address": "fixture:" + identity, "credential_ref": "fixture:reference",
        "desks": ["economy"], "allowed_streams": ["domestic"], "enabled": True,
        "connection_state": "verified", "updated_at": NOW.isoformat()}, identity)


def test_fixture_independent_delivery_and_no_blind_retry():
    approved = ApprovedContent("article", ARTICLE, DECISION, content_hash(ARTICLE), "domestic")
    first = destination("DST-A", "telegram")
    second = destination("DST-B", "bale")
    a = FixtureAdapter("DST-A", "telegram", outcome="unknown")
    b = FixtureAdapter("DST-B", "bale")
    adapters = AdapterRegistry({"DST-A": a, "DST-B": b})
    off = default_off_switches()
    on = OutputSwitches.from_record({**off.record, "publishing": {**off.record["publishing"],
        "enabled": True, "domestic_enabled": True}})
    kwargs = dict(scheduled_at=NOW, deadline=NOW+timedelta(minutes=5), now=NOW)
    held = plan_job(approved, first, switches=off, **kwargs)
    assert held["state"] == "held"
    job = plan_job(approved, first, switches=on, **kwargs)
    # A fixture group exercises independent processing; real scheduler returns per-destination jobs.
    grouped = {**job, "destination_ids": ["DST-A", "DST-B"]}
    ledger = FixtureLedger()
    def run(record=grouped, switches=on, dry_run=True, approval=approved, time=NOW):
        return dispatch_fixture(record, approval_for=lambda: approval, destinations={"DST-A": first, "DST-B": second},
                                adapters=adapters, ledger=ledger, switches=lambda: switches,
                                now=lambda: time, dry_run=dry_run)
    assert all(x["status"] == "skipped" for x in run()["result"]["deliveries"])
    assert run(held, dry_run=False)["result"]["deliveries"][0]["status"] == "skipped"
    assert all(x["status"] == "skipped" for x in run(switches=off, dry_run=False)["result"]["deliveries"])
    assert all(x["status"] == "skipped" for x in run(approval=None, dry_run=False)["result"]["deliveries"])
    assert not a.calls and not b.calls
    result = run(dry_run=False)
    assert result["mode"] == "fixture"
    assert [x["status"] for x in result["result"]["deliveries"]] == ["unknown", "success"]
    assert run(dry_run=False)["result"]["deliveries"] == result["result"]["deliveries"]
    assert len(a.calls) == len(b.calls) == 1
    assert all(x["status"] == "skipped" for x in run(time=NOW+timedelta(minutes=11), dry_run=False)["result"]["deliveries"])


def test_private_changed_and_unapproved_objects_denied():
    with pytest.raises(ValueError, match="private"):
        ApprovedContent("private_report", {}, DECISION, "x", "domestic")
    with pytest.raises(ValueError, match="approval"):
        ApprovedContent("article", {**ARTICLE, "title": "changed"}, DECISION, content_hash(ARTICLE), "domestic")
    with pytest.raises(ValueError, match="approval"):
        ApprovedContent("article", ARTICLE, {**DECISION, "decision": "human_review", "expires_at": None},
                        content_hash(ARTICLE), "domestic")
