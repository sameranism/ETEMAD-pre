"""Private fixture composition: foreign storage -> signed bridge -> central newsroom."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from backend.app.foreign_inbox import ForeignInbox
from backend.app.pipeline import IngestPipeline
from backend.core.sources_service import SourceService
from backend.modules.foreign.bridge import BridgeError, BridgeVerifier, sign_transfer
from backend.modules.foreign.collector import collect_foreign
from backend.modules.foreign.feed_client import ForeignFeedClient, ForeignFeedError
from backend.modules.foreign.repository import ForeignRepository, PollCheckpoint
from backend.modules.processing.normalizer import normalize

FIXTURE_SITE = "https://example.invalid/foreign-stage/"
FIXTURE_FEED = FIXTURE_SITE + "rss"
FIXTURE_BODY = b"<rss><channel><title>Private fixture</title><item><title>Stage foreign fixture</title><link>https://example.invalid/foreign-stage/one</link><description>Synthetic only</description></item></channel></rss>"
SENDER = "private-foreign-fixture"


class ForeignWorkflow:
    """No live HTTP. Only a central-approved synthetic source can be polled."""

    def __init__(self, repository: ForeignRepository, inbox: ForeignInbox,
                 sources: SourceService, pipeline: IngestPipeline,
                 client: ForeignFeedClient, signing_key: bytes) -> None:
        self._repository = repository
        self._inbox = inbox
        self._sources = sources
        self._pipeline = pipeline
        self._client = client
        self._signing_key = signing_key

    def _consume(self, raw: dict[str, Any]) -> None:
        existing = self._pipeline.processed.get(raw["news_id"])
        if existing is not None:
            if existing.news != normalize(raw):
                raise BridgeError("foreign processing identity conflict")
            return
        self._pipeline.process(raw)

    def poll(self, source_id: str) -> dict[str, Any]:
        source = self._sources.require_usable(source_id, stream="foreign")
        if source.get("feed_url") != FIXTURE_FEED:
            raise ForeignFeedError("only the designated private fixture may be polled")
        now = datetime.now(timezone.utc)
        previous = self._repository.checkpoint(source_id)
        try:
            response = self._client.fetch(
                source, etag=previous.etag if previous else None,
                last_modified=previous.last_modified if previous else None,
            )
            raw_records = collect_foreign(response, source, collected_at=now)
        except (ForeignFeedError, ValueError):
            failures = (previous.consecutive_failures if previous else 0) + 1
            self._repository.save_checkpoint(PollCheckpoint(
                source_id=source_id, revision=previous.revision if previous else 0,
                next_poll_at=now + timedelta(minutes=min(60, 5 * 2 ** min(failures - 1, 4))),
                etag=previous.etag if previous else None,
                last_modified=previous.last_modified if previous else None,
                last_success_at=previous.last_success_at if previous else None,
                consecutive_failures=failures,
            ), source=source)
            raise
        created = sum(self._repository.save_raw(raw, source=source) for raw in raw_records)
        self._repository.save_checkpoint(PollCheckpoint(
            source_id=source_id, revision=previous.revision if previous else 0,
            next_poll_at=now + timedelta(minutes=5),
            etag=response.etag or (previous.etag if previous else None),
            last_modified=response.last_modified or (previous.last_modified if previous else None),
            last_success_at=now,
        ), source=source)
        delivered = 0
        for raw in self._repository.pending_delivery(source_id, limit=50):
            receipt = self._inbox.receive(sign_transfer(raw, sender=SENDER, key=self._signing_key))
            self._repository.acknowledge_delivery(raw["news_id"], receipt.receipt_id)
            delivered += 1
        processed = 0
        for _ in range(50):
            if not self._inbox.process_one(self._consume):
                break
            processed += 1
        self._pipeline.flush_handoff()
        return {"schema_version": "1.0", "source_id": source_id, "mode": "fixture",
                "collected": created, "delivered": delivered, "processed": processed,
                "not_modified": response.not_modified}

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        return {"schema_version": "1.0", "mode": "fixture",
                **self._repository.overview(limit=limit), **self._inbox.counts()}


def build_foreign_fixture_workflow(foreign_engine: Engine, central_engine: Engine,
                                   sources: SourceService, pipeline: IngestPipeline) -> ForeignWorkflow:
    # Ephemeral service-only identity: never a staff token or a live credential.
    key = secrets.token_bytes(32)

    def fixture(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or str(request.url) != FIXTURE_FEED:
            return httpx.Response(404)
        etag = '"foreign-stage-v1"'
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, content=FIXTURE_BODY, headers={"ETag": etag})

    return ForeignWorkflow(
        ForeignRepository(foreign_engine),
        ForeignInbox(central_engine, BridgeVerifier({SENDER: key}), sources.get),
        sources, pipeline, ForeignFeedClient(httpx.MockTransport(fixture)), key,
    )
