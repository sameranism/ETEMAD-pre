"""B01/P03 source registry management.

A source is usable only when it is both enabled and rights-cleared. Registration
validates the whole ``source_registry`` document, so a malformed source cannot
be enabled by a side door.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, List, Mapping
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from backend.core.contracts import validate_payload
from backend.core.db import DatabaseRegistry

BLOCKED_SCOPE = "unknown"


class SourceNotFound(KeyError):
    """The requested source id is not registered."""


class SourceNotAllowed(RuntimeError):
    """The source exists but may not be used for this stream or rights scope."""


@dataclass
class SourceService:
    """In-memory registry fixture; the authoritative store is PostgreSQL."""

    registry_id: str = "default"
    _sources: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)
    _updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc), repr=False
    )

    def register(self, source: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(source)
        self._validate_with([candidate])
        with self._lock:
            source_id = str(candidate["source_id"])
            if self._sources.get(source_id) != candidate:
                self._sources[source_id] = candidate
                self._updated_at = datetime.now(timezone.utc)
        return dict(candidate)

    def set_enabled(self, source_id: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            source = self._sources.get(source_id)
            if source is None:
                raise SourceNotFound(source_id)
            updated = dict(source)
            updated["enabled"] = bool(enabled)
            if updated != source:
                self._sources[source_id] = updated
                self._updated_at = datetime.now(timezone.utc)
        self._validate_with(list(self._sources.values()))
        return dict(updated)

    def get(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            source = self._sources.get(source_id)
        if source is None:
            raise SourceNotFound(source_id)
        return dict(source)

    def list(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            values = [dict(item) for item in self._sources.values()]
        values.sort(key=lambda item: item["source_id"])
        if enabled_only:
            values = [item for item in values if item["enabled"]]
        return values

    def require_usable(self, source_id: str, stream: str | None = None) -> dict[str, Any]:
        source = self.get(source_id)
        if not source["enabled"]:
            raise SourceNotAllowed(f"source {source_id} is disabled")
        if source["license_scope"] == BLOCKED_SCOPE:
            raise SourceNotAllowed(f"source {source_id} has no reuse right")
        if stream is not None and source["stream"] not in (stream, "both"):
            raise SourceNotAllowed(f"source {source_id} does not serve stream {stream!r}")
        return source

    def snapshot(self, stream: str) -> dict[str, Any]:
        """Return a stable, rights-cleared stream snapshot for P05/P06.

        Snapshot identifiers are content-addressed. Consumers can persist and
        replay them without querying the central source database.
        """
        with self._lock:
            updated_at = self._updated_at
        return _build_stream_snapshot(
            registry_id=self.registry_id,
            stream=stream,
            sources=self.list(),
            updated_at=updated_at,
        )

    def document(self) -> dict[str, Any]:
        """The full ``source_registry`` record, validated before return."""
        record = {
            "schema_version": "1.0",
            "registry_id": self.registry_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sources": self.list(),
        }
        validate_payload("source_registry", record)
        return record

    def _validate_with(self, sources: List[dict[str, Any]]) -> None:
        validate_payload(
            "source_registry",
            {
                "schema_version": "1.0",
                "registry_id": self.registry_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "sources": sources,
            },
        )


def _build_stream_snapshot(
    *, registry_id: str, stream: str, sources: List[dict[str, Any]], updated_at: datetime,
) -> dict[str, Any]:
    if stream not in {"domestic", "foreign"}:
        raise ValueError("source snapshot stream must be domestic or foreign")
    selected = [
        dict(source) for source in sources
        if source["stream"] == stream
        and source["enabled"]
        and source["license_scope"] != BLOCKED_SCOPE
    ]
    selected.sort(key=lambda source: source["source_id"])
    canonical = json.dumps(
        {"stream": stream, "sources": selected},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    record = {
        "schema_version": "1.0",
        "registry_id": f"{registry_id}:{stream}:{digest}",
        "updated_at": updated_at.astimezone(timezone.utc).isoformat(),
        "sources": selected,
    }
    validate_payload("source_registry", record)
    return record


def _normalized_url(value: str) -> str:
    """Canonical identity for a configured endpoint; never fetch or resolve it."""
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("invalid source URL")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None or parts.fragment):
        raise ValueError("invalid source URL; use auth_ref for credentials")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        host = f"{host}:{port}"
    normalized = urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    if len(normalized.encode("utf-8")) > 2048:
        raise ValueError("source URL exceeds storage limit")
    return normalized


class PostgresSourceService(SourceService):
    """P03-owned central registry. No external fetch or candidate auto-approval.

    Callers must authorize writes before calling register/set_enabled.
    History is internal persistence, not a change to the frozen wire contract.
    """

    def __init__(self, registry: DatabaseRegistry) -> None:
        super().__init__()
        self._registry = registry

    def _engine(self) -> Engine:
        engine = self._registry.engine("central")
        if engine.dialect.name != "postgresql":
            raise ValueError("source registry requires PostgreSQL")
        return engine

    def _prepare(self, source: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        candidate = dict(source)
        self._validate_with([candidate])
        if candidate["enabled"] and candidate["license_scope"] == BLOCKED_SCOPE:
            raise SourceNotAllowed("source has no reuse right")
        for field_name in ("site_url", "feed_url", "api_base_url"):
            value = candidate.get(field_name)
            if value is not None:
                _normalized_url(value)
        endpoint = candidate.get("feed_url") or candidate.get("api_base_url") or candidate["site_url"]
        return candidate, _normalized_url(endpoint)

    @staticmethod
    def _lock_writes(conn: Connection) -> None:
        # Source registry is small; serialize changes to preserve revisions.
        conn.execute(text("LOCK TABLE sources.source_registry IN SHARE ROW EXCLUSIVE MODE"))

    def _store(
        self, conn: Connection, candidate: dict[str, Any], endpoint: str,
        *, candidate_only: bool = False,
    ) -> dict[str, Any]:
        row = conn.execute(text("""
            SELECT stream, url_normalized, revision_no, snapshot
            FROM sources.source_registry WHERE source_id = :id
        """), {"id": candidate["source_id"]}).mappings().one_or_none()
        if row is not None:
            if row["snapshot"] == candidate:
                return dict(candidate)
            if candidate_only:
                raise SourceNotAllowed("candidate cannot overwrite a registered source")
            if row["stream"] != candidate["stream"] or row["url_normalized"] != endpoint:
                raise ValueError("source identity conflict; use a new source id")
        revision = 1 if row is None else int(row["revision_no"]) + 1
        params = {
            "id": candidate["source_id"], "stream": candidate["stream"], "url": endpoint,
            "rev": revision, "snapshot": json.dumps(candidate, ensure_ascii=False),
        }
        conn.execute(text("""
            INSERT INTO sources.source_registry
                (source_id, stream, url_normalized, revision_no, snapshot)
            VALUES (:id, :stream, :url, :rev, CAST(:snapshot AS jsonb))
            ON CONFLICT (source_id) DO UPDATE SET revision_no = EXCLUDED.revision_no,
                snapshot = EXCLUDED.snapshot, updated_at = now()
        """), params)
        conn.execute(text("""
            INSERT INTO sources.source_history (source_id, revision_no, snapshot)
            VALUES (:id, :rev, CAST(:snapshot AS jsonb))
        """), params)
        return dict(candidate)

    def register(self, source: Mapping[str, Any]) -> dict[str, Any]:
        candidate, endpoint = self._prepare(source)
        with self._engine().begin() as conn:
            self._lock_writes(conn)
            return self._store(conn, candidate, endpoint)

    def register_candidate(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """Discovery candidates start disabled and never replace approved records."""
        candidate, endpoint = self._prepare({**source, "enabled": False})
        with self._engine().begin() as conn:
            self._lock_writes(conn)
            return self._store(conn, candidate, endpoint, candidate_only=True)

    def set_enabled(self, source_id: str, enabled: bool) -> dict[str, Any]:
        with self._engine().begin() as conn:
            self._lock_writes(conn)
            snapshot = conn.execute(text("""
                SELECT snapshot FROM sources.source_registry WHERE source_id = :id
            """), {"id": source_id}).scalar_one_or_none()
            if snapshot is None:
                raise SourceNotFound(source_id)
            candidate, endpoint = self._prepare({**snapshot, "enabled": enabled})
            return self._store(conn, candidate, endpoint)

    def get(self, source_id: str) -> dict[str, Any]:
        with self._engine().connect() as conn:
            snapshot = conn.execute(text("""
                SELECT snapshot FROM sources.source_registry WHERE source_id = :id
            """), {"id": source_id}).scalar_one_or_none()
        if snapshot is None:
            raise SourceNotFound(source_id)
        candidate, _ = self._prepare(snapshot)
        return candidate

    def list(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._engine().connect() as conn:
            snapshots = conn.execute(text("""
                SELECT snapshot FROM sources.source_registry ORDER BY source_id
            """)).scalars().all()
        candidates = [self._prepare(snapshot)[0] for snapshot in snapshots]
        return [s for s in candidates if s["enabled"]] if enabled_only else candidates

    def snapshot(self, stream: str) -> dict[str, Any]:
        with self._engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT snapshot, updated_at FROM sources.source_registry
                ORDER BY source_id
            """)).mappings().all()
        sources = [self._prepare(row["snapshot"])[0] for row in rows]
        updated_at = max(
            (row["updated_at"] for row in rows),
            default=datetime(1970, 1, 1, tzinfo=timezone.utc),
        )
        return _build_stream_snapshot(
            registry_id=self.registry_id,
            stream=stream,
            sources=sources,
            updated_at=updated_at,
        )

    def history(self, source_id: str) -> List[dict[str, Any]]:
        """Return ordered internal revisions without exposing credentials."""
        with self._engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT revision_no, snapshot FROM sources.source_history
                WHERE source_id = :id ORDER BY revision_no
            """), {"id": source_id}).mappings().all()
        return [
            {"revision_no": row["revision_no"], "source": self._prepare(row["snapshot"])[0]}
            for row in rows
        ]
