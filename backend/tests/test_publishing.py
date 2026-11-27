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


def test_telegram_official_adapter_is_default_off_and_handles_ambiguous_delivery():
    from backend.adapters.telegram import TelegramAdapter
    from backend.core.secrets import InMemorySecretStore
    from backend.modules.publishing.formatter import format_text

    dest = Destination({**destination("DST-TG", "telegram").config,
                        "address": "@test_channel", "credential_ref": "telegram/test"}, "telegram")
    approved = ApprovedContent("article", ARTICLE, DECISION, content_hash(ARTICLE), "domestic")
    off = default_off_switches()
    job = plan_job(approved, dest, scheduled_at=NOW, deadline=NOW+timedelta(minutes=5),
                   now=NOW, switches=off)
    payload = format_text(approved, job, dest, max_chars=4096)
    secrets = InMemorySecretStore({"telegram/test": "123456:" + "a"*30})
    calls = []

    def transport(token, body):
        calls.append(body)
        return 200, b'{"ok":true,"result":{"message_id":42}}'

    adapter = TelegramAdapter("telegram", dest.config, secrets, transport=transport)
    assert adapter.publish(payload, idempotency_key="key").status == "skipped"
    assert calls == []
    live = TelegramAdapter("telegram", dest.config, secrets, transport=transport,
                           authorized=lambda: True)
    assert live.publish(payload, idempotency_key="key").message_id == "42"
    assert calls == [{"chat_id": "@test_channel", "text": payload["text"]}]
    assert live.status("key") is None
    for changed in ({"address": "@other_channel"}, {"credential_ref": "telegram/other"},
                    {"destination_id": "DST-OTHER"}):
        with pytest.raises(ValueError, match="destination binding changed"):
            live.validate_config({**dest.config, **changed})
    live.validate_config(dest.config)
    with pytest.raises(ValueError, match="explicit adapter identity"):
        TelegramAdapter(" ", dest.config, secrets, transport=transport)
    assert len(calls) == 1
    with pytest.raises(ValueError):
        live.dry_run({**payload, "destination_id": "wrong"})
    with pytest.raises(ValueError):
        live.dry_run({**payload, "text": "a"*4097})
    with pytest.raises(ValueError):
        AdapterRegistry({"telegram": live})
    for code, body, expected in (
        (400, b'{"ok":false}', "failed"),
        (429, b'{"ok":false}', "failed"),
        (500, b'{"ok":false}', "unknown"),
        (200, b'{"ok":true}', "unknown"),
        (200, b'{"ok":true,"result":{"message_id":42}}' + b" " * 1_048_576, "unknown"),
    ):
        def responder(token, data):
            return code, body
        live = TelegramAdapter("telegram", dest.config, secrets, transport=responder,
                               authorized=lambda: True)
        assert live.publish(payload, idempotency_key="key").status == expected

    def timeout(token, data):
        raise TimeoutError("synthetic transport timeout")
    live = TelegramAdapter("telegram", dest.config, secrets, transport=timeout,
                           authorized=lambda: True)
    assert live.publish(payload, idempotency_key="key").status == "unknown"
