"""S02-015: P04 country discovery fixture tests; no external requests."""
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from backend.modules.foreign.country_discovery import (
    DocumentEvidence,
    SiteEvidence,
    discover_country,
)

NOW = datetime(2026, 11, 26, 19, 0, tzinfo=timezone.utc)
SITE = "https://example.invalid/"
FEED = "https://example.invalid/rss"
HTML = b"""<html><head>
<link rel="alternate" type="application/rss+xml" href="/rss">
<link rel="alternate" type="application/rss+xml" href="https://EXAMPLE.INVALID:443/rss">
<link rel="alternate" type="application/atom+xml" href="/unknown">
<link rel="alternate" type="application/rss+xml" href="javascript:alert(1)">
<link rel="alternate" type="application/rss+xml" href="https://example.invalid/?token=fixture">
</head></html>"""
RSS = b"<rss version='2.0'><channel><title>Fixture</title></channel></rss>"


def document(url, body, status=200):
    return DocumentEvidence(url, status, body, NOW)


def test_country_discovery_checks_evidence_without_approving_sources():
    result = discover_country("DE", [
        SiteEvidence(document(SITE, HTML), "DE", "de"),
        SiteEvidence(document(SITE, HTML), "FR", "fr"),
    ], language="de", feeds={FEED: document(FEED, RSS)}, generated_at=NOW)
    assert len(result.pack["sources"]) == 2
    assert result.pack["verified_feed_count"] == 1
    assert all(not x["enabled"] and x["license_scope"] == "unknown" for x in result.pack["sources"])
    assert all(x["country"] == "DE" and x["stream"] == "foreign" for x in result.pack["sources"])
    assert json.loads(result.json_export()) == result.pack
    outlines = ET.fromstring(result.opml_export()).findall("./body/outline")
    assert len(outlines) == 2
    assert all(x.get("etemadApproval") == "pending" and x.get("etemadEnabled") == "false" for x in outlines)
    checked = next(x for x in result.evidence if x.verified)
    assert checked.feed_uri == FEED and checked.feed_sha256 == document(FEED, RSS).digest()


@pytest.mark.parametrize("response", [
    document(FEED, b"<html>not a feed</html>"),
    document(FEED, RSS, 429),
    document("https://other.invalid/rss", RSS),
    document(FEED, b"<!DOCTYPE rss [<!ENTITY x 'unsafe'>]><rss><channel><title>&x;</title></channel></rss>"),
])
def test_bad_or_mismatched_response_is_unverified(response):
    result = discover_country("DE", [SiteEvidence(document(SITE, HTML), "DE", "de")],
                              feeds={FEED: response})
    assert result.pack["verified_feed_count"] == 0
    assert all(not x["enabled"] for x in result.pack["sources"])


def test_export_rejects_enabled_candidate_and_invalid_country():
    result = discover_country("DE", [SiteEvidence(document(SITE, HTML), "DE", "de")])
    result.pack["sources"][0]["enabled"] = True
    with pytest.raises(ValueError, match="disabled"):
        result.json_export()
    with pytest.raises(ValueError):
        discover_country("de", [])


def _approved_source(**updates):
    return {"source_id": "SRC-DE-FIXTURE", "stream": "foreign", "enabled": True,
            "license_scope": "summary", "auth_ref": None, "feed_url": FEED,
            "language": "de", "country": "DE", **updates}


def test_foreign_conditional_fetch_and_collection():
    import httpx

    from backend.modules.foreign.collector import collect_foreign
    from backend.modules.foreign.feed_client import ForeignFeedClient

    rss = b"""<rss version="2.0"><channel><title>Fixture</title>
    <item><title>Report about another country</title><link>https://example.invalid/news/1</link>
    <description>Original summary</description><pubDate>Thu, 26 Nov 2026 20:00:00 +0100</pubDate></item>
    <item><title>Duplicate</title><link>https://example.invalid/news/1</link></item>
    </channel></rss>"""
    def handler(request):
        if request.headers.get("If-None-Match") == '"fixture"':
            return httpx.Response(304)
        return httpx.Response(200, content=rss, headers={"ETag": '"fixture"'})
    client = ForeignFeedClient(httpx.MockTransport(handler))
    source = _approved_source()
    response = client.fetch(source)
    records = collect_foreign(response, source, collected_at=NOW)
    assert len(records) == 1
    assert records[0]["stream"] == "foreign"
    assert records[0]["source_country"] == "DE" and records[0]["subject_countries"] == []
    assert records[0]["original_text"] == "Original summary"
    assert records[0]["published_at"] == NOW.isoformat()
    assert collect_foreign(response, source, collected_at=NOW) == records
    metadata = collect_foreign(response, _approved_source(license_scope="metadata"), collected_at=NOW)
    assert metadata[0]["original_text"] is None
    assert metadata[0]["reuse_permission"] == "link_only"
    assert collect_foreign(client.fetch(source, etag=response.etag), source) == []


def test_foreign_client_denies_unapproved_and_rate_limited_sources():
    import httpx

    from backend.modules.foreign.feed_client import ForeignFeedClient, ForeignFeedError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429)
    client = ForeignFeedClient(httpx.MockTransport(handler))
    for source in (_approved_source(enabled=False), _approved_source(stream="domestic"),
                   _approved_source(license_scope="unknown"), _approved_source(auth_ref="fixture")):
        with pytest.raises(ForeignFeedError):
            client.fetch(source)
    assert calls == []
    with pytest.raises(ForeignFeedError, match="rate limited"):
        client.fetch(_approved_source())
    assert len(calls) == 1


def test_foreign_client_timeout_is_bounded():
    import httpx

    from backend.modules.foreign.feed_client import ForeignFeedClient, ForeignFeedError
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("fixture")
    client = ForeignFeedClient(httpx.MockTransport(handler))
    with pytest.raises(ForeignFeedError, match="transport failed"):
        client.fetch(_approved_source())
    assert len(calls) == 2


def test_foreign_collector_rejects_unsafe_or_mismatched_document():
    from backend.modules.foreign.collector import collect_foreign
    from backend.modules.foreign.feed_client import ForeignFeedResponse
    for body in (b"<html/>", b"<rss>", b"<!DOCTYPE rss><rss/>"):
        with pytest.raises(ValueError):
            collect_foreign(ForeignFeedResponse(FEED, body, 200), _approved_source())
    with pytest.raises(ValueError, match="source mismatch"):
        collect_foreign(ForeignFeedResponse("https://other.invalid/rss", RSS, 200), _approved_source())

def _bridge_raw(**updates):
    return {"schema_version": "1.0", "news_id": "NEWS-BRIDGE-FIXTURE", "stream": "foreign",
            "source_id": "SRC-DE-FIXTURE", "source_country": "DE", "language": "de",
            "original_url": "https://example.invalid/bridge/1", "original_title": "Fixture",
            "original_text": "Synthetic summary", "subject_countries": [],
            "collected_at": NOW.isoformat(), "content_scope": "summary",
            "reuse_permission": "unverified", **updates}


def test_bridge_signature_and_offline_resign():
    from dataclasses import replace

    from backend.modules.foreign.bridge import BridgeError, BridgeVerifier, sign_transfer

    key = b"fixture-only-foreign-signing-key-0001"
    verifier = BridgeVerifier({"foreign-fixture": key})
    raw = _bridge_raw()
    packet = sign_transfer(raw, sender="foreign-fixture", key=key, sent_at=1000)
    result = verifier.verify(packet, now=1000)
    assert result.raw == raw
    assert result.envelope["payload_ref"] == "sha256:" + result.digest
    assert result.envelope["classification"] == "internal"
    for invalid in (replace(packet, body=packet.body + b" "),
                    replace(packet, sent_at=1001), replace(packet, sender="unknown"),
                    replace(packet, signature="0" * 64)):
        with pytest.raises(BridgeError):
            verifier.verify(invalid, now=1000)
    with pytest.raises(BridgeError):
        verifier.verify(packet, now=2000)
    retry = sign_transfer(raw, sender="foreign-fixture", key=key, sent_at=2000)
    assert verifier.verify(retry, now=2000).envelope == result.envelope
    assert retry.signature != packet.signature


@pytest.mark.parametrize("updates", [
    {"stream": "domestic"}, {"content_scope": "full_text"},
    {"content_scope": "metadata", "reuse_permission": "link_only"},
    {"reuse_permission": "blocked"},
])
def test_bridge_denies_unsupported_rights(updates):
    from backend.modules.foreign.bridge import BridgeError, sign_transfer

    with pytest.raises(BridgeError):
        sign_transfer(_bridge_raw(**updates), sender="foreign-fixture",
                      key=b"fixture-only-foreign-signing-key-0001", sent_at=1000)
