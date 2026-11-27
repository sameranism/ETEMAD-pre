"""P14 durable rendition manifests; original image rights never inferred."""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.contracts import validate_payload
from backend.core.object_store import ObjectStore
from backend.modules.editorial.image_processor import PreparedImage, image_identity, verify_asset
from backend.modules.editorial.translator import canonical


class ImageRepository:
    def __init__(self, engine: Engine, objects: ObjectStore) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("image repository requires PostgreSQL")
        self._engine = engine
        self._objects = objects

    def save(self, article: Mapping[str, Any], version: int, prepared: PreparedImage,
             *, actor_id: str) -> dict[str, Any]:
        validate_payload("article", article)
        validate_payload("image_manifest", prepared.manifest)
        if prepared.manifest["article_id"] != article["article_id"] or not 1 <= len(actor_id) <= 64:
            raise ValueError("image identity mismatch")
        if len(prepared.manifest["assets"]) != len(prepared.evidence["assets"]):
            raise ValueError("image evidence count mismatch")
        for asset, evidence in zip(prepared.manifest["assets"], prepared.evidence["assets"], strict=True):
            verify_asset(self._objects, asset, evidence)
        image_id = image_identity(str(article["article_id"]), version, prepared)
        params = {"image": image_id, "news": article["news_id"], "version": version,
                  "article": article["article_id"], "manifest": canonical(prepared.manifest),
                  "evidence": canonical(prepared.evidence), "actor": actor_id}
        with self._engine.begin() as conn:
            current = conn.execute(text("""
                SELECT payload FROM editorial.articles WHERE news_id=:news AND version=:version
            """), params).scalar_one_or_none()
            if current != dict(article):
                raise ValueError("article revision mismatch")
            conn.execute(text("""
                INSERT INTO editorial.image_assets
                    (image_id,news_id,article_version,article_id,manifest,evidence,actor_id)
                VALUES (:image,:news,:version,:article,CAST(:manifest AS jsonb),CAST(:evidence AS jsonb),:actor)
                ON CONFLICT (image_id) DO NOTHING
            """), params)
            row = conn.execute(text("""
                SELECT image_id,article_version,manifest,evidence FROM editorial.image_assets
                WHERE image_id=:image AND news_id=:news
            """), params).mappings().one()
            return dict(row)

    def list(self, news_id: str, version: int) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            return [dict(row) for row in conn.execute(text("""
                SELECT image_id,article_version,manifest,evidence FROM editorial.image_assets
                WHERE news_id=:news AND article_version=:version
                ORDER BY created_at DESC,image_id LIMIT 20
            """), {"news": news_id, "version": version}).mappings()]

    def read(self, news_id: str, version: int, image_id: str, index: int) -> tuple[bytes, str] | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT manifest,evidence FROM editorial.image_assets
                WHERE news_id=:news AND article_version=:version AND image_id=:image
            """), {"news": news_id, "version": version, "image": image_id}).mappings().one_or_none()
        if row is None or not 0 <= index < len(row["manifest"]["assets"]):
            return None
        validate_payload("image_manifest", row["manifest"])
        return verify_asset(self._objects, row["manifest"]["assets"][index], row["evidence"]["assets"][index])
