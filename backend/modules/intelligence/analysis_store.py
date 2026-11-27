"""P15 durable, subject-scoped analysis history, never an executive report.

Stored source quotations are readable only after the API revalidates the current
news grants, source licensing and immutable archive revisions on every GET.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.auth import Principal
from backend.core.contracts import validate_payload


class PostgresAnalysisHistory:
    """Immutable analysis revisions, isolated by subject and role."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("analysis history requires PostgreSQL")
        self._engine = engine

    @staticmethod
    def _check(result: Mapping[str, Any], evidence: list[dict[str, Any]]) -> None:
        validate_payload("analysis_result", result)
        refs = [item.get("source_ref") for item in evidence]
        if (not refs or len(refs) > 20 or any(not isinstance(ref, str) for ref in refs)
                or len(set(refs)) != len(refs) or set(refs) != set(result["source_refs"])):
            raise ValueError("analysis evidence reference mismatch")
        for item in evidence:
            if (not all(isinstance(item.get(k), str) and item[k] for k in
                        ("source_ref", "news_id", "source_id", "sha256", "stream", "content_scope"))
                    or not isinstance(item.get("text_redacted"), bool)):
                raise ValueError("invalid analysis evidence")
        if any(not set(claim["source_refs"]) <= set(refs) for claim in result["claims"]):
            raise ValueError("analysis claim references missing evidence")

    def save(self, principal: Principal, result: Mapping[str, Any],
             evidence: list[dict[str, Any]]) -> dict[str, Any]:
        self._check(result, evidence)
        params = {"subject": principal.subject_id, "role": principal.role,
                  "id": result["analysis_id"],
                  "result": json.dumps(result, ensure_ascii=False, allow_nan=False),
                  "evidence": json.dumps(evidence, ensure_ascii=False, allow_nan=False)}
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO insights.analyses (subject_id,role,analysis_id,result,evidence)
                VALUES (:subject,:role,:id,CAST(:result AS jsonb),CAST(:evidence AS jsonb))
                ON CONFLICT (subject_id,role,analysis_id) DO NOTHING
            """), params)
            row = conn.execute(text("""
                SELECT result,evidence FROM insights.analyses
                WHERE subject_id=:subject AND role=:role AND analysis_id=:id
            """), params).mappings().one()
        saved = {"result": row["result"], "evidence": row["evidence"]}
        current = {k: v for k, v in result.items() if k != "created_at"}
        original = {k: v for k, v in saved["result"].items() if k != "created_at"}
        if original != current or saved["evidence"] != evidence:
            raise ValueError("immutable analysis history conflict")
        self._check(saved["result"], saved["evidence"])
        return saved

    def latest(self, principal: Principal) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT result,evidence FROM insights.analyses
                WHERE subject_id=:subject AND role=:role
                ORDER BY created_at DESC,analysis_id DESC LIMIT 1
            """), {"subject": principal.subject_id, "role": principal.role}).mappings().one_or_none()
        if row is None:
            return None
        saved = {"result": row["result"], "evidence": row["evidence"]}
        self._check(saved["result"], saved["evidence"])
        return saved

    def clear(self, principal: Principal) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                DELETE FROM insights.analyses WHERE subject_id=:subject
            """), {"subject": principal.subject_id})
