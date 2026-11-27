"""P18/P19 immutable private-subject report persistence; no publication API."""
from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.auth import Principal
from backend.core.contracts import validate_payload
from backend.core.permissions import GrantLookup, decide_access
from backend.modules.intelligence.private_reports import PRIVATE_SLOTS
from backend.modules.intelligence.public_reports import PUBLIC_SLOTS


class ReportRepository:
    def __init__(self, engine: Engine, grants: GrantLookup) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("reports require PostgreSQL")
        self._engine = engine
        self._grants = grants

    def _table(self, principal: Principal, slot: int) -> tuple[str, str, str]:
        if slot in PUBLIC_SLOTS:
            if decide_access(principal.subject_id, principal.role, "analysis_workspace", "default",
                             "report", self._grants)["allowed"] is not True:
                raise PermissionError("report unavailable")
            return "insights.public_report_drafts", "public_report", PUBLIC_SLOTS[slot][0]
        if slot in PRIVATE_SLOTS:
            if decide_access(principal.subject_id, principal.role, "private_report", f"slot-{slot}",
                             "read", self._grants, classification="executive_private")["allowed"] is not True:
                raise PermissionError("report unavailable")
            return "insights.executive_reports", "private_report", PRIVATE_SLOTS[slot][0]
        raise ValueError("invalid report slot")

    def save(self, principal: Principal, slot: int, report: Mapping[str, Any],
             evidence: list[dict[str, Any]]) -> dict[str, Any]:
        table, contract, kind = self._table(principal, slot)
        validate_payload(contract, report)
        refs = {item["source_ref"] for item in evidence}
        if (report["report_type"] != kind or report["generation_status"] != "draft"
                or set(report["source_refs"]) != refs or len(refs) != len(evidence)
                or any(not set(s["source_refs"]) <= refs for s in report["sections"])):
            raise ValueError("report/evidence mismatch")
        with self._engine.begin() as conn:
            conn.execute(text(f"""
                INSERT INTO {table} (subject_id,report_id,slot,payload,evidence)
                VALUES (:subject,:id,:slot,CAST(:payload AS jsonb),CAST(:evidence AS jsonb))
                ON CONFLICT (subject_id,report_id) DO NOTHING
            """), {"subject": principal.subject_id, "id": report["report_id"], "slot": slot,
                   "payload": json.dumps(dict(report), ensure_ascii=False),
                   "evidence": json.dumps(evidence, ensure_ascii=False)})
            row = conn.execute(text(f"""
                SELECT payload,evidence FROM {table}
                WHERE subject_id=:subject AND report_id=:id AND slot=:slot
            """), {"subject": principal.subject_id, "id": report["report_id"], "slot": slot}).mappings().one()
            expected = {k: v for k, v in report.items() if k != "generated_at"}
            if ({k: v for k, v in row["payload"].items() if k != "generated_at"} != expected
                    or row["evidence"] != evidence):
                raise ValueError("immutable report conflict")
            return dict(row)

    def latest(self, principal: Principal, slot: int) -> dict[str, Any] | None:
        table, contract, kind = self._table(principal, slot)
        with self._engine.connect() as conn:
            row = conn.execute(text(f"""
                SELECT payload,evidence FROM {table} WHERE subject_id=:subject AND slot=:slot
                ORDER BY created_at DESC,report_id DESC LIMIT 1
            """), {"subject": principal.subject_id, "slot": slot}).mappings().one_or_none()
        if row is None:
            return None
        validate_payload(contract, row["payload"])
        if row["payload"]["report_type"] != kind:
            raise ValueError("report slot mismatch")
        return dict(row)
