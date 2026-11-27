"""B01/P01 application wiring: one Services object, injected via app.state.

News repositories support explicitly selected PostgreSQL storage. Other stores
retain their existing fixture wiring; selecting PostgreSQL never implies that
unrelated production prerequisites have passed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from fastapi import Request
from sqlalchemy.engine import make_url

from backend.app.foreign_workflow import ForeignWorkflow, build_foreign_fixture_workflow
from backend.app.pipeline import IngestPipeline
from backend.core.archive import InMemoryArchiveStore, PostgresNewsArchive
from backend.core.audit import Auditor, InMemoryAuditSink, PostgresAuditSink
from backend.core.auth import PostgresAccountStore
from backend.core.db import DatabaseRegistry
from backend.core.events import InMemoryEventBus
from backend.core.jobs import JobQueue
from backend.core.object_store import LocalObjectStore
from backend.core.orchestrator import Orchestrator
from backend.core.outbox import Outbox
from backend.core.output_switches import OutputSwitches, default_off_switches
from backend.core.permissions import InMemoryGrantStore, PostgresGrantStore
from backend.core.settings_service import (
    DOMESTIC_INGEST_CONFIG_ID,
    DOMESTIC_INGEST_SCOPE,
    InMemoryRevisionStore,
    PostgresIngestScheduleStore,
    SettingsService,
)
from backend.core.sources_service import PostgresSourceService, SourceService
from backend.modules.domestic.feed_client import (
    FeedTransport,
    FixtureFeedTransport,
    HttpxFeedTransport,
)
from backend.modules.domestic.repository import PostgresRawNewsRepository, RawNewsRepository
from backend.modules.domestic.scheduler import (
    DomesticPollPlan,
    PollResult,
    PollStateStore,
    PostgresPollStateStore,
    PostgresScheduleStore,
    ScheduleStore,
)
from backend.modules.editorial.generation import EditorialGenerator
from backend.modules.editorial.image_repository import ImageRepository
from backend.modules.editorial.repository import EditorialRepository
from backend.modules.intelligence.chat_memory import PostgresChatMemory
from backend.modules.processing.repository import PostgresProcessingRepository, ProcessingRepository

_LOG = logging.getLogger(__name__)


@dataclass
class Services:
    registry: DatabaseRegistry
    session_key: bytes
    grants: InMemoryGrantStore
    audit_sink: InMemoryAuditSink | PostgresAuditSink
    auditor: Auditor
    settings: SettingsService
    sources: SourceService
    jobs: JobQueue
    bus: InMemoryEventBus
    outbox: Outbox
    archive: InMemoryArchiveStore
    objects: LocalObjectStore
    switches: Callable[[], OutputSwitches]
    pipeline: IngestPipeline
    transport: FeedTransport
    accounts: PostgresAccountStore | None = None
    foreign_workflow: ForeignWorkflow | None = None
    editorial: EditorialRepository | None = None
    editorial_generator: EditorialGenerator | None = None
    images: ImageRepository | None = None
    news_archive: PostgresNewsArchive | None = None
    chat_memory: PostgresChatMemory | None = None
    orchestrator: Orchestrator = field(init=False)

    def run_domestic_schedule(self, *, worker_id: str, limit: int = 10) -> list[PollResult]:
        """Run one private worker tick using the latest scoped P02 configuration."""
        revision = self.settings.get_latest(
            "schedule_config", DOMESTIC_INGEST_CONFIG_ID, DOMESTIC_INGEST_SCOPE,
        )
        if revision is None:
            return []
        return self.pipeline.run_scheduled_batch(
            DomesticPollPlan.from_record(revision.value), worker_id=worker_id, limit=limit,
        )

    def __post_init__(self) -> None:
        self.orchestrator = Orchestrator(
            jobs=self.jobs,
            bus=self.bus,
            archive=self.archive,
            switches=self.switches,
        )


def build_services(environ: Mapping[str, str] | None = None) -> Services:
    environ = os.environ if environ is None else environ
    registry = DatabaseRegistry(environ=environ)
    storage = environ.get("ETEMAD_NEWS_STORAGE", "fixture")
    if storage not in {"fixture", "postgres"}:
        raise ValueError("invalid ETEMAD_NEWS_STORAGE")
    raw_repository: RawNewsRepository | PostgresRawNewsRepository
    processing_repository: ProcessingRepository | PostgresProcessingRepository
    if storage == "postgres":
        try:
            domestic_url = make_url(registry.url_for("domestic"))
            central_url = make_url(registry.url_for("central"))
        except (ValueError, RuntimeError):
            raise ValueError("separate domestic and central PostgreSQL URLs are required") from None
        urls = (domestic_url, central_url)
        if any(url.get_backend_name() != "postgresql" or not url.database for url in urls):
            raise ValueError("both news database roles require PostgreSQL database names")
        endpoints = {(url.host, url.port or 5432, url.database) for url in urls}
        if len(endpoints) != 2:
            raise ValueError("domestic and central databases must be separate")
        raw_repository = PostgresRawNewsRepository(registry)
        processing_repository = PostgresProcessingRepository(registry)
    else:
        raw_repository = RawNewsRepository()
        processing_repository = ProcessingRepository()

    session_key = environ.get("ETEMAD_SESSION_KEY", "").strip().encode("utf-8")
    if storage == "postgres" and not session_key:
        raise ValueError("ETEMAD_SESSION_KEY must be configured for persistent news storage")
    if not session_key:
        # Ephemeral per-process key: development only. Production must set the
        # variable; tokens do not survive a restart without it.
        session_key = secrets.token_bytes(32)
        _LOG.warning("ETEMAD_SESSION_KEY is not set; using an ephemeral session key")

    identity = environ.get("ETEMAD_IDENTITY_STORAGE", "fixture")
    if identity not in {"fixture", "postgres"}:
        raise ValueError("invalid ETEMAD_IDENTITY_STORAGE")
    accounts: PostgresAccountStore | None = None
    grant_store: InMemoryGrantStore
    audit_sink: InMemoryAuditSink | PostgresAuditSink
    if identity == "postgres":
        if (storage != "postgres" or environ.get("ETEMAD_ENV") != "staging"
                or environ.get("ETEMAD_FEED_MODE", "fixture") != "fixture"
                or len(environ.get("ETEMAD_SESSION_KEY", "").strip().encode("utf-8")) < 32):
            raise ValueError("persistent staff identity requires private staging, fixture feeds and a strong key")
        # A fixture bearer must never be accepted in persistent identity mode.
        marker_key = hmac.new(session_key, b"etemad-account-marker-v1", hashlib.sha256).digest()
        session_key = hmac.new(session_key, b"etemad-staff-session-v1", hashlib.sha256).digest()
        accounts = PostgresAccountStore(registry, marker_key)
        grant_store = PostgresGrantStore(registry)
        audit_sink = PostgresAuditSink(registry)
    else:
        grant_store = InMemoryGrantStore()
        audit_sink = InMemoryAuditSink()
    source_storage = environ.get("ETEMAD_SOURCE_STORAGE", "fixture")
    if source_storage not in {"fixture", "postgres"}:
        raise ValueError("invalid ETEMAD_SOURCE_STORAGE")
    sources: SourceService
    if source_storage == "postgres":
        if (storage != "postgres" or environ.get("ETEMAD_ENV") != "staging"
                or environ.get("ETEMAD_FEED_MODE", "fixture") != "fixture"):
            raise ValueError("persistent source registry requires private staging and fixture feeds")
        sources = PostgresSourceService(registry)
    else:
        sources = SourceService()
    schedule_storage = environ.get("ETEMAD_SCHEDULE_STORAGE", "fixture")
    if schedule_storage not in {"fixture", "postgres"}:
        raise ValueError("invalid ETEMAD_SCHEDULE_STORAGE")
    revision_store: InMemoryRevisionStore
    if schedule_storage == "postgres":
        if source_storage != "postgres":
            raise ValueError("persistent schedule requires private PostgreSQL source registry")
        revision_store = PostgresIngestScheduleStore(registry)
    else:
        revision_store = InMemoryRevisionStore()
    settings = SettingsService(revision_store, grant_store, credential_ref_check=lambda ref: bool(ref))
    jobs = JobQueue()
    bus = InMemoryEventBus()
    outbox = Outbox()
    archive = InMemoryArchiveStore()
    objects = LocalObjectStore(Path(environ.get("ETEMAD_OBJECT_ROOT", "backend/.local-objects")))

    news_archive = PostgresNewsArchive(registry.engine("central"), objects) if storage == "postgres" else None

    def switches() -> OutputSwitches:
        stored = settings.get_latest("output_controls", "controls-main", "global")
        if stored is None:
            return default_off_switches()
        try:
            return OutputSwitches.from_record(stored.value)
        except Exception:  # noqa: BLE001 - a broken config must not enable outputs
            _LOG.error("stored output_controls is invalid; falling back to all-off")
            return default_off_switches()

    # Feed transport: fixture unless the operator explicitly asks for live polling.
    transport: FeedTransport
    if environ.get("ETEMAD_FEED_MODE", "fixture") == "live":
        transport = HttpxFeedTransport()
    else:
        transport = FixtureFeedTransport(responses={})

    poll_states: PollStateStore
    schedules: ScheduleStore
    if storage == "postgres":
        persistent_poll_states = PostgresPollStateStore(registry)
        poll_states = persistent_poll_states
        schedules = PostgresScheduleStore(registry, persistent_poll_states)
    else:
        poll_states = PollStateStore()
        schedules = ScheduleStore(poll_states)
    pipeline = IngestPipeline(
        sources=sources,
        repository=raw_repository,
        processed=processing_repository,
        poll_states=poll_states,
        schedules=schedules,
        transport=transport,
        news_archive=news_archive,
    )

    foreign_workflow: ForeignWorkflow | None = None
    foreign_mode = environ.get("ETEMAD_FOREIGN_MODE", "off")
    if foreign_mode not in {"off", "fixture"}:
        raise ValueError("invalid foreign mode")
    if foreign_mode == "fixture":
        if (storage != "postgres" or source_storage != "postgres"
                or environ.get("ETEMAD_ENV") != "staging"
                or environ.get("ETEMAD_FEED_MODE", "fixture") != "fixture"):
            raise ValueError("foreign workflow requires private fixture staging")
        foreign_urls = [make_url(registry.url_for(role)) for role in ("central", "domestic", "foreign")]
        if any(url.get_backend_name() != "postgresql" or not url.database for url in foreign_urls):
            raise ValueError("foreign workflow requires three PostgreSQL databases")
        if len({(url.host, url.port or 5432, url.database) for url in foreign_urls}) != 3:
            raise ValueError("foreign database must be separate")
        foreign_workflow = build_foreign_fixture_workflow(
            registry.engine("foreign"), registry.engine("central"), sources, pipeline,
        )

    return Services(
        registry=registry,
        session_key=session_key,
        grants=grant_store,
        audit_sink=audit_sink,
        auditor=Auditor(audit_sink),
        settings=settings,
        sources=sources,
        jobs=jobs,
        bus=bus,
        outbox=outbox,
        archive=archive,
        objects=objects,
        switches=switches,
        pipeline=pipeline,
        transport=transport,
        accounts=accounts,
        foreign_workflow=foreign_workflow,
        news_archive=news_archive,
        chat_memory=PostgresChatMemory(registry.engine("central")) if storage == "postgres" else None,
        images=ImageRepository(registry.engine("central"), objects) if storage == "postgres" else None,
        editorial=EditorialRepository(registry.engine("central")) if storage == "postgres" else None,
    )


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:  # pragma: no cover - composition error, not a request path
        raise RuntimeError("application services are not initialised")
    return services
