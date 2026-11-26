"""P04/S02-008: country feed candidates from supplied, bounded evidence.

This module performs no network or database access. An authorized caller
supplies public site documents and optional feed responses. Technical feed
verification is not editorial approval; every exported candidate is disabled.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from backend.core.contracts import validate_payload

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_SITES = 100
MAX_CANDIDATES = 500
_FEED_TYPES = {"application/rss+xml", "application/atom+xml"}
_SENSITIVE_QUERY = re.compile(r"password|secret|token|credential|api[_-]?key|auth", re.I)


def _url(value: str) -> str:
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("invalid discovery URL")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None or parts.fragment):
        raise ValueError("discovery requires public HTTP(S) URL without credentials")
    if any(_SENSITIVE_QUERY.search(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        raise ValueError("credential-like query parameters are not supported")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        host = f"{host}:{port}"
    normalized = urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    if len(normalized.encode("utf-8")) > 2048:
        raise ValueError("discovery URL exceeds limit")
    return normalized


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evidence timestamps require a timezone")
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class DocumentEvidence:
    """A supplied response, not an instruction to fetch its URL."""

    url: str
    status_code: int
    body: bytes
    checked_at: datetime

    def text(self) -> str:
        _url(self.url)
        _utc(self.checked_at)
        if self.status_code != 200 or not 0 < len(self.body) <= MAX_DOCUMENT_BYTES:
            raise ValueError("document unavailable or outside size limit")
        return self.body.decode("utf-8-sig")

    def digest(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True)
class SiteEvidence:
    document: DocumentEvidence
    country: str
    language: str


@dataclass(frozen=True)
class CandidateEvidence:
    source_id: str
    discovery_uri: str
    discovery_sha256: str
    discovered_at: str
    feed_uri: str
    feed_checked_at: str | None
    feed_sha256: str | None
    verified: bool


@dataclass(frozen=True)
class DiscoveryResult:
    pack: dict[str, Any]
    evidence: tuple[CandidateEvidence, ...]

    def json_export(self) -> str:
        validate_payload("country_source_pack", self.pack)
        if any(source["enabled"] for source in self.pack["sources"]):
            raise ValueError("discovery exports must remain disabled")
        return json.dumps(self.pack, ensure_ascii=False, sort_keys=True, indent=2)

    def opml_export(self) -> str:
        self.json_export()
        root = ET.Element("opml", version="2.0")
        head = ET.SubElement(root, "head")
        ET.SubElement(head, "title").text = "ETEMAD unapproved feed candidates"
        body = ET.SubElement(root, "body")
        for source in self.pack["sources"]:
            ET.SubElement(body, "outline", {
                "text": source["source_id"], "title": source["source_id"],
                "type": "rss", "xmlUrl": source["feed_url"],
                "htmlUrl": source["site_url"], "etemadEnabled": "false",
                "etemadApproval": "pending", "language": source["language"],
            })
        return ET.tostring(root, encoding="unicode")


class _FeedLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = dict(attrs)
        if ("alternate" in (values.get("rel") or "").lower().split()
                and (values.get("type") or "").lower().split(";")[0].strip() in _FEED_TYPES):
            href = values.get("href")
            if href and len(self.links) < MAX_CANDIDATES:
                self.links.append(href)


def _is_feed(document: DocumentEvidence) -> bool:
    try:
        content = document.text()
        # No DTD/entity declarations or non-UTF-8 encodings are accepted.
        if "\x00" in content or re.search(r"<!\s*(DOCTYPE|ENTITY)", content, re.I):
            return False
        root = ET.fromstring(content)
        if root.tag == "rss":
            channel = root.find("channel")
            return channel is not None and bool((channel.findtext("title") or "").strip())
        return root.tag == "{http://www.w3.org/2005/Atom}feed" and bool(
            (root.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        )
    except (ValueError, UnicodeError, ET.ParseError):
        return False


def discover_country(
    country: str,
    sites: Sequence[SiteEvidence],
    *,
    language: str | None = None,
    feeds: Mapping[str, DocumentEvidence] | None = None,
    generated_at: datetime | None = None,
) -> DiscoveryResult:
    """Find advertised RSS/Atom candidates; no claim of exhaustive coverage."""
    if not re.fullmatch(r"[A-Z]{2}", country) or len(sites) > MAX_SITES:
        raise ValueError("invalid country or too many site documents")
    if language is not None and not language.strip():
        raise ValueError("invalid language filter")
    generated = _utc(generated_at or datetime.now(timezone.utc))
    checked = {_url(key): document for key, document in (feeds or {}).items()}
    sources: list[dict[str, Any]] = []
    evidence: list[CandidateEvidence] = []
    seen: set[str] = set()
    for site in sites:
        if site.country != country or (language is not None and site.language != language):
            continue
        if not site.language.strip():
            raise ValueError("site language is required")
        site_url = _url(site.document.url)
        parser = _FeedLinks()
        parser.feed(site.document.text())
        parser.close()
        for href in parser.links:
            try:
                feed_url = _url(urljoin(site_url, href))
            except ValueError:
                continue
            if feed_url in seen:
                continue
            if len(sources) >= MAX_CANDIDATES:
                raise ValueError("candidate limit exceeded")
            seen.add(feed_url)
            source_id = "SRC-" + country + "-" + hashlib.sha256(
                feed_url.encode("utf-8")
            ).hexdigest()[:24].upper()
            response = checked.get(feed_url)
            matched = response is not None and _url(response.url) == feed_url
            verified = bool(matched and response is not None and _is_feed(response))
            sources.append({
                "source_id": source_id, "site_url": site_url, "feed_url": feed_url,
                "api_base_url": None, "country": country, "language": site.language,
                "stream": "foreign", "auth_ref": None, "license_scope": "unknown",
                "enabled": False,
            })
            evidence.append(CandidateEvidence(
                source_id, site_url, site.document.digest(), _utc(site.document.checked_at),
                feed_url,
                _utc(response.checked_at) if matched and response is not None else None,
                response.digest() if matched and response is not None else None,
                verified,
            ))
    sources.sort(key=lambda source: str(source["source_id"]))
    evidence.sort(key=lambda item: item.source_id)
    identity = hashlib.sha256(json.dumps(
        sources, sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()[:24]
    pack = {
        "schema_version": "1.0", "pack_id": f"PACK-{country}-{identity}",
        "country": country, "generated_at": generated, "sources": sources,
        "verified_feed_count": sum(item.verified for item in evidence),
    }
    validate_payload("country_source_pack", pack)
    return DiscoveryResult(pack, tuple(evidence))
