"""P27 preference persistence. The caller must enforce current desk grants."""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.auth import Principal


class ViewLimit(ValueError):
    """Bounded per-user preference collection."""


class SavedViews:
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("saved views require PostgreSQL")
        self._engine = engine

    @staticmethod
    def _identity(principal: Principal) -> dict[str, str]:
        if principal.role != "staff" or principal.must_reset_password:
            raise PermissionError("saved view unavailable")
        return {"subject": principal.subject_id, "role": principal.role}

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            return [dict(row) for row in conn.execute(text("""
                SELECT view_id,name,filters_json FROM ui.saved_views
                WHERE user_id=:subject AND dashboard_type=:role
                ORDER BY created_at,view_id LIMIT 10
            """), self._identity(principal)).mappings()]

    def save(self, principal: Principal, name: str, desk: str, stream: str) -> dict[str, Any]:
        params = {**self._identity(principal), "name": name, "id": str(uuid4()),
                  "filters": json.dumps({"desk": desk, "stream": stream})}
        if not 1 <= len(name.strip()) <= 64 or name != name.strip():
            raise ValueError("invalid view name")
        with self._engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:subject,29))"), params)
            exists = conn.execute(text("""
                SELECT count(*) FROM ui.saved_views WHERE user_id=:subject AND dashboard_type=:role
            """), params).scalar_one()
            if exists >= 10 and not conn.execute(text("""
                SELECT EXISTS(SELECT 1 FROM ui.saved_views WHERE user_id=:subject
                  AND dashboard_type=:role AND name=:name)
            """), params).scalar_one():
                raise ViewLimit("saved view limit")
            row = conn.execute(text("""
                INSERT INTO ui.saved_views(view_id,user_id,dashboard_type,name,filters_json)
                VALUES (:id,:subject,:role,:name,CAST(:filters AS jsonb))
                ON CONFLICT (user_id,dashboard_type,name) DO UPDATE SET filters_json=EXCLUDED.filters_json
                RETURNING view_id,name,filters_json
            """), params).mappings().one()
            return dict(row)

    def delete(self, principal: Principal, view_id: str) -> bool:
        with self._engine.begin() as conn:
            return conn.execute(text("""
                DELETE FROM ui.saved_views WHERE user_id=:subject AND dashboard_type=:role
                    AND view_id=:id RETURNING view_id
            """), {**self._identity(principal), "id": view_id}).scalar_one_or_none() is not None
