"""Durable job reservation for fixture dispatch; no outbound integration.

Reserve and commit unknown BEFORE invoking the operation. A process crash or
concurrent worker can never cause a second invocation for that key. The
conservative outcome is unknown until reconciliation, not an automatic retry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, cast

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.adapters.base import Delivery
from backend.core.contracts import validate_payload
from backend.modules.publishing.scheduler import canonical, stamp


class PostgresFixtureLedger:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("publication history requires PostgreSQL")
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def enqueue(self, job: Mapping[str, Any]) -> None:
        validate_payload("publication_job", job)
        if (len(job["destination_ids"]) != 1 or job["state"] not in {"scheduled", "held"}
                or job["expires_at"] is None
                or stamp(job["scheduled_at"]) >= stamp(job["expires_at"])):
            raise ValueError("invalid initial publication job")
        params = dict(job_id=job["job_id"], key=job["idempotency_key"],
                      dest=job["destination_ids"][0], kind=job["object_type"],
                      scheduled=stamp(job["scheduled_at"]), expires=stamp(job["expires_at"]),
                      state=job["state"], record=canonical(job))
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO publish.jobs
                    (job_id,idempotency_key,destination_id,object_type,scheduled_at,expires_at,state,record)
                VALUES (:job_id,:key,:dest,:kind,:scheduled,:expires,:state,CAST(:record AS jsonb))
                ON CONFLICT (idempotency_key) DO NOTHING
            """), params)
            record = conn.execute(text(
                "SELECT record FROM publish.jobs WHERE idempotency_key=:key"
            ), params).scalar_one()
            if canonical(record) != params["record"]:
                raise ValueError("publication intent conflict")

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT record,state FROM publish.jobs WHERE job_id=:job_id
            """), {"job_id": job_id}).mappings().one_or_none()
        if row is None:
            return None
        record = {**row["record"], "state": row["state"]}
        validate_payload("publication_job", record)
        return record

    def once(self, key: tuple[str, str], operation: Callable[[], Delivery]) -> Delivery:
        params = {"key": key[0], "dest": key[1]}
        with self._engine.begin() as conn:
            job = conn.execute(text("""
                SELECT destination_id,state,scheduled_at,expires_at FROM publish.jobs
                WHERE idempotency_key=:key FOR UPDATE
            """), params).mappings().one_or_none()
            if job is None or job["destination_id"] != key[1]:
                raise ValueError("publication job is not registered")
            existing = conn.execute(text("""
                SELECT status,message_id,error_code FROM publish.attempts
                WHERE idempotency_key=:key AND destination_id=:dest
            """), params).mappings().one_or_none()
            if existing is not None:
                return Delivery(cast(Any, existing["status"]), existing["message_id"],
                                existing["error_code"])
            if (job["state"] != "scheduled"
                    or not job["scheduled_at"] <= self._clock() < job["expires_at"]):
                return Delivery("skipped", error_code="JOB_NOT_SCHEDULED")
            conn.execute(text("""
                INSERT INTO publish.attempts (idempotency_key,destination_id,status,error_code)
                VALUES (:key,:dest,'unknown','ATTEMPT_UNCONFIRMED')
            """), params)
            conn.execute(text("""
                UPDATE publish.jobs SET state='in_progress' WHERE idempotency_key=:key
            """), params)
        # No transaction or database lock is held while calling the adapter.
        try:
            outcome = operation()
        except Exception:
            outcome = Delivery("unknown", error_code="ATTEMPT_UNCONFIRMED")
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE publish.attempts SET status=:status,message_id=:message,error_code=:error,
                    ended_at=now() WHERE idempotency_key=:key AND destination_id=:dest
            """), {**params, "status": outcome.status, "message": outcome.message_id,
                   "error": outcome.error_code})
            conn.execute(text("""
                UPDATE publish.jobs SET state='completed' WHERE idempotency_key=:key
            """), params)
        return outcome


    def overview(self, *, limit: int = 100) -> dict[str, Any]:
        """Recorded fixture queue history, not verified live delivery metrics."""
        if not 1 <= limit <= 100:
            raise ValueError("invalid queue limit")
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT j.record,j.state,a.status AS attempt_status,a.message_id,
                       a.error_code,a.started_at,a.ended_at
                FROM publish.jobs j LEFT JOIN publish.attempts a
                  ON a.idempotency_key=j.idempotency_key AND a.destination_id=j.destination_id
                ORDER BY j.scheduled_at DESC,j.job_id LIMIT :limit
            """), {"limit": limit+1}).mappings().all()
        items = []
        for row in rows[:limit]:
            job = {**row["record"], "state": row["state"]}
            validate_payload("publication_job", job)
            items.append({
                "job_id": job["job_id"], "object_id": job["object_id"],
                "object_type": job["object_type"], "stream": job["stream"],
                "destination_id": job["destination_ids"][0],
                "state": job["state"], "scheduled_at": job["scheduled_at"],
                "expires_at": job["expires_at"],
                "attempt": None if row["attempt_status"] is None else {
                    "status": row["attempt_status"], "message_id": row["message_id"],
                    "error_code": row["error_code"],
                    "started_at": row["started_at"].isoformat(),
                    "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
                },
                "can_hold": job["state"] == "scheduled" and row["attempt_status"] is None,
            })
        return {"items": items, "bounded": len(rows) > limit}

    def hold(self, job_id: str) -> str | None:
        """Hold an unattempted job only; never interrupt or replay a delivery."""
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                SELECT idempotency_key,state FROM publish.jobs
                WHERE job_id=:job FOR UPDATE
            """), {"job": job_id}).mappings().one_or_none()
            if row is None:
                return None
            if row["state"] not in {"scheduled", "held"}:
                raise ValueError("job has already progressed")
            attempted = conn.execute(text("""
                SELECT EXISTS(SELECT 1 FROM publish.attempts WHERE idempotency_key=:key)
            """), {"key": row["idempotency_key"]}).scalar_one()
            if attempted:
                raise ValueError("delivery already attempted")
            conn.execute(text("UPDATE publish.jobs SET state='held' WHERE job_id=:job"),
                         {"job": job_id})
            return "held"
