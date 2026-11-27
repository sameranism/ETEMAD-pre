"""B01/P29 job lifecycle with idempotency and the ``held`` state.

A job that is held because an output is off is never dispatched automatically
when the output comes back: it must be revalidated first (ADR-004).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any, Callable, Mapping
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    HELD = "held"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidTransition(RuntimeError):
    """The requested state change is not allowed for the current state."""


_ALLOWED = {
    JobState.QUEUED: {JobState.RUNNING, JobState.HELD, JobState.FAILED, JobState.CANCELLED},
    JobState.HELD: {JobState.QUEUED, JobState.FAILED, JobState.CANCELLED},
    JobState.RUNNING: {JobState.DONE, JobState.FAILED, JobState.HELD},
    JobState.DONE: set(),
    JobState.FAILED: {JobState.QUEUED, JobState.CANCELLED},
    JobState.CANCELLED: set(),
}


@dataclass(frozen=True)
class Job:
    job_id: str
    kind: str
    idempotency_key: str
    state: JobState = JobState.QUEUED
    attempt: int = 0
    last_error: str | None = None
    hold_reason: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    payload_ref: str = ""
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    max_attempts: int = 3
    deadline_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=1))

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "idempotency_key": self.idempotency_key,
            "state": self.state.value,
            "attempt": self.attempt,
            "last_error": self.last_error,
            "hold_reason": self.hold_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "payload_ref": self.payload_ref,
            "correlation_id": self.correlation_id,
            "max_attempts": self.max_attempts,
            "deadline_at": self.deadline_at.isoformat(),
        }


class JobQueue:
    """In-memory fixture queue; the production queue is Redis (ADR-003)."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._by_key: dict[str, str] = {}
        self._lock = Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        return value.astimezone(timezone.utc)

    def _now(self) -> datetime:
        return self._utc(self._clock())

    def enqueue(
        self, *, kind: str, idempotency_key: str, payload_ref: str = "",
        max_attempts: int = 3, deadline_at: datetime | None = None,
    ) -> Job:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        deadline = self._utc(deadline_at) if deadline_at is not None else None
        with self._lock:
            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                job = self._jobs[existing]
                if job.kind != kind or job.payload_ref != payload_ref:
                    raise ValueError("idempotency key conflicts with existing job")
                return job
            now = self._now()
            expires = deadline if deadline is not None else now + timedelta(days=1)
            if expires <= now:
                raise ValueError("deadline must be in the future")
            job = Job(
                job_id=str(uuid4()),
                kind=kind,
                idempotency_key=idempotency_key,
                payload_ref=payload_ref,
                max_attempts=max_attempts,
                deadline_at=expires,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            self._jobs[job.job_id] = job
            self._by_key[idempotency_key] = job.job_id
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _transition(self, job: Job, target: JobState, **changes: Any) -> Job:
        if target not in _ALLOWED[job.state]:
            raise InvalidTransition(f"{job.state.value} -> {target.value} is not allowed")
        updated = replace(job, state=target, updated_at=self._now().isoformat(), **changes)
        self._jobs[job.job_id] = updated
        return updated

    def _require_execution(self, job: Job) -> None:
        if job.kind.casefold() in {"publish", "publishing", "publication"}:
            raise InvalidTransition("publishing requires an authorized execution path")
        if self._now() >= job.deadline_at:
            raise InvalidTransition("job deadline expired")
        if job.attempt >= job.max_attempts:
            raise InvalidTransition("job attempt limit reached")

    def start(self, job_id: str) -> Job:
        with self._lock:
            job = self._require(job_id)
            self._require_execution(job)
            return self._transition(job, JobState.RUNNING, attempt=job.attempt + 1)

    def complete(self, job_id: str) -> Job:
        with self._lock:
            return self._transition(self._require(job_id), JobState.DONE, last_error=None)

    def fail(self, job_id: str, error: str) -> Job:
        """Record a controlled code, not caller-supplied exception text."""
        with self._lock:
            return self._transition(self._require(job_id), JobState.FAILED, last_error="job_failed")

    def hold(self, job_id: str, reason: str) -> Job:
        with self._lock:
            return self._transition(self._require(job_id), JobState.HELD, hold_reason="job_held")

    def cancel(self, job_id: str) -> Job:
        """Cancel only jobs not currently executing; terminal jobs stay terminal."""
        with self._lock:
            return self._transition(self._require(job_id), JobState.CANCELLED, hold_reason=None)

    def retry(self, job_id: str) -> Job:
        with self._lock:
            job = self._require(job_id)
            if job.state is not JobState.FAILED:
                raise InvalidTransition("only a failed job can be retried")
            self._require_execution(job)
            return self._transition(job, JobState.QUEUED, last_error=None, hold_reason=None)

    def resume(self, job_id: str, revalidate: Callable[[Job], bool]) -> Job:
        """Revalidate a held fixture snapshot outside the lock; reject stale decisions."""
        with self._lock:
            snapshot = self._require(job_id)
            if snapshot.state is not JobState.HELD:
                raise InvalidTransition("only a held job can be resumed")
            self._require_execution(snapshot)
        if revalidate(snapshot) is not True:
            raise InvalidTransition("revalidation refused the resume")
        with self._lock:
            job = self._require(job_id)
            if job is not snapshot:
                raise InvalidTransition("job changed during revalidation")
            self._require_execution(job)
            return self._transition(job, JobState.QUEUED, hold_reason=None)

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

    def counts(self) -> Mapping[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for job in self._jobs.values():
                counts[job.state.value] = counts.get(job.state.value, 0) + 1
            return counts


class PostgresWorkflowJobs:
    """Durable, scoped P29 queue. No external work is executed here.

    A lost lease is failed, never blindly retried: the worker may already have
    performed a side effect. A subsequent retry needs fresh revalidation.
    """
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("workflow jobs require PostgreSQL")
        self._engine = engine

    @staticmethod
    def _scope(project: str, stream: str) -> None:
        if (stream, project) not in {("foreign", "P12"), ("domestic", "P13"), ("manual", "P13")}:
            raise ValueError("invalid workflow scope")

    def claim(self, *, project: str, stream: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        self._scope(project, stream)
        if not 1 <= lease_seconds <= 900:
            raise ValueError("invalid lease duration")
        with self._engine.begin() as conn:
            # Unknown completion is retained for explicit review, never sent again automatically.
            conn.execute(text("""
                UPDATE orchestration.jobs SET state='failed',last_error='lease_expired',
                    lease_token=NULL,lease_until=NULL,updated_at=now()
                WHERE project_id=:project AND stream=:stream AND state='running' AND lease_until<=now()
            """), {"project": project, "stream": stream})
            conn.execute(text("""
                UPDATE orchestration.jobs SET state='failed',last_error='deadline_expired',updated_at=now()
                WHERE project_id=:project AND stream=:stream AND state='queued' AND deadline_at<=now()
            """), {"project": project, "stream": stream})
            row = conn.execute(text("""
                WITH picked AS (
                  SELECT job_id FROM orchestration.jobs WHERE project_id=:project AND stream=:stream
                    AND state='queued' AND attempts<max_attempts AND deadline_at>now()
                  ORDER BY created_at,job_id LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                UPDATE orchestration.jobs j SET state='running',attempts=attempts+1,
                    lease_token=:token,lease_until=LEAST(deadline_at,now()+:seconds*interval '1 second'),
                    last_error=NULL,updated_at=now() FROM picked WHERE j.job_id=picked.job_id
                RETURNING j.*
            """), {"project": project, "stream": stream, "token": str(uuid4()),
                   "seconds": lease_seconds}).mappings().one_or_none()
            return dict(row) if row is not None else None

    def finish(self, *, job_id: str, lease_token: str, success: bool) -> None:
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                UPDATE orchestration.jobs SET state=:state,last_error=:error,
                    lease_token=NULL,lease_until=NULL,updated_at=now()
                WHERE job_id=:id AND state='running' AND lease_token=:token AND lease_until>now()
                RETURNING job_id
            """), {"id": job_id, "token": lease_token, "state": "done" if success else "failed",
                   "error": None if success else "job_failed"}).scalar_one_or_none()
            if row is None:
                raise InvalidTransition("workflow lease is stale")

    def hold(self, *, job_id: str) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE orchestration.jobs SET state='held',updated_at=now()
                WHERE job_id=:id AND state='queued' RETURNING job_id
            """), {"id": job_id}).scalar_one_or_none()
            if result is None:
                raise InvalidTransition("only a queued workflow may be held")

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT job_id,project_id,stream,state,attempts,max_attempts,last_error,deadline_at,
                       created_at,updated_at,correlation_id
                FROM orchestration.jobs WHERE job_id=:id
            """), {"id": job_id}).mappings().one_or_none()
            return dict(row) if row is not None else None

    def retry(self, *, job_id: str, revalidate: Callable[[Mapping[str, Any]], bool]) -> None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT * FROM orchestration.jobs WHERE job_id=:id AND state IN ('failed','held')
                    AND attempts<max_attempts AND deadline_at>now()
            """), {"id": job_id}).mappings().one_or_none()
        if row is None or revalidate(dict(row)) is not True:
            raise InvalidTransition("workflow retry not authorized")
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE orchestration.jobs SET state='queued',last_error=NULL,updated_at=now()
                WHERE job_id=:id AND state=:state AND attempts=:attempts AND updated_at=:updated
                    AND attempts<max_attempts AND deadline_at>now() RETURNING job_id
            """), {"id": job_id, "state": row["state"], "attempts": row["attempts"],
                   "updated": row["updated_at"]}).scalar_one_or_none()
            if result is None:
                raise InvalidTransition("workflow changed during revalidation")

    def counts(self) -> dict[str, int]:
        with self._engine.connect() as conn:
            return dict(conn.execute(text("""
                SELECT state,count(*) FROM orchestration.jobs GROUP BY state
            """)).tuples().all())
