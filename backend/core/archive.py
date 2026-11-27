"""B01/P26 archive records: identifiers, checksums and storage references.

The archive never copies another package's payload into its own tables; it keeps
a reference plus a checksum so integrity can be re-verified later.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.contracts import validate_payload
from backend.core.object_store import ObjectStore

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class InvalidArchiveKey(ValueError):
    """An object key or identifier would escape its scope."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_key(archive_id: str, extension: str = "json") -> str:
    """Content-scoped key, no path traversal, no user-controlled slashes."""
    if not _SAFE_ID.fullmatch(archive_id):
        raise InvalidArchiveKey("archive_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    if not re.fullmatch(r"[A-Za-z0-9]{1,8}", extension):
        raise InvalidArchiveKey("extension must be alphanumeric")
    return f"archive/{archive_id}.{extension}"


def build_archive_record(
    *,
    object_type: str,
    object_id: str,
    stream: str,
    classification: str,
    payload_ref: str,
    source_refs: list[str] | None = None,
    retention_policy_id: str = "standard-24m",
    stored_at: str | None = None,
    archive_id: str | None = None,
) -> dict[str, Any]:
    """Create and validate one ``archive_record`` v1.0."""
    record = {
        "schema_version": "1.0",
        "archive_id": archive_id or str(uuid4()),
        "object_type": object_type,
        "object_id": object_id,
        "stream": stream,
        "classification": classification,
        "payload_ref": payload_ref,
        "source_refs": list(source_refs or []),
        "retention_policy_id": retention_policy_id,
        "stored_at": stored_at or datetime.now(timezone.utc).isoformat(),
    }
    validate_payload("archive_record", record)
    return record


class ArchiveStore(Protocol):
    def put(self, record: Mapping[str, Any]) -> None:
        """Persist an already validated archive record."""

    def get(self, archive_id: str) -> dict[str, Any] | None:
        """Return a copy of the record or None."""


@dataclass
class InMemoryArchiveStore:
    """Fixture store; never durable."""

    _records: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def put(self, record: Mapping[str, Any]) -> None:
        validate_payload("archive_record", record)
        self._records[str(record["archive_id"])] = dict(record)

    def get(self, archive_id: str) -> dict[str, Any] | None:
        record = self._records.get(archive_id)
        return dict(record) if record is not None else None

    def __len__(self) -> int:
        return len(self._records)


class ArchiveIntegrityError(ValueError):
    """Stored original failed identity or checksum verification."""


class PostgresNewsArchive:
    """P26 raw-news-only archive. Metadata in PostgreSQL, originals in object storage.

    Private reports deliberately have no path into this index. The application
    must authorize discovery and retrieval per news ID and enforce current rights.
    """

    def __init__(self, engine: Engine, objects: ObjectStore) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("archive requires PostgreSQL")
        self._engine = engine
        self._objects = objects

    def capture(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        validate_payload("raw_news", raw)
        source = dict(raw)
        if any(not 1 <= len(str(source[k])) <= 128 for k in ("news_id", "source_id")):
            raise ValueError("invalid archive identity")
        # Never retain text beyond the source's declared rights/scope in a new copy.
        if source["content_scope"] == "metadata" or source["reuse_permission"] in {"blocked", "link_only"}:
            source["original_text"] = None
        # A collection retry must not fork the same original because of a new poll timestamp.
        identity = {k: v for k, v in source.items() if k != "collected_at"}
        checksum = sha256_hex(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8"))
        artifact_id = "raw-" + checksum
        key = "archive/news/" + checksum + ".json"
        params = {"news_id": source["news_id"], "stream": source["stream"], "source_id": source["source_id"],
                  "title": source["original_title"], "url": source["original_url"], "key": key,
                  "published": source.get("published_at"), "received": source["collected_at"], "artifact": artifact_id}
        with self._engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:news_id, 0))"), params)
            record = conn.execute(text("""
                SELECT stream,source_id,original_url FROM archive.news_records WHERE news_id=:news_id
            """), params).mappings().one_or_none()
            if record and (record["stream"], record["source_id"], record["original_url"]) != (
                    source["stream"], source["source_id"], source["original_url"]):
                raise ArchiveIntegrityError("archive news identity conflict")
            existing = conn.execute(text("""
                SELECT sha256 FROM archive.artifact_index WHERE artifact_id=:artifact AND news_id=:news_id
            """), params).scalar_one_or_none()
            if existing is not None:
                # Detect corruption rather than replace an immutable source object.
                data = self._objects.get(key)
                if data is None or sha256_hex(data) != existing:
                    raise ArchiveIntegrityError("archive original checksum mismatch")
                return self._record(json.loads(data), artifact_id, key)
            data = json.dumps(source, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8")
            prior = self._objects.get(key)
            if prior is not None:
                # Recover an orphan left by a failed database commit, only if
                # it represents exactly this source apart from collection time.
                try:
                    recovered = json.loads(prior)
                    validate_payload("raw_news", recovered)
                    if {k: v for k, v in recovered.items() if k != "collected_at"} != identity:
                        raise ValueError("wrong identity")
                except (ValueError, TypeError):
                    raise ArchiveIntegrityError("archive object conflict") from None
                data = prior
            else:
                if self._objects.put(key, data, content_type="application/json") != key:
                    raise ArchiveIntegrityError("archive object key changed")
            if self._objects.get(key) != data:
                raise ArchiveIntegrityError("archive object write verification failed")
            params["sha"] = sha256_hex(data)
            conn.execute(text("""
                INSERT INTO archive.news_records
                    (news_id,stream,source_id,title,original_url,source_ref,published_at,received_at,classification)
                VALUES (:news_id,:stream,:source_id,:title,:url,:key,CAST(:published AS timestamptz),
                        CAST(:received AS timestamptz),'internal')
                ON CONFLICT (news_id) DO NOTHING
            """), params)
            conn.execute(text("""
                INSERT INTO archive.artifact_index
                    (artifact_id,news_id,object_type,owner_project,object_uri,sha256,visibility)
                VALUES (:artifact,:news_id,'raw_news','P26',:key,:sha,'internal')
            """), params)
        return self._record(json.loads(data), artifact_id, key)

    @staticmethod
    def _record(source: Mapping[str, Any], artifact_id: str, key: str) -> dict[str, Any]:
        return build_archive_record(object_type="raw_news", object_id=str(source["news_id"]),
                                    stream=str(source["stream"]), classification="internal",
                                    payload_ref=key, archive_id=artifact_id,
                                    source_refs=[str(source["original_url"])],
                                    stored_at=str(source["collected_at"]))

    def search(self, query: str, *, stream: str | None = None, limit: int = 50,
               offset: int = 0) -> list[dict[str, Any]]:
        if not 0 <= len(query) <= 200 or stream not in {None, "domestic", "foreign", "manual"}:
            raise ValueError("invalid archive filter")
        if not 1 <= limit <= 100 or not 0 <= offset <= 5000:
            raise ValueError("invalid archive page")
        # Literal substring; user wildcard characters have no special meaning.
        escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT news_id,stream,source_id,title,original_url,published_at,received_at
                FROM archive.news_records
                WHERE classification='internal'
                  AND (CAST(:stream AS text) IS NULL OR stream=:stream)
                  AND (title ILIKE :q ESCAPE '!' OR original_url ILIKE :q ESCAPE '!')
                ORDER BY received_at DESC, news_id LIMIT :limit OFFSET :offset
            """), {"stream": stream, "q": "%" + escaped + "%", "limit": limit, "offset": offset}).mappings()
            return [{**dict(row), "received_at": row["received_at"].isoformat(),
                     "published_at": row["published_at"].isoformat() if row["published_at"] else None}
                    for row in rows]

    def candidates_in_period(self, *, start: datetime, end: datetime,
                             stream: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if (start.tzinfo is None or end.tzinfo is None or start >= end
                or stream not in {None, "domestic", "foreign", "manual"} or not 1 <= limit <= 100):
            raise ValueError("invalid archive period")
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT news_id,stream,source_id,title,original_url,published_at,received_at
                FROM archive.news_records
                WHERE classification='internal' AND received_at >= :start AND received_at < :end
                  AND (CAST(:stream AS text) IS NULL OR stream=:stream)
                ORDER BY received_at DESC, news_id LIMIT :limit
            """), {"start": start, "end": end, "stream": stream, "limit": limit}).mappings()
            return [{**dict(row), "received_at": row["received_at"].isoformat(),
                     "published_at": row["published_at"].isoformat() if row["published_at"] else None}
                    for row in rows]

    def versions(self, news_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT artifact_id,sha256,created_at FROM archive.artifact_index
                WHERE news_id=:id AND visibility='internal' ORDER BY created_at DESC,artifact_id LIMIT 100
            """), {"id": news_id}).mappings()
            return [{**dict(row), "created_at": row["created_at"].isoformat()} for row in rows]

    def original(self, news_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT object_uri,sha256 FROM archive.artifact_index
                WHERE news_id=:id AND artifact_id=:artifact AND visibility='internal'
                  AND object_type='raw_news'
            """), {"id": news_id, "artifact": artifact_id}).mappings().one_or_none()
        if row is None:
            return None
        data = self._objects.get(row["object_uri"])
        if data is None or sha256_hex(data) != row["sha256"]:
            raise ArchiveIntegrityError("archive original checksum mismatch")
        raw = json.loads(data)
        validate_payload("raw_news", raw)
        if raw["news_id"] != news_id:
            raise ArchiveIntegrityError("archive original identity mismatch")
        return dict(raw)
