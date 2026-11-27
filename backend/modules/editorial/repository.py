"""P13/P12 durable, append-only editorial revisions; no publication side effects."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from backend.core.contracts import validate_payload
from backend.modules.editorial.generation import EditorialGenerator
from backend.modules.editorial.policy import PolicyContext, evaluate_policy
from backend.modules.editorial.quality import assess_quality
from backend.modules.editorial.translator import (
    _numbers,
    _quotes,
    canonical,
    checked_source,
    digest,
)

FIELDS = ("title", "lead", "body", "short_summary")


class RevisionConflict(ValueError):
    """Stale or conflicting idempotency request."""


class EditorialRepository:
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("editorial storage requires PostgreSQL")
        self._engine = engine

    @staticmethod
    def _read(conn: Connection, news_id: str, version: int | None = None) -> dict[str, Any] | None:
        row = conn.execute(text("""
            SELECT a.version,a.source_hash,a.source_snapshot,a.payload AS article,
                   a.quality,a.policy,a.created_at,t.payload AS translation,t.provenance
            FROM editorial.articles a LEFT JOIN editorial.translations t USING (translation_id)
            WHERE a.news_id=:id AND (CAST(:version AS integer) IS NULL OR a.version=:version)
            ORDER BY a.version DESC LIMIT 1
        """), {"id": news_id, "version": version}).mappings().one_or_none()
        if row is None:
            return None
        result = dict(row)
        result["created_at"] = row["created_at"].isoformat()
        validate_payload("article", result["article"])
        validate_payload("validation_decision", result["policy"])
        if result["translation"] is not None:
            validate_payload("translation", result["translation"])
        return result

    def get(self, news_id: str, version: int | None = None) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            return self._read(conn, news_id, version)

    def history(self, news_id: str, *, before: int | None = None) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            return [
                {"version": r.version, "created_at": r.created_at.isoformat(),
                 "source_hash": r.source_hash}
                for r in conn.execute(text("""
                    SELECT version,created_at,source_hash FROM editorial.articles
                    WHERE news_id=:id AND (CAST(:before AS integer) IS NULL OR version < :before)
                    ORDER BY version DESC LIMIT 50
                """), {"id": news_id, "before": before})
            ]

    def save_manual(self, raw: Mapping[str, Any], *, desk: str, fields: Mapping[str, str],
                    translated_text: str | None, expected_version: int, expected_source_hash: str,
                    request_id: str, actor_id: str) -> dict[str, Any]:
        source = checked_source(raw)
        news_id = source["news_id"]
        source_hash = digest(canonical(source))
        if source_hash != expected_source_hash:
            raise RevisionConflict("source changed")
        if (not 0 <= expected_version < 2**31-1 or not 1 <= len(news_id) <= 64
                or not 1 <= len(actor_id) <= 64 or not 1 <= len(request_id) <= 128
                or not 1 <= len(desk) <= 64 or set(fields) != set(FIELDS)
                or any(not v.strip() or len(v) > 60_000 for v in fields.values())):
            raise ValueError("invalid editorial revision")
        if translated_text is not None and (source["stream"] != "foreign" or source["language"] == "fa"
                or not translated_text.strip() or len(translated_text) > 60_000):
            raise ValueError("invalid manual translation")
        original = source["original_text"]
        stamp = datetime.now(timezone.utc).isoformat()
        # This is explicitly manual, unconfigured work; not an approved policy or AI result.
        quality = assess_quality(original, "\n".join(fields[k] for k in FIELDS),
                                 source_refs=(news_id,), attributed_refs=(news_id,))
        article = {
            "schema_version": "1.0", "article_id": "ART-" + digest(canonical([news_id, source["stream"], desk]))[:32],
            "news_id": news_id, "stream": source["stream"], "desk": desk, **dict(fields),
            "source_refs": [news_id], "framework_version": "manual/unconfigured",
            "editorial_rules_version": "manual/unconfigured",
            "status": "draft" if quality.passed else "held", "created_at": stamp,
        }
        validate_payload("article", article)
        translation = None
        provenance = {"kind": "manual", "actor_id": actor_id}
        if translated_text is not None:
            qa = assess_quality(original, translated_text, source_refs=(news_id,), attributed_refs=(news_id,))
            warnings = {"TRANSLATION_SEMANTIC_REVIEW_REQUIRED"}
            if not qa.passed:
                warnings.update(qa.reason_codes)
            if _numbers(original) != _numbers(translated_text):
                warnings.add("FACT_NUMBER_MISMATCH")
            if _quotes(original) != _quotes(translated_text):
                warnings.add("DIRECT_QUOTE_MISMATCH")
            if len(warnings) > 1:
                article["status"] = "held"
            translation = {
                "schema_version": "1.0", "translation_id": "TR-" + digest(canonical(
                    [news_id, source_hash, translated_text, actor_id]))[:32],
                "news_id": news_id, "source_language": source["language"], "target_language": "fa",
                "translated_text": translated_text, "source_text_ref": news_id,
                "glossary_version": "manual/unconfigured", "model_id": "human/manual",
                "warnings": sorted(warnings), "created_at": stamp,
            }
            validate_payload("translation", translation)
        policy = evaluate_policy(article, object_type="article", rights={},
                                 context=PolicyContext("manual/unconfigured"))
        request_hash = digest(canonical([source_hash, expected_version, desk, dict(fields), translated_text, actor_id]))
        return self._write(
            source, desk=desk, expected_version=expected_version, request_id=request_id,
            request_hash=request_hash, actor_id=actor_id,
            prepare=lambda: (article, asdict(quality), policy, translation, provenance),
        )

    def save_generated(
        self, raw: Mapping[str, Any], *, desk: str, generator: EditorialGenerator,
        expected_version: int, expected_source_hash: str, request_id: str, actor_id: str,
        revalidate: Callable[[], None],
    ) -> dict[str, Any]:
        source = checked_source(raw)
        source_hash = digest(canonical(source))
        news_id = source["news_id"]
        if source_hash != expected_source_hash:
            raise RevisionConflict("source changed")
        if (not 0 <= expected_version < 2**31-1 or not 1 <= len(news_id) <= 64
                or not 1 <= len(actor_id) <= 64 or not 1 <= len(request_id) <= 128
                or not 1 <= len(desk) <= 64 or not generator.available(source, desk)):
            raise ValueError("invalid generation request")
        request_hash = digest(canonical(["generated", source_hash, expected_version, desk,
                                         actor_id, generator.fingerprint(source, desk)]))

        def prepare() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any],
                               dict[str, Any] | None, dict[str, Any]]:
            # The replay and version checks happen before the model is invoked.
            # Only trusted application composition supplies the gateway.
            revalidate()
            result = generator.generate(source, desk=desk)
            revalidate()
            article = dict(result.article.record)
            original = source["original_text"]
            if result.article.source_hash != digest(original):
                raise ValueError("generated article source mismatch")
            translation = None
            provenance: dict[str, Any] = {}
            if result.translation is not None:
                if result.translation.source_hash != digest(original):
                    raise ValueError("generated translation source mismatch")
                translation = dict(result.translation.record)
                provenance = dict(result.translation.provenance)
                # Each actual generation keeps its own model-request provenance.
                translation["translation_id"] = "TR-" + digest(canonical([
                    translation["translation_id"], source_hash, provenance["request_id"],
                ]))[:32]
                validate_payload("translation", translation)
            quality = asdict(assess_quality(original, "\n".join(article[k] for k in FIELDS),
                                            source_refs=(news_id,), attributed_refs=(news_id,)))
            if not quality["passed"]:
                article["status"] = "held"
            quality["generation"] = {"mode": result.mode, "article_provenance": result.article.provenance,
                                     "warnings": list(result.article.warnings)}
            # Even a successful deterministic check is not semantic or legal approval.
            policy = evaluate_policy(article, object_type="article", rights={},
                                     context=PolicyContext(article["editorial_rules_version"]))
            return article, quality, policy, translation, provenance

        return self._write(
            source, desk=desk, expected_version=expected_version, request_id=request_id,
            request_hash=request_hash, actor_id=actor_id, prepare=prepare,
        )

    def _write(
        self, source: dict[str, Any], *, desk: str, expected_version: int, request_id: str,
        request_hash: str, actor_id: str,
        prepare: Callable[[], tuple[dict[str, Any], dict[str, Any], dict[str, Any],
                                    dict[str, Any] | None, dict[str, Any]]],
    ) -> dict[str, Any]:
        news_id = source["news_id"]
        source_hash = digest(canonical(source))
        with self._engine.begin() as conn:
            # One transaction and a per-news lock protect concurrent writers and replay.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"), {"id": news_id})
            previous = conn.execute(text("""
                SELECT version,request_hash FROM editorial.articles WHERE news_id=:id AND request_id=:request
            """), {"id": news_id, "request": request_id}).one_or_none()
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise RevisionConflict("conflicting replay")
                replay = self._read(conn, news_id, previous.version)
                assert replay is not None
                return replay
            latest = self._read(conn, news_id)
            if (latest["version"] if latest else 0) != expected_version:
                raise RevisionConflict("stale revision")
            if latest is not None and latest["article"]["desk"] != desk:
                raise RevisionConflict("desk identity changed")
            article, quality, policy, translation, provenance = prepare()
            validate_payload("article", article)
            if translation is not None:
                conn.execute(text("""
                    INSERT INTO editorial.translations (translation_id,news_id,source_hash,payload,provenance)
                    VALUES (:tid,:id,:hash,CAST(:payload AS jsonb),CAST(:provenance AS jsonb))
                    ON CONFLICT (translation_id) DO NOTHING
                """), {"tid": translation["translation_id"], "id": news_id, "hash": source_hash,
                       "payload": canonical(translation), "provenance": canonical(provenance)})
            conn.execute(text("""
                INSERT INTO editorial.articles
                    (news_id,version,article_id,request_id,request_hash,source_hash,source_snapshot,
                     payload,quality,policy,actor_id,translation_id)
                VALUES (:id,:version,:article,:request,:request_hash,:source_hash,CAST(:source AS jsonb),
                        CAST(:payload AS jsonb),CAST(:quality AS jsonb),CAST(:policy AS jsonb),:actor,:tid)
            """), {"id": news_id, "version": expected_version + 1, "article": article["article_id"],
                   "request": request_id, "request_hash": request_hash, "source_hash": source_hash,
                   "source": canonical(source), "payload": canonical(article), "quality": canonical(quality),
                   "policy": canonical(policy), "actor": actor_id,
                   "tid": translation["translation_id"] if translation else None})
            result = self._read(conn, news_id, expected_version + 1)
            assert result is not None
            return result
