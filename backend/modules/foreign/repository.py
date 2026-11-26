"""S02-012: foreign-owned persistence. Only a foreign Engine is supplied."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.contracts import validate_payload
from backend.modules.foreign.country_discovery import DiscoveryResult
from backend.modules.foreign.feed_client import approved_feed_url


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("poll timestamp requires timezone")


@dataclass(frozen=True)
class PollCheckpoint:
    source_id: str
    revision: int
    next_poll_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0


class ForeignRepository:
    """No DB registry, other role URLs, or cross-database joins."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("foreign storage requires PostgreSQL")
        self._engine = engine

    def save_candidates(self, result: DiscoveryResult) -> int:
        result.json_export()
        by_id = {item.source_id: item for item in result.evidence}
        sources = result.pack["sources"]
        if len(by_id) != len(result.evidence) or set(by_id) != {s["source_id"] for s in sources}:
            raise ValueError("candidate evidence mismatch")
        if len(sources) != len(by_id) or sum(item.verified for item in by_id.values()) != result.pack["verified_feed_count"]:
            raise ValueError("candidate evidence count mismatch")
        created = 0
        with self._engine.begin() as conn:
            for source in sources:
                item = by_id[source["source_id"]]
                if (source["country"] != result.pack["country"] or source["stream"] != "foreign"
                        or source["license_scope"] != "unknown" or source.get("auth_ref") is not None
                        or source["site_url"] != item.discovery_uri or source["feed_url"] != item.feed_uri):
                    raise ValueError("candidate evidence or rights mismatch")
                params = {"id": source["source_id"], "country": source["country"],
                          "url": source["feed_url"], "payload": _json(source),
                          "evidence": _json(asdict(item)), "verified": item.verified}
                inserted = conn.execute(text("""
                    INSERT INTO "foreign".source_candidates
                      (candidate_id,country_code,feed_url,payload,evidence,verified)
                    VALUES (:id,:country,:url,CAST(:payload AS jsonb),CAST(:evidence AS jsonb),:verified)
                    ON CONFLICT (candidate_id) DO NOTHING RETURNING candidate_id
                """), params).scalar_one_or_none()
                if inserted is None:
                    existing = conn.execute(text("""
                        SELECT payload FROM "foreign".source_candidates WHERE candidate_id=:id
                    """), params).scalar_one()
                    if existing != source:
                        raise ValueError("candidate identity conflict")
                created += int(inserted is not None)
        return created

    def candidates(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("invalid page limit")
        with self._engine.connect() as conn:
            return [dict(row) for row in conn.execute(text("""
                SELECT payload, evidence, verified FROM "foreign".source_candidates
                ORDER BY candidate_id LIMIT :limit
            """), {"limit": limit}).mappings()]

    def save_raw(self, record: Mapping[str, Any], *, source: Mapping[str, Any]) -> bool:
        approved_feed_url(source)
        stored = json.loads(_json(dict(record)))
        validate_payload("raw_news", stored)
        if (stored["stream"] != "foreign" or stored["source_id"] != source["source_id"]
                or stored.get("source_country") != source["country"]
                or stored["language"] != source["language"] or stored["reuse_permission"] == "blocked"
                or stored["content_scope"] not in {"metadata", "summary"}
                or (stored["content_scope"] == "metadata" and stored["original_text"] is not None)
                or (source["license_scope"] == "metadata" and stored["content_scope"] != "metadata")):
            raise ValueError("raw record outside approved foreign source scope")
        digest = _hash(_json([stored["language"], stored["original_title"], stored["original_text"]]))
        params = {"id": stored["news_id"], "source": stored["source_id"],
                  "country": stored["source_country"], "url": stored["original_url"],
                  "fetched": stored["collected_at"], "ref": stored.get("source_payload_ref"),
                  "digest": digest, "url_hash": _hash(stored["original_url"]), "payload": _json(stored)}
        with self._engine.begin() as conn:
            inserted = conn.execute(text("""
                INSERT INTO "foreign".raw_items
                  (raw_id,source_id,country_code,source_url,fetched_at,content_ref,source_hash,url_hash,payload)
                VALUES (:id,:source,:country,:url,CAST(:fetched AS timestamptz),:ref,:digest,:url_hash,CAST(:payload AS jsonb))
                ON CONFLICT DO NOTHING RETURNING raw_id
            """), params).scalar_one_or_none()
            if inserted is not None:
                return True
            existing = conn.execute(text("""
                SELECT raw_id, source_id, source_url, source_hash FROM "foreign".raw_items
                WHERE raw_id=:id OR (source_id=:source AND url_hash=:url_hash)
            """), params).mappings().all()
            if existing:
                if len(existing) != 1 or any(existing[0][key] != expected for key, expected in (
                    ("raw_id", params["id"]), ("source_id", params["source"]),
                    ("source_url", params["url"]), ("source_hash", digest),
                )):
                    raise ValueError("immutable raw identity or content conflict")
                return False
            # Same source and content under a different URL: do not overwrite original.
            duplicate = conn.execute(text("""
                SELECT 1 FROM "foreign".raw_items WHERE source_id=:source AND source_hash=:digest
            """), params).scalar_one_or_none()
            if duplicate is None:
                raise ValueError("unresolved raw identity conflict")
            return False

    def raw_items(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("invalid page limit")
        with self._engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text("""
                SELECT payload FROM "foreign".raw_items
                ORDER BY fetched_at DESC, raw_id LIMIT :limit
            """), {"limit": limit}).scalars()]
        for row in rows:
            validate_payload("raw_news", row)
        return rows

    def checkpoint(self, source_id: str) -> PollCheckpoint | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT source_id,revision,next_poll_at,etag,last_modified,last_success_at,consecutive_failures
                FROM "foreign".poll_state WHERE source_id=:id
            """), {"id": source_id}).mappings().one_or_none()
        return PollCheckpoint(**dict(row)) if row is not None else None

    def save_checkpoint(self, state: PollCheckpoint, *, source: Mapping[str, Any]) -> PollCheckpoint:
        approved_feed_url(source)
        if (state.source_id != source["source_id"] or state.revision < 0
                or state.consecutive_failures < 0):
            raise ValueError("invalid poll checkpoint")
        _aware(state.next_poll_at)
        if state.last_success_at is not None:
            _aware(state.last_success_at)
        for value in (state.etag, state.last_modified):
            if value is not None and (len(value) > 256 or any(ord(c) < 32 or ord(c) > 126 for c in value)):
                raise ValueError("invalid poll validator")
        with self._engine.begin() as conn:
            params = asdict(state)
            if state.revision == 0:
                sql = """
                    INSERT INTO "foreign".poll_state
                      (source_id,revision,next_poll_at,etag,last_modified,last_success_at,consecutive_failures)
                    VALUES (:source_id,1,:next_poll_at,:etag,:last_modified,:last_success_at,:consecutive_failures)
                    ON CONFLICT (source_id) DO NOTHING RETURNING revision
                """
            else:
                sql = """
                    UPDATE "foreign".poll_state SET revision=revision+1,next_poll_at=:next_poll_at,
                      etag=:etag,last_modified=:last_modified,last_success_at=:last_success_at,
                      consecutive_failures=:consecutive_failures
                    WHERE source_id=:source_id AND revision=:revision RETURNING revision
                """
            revision = conn.execute(text(sql), params).scalar_one_or_none()
            if revision is None:
                raise ValueError("stale poll checkpoint")
        return PollCheckpoint(**{**params, "revision": revision})
