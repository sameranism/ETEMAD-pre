"""B01/P29 workflow orchestration for one news record.

The orchestrator is intentionally small and synchronous: it validates the
incoming contract, archives the reference, records workflow status, and gates
every external side effect behind the three output switches (ADR-004).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.engine import Connection

from backend.core.archive import ArchiveStore, build_archive_record, object_key
from backend.core.contracts import validate_payload
from backend.core.events import EventBus, make_event
from backend.core.jobs import Job, JobQueue, JobState
from backend.core.outbox import PostgresOutbox
from backend.core.output_switches import OutputSwitches


@dataclass(frozen=True)
class WorkflowResult:
    workflow: dict[str, Any]
    job: Job | None
    archived: bool


class Orchestrator:
    """Coordinates B01 hand-offs; never performs an external side effect itself."""

    def __init__(
        self,
        *,
        jobs: JobQueue,
        bus: EventBus,
        archive: ArchiveStore,
        switches: Callable[[], OutputSwitches],
    ) -> None:
        self._jobs = jobs
        self._bus = bus
        self._archive = archive
        self._switches = switches

    def _status(self, job: Job) -> dict[str, Any]:
        states = {
            JobState.QUEUED: "new",
            JobState.RUNNING: "running",
            JobState.HELD: "held",
            JobState.DONE: "completed",
            JobState.FAILED: "failed",
            JobState.CANCELLED: "cancelled",
        }
        status = {
            "schema_version": "1.0",
            "workflow_id": f"wf-{job.job_id}",
            "correlation_id": job.correlation_id,
            "current_project": "P07",
            "state": states[job.state],
            "attempt_count": job.attempt,
            "last_error_code": job.last_error,
            "updated_at": job.updated_at,
        }
        validate_payload("workflow_status", status)
        return status

    def ingest_domestic(self, record: Mapping[str, Any]) -> WorkflowResult:
        """Accept one ``normalized_news`` record, archive its reference, emit an event."""
        validate_payload("normalized_news", record)
        job = self._jobs.enqueue(
            kind="ingest",
            idempotency_key=f"ingest:{record['news_id']}:{record['fingerprint']}",
            payload_ref=object_key(str(record["news_id"]), "json"),
        )

        archive_record = build_archive_record(
            object_type="normalized_news",
            object_id=str(record["news_id"]),
            stream=str(record["stream"]),
            classification="internal",
            payload_ref=object_key(str(record["news_id"]), "json"),
            source_refs=[str(record.get("original_url", ""))],
            archive_id=job.job_id,
            stored_at=job.created_at,
        )
        self._archive.put(archive_record)

        event = make_event(
            event_type="news.ingested",
            producer_project="P07",
            object_id=str(record["news_id"]),
            correlation_id=job.correlation_id,
            payload_ref=archive_record["payload_ref"],
            classification="internal",
            idempotency_key=job.job_id,
        )
        self._bus.publish(event)

        current = self._jobs.get(job.job_id)
        assert current is not None  # the fixture queue retains all jobs
        return WorkflowResult(workflow=self._status(current), job=current, archived=True)

    def request_publish(self, *, stream: str, job: Job | None = None) -> dict[str, Any]:
        """Gate a publishing side effect; hold the job when the switch is off."""
        switches = self._switches()
        if not switches.publishing_stream_enabled(stream):
            reason = f"publishing:{stream} disabled at revision {switches.revision}"
            if job is not None:
                self._jobs.hold(job.job_id, reason)
            return {"allowed": False, "reason": reason, "switch_revision": switches.revision}
        return {"allowed": True, "reason": "enabled", "switch_revision": switches.revision}


class ProcessingHandoff:
    """Translate the existing P09 route into a versioned P29 event reference."""
    def __init__(self, outbox: PostgresOutbox) -> None:
        self._outbox = outbox

    def record(self, conn: Connection, classified: Mapping[str, Any], routing: Mapping[str, Any],
               revision: int, digest: str) -> None:
        validate_payload("classified_news", classified)
        news_id = str(classified["news_id"])
        stream = str(classified["stream"])
        project = str(routing.get("target_project", ""))
        if routing.get("news_id") != news_id or revision < 1:
            raise ValueError("invalid processing handoff identity")
        identity = str(uuid5(NAMESPACE_URL, f"etemad:P09:{news_id}:{revision}:{digest}"))
        event = make_event(event_type="news.classified", producer_project="P09",
                           object_id=news_id, correlation_id=identity, event_id=identity,
                           idempotency_key=identity, classification="internal",
                           occurred_at=str(classified["classified_at"]),
                           payload_ref=f"processing/history/{quote(news_id, safe='')}/{revision}")
        self._outbox.append_in_transaction(conn, event, stream=stream, project=project)

    def flush(self) -> int:
        return self._outbox.dispatch_to_jobs()
