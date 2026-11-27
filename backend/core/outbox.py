"""B01/P29 transactional outbox.

The outbox is the only bridge between a database change and an event publish:
append the envelope in the same unit of work, then dispatch it. Dispatch is
idempotent at the durable consumer; delivery may be repeated after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Mapping
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from backend.core.events import dedupe_key, event_json, validate_shared_event


@dataclass
class OutboxEntry:
    entry_id: str
    event: dict[str, Any]
    created_at: str
    dispatched_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "event": dict(self.event),
            "created_at": self.created_at,
            "dispatched_at": self.dispatched_at,
        }


class Outbox:
    """Fixture outbox; production uses a table in the owning database."""

    def __init__(self) -> None:
        self._entries: dict[str, OutboxEntry] = {}
        self._lock = Lock()

    def append(self, event: Mapping[str, Any]) -> OutboxEntry:
        envelope = validate_shared_event(event)
        entry = OutboxEntry(
            entry_id=str(uuid4()),
            event=envelope,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._entries[entry.entry_id] = entry
        return entry

    def pending(self) -> list[OutboxEntry]:
        with self._lock:
            return [entry for entry in self._entries.values() if entry.dispatched_at is None]

    def dispatch(self, publish: Callable[[Mapping[str, Any]], None]) -> int:
        """Publish pending entries; returns how many were dispatched this call."""
        sent = 0
        for entry in self.pending():
            # Revalidate a snapshot: pending entries may have changed since append.
            envelope = validate_shared_event(entry.event)
            publish(envelope)
            with self._lock:
                if entry.dispatched_at is None:
                    entry.dispatched_at = datetime.now(timezone.utc).isoformat()
                    sent += 1
        return sent

    def keys(self) -> set[str]:
        with self._lock:
            return {dedupe_key(entry.event) for entry in self._entries.values()}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class PostgresOutbox:
    """P29 shared internal event handoff; producer passes its active transaction.

    This slice accepts only the declared P09 -> P12/P13 routing. No general
    subscriber, external send or private/restricted route is enabled.
    """

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("outbox requires PostgreSQL")
        self._engine = engine

    def append_in_transaction(
        self, conn: Connection, event: Mapping[str, Any], *, stream: str, project: str,
    ) -> None:
        envelope = validate_shared_event(event)
        if (conn.engine is not self._engine or not conn.in_transaction()
                or envelope["classification"] != "internal" or envelope["producer_project"] != "P09"
                or envelope["event_type"] != "news.classified"
                or (stream, project) not in {("foreign", "P12"), ("domestic", "P13"), ("manual", "P13")}
                or len(envelope["payload_ref"]) > 256):
            raise ValueError("invalid transactional workflow handoff")
        params = {"id": envelope["event_id"], "key": dedupe_key(envelope),
                  "envelope": event_json(envelope), "stream": stream, "project": project}
        conn.execute(text("""
            INSERT INTO orchestration.outbox (event_id,event_key,envelope,stream,project_id)
            VALUES (:id,:key,CAST(:envelope AS jsonb),:stream,:project)
            ON CONFLICT (event_key) DO NOTHING
        """), params)
        row = conn.execute(text("""
            SELECT envelope,stream,project_id FROM orchestration.outbox WHERE event_key=:key
        """), params).mappings().one()
        if (row["envelope"], row["stream"], row["project_id"]) != (envelope, stream, project):
            raise ValueError("conflicting workflow event replay")

    def dispatch_to_jobs(self, *, limit: int = 100) -> int:
        """Atomically materialize each event in the durable P29 job queue.

        Database transaction is the consumer acknowledgement. A process crash
        before commit leaves both changes pending; no external I/O occurs here.
        """
        if not 1 <= limit <= 100:
            raise ValueError("invalid outbox batch limit")
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT event_id,event_key,envelope,stream,project_id,created_at FROM orchestration.outbox
                WHERE published_at IS NULL ORDER BY created_at,event_id
                LIMIT :limit FOR UPDATE SKIP LOCKED
            """), {"limit": limit}).mappings().all()
            for row in rows:
                event = validate_shared_event(row["envelope"])
                if (event["classification"] != "internal" or event["producer_project"] != "P09"
                        or event["event_type"] != "news.classified"
                        or (row["stream"], row["project_id"]) not in
                        {("foreign", "P12"), ("domestic", "P13"), ("manual", "P13")}):
                    raise ValueError("invalid pending workflow event")
                conn.execute(text("""
                    INSERT INTO orchestration.jobs
                        (job_id,event_id,project_id,stream,idempotency_key,payload_ref,correlation_id,deadline_at)
                    VALUES (:job,:id,:project,:stream,:key,:ref,:correlation,:created+interval '1 day')
                    ON CONFLICT (event_id) DO NOTHING
                """), {"job": str(uuid4()), "id": row["event_id"], "project": row["project_id"],
                       "stream": row["stream"], "key": row["event_key"], "ref": event["payload_ref"],
                       "correlation": event["correlation_id"], "created": row["created_at"]})
                conn.execute(text("""
                    UPDATE orchestration.outbox SET published_at=now() WHERE event_id=:id
                """), {"id": row["event_id"]})
            return len(rows)
