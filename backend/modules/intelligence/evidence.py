"""P15 immutable, rights-scoped news evidence; never a private-report container."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from backend.core.contracts import validate_payload

_RAW_ID = re.compile(r"raw-[a-f0-9]{64}")


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if (self.start.tzinfo is None or self.end.tzinfo is None
                or self.start.utcoffset() is None or self.end.utcoffset() is None
                or not self.start < self.end or self.end - self.start > timedelta(days=31)):
            raise ValueError("invalid bounded evidence period")

    def as_record(self) -> dict[str, str]:
        return {"from": self.start.astimezone(timezone.utc).isoformat(),
                "to": self.end.astimezone(timezone.utc).isoformat()}


@dataclass(frozen=True)
class Evidence:
    news_id: str
    artifact_id: str
    source_id: str
    stream: str
    title: str
    text: str | None
    original_url: str
    collected_at: datetime
    reuse_permission: str
    scope: str
    checksum: str
    redacted: bool

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], artifact_id: str, checksum: str, *,
                 allowed_scope: str) -> Evidence:
        validate_payload("raw_news", raw)
        if not _RAW_ID.fullmatch(artifact_id) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("invalid archive evidence identity")
        scopes = {"metadata": 0, "summary": 1, "full_text": 2}
        if allowed_scope not in scopes:
            raise ValueError("unknown source rights")
        redacted = (scopes[allowed_scope] < scopes[raw["content_scope"]]
                    or raw["content_scope"] == "metadata"
                    or raw["reuse_permission"] not in {"licensed", "unverified"})
        stamp = datetime.fromisoformat(raw["collected_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("evidence timestamp requires timezone")
        return cls(raw["news_id"], artifact_id, raw["source_id"], raw["stream"], raw["original_title"],
                   None if redacted else raw["original_text"], raw["original_url"], stamp,
                   raw["reuse_permission"], raw["content_scope"], checksum, redacted)

    def reference(self) -> dict[str, Any]:
        return {"source_ref": self.artifact_id, "news_id": self.news_id, "source_id": self.source_id,
                "stream": self.stream, "title": self.title, "original_url": self.original_url,
                "collected_at": self.collected_at.isoformat(), "sha256": self.checksum,
                "reuse_permission": self.reuse_permission, "content_scope": self.scope,
                "text_redacted": self.redacted}


@dataclass(frozen=True)
class EvidenceBundle:
    period: Period
    evidence: tuple[Evidence, ...]
    bounded: bool

    def __post_init__(self) -> None:
        if len(self.evidence) > 20 or len({e.artifact_id for e in self.evidence}) != len(self.evidence):
            raise ValueError("invalid evidence bundle")
        if any(not self.period.start <= e.collected_at < self.period.end for e in self.evidence):
            raise ValueError("evidence outside time range")

    @property
    def refs(self) -> list[str]:
        return [e.artifact_id for e in self.evidence]

    @property
    def fingerprint(self) -> str:
        value = "\n".join(f"{e.artifact_id}:{e.checksum}:{e.redacted}" for e in self.evidence)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
