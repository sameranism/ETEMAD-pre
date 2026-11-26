"""P06/S02-009: bounded conditional feed requests for approved foreign sources.

Only mock HTTP transport is wired in this slice. Real transport requires a
separate authorization and an egress policy; never fall back to live HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_ATTEMPTS = 2


class ForeignFeedError(RuntimeError):
    """Safe diagnostic; no source URLs or credentials in messages."""


def approved_feed_url(source: Mapping[str, Any]) -> str:
    if (source.get("stream") != "foreign" or source.get("enabled") is not True
            or source.get("license_scope") not in {"metadata", "summary", "full_text"}
            or source.get("auth_ref") is not None):
        raise ForeignFeedError("source not approved for unauthenticated foreign RSS")
    url = source.get("feed_url")
    if not isinstance(url, str):
        raise ForeignFeedError("missing approved feed URL")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.fragment or any(ch.isspace() or ord(ch) < 32 for ch in url)):
            raise ValueError
        _ = parts.port
    except ValueError:
        raise ForeignFeedError("invalid approved feed URL") from None
    return url


@dataclass(frozen=True)
class ForeignFeedResponse:
    url: str
    body: bytes
    status_code: int
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


class ForeignFeedClient:
    def __init__(self, transport: httpx.MockTransport, *, timeout: float = 10.0) -> None:
        if not isinstance(transport, httpx.MockTransport) or not 0 < timeout <= 30:
            raise ValueError("fixture transport and bounded timeout required")
        self._transport = transport
        self._timeout = timeout

    def fetch(self, source: Mapping[str, Any], *, etag: str | None = None,
              last_modified: str | None = None) -> ForeignFeedResponse:
        url = approved_feed_url(source)
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml"}
        for name, value in (("If-None-Match", etag), ("If-Modified-Since", last_modified)):
            if value:
                if len(value) > 256 or any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
                    raise ForeignFeedError("invalid conditional header")
                headers[name] = value
        with httpx.Client(transport=self._transport, timeout=self._timeout,
                          follow_redirects=False, trust_env=False) as client:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    with client.stream("GET", url, headers=headers) as response:
                        status = response.status_code
                        if status == 429:
                            raise ForeignFeedError("rate limited; defer to scheduler")
                        if status >= 500 and attempt + 1 < MAX_ATTEMPTS:
                            continue
                        if status not in (200, 304):
                            raise ForeignFeedError(f"feed HTTP status {status}")
                        body = bytearray()
                        if status == 200:
                            for chunk in response.iter_bytes():
                                if len(body) + len(chunk) > MAX_BODY_BYTES:
                                    raise ForeignFeedError("feed exceeds size limit")
                                body.extend(chunk)
                        return ForeignFeedResponse(
                            url, bytes(body), status, response.headers.get("ETag"),
                            response.headers.get("Last-Modified"),
                        )
                except httpx.TransportError:
                    if attempt + 1 == MAX_ATTEMPTS:
                        raise ForeignFeedError("feed transport failed") from None
        raise ForeignFeedError("feed unavailable")
