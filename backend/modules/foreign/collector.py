"""P06/S02-010: foreign RSS/Atom to frozen raw_news records; no DB/network."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

from backend.core.contracts import validate_payload
from backend.modules.foreign.feed_client import (
    MAX_BODY_BYTES,
    ForeignFeedResponse,
    approved_feed_url,
)

ATOM = "{http://www.w3.org/2005/Atom}"
MAX_ITEMS = 500


def _time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo is not None else None


def _article_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return (p.scheme in {"http", "https"} and bool(p.hostname)
                and p.username is None and p.password is None
                and not any(ch.isspace() or ord(ch) < 32 for ch in url))
    except ValueError:
        return False


def collect_foreign(
    response: ForeignFeedResponse,
    source: Mapping[str, Any],
    *,
    collected_at: datetime | None = None,
    source_payload_ref: str | None = None,
) -> list[dict[str, Any]]:
    if response.url != approved_feed_url(source):
        raise ValueError("response source mismatch")
    if response.not_modified:
        return []
    if response.status_code != 200 or not 0 < len(response.body) <= MAX_BODY_BYTES:
        raise ValueError("invalid feed response")
    xml = response.body.decode("utf-8-sig")
    if "\x00" in xml or re.search(r"<!\s*(DOCTYPE|ENTITY)", xml, re.I):
        raise ValueError("unsafe feed XML")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ValueError("invalid feed XML") from None
    if root.tag == "rss" and root.find("channel") is not None:
        entries = root.findall("./channel/item")
        atom = False
    elif root.tag == ATOM + "feed":
        entries = root.findall(ATOM + "entry")
        atom = True
    else:
        raise ValueError("unsupported feed document")
    if len(entries) > MAX_ITEMS:
        raise ValueError("feed item limit exceeded")
    stamp = collected_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("collection time must be timezone aware")
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for item in entries:
        prefix = ATOM if atom else ""
        title = item.findtext(prefix + "title") or ""
        if atom:
            link = next((e.get("href", "") for e in item.findall(ATOM + "link")
                         if e.get("rel", "alternate") == "alternate"), "")
            original = item.findtext(ATOM + "summary")
            date = item.findtext(ATOM + "published") or item.findtext(ATOM + "updated")
        else:
            link = item.findtext("link") or ""
            original = item.findtext("description")
            date = item.findtext("pubDate")
        if not title.strip() or not _article_url(link) or link in seen:
            continue
        seen.add(link)
        scope = "metadata" if source["license_scope"] == "metadata" else "summary"
        identity = hashlib.sha256(
            (str(source["source_id"]) + "|" + link).encode("utf-8")
        ).hexdigest()[:32].upper()
        record = {
            "schema_version": "1.0", "news_id": "NEWS-" + identity,
            "stream": "foreign", "source_id": source["source_id"],
            "original_url": link, "original_title": title,
            "original_text": original if scope == "summary" else None,
            "language": source["language"], "source_country": source["country"],
            "subject_countries": [], "published_at": _time(date),
            "collected_at": stamp.astimezone(timezone.utc).isoformat(),
            "content_scope": scope,
            "reuse_permission": "link_only" if scope == "metadata" else "unverified",
            "source_payload_ref": source_payload_ref,
        }
        validate_payload("raw_news", record)
        records.append(record)
    return records
