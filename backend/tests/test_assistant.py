"""P16 assistant uses existing grants and never routes into public Studio."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.dependencies import build_services
from backend.app.main import create_app
from backend.core.auth import Principal, issue_session
from backend.core.permissions import Grant

KEY = "assistant-test-session-key"
PAYLOAD = {
    "query": "Atlas",
    "period_start": "2026-11-26T00:00:00Z",
    "period_end": "2026-11-28T00:00:00Z",
    "stream": "foreign",
}
PATH = "/v1/analysis/assistant"


class EmptyArchive:
    """An empty authorized P26 read, without network or live data."""

    def candidates_in_period(self, **kwargs):
        assert kwargs["start"] < datetime(2026, 11, 27, tzinfo=timezone.utc) < kwargs["end"]
        return []


@pytest.fixture
def assistant():
    services = build_services(environ={"ETEMAD_SESSION_KEY": KEY})
    services.news_archive = EmptyArchive()
    principal = Principal("assistant-staff", "staff")
    headers = {"Authorization": "Bearer " + issue_session(principal, services.session_key)}
    with TestClient(create_app(services=services)) as client:
        yield client, services, headers


def grant(services, resource, action):
    item = Grant("assistant-staff", "staff", resource, "default", action)
    services.grants.grant(item)
    return item


def test_catalog_and_dispatch_are_grant_bound(assistant):
    client, services, headers = assistant
    request = {"action": "research", "evidence": PAYLOAD}
    assert client.get(PATH).status_code == 401
    assert client.post(PATH, json=request).status_code == 401
    assert client.get(PATH, headers=headers).status_code == 403
    assert client.post(PATH, headers=headers, json=request).status_code == 403
    chat_grant = grant(services, "analysis_workspace", "chat")
    assert client.post(PATH, headers=headers, json=request).status_code == 403
    grant(services, "archive", "search")
    catalog = client.get(PATH, headers=headers)
    assert catalog.status_code == 200 and catalog.headers["Cache-Control"] == "no-store"
    assert catalog.json()["product"]["audience"] == "internal_newsroom"
    assert catalog.json()["product"]["studio_audience"] == "public_subscription"
    assert [t["action"] for t in catalog.json()["tools"]] == ["research"]
    result = client.post(PATH, headers=headers, json=request)
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    assert result.json()["mode"] == "extractive"
    assert result.json()["result"]["citations"] == []
    assert client.post(PATH, headers=headers, json={**request, "action": "analysis"}).status_code == 403
    grant(services, "analysis_workspace", "run")
    assert client.post(PATH, headers=headers, json={**request, "action": "analysis"}).json()["result"] is None
    services.grants.revoke(chat_grant)
    assert client.post(PATH, headers=headers, json=request).status_code == 403


@pytest.mark.parametrize("extra", [
    {"action": "studio"},
    {"action": "private_report"},
    {"action": "publish"},
    {"action": "https://example.invalid"},
    {"action": "public_report", "report_slot": 5},
    {"action": "public_report"},
    {"report_slot": 1},
    {"subject_id": "another"},
    {"evidence": {**PAYLOAD, "classification": "executive_private"}},
])
def test_unregistered_actions_and_scope_escalation_are_rejected(assistant, extra):
    client, services, headers = assistant
    grant(services, "analysis_workspace", "chat")
    grant(services, "archive", "search")
    response = client.post(PATH, headers=headers, json={"action": "research", "evidence": PAYLOAD, **extra})
    assert response.status_code == 400


def test_research_and_report_use_only_current_authorized_evidence(assistant):
    from backend.core.output_switches import OutputSwitches, default_off_switches

    client, services, headers = assistant
    raw = {"schema_version": "1.0", "news_id": "NEWS-ASSISTANT-1", "stream": "foreign",
           "source_id": "SRC-ASSISTANT-1", "original_url": "https://example.invalid/atlas",
           "original_title": "Atlas evidence", "original_text": "Atlas source records a change.",
           "language": "en", "subject_countries": [], "collected_at": "2026-11-27T00:00:00Z",
           "content_scope": "summary", "reuse_permission": "unverified"}
    artifact = "raw-" + "a" * 64
    loads = []

    class Archive:
        def candidates_in_period(self, **kwargs):
            return [raw, {**raw, "news_id": "NEWS-ASSISTANT-DENIED"}]

        def versions(self, news_id):
            loads.append(news_id)
            assert news_id == raw["news_id"]
            return [{"artifact_id": artifact, "sha256": "b" * 64}]

        def original(self, news_id, artifact_id):
            assert news_id == raw["news_id"] and artifact_id == artifact
            return raw

    services.news_archive = Archive()
    services.sources.register({"source_id": raw["source_id"], "site_url": "https://example.invalid",
        "feed_url": "https://example.invalid/rss", "country": "DE", "language": "en",
        "stream": "foreign", "license_scope": "summary", "enabled": True})
    for action in ("chat", "report"):
        grant(services, "analysis_workspace", action)
    grant(services, "archive", "search")
    for resource in ("archive", "newsroom"):
        services.grants.grant(Grant("assistant-staff", "staff", resource, raw["news_id"], "read"))
    request = {"action": "research", "evidence": PAYLOAD}
    result = client.post(PATH, headers=headers, json=request)
    assert result.status_code == 200
    assert result.json()["result"]["citations"] == [{"source_ref": artifact, "quote_span": raw["original_text"]}]
    assert loads == [raw["news_id"]]
    report_request = {**request, "action": "public_report", "report_slot": 2}
    assert [t["action"] for t in client.get(PATH, headers=headers).json()["tools"]] == ["research"]
    assert client.post(PATH, headers=headers, json=report_request).status_code == 422
    record = {**default_off_switches().record, "public_analysis": {"enabled": True}}
    services.switches = lambda: OutputSwitches.from_record(record)
    assert [t["action"] for t in client.get(PATH, headers=headers).json()["tools"]] == ["research", "public_report"]
    report = client.post(PATH, headers=headers, json=report_request)
    assert report.status_code == 200
    assert report.json()["mode"] == "bounded_draft"
    assert report.json()["result"]["source_refs"] == [artifact]
    assert loads == [raw["news_id"], raw["news_id"]]


def test_auto_routes_read_only_and_never_bypasses_grants(assistant):
    client, services, headers = assistant
    grant(services, "archive", "search")
    grant(services, "analysis_workspace", "chat")
    req = {"action": "auto", "evidence": PAYLOAD}
    assert client.post(PATH, headers=headers, json=req).json()["assistant_action"] == "research"
    analysis_request = {**req, "evidence": {**PAYLOAD, "query": "تحلیل پوشش اطلس"}}
    assert client.post(PATH, headers=headers, json=analysis_request).status_code == 403
    grant(services, "analysis_workspace", "run")
    assert client.post(PATH, headers=headers, json=analysis_request).json()["assistant_action"] == "analysis"
    # No wording in the user's query can select the studio, private data,
    # publication or report generation in automatic mode.
    for query in ("publish", "studio", "گزارش محرمانه"):
        body = client.post(PATH, headers=headers, json={**req, "evidence": {**PAYLOAD, "query": query}}).json()
        assert body["assistant_action"] == "research"
    assert client.post(PATH, headers=headers, json={**req, "report_slot": 1}).status_code == 400
