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
