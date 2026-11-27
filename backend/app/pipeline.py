"""Application pipeline: domestic ingest -> extract -> normalise -> cluster -> classify -> route.

This module is the only place that wires package modules together (P05 -> P07 ->
P08 -> P09). The modules themselves never import each other; they exchange
contract records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.core.archive import PostgresNewsArchive
from backend.core.orchestrator import ProcessingHandoff
from backend.core.sources_service import SourceService
from backend.modules.domestic.collector import collect
from backend.modules.domestic.feed_client import (
    FeedError,
    FeedTransport,
    FixtureFeedTransport,
    fetch_with_retry,
)
from backend.modules.domestic.repository import PostgresRawNewsRepository, RawNewsRepository
from backend.modules.domestic.scheduler import (
    BASE_INTERVAL_SECONDS,
    DomesticPollPlan,
    PollClaim,
    PollResult,
    PollStateConflict,
    PollStateStore,
    ScheduleStore,
    record_failure,
    record_success,
)
from backend.modules.processing.classifier import classify
from backend.modules.processing.deduplicator import Clusterer
from backend.modules.processing.extractor import extract
from backend.modules.processing.normalizer import normalize
from backend.modules.processing.repository import (
    PostgresProcessingRepository,
    ProcessingItem,
    ProcessingRepository,
)
from backend.modules.processing.router import route


@dataclass
class IngestPipeline:
    sources: SourceService
    repository: RawNewsRepository | PostgresRawNewsRepository
    poll_states: PollStateStore
    schedules: ScheduleStore
    transport: FeedTransport
    clusterer: Clusterer = field(default_factory=Clusterer)
    processed: ProcessingRepository | PostgresProcessingRepository = field(
        default_factory=ProcessingRepository
    )

    news_archive: PostgresNewsArchive | None = None
    handoff: ProcessingHandoff | None = None

    # --- ingest -----------------------------------------------------------
    def poll_source(
        self, source_id: str, *, snapshot_source: Mapping[str, Any] | None = None,
        interval_seconds: int = BASE_INTERVAL_SECONDS,
    ) -> PollResult:
        try:
            if snapshot_source is None:
                source = self.sources.require_usable(source_id, stream="domestic")
            else:
                source = dict(snapshot_source)
                current = self.sources.require_usable(source_id, stream="domestic")
                if current != source:
                    raise ValueError("source changed after snapshot")
                if (
                    source.get("source_id") != source_id
                    or source.get("stream") != "domestic"
                    or source.get("enabled") is not True
                    or source.get("license_scope") == "unknown"
                ):
                    raise ValueError("invalid domestic source snapshot")
        except Exception as exc:  # noqa: BLE001 - surfaced as a blocked poll, not a crash
            return PollResult(source_id=source_id, status="blocked", error=type(exc).__name__)

        state = self.poll_states.get_or_create(source_id)
        feed_url = source.get("feed_url") or source.get("site_url")
        if not feed_url:
            record_failure(state, error="source has no feed_url", interval_seconds=interval_seconds)
            self.poll_states.save(state)
            return PollResult(source_id=source_id, status="failed", error="missing feed_url", state=state.as_dict())

        try:
            response = fetch_with_retry(
                self.transport,
                str(feed_url),
                etag=state.etag,
                last_modified=state.last_modified,
            )
        except FeedError as exc:
            record_failure(state, error=str(exc), interval_seconds=interval_seconds)
            self.poll_states.save(state)
            return PollResult(source_id=source_id, status="failed", error=str(exc), state=state.as_dict())

        if response.not_modified:
            record_success(
                state, etag=response.etag, last_modified=response.last_modified,
                interval_seconds=interval_seconds,
            )
            self.poll_states.save(state)
            return PollResult(source_id=source_id, status="not_modified", collected=0, state=state.as_dict())

        try:
            raw_records = collect(response.body, source=source)
        except Exception as exc:  # noqa: BLE001 - a broken feed is a failed poll
            record_failure(state, error=f"parse:{type(exc).__name__}", interval_seconds=interval_seconds)
            self.poll_states.save(state)
            return PollResult(
                source_id=source_id, status="failed", error=f"parse:{type(exc).__name__}", state=state.as_dict()
            )

        created, _skipped = self.repository.upsert_many(raw_records)
        record_success(
                state, etag=response.etag, last_modified=response.last_modified,
                interval_seconds=interval_seconds,
            )
        self.poll_states.save(state)
        return PollResult(source_id=source_id, status="ok", collected=created, state=state.as_dict())

    def sync_domestic_snapshot(self) -> str:
        """Import the current P03 contract snapshot into P05-owned storage."""
        return self.schedules.apply_snapshot(self.sources.snapshot("domestic"))

    def claim_due(
        self, *, worker_id: str, limit: int = 10, lease_seconds: int = 120,
    ) -> list[PollClaim]:
        self.sync_domestic_snapshot()
        return self.schedules.claim_due(
            worker_id=worker_id, limit=limit, lease_seconds=lease_seconds,
        )

    def poll_claim(
        self, claim: PollClaim, *, interval_seconds: int = BASE_INTERVAL_SECONDS,
    ) -> PollResult:
        """Recheck the active claim and source before starting a new request."""
        if not self.schedules.is_current(claim):
            raise PollStateConflict("poll claim expired or snapshot changed")
        try:
            return self.poll_source(
                claim.source_id, snapshot_source=claim.source, interval_seconds=interval_seconds,
            )
        finally:
            self.schedules.release(claim)

    def run_scheduled_batch(
        self, plan: DomesticPollPlan, *, worker_id: str, limit: int = 10,
    ) -> list[PollResult]:
        """One bounded private-fixture worker tick; claim each item just before polling."""
        if not 1 <= limit <= 100:
            raise ValueError("invalid poll batch limit")
        if not plan.enabled:
            return []
        if not isinstance(self.transport, FixtureFeedTransport):
            raise ValueError("automatic domestic polling is fixture-only until live authorization")
        self.sync_domestic_snapshot()
        results: list[PollResult] = []
        for _ in range(limit):
            claims = self.schedules.claim_due(worker_id=worker_id, limit=1)
            if not claims:
                break
            result = self.poll_claim(claims[0], interval_seconds=plan.interval_seconds)
            results.append(result)
            if result.status == "blocked":
                # Refresh a changed snapshot next tick instead of spinning.
                break
        return results

    # --- processing -------------------------------------------------------
    def process(self, raw_record: Mapping[str, Any]) -> ProcessingItem:
        """Extract, normalise, cluster, classify and route one record."""
        if self.news_archive is not None:
            self.news_archive.capture(raw_record)
        extraction = extract(raw_record)
        normalized = normalize(raw_record)
        cluster = self.clusterer.cluster(normalized)
        classified = classify(normalized, event_id=str(cluster["event_id"]))
        routing = route(classified)
        result = self.processed.save(
            news=normalized,
            cluster=cluster,
            classification=classified,
            extraction=extraction.as_dict(),
            routing=routing.as_dict(),
        )
        self.flush_handoff()
        return result

    def flush_handoff(self) -> None:
        if self.handoff is not None:
            self.handoff.flush()

    def process_pending(self) -> int:
        """Process every stored raw record not processed yet."""
        self.flush_handoff()
        processed = 0
        for raw in self.repository.list(limit=500):
            if self.processed.contains(str(raw["news_id"])):
                continue
            self.process(raw)
            processed += 1
        return processed

    # --- read side --------------------------------------------------------
    def feed(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Projection used by the newsroom UI; derived from the contracts only."""
        projection: list[dict[str, Any]] = []
        for item in self.processed.list(limit=limit):
            news = item.news
            classification = item.classification
            routing = item.routing
            projection.append(
                {
                    "news_id": news["news_id"],
                    "title": news["original_title"],
                    "stream": news["stream"],
                    "source_id": news["source_id"],
                    "original_url": news["original_url"],
                    "published_at": news.get("published_at"),
                    "collected_at": news["collected_at"],
                    "reuse_permission": news["reuse_permission"],
                    "content_scope": news["content_scope"],
                    "text": news.get("normalized_text"),
                    "event_id": item.cluster["event_id"],
                    "duplicate_of": item.cluster["duplicate_of"],
                    "desks": classification["desks"],
                    "topics": classification["topics"],
                    "priority": classification["operational_priority"],
                    "queue": routing.get("queue"),
                    "target_project": routing.get("target_project"),
                    "word_count": item.extraction.get("word_count", 0),
                    "quotes": item.extraction.get("quotes", []),
                }
            )
        return projection

    def item(self, news_id: str) -> dict[str, Any] | None:
        item = self.processed.get(news_id)
        return item.as_dict() if item is not None else None
