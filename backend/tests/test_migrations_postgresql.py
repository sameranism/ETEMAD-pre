"""Round-trip active Alembic chains against disposable loopback PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.core.db import DatabaseRegistry
from backend.core.sources_service import PostgresSourceService, SourceNotAllowed
from backend.modules.domestic.repository import PostgresRawNewsRepository
from backend.modules.processing.classifier import classify
from backend.modules.processing.deduplicator import Clusterer
from backend.modules.processing.normalizer import normalize
from backend.modules.processing.repository import PostgresProcessingRepository

ADMIN_URL = os.environ.get("ETEMAD_TEST_PG_ADMIN_URL")
BACKEND = Path(__file__).resolve().parents[1]

TABLES_SQL = """
    SELECT table_schema, table_name FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'public')
    AND table_type = 'BASE TABLE'
"""


@pytest.mark.skipif(not ADMIN_URL, reason="ETEMAD_TEST_PG_ADMIN_URL is unset")
def test_migration_round_trip_and_database_separation(monkeypatch, tmp_path):
    monkeypatch.setenv("ETEMAD_OBJECT_ROOT", str(tmp_path / "objects"))
    from alembic import command
    from alembic.config import Config

    assert ADMIN_URL is not None
    admin = make_url(ADMIN_URL)
    assert admin.host == "127.0.0.1" and admin.database == "postgres"
    assert admin.username == "fixture_admin" and not admin.query

    suffix = uuid4().hex[:12]
    databases = {role: f"etemad_test_{role}_{suffix}" for role in ("central", "domestic", "foreign")}

    def conninfo(database):
        url = admin.set(drivername="postgresql", database=database)
        return url.render_as_string(hide_password=False)

    def tables(role):
        with psycopg.connect(conninfo(databases[role])) as conn:
            return set(conn.execute(TABLES_SQL).fetchall())

    monkeypatch.chdir(BACKEND)
    created = []
    with psycopg.connect(conninfo("postgres"), autocommit=True) as control:
        try:
            for role, database in databases.items():
                control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
                created.append(database)
                test_url = admin.set(drivername="postgresql+psycopg", database=database)
                monkeypatch.setenv(
                    f"ETEMAD_DB_{role.upper()}_URL", test_url.render_as_string(hide_password=False),
                )

            central = Config("alembic.ini")
            command.upgrade(central, "head")
            central_tables = tables("central")
            assert len(central_tables) == 20
            assert ("editorial", "image_assets") in central_tables
            assert {("archive", "news_records"), ("archive", "artifact_index")} <= central_tables
            assert {("editorial", "articles"), ("editorial", "translations")} <= central_tables
            assert ("processing", "foreign_inbox") in central_tables
            assert {('sources', 'source_registry'), ('sources', 'source_history')} <= central_tables
            assert ("identity", "login_throttle") in central_tables
            assert ("processing", "artifact_history") in central_tables
            assert {schema for schema, _ in central_tables} == {
                "audit", "control", "identity", "processing", "sources", "editorial", "archive", "publish",
            }
            assert tables("domestic") == tables("foreign") == set()
            command.downgrade(central, "001_baseline")
            assert tables("central") == {
                row for row in central_tables
                if row[0] not in {"processing", "sources", "editorial", "archive", "publish"} and row != ("identity", "login_throttle")
            }
            command.downgrade(central, "base")
            assert tables("central") == set()
            command.upgrade(central, "head")
            assert tables("central") == central_tables

            domestic = Config("alembic_domestic.ini")
            command.upgrade(domestic, "head")
            domestic_tables = tables("domestic")
            assert len(domestic_tables) == 7
            assert {
                ("domestic", "raw_content_identity"),
                ("domestic", "source_snapshots"),
                ("domestic", "source_snapshot_entries"),
                ("domestic", "current_source_snapshot"),
                ("domestic", "poll_claims"),
            } <= domestic_tables
            assert not domestic_tables & central_tables
            command.downgrade(domestic, "base")
            assert tables("domestic") == set()
            command.upgrade(domestic, "head")
            assert tables("domestic") == domestic_tables
            assert tables("central") == central_tables
            assert tables("foreign") == set()
            _exercise_domestic_identity(domestic)
            _exercise_processing_history(central)
            _exercise_source_registry(central)
            assert tables("domestic") == domestic_tables
            assert tables("foreign") == set()
            foreign = Config("alembic_foreign.ini")
            command.upgrade(foreign, "head")
            foreign_tables = {
                ("foreign", "source_candidates"), ("foreign", "poll_state"), ("foreign", "raw_items"),
            }
            assert tables("foreign") == foreign_tables
            command.downgrade(foreign, "base")
            assert tables("foreign") == set()
            command.upgrade(foreign, "head")
            _exercise_foreign_storage(foreign, os.environ["ETEMAD_DB_FOREIGN_URL"])
            _exercise_foreign_inbox(central)
            _exercise_editorial_workspace(central)
            _exercise_foreign_dashboard()
            _exercise_archive_workspace(central)
            _exercise_image_workspace(central)
            _exercise_analysis_workspace()
            _exercise_publication_queue(central)
            assert tables("foreign") == foreign_tables
            assert tables("central") == central_tables
            assert tables("domestic") == domestic_tables
        finally:
            for database in reversed(created):
                control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def _exercise_processing_history(central):
    from alembic import command

    registry = DatabaseRegistry()
    repo = PostgresProcessingRepository(registry)
    stamp = "2026-11-23T07:00:00Z"
    raw = {
        "schema_version": "1.0",
        "news_id": "NEWS-TEST-1",
        "stream": "domestic",
        "source_id": "SRC-TEST-1",
        "original_url": "https://example.invalid/one",
        "original_title": "Fixture title",
        "original_text": "Synthetic original",
        "language": "en",
        "subject_countries": [],
        "collected_at": stamp,
        "content_scope": "summary",
        "reuse_permission": "unverified",
    }
    news = normalize(raw)
    cluster = Clusterer().cluster(news, created_at=stamp)
    classified = classify(news, event_id=cluster["event_id"], classified_at=stamp)
    first = {"news": news, "cluster": cluster, "classification": classified}
    try:
        assert repo.save(**first).news == news
        assert repo.save(**first).news == news
        assert len(repo) == 1
        assert repo.get("NEWS-TEST-1").news == news
        assert repo.by_event(cluster["event_id"])[0].news == news
        assert repo.by_desk(classified["desks"][0])[0].news == news
        assert repo.list(limit=0) == []
        assert repo.list(limit=1)[0].news == news

        retry = deepcopy(first)
        retry["cluster"]["created_at"] = "2026-11-23T08:00:00Z"
        retry["classification"]["classified_at"] = "2026-11-23T08:00:00Z"
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: repo.save(**retry), range(8)))
        assert all(item.news == news for item in results)

        revised = deepcopy(first)
        revised["news"] = normalize({**raw, "original_text": "Corrected synthetic original"})
        repo.save(**revised)
        assert repo.get("NEWS-TEST-1").news == revised["news"]
        assert repo.save(**first).news == revised["news"]

        conflicting = deepcopy(first)
        conflicting["cluster"]["news_id"] = "NEWS-OTHER"
        with pytest.raises(ValueError, match="identity mismatch"):
            repo.save(**conflicting)
        conflicting = deepcopy(first)
        conflicting["news"]["source_id"] = "SRC-OTHER"
        with pytest.raises(ValueError, match="identity conflict"):
            repo.save(**conflicting)

        # Identical content from another source remains an independent record.
        other = deepcopy(first)
        other["news"] = normalize({**raw, "news_id": "NEWS-TEST-2",
                                   "source_id": "SRC-TEST-2",
                                   "original_url": "https://example.invalid/two"})
        other["cluster"]["news_id"] = "NEWS-TEST-2"
        other["cluster"]["duplicate_of"] = "NEWS-TEST-1"
        other["classification"]["news_id"] = "NEWS-TEST-2"
        repo.save(**other)
        assert len(repo) == 2

        # A failed child write must roll back the parent insert.
        bad = deepcopy(other)
        bad["news"]["news_id"] = "NEWS-TEST-3"
        bad["news"]["original_url"] = "https://example.invalid/three"
        bad["cluster"]["news_id"] = "NEWS-TEST-3"
        bad["classification"]["news_id"] = "NEWS-TEST-3"
        bad["cluster"]["event_id"] = "x" * 33
        bad["classification"]["event_id"] = "x" * 33
        with pytest.raises(DBAPIError):
            repo.save(**bad)

        engine = registry.engine("central")
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT count(*) FROM processing.normalized_news"
            )).scalar_one() == 2
            rows = conn.execute(text(
                "SELECT revision_no, snapshot FROM processing.artifact_history "
                "WHERE news_id = 'NEWS-TEST-1' ORDER BY revision_no"
            )).all()
            assert [row.revision_no for row in rows] == [1, 2]
            assert rows[0].snapshot["news"]["original_text"] == "Synthetic original"
            assert rows[1].snapshot["news"]["original_text"] == "Corrected synthetic original"
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO processing.cluster_result "
                    "(news_id, event_id, related_news_ids, similarity_method, created_at) "
                    "VALUES ('orphan', 'EVENT-X', '[]', 'test', now())"
                ))
        with pytest.raises(DBAPIError, match="processing history is not empty"):
            command.downgrade(central, "002_processing")
        assert repo.get("NEWS-TEST-1").news == revised["news"]
    finally:
        registry.dispose()


def _exercise_domestic_identity(domestic):
    from alembic import command

    registry = DatabaseRegistry()
    repo = PostgresRawNewsRepository(registry)
    raw = {
        "schema_version": "1.0", "news_id": "NEWS-DOM-1",
        "stream": "domestic", "source_id": "SRC-DOM-1",
        "original_url": "https://example.invalid/one",
        "original_title": "Fixture title", "original_text": "Synthetic content",
        "language": "en", "subject_countries": [],
        "collected_at": "2026-11-23T07:00:00Z",
        "content_scope": "summary", "reuse_permission": "unverified",
    }
    try:
        first, created = repo.upsert(raw)
        assert created
        assert first["original_text"] == raw["original_text"]
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: repo.upsert(raw), range(8)))
        assert all(not created for _, created in results)
        assert len(repo) == 1

        alias = {**raw, "news_id": "NEWS-DOM-2",
                 "original_url": "https://example.invalid/two"}
        assert repo.upsert(alias)[1]
        different_source = {**raw, "news_id": "NEWS-DOM-3", "source_id": "SRC-DOM-2"}
        assert repo.upsert(different_source)[1]
        assert repo.list(stream="foreign") == []
        assert len(repo.list(stream="domestic")) == 3
        with pytest.raises(ValueError, match="only domestic"):
            repo.upsert({**raw, "stream": "foreign"})
        with pytest.raises(ValueError, match="immutable identity"):
            repo.upsert({**raw, "original_text": "Revised source text"})
        with pytest.raises(ValueError, match="identity"):
            repo.upsert({**raw, "news_id": "NEWS-DOM-COLLISION"})
        new = {**raw, "news_id": "NEWS-DOM-NEW",
               "original_url": "https://example.invalid/new"}
        with pytest.raises(ValueError):
            repo.upsert_many([new, {**raw, "original_text": "Different"}])
        assert repo.get(new["news_id"]) is None
        assert len(repo) == 3
        engine = registry.engine("domestic")
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT count(*) FROM domestic.raw_content_identity"
            )).scalar_one() == 2
        # Derived identity can be reconstructed without losing raw records.
        registry.dispose()
        command.downgrade(domestic, "001_ingest")
        command.upgrade(domestic, "head")
        assert len(repo) == 3
        assert repo.get(raw["news_id"])["original_text"] == raw["original_text"]
        assert not repo.upsert(raw)[1]
    finally:
        registry.dispose()


def _exercise_source_registry(central):
    from alembic import command

    registry = DatabaseRegistry()
    store = PostgresSourceService(registry)
    domestic = {
        "source_id": "SRC-SYNTHETIC-DOMESTIC",
        "site_url": "https://example.invalid",
        "feed_url": "https://example.invalid/rss",
        "api_base_url": None, "country": "IR", "language": "fa",
        "stream": "domestic", "auth_ref": None, "license_scope": "summary", "enabled": True,
    }
    foreign = {
        **domestic, "source_id": "SRC-SYNTHETIC-FOREIGN", "stream": "foreign", "country": "DE",
    }
    try:
        assert store.register(domestic) == domestic
        assert store.register(domestic) == domestic
        assert len(store.history(domestic["source_id"])) == 1
        candidate = store.register_candidate(foreign)
        assert candidate["enabled"] is False
        with pytest.raises(SourceNotAllowed):
            store.require_usable(foreign["source_id"], "foreign")
        store.set_enabled(foreign["source_id"], True)
        with pytest.raises(SourceNotAllowed):
            store.require_usable(foreign["source_id"], "domestic")
        with pytest.raises(SourceNotAllowed):
            store.register_candidate(foreign)
        with pytest.raises(SourceNotAllowed):
            store.register({**domestic, "license_scope": "unknown"})
        with pytest.raises(ValueError, match="identity conflict"):
            store.register({**domestic, "feed_url": "https://example.invalid/changed"})
        with pytest.raises(IntegrityError):
            store.register({
                **domestic, "source_id": "SRC-DUPLICATE",
                "feed_url": "https://EXAMPLE.INVALID:443/rss",
            })
        with pytest.raises(ValueError, match="auth_ref"):
            store.register({**domestic, "site_url": "https://user@example.invalid"})
        store.set_enabled(domestic["source_id"], False)
        registry.dispose()
        store = PostgresSourceService(registry)
        assert not store.get(domestic["source_id"])["enabled"]
        assert store.list(enabled_only=True) == [foreign]
        revisions = store.history(domestic["source_id"])
        assert [x["revision_no"] for x in revisions] == [1, 2]
        assert [x["source"]["enabled"] for x in revisions] == [True, False]
        assert len(store.document()["sources"]) == 2
        with pytest.raises(DBAPIError, match="source registry is not empty"):
            command.downgrade(central, "004_login_throttle")
    finally:
        registry.dispose()

def _exercise_foreign_storage(foreign, foreign_url):
    from dataclasses import replace
    from datetime import datetime, timezone

    from alembic import command
    from sqlalchemy import create_engine

    from backend.modules.foreign.collector import collect_foreign
    from backend.modules.foreign.country_discovery import (
        DocumentEvidence,
        SiteEvidence,
        discover_country,
    )
    from backend.modules.foreign.feed_client import ForeignFeedError, ForeignFeedResponse
    from backend.modules.foreign.repository import ForeignRepository, PollCheckpoint

    now = datetime(2026, 11, 26, 19, tzinfo=timezone.utc)
    site = "https://example.invalid/"
    feed = site + "rss"
    body = b"""<rss><channel><title>Fixture</title><item><title>Foreign fixture</title>
    <link>https://example.invalid/foreign/1</link><description>Original text</description>
    </item></channel></rss>"""
    result = discover_country(
        "DE", [SiteEvidence(DocumentEvidence(site, 200, b'<link rel="alternate" type="application/rss+xml" href="/rss">', now), "DE", "de")],
        feeds={feed: DocumentEvidence(feed, 200, body, now)}, generated_at=now,
    )
    candidate = result.pack["sources"][0]
    approved = {**candidate, "enabled": True, "license_scope": "summary"}
    engine = create_engine(foreign_url)
    try:
        repo = ForeignRepository(engine)
        assert repo.save_candidates(result) == 1
        assert repo.save_candidates(result) == 0
        assert repo.candidates()[0]["payload"]["enabled"] is False
        assert repo.candidates()[0]["verified"] is True
        record = collect_foreign(ForeignFeedResponse(feed, body, 200), approved, collected_at=now)[0]
        with pytest.raises(ForeignFeedError):
            repo.save_raw(record, source=candidate)
        assert repo.save_raw(record, source=approved)
        assert not repo.save_raw(record, source=approved)
        with pytest.raises(ValueError, match="immutable"):
            repo.save_raw({**record, "original_text": "changed"}, source=approved)
        with pytest.raises(ValueError, match="scope"):
            repo.save_raw({**record, "stream": "domestic"}, source=approved)
        assert not repo.save_raw({**record, "news_id": "NEWS-DUPLICATE",
                                  "original_url": site + "foreign/2"}, source=approved)
        first = PollCheckpoint(approved["source_id"], 0, now, etag='"fixture"')
        saved = repo.save_checkpoint(first, source=approved)
        assert saved.revision == 1
        with pytest.raises(ValueError, match="stale"):
            repo.save_checkpoint(first, source=approved)
        assert repo.save_checkpoint(replace(saved, consecutive_failures=1), source=approved).revision == 2
        engine.dispose()
        restarted = ForeignRepository(engine)
        assert restarted.raw_items() == [record]
        assert restarted.checkpoint(approved["source_id"]).revision == 2
        with pytest.raises(DBAPIError, match="not empty"):
            command.downgrade(foreign, "base")
    finally:
        engine.dispose()

def _exercise_foreign_inbox(central):
    from dataclasses import replace

    from alembic import command

    from backend.app.dependencies import build_services
    from backend.app.foreign_inbox import ForeignInbox
    from backend.modules.foreign.bridge import BridgeError, BridgeVerifier, sign_transfer

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    # Isolate the older migration's downgrade guard from the later P26 archive.
    services.pipeline.news_archive = None
    source = {
        "source_id": "SRC-BRIDGE-FIXTURE", "site_url": "https://example.invalid/bridge/",
        "feed_url": "https://example.invalid/bridge/rss", "country": "DE", "language": "de",
        "stream": "foreign", "license_scope": "summary", "enabled": True,
    }
    raw = {
        "schema_version": "1.0", "news_id": "NEWS-BRIDGE-FIXTURE", "stream": "foreign",
        "source_id": source["source_id"], "source_country": "DE", "language": "de",
        "original_url": "https://example.invalid/bridge/one", "original_title": "Bridge fixture",
        "original_text": "Synthetic original foreign summary", "subject_countries": [],
        "collected_at": "2026-11-26T19:00:00+00:00", "content_scope": "summary",
        "reuse_permission": "unverified",
    }
    # Deterministic test material only; no actual service credential.
    key = b"fixture-only-foreign-signing-key-0001"
    verifier = BridgeVerifier({"foreign-fixture": key})
    packet = sign_transfer(raw, sender="foreign-fixture", key=key, sent_at=1000)
    engine = services.registry.engine("central")
    inbox = ForeignInbox(engine, verifier, services.sources.get)
    try:
        with pytest.raises(BridgeError, match="not currently approved"):
            inbox.receive(packet, now=1000)
        services.sources.register(source)
        with pytest.raises(BridgeError):
            inbox.receive(replace(packet, body=packet.body + b" "), now=1000)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: inbox.receive(packet, now=1000), range(2)))
        assert sorted(r.duplicate for r in results) == [False, True]
        assert len({r.receipt_id for r in results}) == 1
        with pytest.raises(BridgeError):
            inbox.receive(packet, now=2000)
        refreshed = sign_transfer(raw, sender="foreign-fixture", key=key, sent_at=2000)
        restarted = ForeignInbox(engine, verifier, services.sources.get)
        assert restarted.receive(refreshed, now=2000).duplicate is True
        conflicting = sign_transfer({**raw, "original_text": "Changed"}, sender="foreign-fixture",
                                    key=key, sent_at=2000)
        with pytest.raises(BridgeError, match="conflicting"):
            restarted.receive(conflicting, now=2000)
        services.sources.set_enabled(source["source_id"], False)
        with pytest.raises(BridgeError, match="not currently approved"):
            restarted.process_one(lambda _: pytest.fail("revoked source processed"))
        services.sources.set_enabled(source["source_id"], True)
        def crash_after_commit(record):
            services.pipeline.process(record)
            raise RuntimeError("fixture simulated crash after processing commit")
        with pytest.raises(RuntimeError, match="simulated crash"):
            restarted.process_one(crash_after_commit)
        def recover(record):
            item = services.pipeline.processed.get(record["news_id"])
            assert item is not None and item.news == normalize(record)
        assert restarted.process_one(recover)
        assert not restarted.process_one(recover)
        assert any(row["news_id"] == raw["news_id"] and row["stream"] == "foreign"
                   for row in services.pipeline.feed(limit=50))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM processing.foreign_inbox")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM processing.foreign_inbox WHERE processed_at IS NOT NULL")).scalar_one() == 1
        with pytest.raises(DBAPIError, match="foreign inbox is not empty"):
            command.downgrade(central, "005_source_registry")
    finally:
        services.registry.dispose()

def _exercise_foreign_dashboard():
    from fastapi.testclient import TestClient

    from backend.app.dependencies import build_services
    from backend.app.foreign_workflow import FIXTURE_FEED, FIXTURE_SITE
    from backend.app.main import create_app
    from backend.core.auth import Principal, issue_session
    from backend.core.permissions import Grant

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture", "ETEMAD_FOREIGN_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    assert services.foreign_workflow is not None
    source = {"source_id": "SRC-FOREIGN-DASHBOARD-FIXTURE",
        "site_url": FIXTURE_SITE, "feed_url": FIXTURE_FEED, "country": "DE", "language": "de",
        "stream": "foreign", "license_scope": "summary", "enabled": True}
    services.sources.register(source)
    fixture_principal = Principal("fixture-staff", "staff")
    headers = {"Authorization": "Bearer " + issue_session(fixture_principal, services.session_key)}
    poll_path = "/v1/foreign/" + source["source_id"] + "/poll"
    try:
        with TestClient(create_app(services=services)) as client:
            assert client.get("/v1/foreign/status").status_code == 401
            assert client.get("/v1/foreign/status", headers=headers).status_code == 403
            assert client.post(poll_path, headers=headers).status_code == 403
            services.grants.grant(Grant("fixture-staff", "staff", "foreign_monitor", "default", "read"))
            services.grants.grant(Grant("fixture-staff", "staff", "source_registry", source["source_id"], "poll"))
            services.grants.grant(Grant("fixture-staff", "staff", "newsroom", "feed", "read"))
            before = client.get("/v1/foreign/status?limit=50", headers=headers)
            assert before.status_code == 200
            assert before.headers["Cache-Control"] == "no-store"
            assert client.get("/v1/foreign/status?limit=201", headers=headers).status_code == 400
            initial = before.json()
            assert "etag" not in before.text and "feed_url" not in before.text
            first = client.post(poll_path, headers=headers)
            assert first.status_code == 200, first.text
            assert first.json()["collected"] == first.json()["delivered"] == first.json()["processed"] == 1
            again = client.post(poll_path, headers=headers)
            assert again.status_code == 200 and again.json()["not_modified"] is True
            assert again.json()["collected"] == again.json()["delivered"] == again.json()["processed"] == 0
            after = client.get("/v1/foreign/status", headers=headers).json()
            for field in ("raw_count", "delivered_count", "received_count", "processed_count"):
                assert after[field] == initial[field] + 1
            assert all(candidate["enabled"] is False for candidate in after["candidates"])
            news = client.get("/v1/news", headers=headers).json()
            assert sum(item["source_id"] == source["source_id"] and item["stream"] == "foreign"
                       for item in news["items"]) == 1
            services.sources.set_enabled(source["source_id"], False)
            assert client.post(poll_path, headers=headers).status_code == 403
    finally:
        services.registry.dispose()

def _exercise_editorial_workspace(central):
    from alembic import command
    from fastapi.testclient import TestClient

    from backend.app.dependencies import build_services
    from backend.app.main import create_app
    from backend.core.ai_gateway import Gateway, ModelBinding
    from backend.core.auth import Principal, issue_session
    from backend.core.permissions import Grant
    from backend.modules.editorial.generation import EditorialGenerator, EditorialProfile
    from backend.modules.editorial.repository import EditorialRepository

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    # Isolate the older migration's downgrade guard from the later P26 archive.
    services.pipeline.news_archive = None
    source = {"source_id": "SRC-EDITORIAL-FIXTURE", "site_url": "https://example.invalid/editorial",
        "feed_url": "https://example.invalid/editorial/rss", "country": "DE", "language": "en",
        "stream": "foreign", "license_scope": "summary", "enabled": True}
    raw = {"schema_version": "1.0", "news_id": "NEWS-EDITORIAL-FIXTURE", "stream": "foreign",
        "source_id": source["source_id"], "source_country": "DE", "language": "en",
        "original_url": "https://example.invalid/editorial/one", "original_title": "Fixture report",
        "original_text": 'Alice said “12 units”.', "subject_countries": [],
        "collected_at": "2026-11-26T19:00:00+00:00", "content_scope": "summary",
        "reuse_permission": "unverified"}
    news_id = raw["news_id"]
    principal = Principal("fixture-editor", "staff")
    headers = {"Authorization": "Bearer " + issue_session(principal, services.session_key)}
    path = "/v1/editorial/news/" + news_id + "/draft"
    services.sources.register(source)
    services.pipeline.process(raw)
    try:
        with TestClient(create_app(services=services)) as client:
            assert client.get(path).status_code == 401
            assert client.get(path, headers=headers).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "newsroom", news_id, "read"))
            assert client.get(path, headers=headers).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "editorial_workspace", news_id, "read"))
            first = client.get(path, headers=headers)
            assert first.status_code == 200 and first.headers["Cache-Control"] == "no-store"
            initial = first.json()
            assert initial["saved"] is None and initial["latest_version"] == 0
            assert initial["source"]["original_text"] == raw["original_text"]
            payload = {"expected_version": 0, "source_hash": initial["source_hash"],
                "desk": initial["desks"][0], "title": "گزارش نمونه", "lead": "Alice",
                "body": 'Alice گفت «12 units».', "short_summary": "خلاصه نمونه",
                "translated_text": 'Alice گفت «12 units».'}
            h = {**headers, "Idempotency-Key": "fixture-save-1"}
            assert client.put(path, headers=h, json=payload).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "editorial_workspace", news_id, "write"))
            response = client.put(path, headers=h, json=payload)
            assert response.status_code == 200, response.text
            saved = response.json()["saved"]
            assert saved["version"] == 1 and saved["article"]["status"] == "draft"
            assert saved["translation"]["model_id"] == "human/manual"
            assert saved["policy"]["decision"] == "human_review"
            assert client.put(path, headers=h, json=payload).json()["saved"] == saved
            assert client.put(path, headers=h, json={**payload, "title": "changed"}).status_code == 409
            assert client.put(path, headers={**h, "Idempotency-Key": "stale"}, json=payload).status_code == 409
            updated = {**payload, "expected_version": 1, "body": "Alice گفت 99 واحد.",
                       "translated_text": "Alice گفت واحد."}
            response = client.put(path, headers={**h, "Idempotency-Key": "fixture-save-2"}, json=updated)
            assert response.status_code == 200, response.text
            second = response.json()["saved"]
            assert second["version"] == 2 and second["article"]["status"] == "held"
            assert "FACT_NUMBER_MISMATCH" in second["translation"]["warnings"]
            assert "DIRECT_QUOTE_MISMATCH" in second["translation"]["warnings"]
            current = client.get(path, headers=headers).json()
            assert current["saved"] == second
            assert current["latest_version"] == current["saved"]["version"]
            old = client.get(path + "?version=1", headers=headers).json()
            assert old["saved"] == saved and old["latest_version"] == 2
            assert [r["version"] for r in old["history"]] == [2, 1]
            assert client.get(path + "?version=3", headers=headers).status_code == 404
            assert services.pipeline.item(news_id)["news"]["original_text"] == raw["original_text"]
            assert client.get(path.replace(news_id, "NEWS-OTHER"), headers=headers).status_code == 403

            # AI composition is explicitly injected in a disposable test, never a live provider.
            import json

            class FixtureModel:
                name = "fixture"
                calls: list[str] = []

                def complete(self, prompt, *, model, timeout_s):
                    material = json.loads(prompt)
                    assert material and timeout_s > 0
                    self.calls.append(model)
                    if model == "fixture-translation":
                        return 'Alice گفت «12 units».', 0.0
                    assert model == "fixture-editorial"
                    return json.dumps({k: 'Alice گفت «12 units».' for k in
                                       ("title", "lead", "body", "short_summary")}, ensure_ascii=False), 0.0

            generation_path = path.removesuffix("/draft") + "/generate"
            generate = {"expected_version": 2, "source_hash": initial["source_hash"], "desk": initial["desks"][0]}
            gh = {**headers, "Idempotency-Key": "fixture-generation-1"}
            assert client.post(generation_path, headers=gh, json=generate).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "editorial_workspace", news_id, "generate"))
            assert client.post(generation_path, headers=gh, json=generate).status_code == 503
            assert client.get(path, headers=headers).json()["ai_available"] is False
            provider = FixtureModel()
            gateway = Gateway({task: ModelBinding(task, "fixture", "fixture-" + task) for task in
                               ("translation", "editorial")}, {"fixture": provider}, enabled=True)
            ruleset = {"schema_version": "1.0", "ruleset_id": "RULE-PG-FIXTURE", "revision": 1,
                "stream": "foreign", "desk": generate["desk"], "replacement_rules": [],
                "prohibited_content_categories": [], "updated_at": raw["collected_at"]}
            framework = {"schema_version": "1.0", "framework_id": "FW-PG-FIXTURE",
                "stream": "foreign", "desk": generate["desk"], "headline_max_chars": 110,
                "summary_max_chars": 300, "required_sections": ["title", "lead", "body", "sources"],
                "attribution_required": True, "updated_at": raw["collected_at"]}
            profiles = {("foreign", generate["desk"]): EditorialProfile(ruleset, framework)}
            # Even an injected live composition cannot send unlicensed material.
            assert not EditorialGenerator(gateway, profiles, mode="live").available(raw, generate["desk"])
            services.editorial_generator = EditorialGenerator(gateway, profiles, mode="fixture")
            view = client.get(path, headers=headers).json()
            assert view["ai_available"] and view["generation_mode"] == "fixture"
            assert client.post(generation_path, headers=gh, json={**generate, "expected_version": 1}).status_code == 409
            assert provider.calls == []
            result = client.post(generation_path, headers=gh, json=generate)
            assert result.status_code == 200, result.text
            generated = result.json()["saved"]
            assert generated["version"] == 3 and generated["article"]["status"] == "draft"
            assert generated["translation"]["model_id"] == "fixture/fixture-translation"
            assert generated["quality"]["generation"]["mode"] == "fixture"
            assert generated["quality"]["generation"]["article_provenance"]["model"] == "fixture-editorial"
            assert generated["policy"]["decision"] == "human_review"
            assert client.post(generation_path, headers=gh, json=generate).json()["saved"] == generated
            assert provider.calls == ["fixture-translation", "fixture-editorial"]
            assert client.get(path, headers=headers).json()["saved"] == generated
            assert client.get(path + "?version=1", headers=headers).json()["saved"] == saved
            services.sources.set_enabled(source["source_id"], False)
            assert client.get(path, headers=headers).status_code == 403
            assert client.put(path, headers={**h, "Idempotency-Key": "revoked"}, json=updated).status_code == 403
            assert client.post(generation_path, headers=gh, json=generate).status_code == 403
            assert len(provider.calls) == 2
        engine = services.registry.engine("central")
        engine.dispose()
        reopened = EditorialRepository(engine)
        assert reopened.get(news_id)["version"] == 3
        assert reopened.get(news_id, 1) == saved
        with pytest.raises(DBAPIError, match="editorial history is not empty"):
            command.downgrade(central, "006_foreign_inbox")
    finally:
        services.registry.dispose()

def _exercise_archive_workspace(central):
    from alembic import command
    from fastapi.testclient import TestClient

    from backend.app.dependencies import build_services
    from backend.app.main import create_app
    from backend.core.archive import ArchiveIntegrityError, PostgresNewsArchive
    from backend.core.auth import Principal, issue_session
    from backend.core.permissions import Grant

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    source = {"source_id": "SRC-ARCHIVE-FIXTURE", "site_url": "https://example.invalid/archive",
        "feed_url": "https://example.invalid/archive/rss", "country": "DE", "language": "en",
        "stream": "foreign", "license_scope": "summary", "enabled": True}
    raw = {"schema_version": "1.0", "news_id": "NEWS-ARCHIVE-FIXTURE", "stream": "foreign",
        "source_id": source["source_id"], "source_country": "DE", "language": "en",
        "original_url": "https://example.invalid/archive/one", "original_title": "Archive fixture % literal",
        "original_text": "Original immutable text", "subject_countries": [],
        "collected_at": "2026-11-26T19:00:00+00:00", "content_scope": "summary",
        "reuse_permission": "unverified"}
    news_id = raw["news_id"]
    principal = Principal("fixture-archive", "staff")
    headers = {"Authorization": "Bearer " + issue_session(principal, services.session_key)}
    repo = services.news_archive
    assert repo is not None
    services.sources.register(source)
    services.pipeline.process(raw)
    try:
        assert len(repo.versions(news_id)) == 1
        first = repo.capture({**raw, "collected_at": "2026-11-27T01:00:00+00:00"})
        assert first["stored_at"] == raw["collected_at"]
        assert len(repo.versions(news_id)) == 1
        revised = repo.capture({**raw, "original_text": "A later correction"})
        assert revised["archive_id"] != first["archive_id"]
        assert repo.original(news_id, first["archive_id"]) == raw
        with pytest.raises(ArchiveIntegrityError, match="identity conflict"):
            repo.capture({**raw, "original_url": "https://example.invalid/archive/different"})
        assert len(repo.versions(news_id)) == 2
        # A separate object with identical search terms must not leak via search counts.
        repo.capture({**raw, "news_id": "NEWS-ARCHIVE-DENIED"})
        with TestClient(create_app(services=services)) as client:
            path = "/v1/archive/news"
            assert client.get(path).status_code == 401
            assert client.get(path, headers=headers).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "archive", "default", "search"))
            assert client.get(path, headers=headers).json()["items"] == []
            services.grants.grant(Grant(principal.subject_id, "staff", "archive", news_id, "read"))
            assert client.get(path, headers=headers).json()["items"] == []
            services.grants.grant(Grant(principal.subject_id, "staff", "newsroom", news_id, "read"))
            matches = client.get(path, params={"q": "% literal", "stream": "foreign"}, headers=headers)
            assert matches.status_code == 200 and matches.headers["Cache-Control"] == "no-store"
            assert matches.json()["count"] == 1 and matches.json()["items"][0]["news_id"] == news_id
            assert client.get(path, params={"q": "notfound%"}, headers=headers).json()["count"] == 0
            vp = path + "/" + news_id + "/versions"
            assert len(client.get(vp, headers=headers).json()["versions"]) == 2
            original_path = vp + "/" + first["archive_id"]
            assert client.get(original_path, headers=headers).json()["source"] == raw
            assert client.get(vp + "/raw-" + "0"*64, headers=headers).status_code == 404
            assert client.get(vp.replace(news_id, "NEWS-ARCHIVE-DENIED"), headers=headers).status_code == 403
            services.sources.register({**source, "license_scope": "metadata"})
            redacted = client.get(original_path, headers=headers).json()
            assert redacted["text_redacted"] and redacted["source"]["original_text"] is None
            services.sources.set_enabled(source["source_id"], False)
            assert client.get(original_path, headers=headers).status_code == 403
            assert client.get(path, headers=headers).json()["count"] == 0
            services.sources.register(source)
            engine = services.registry.engine("central")
            engine.dispose()
            reopened = PostgresNewsArchive(engine, services.objects)
            assert reopened.original(news_id, first["archive_id"]) == raw
            # Corrupt only a disposable test object, not the original stage data.
            services.objects.put(first["payload_ref"], b"corrupt")
            assert client.get(original_path, headers=headers).status_code == 422
            with pytest.raises(ArchiveIntegrityError, match="checksum"):
                reopened.capture(raw)
        with pytest.raises(DBAPIError, match="news archive is not empty"):
            command.downgrade(central, "007_editorial_revisions")
    finally:
        services.registry.dispose()

def _exercise_image_workspace(central):
    import base64
    from io import BytesIO

    from alembic import command
    from fastapi.testclient import TestClient
    from PIL import Image

    from backend.app.dependencies import build_services
    from backend.app.main import create_app
    from backend.core.auth import Principal, issue_session
    from backend.core.permissions import Grant

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    news_id = "NEWS-EDITORIAL-FIXTURE"
    source_id = "SRC-EDITORIAL-FIXTURE"
    principal = Principal("fixture-image-staff", "staff")
    headers = {"Authorization": "Bearer " + issue_session(principal, services.session_key)}
    path = "/v1/editorial/news/" + news_id + "/images"
    services.sources.set_enabled(source_id, True)
    buffer = BytesIO()
    Image.new("RGB", (200, 100), "blue").save(buffer, format="PNG")
    payload = {"article_version": 3, "image_data": base64.b64encode(buffer.getvalue()).decode("ascii"),
               "alt_text": "نمونه آزمایشی", "source_ref": "fixture:uploaded-image", "origin": "ai_declared",
               "rights_ref": "fixture:unverified-claim", "profiles": ["landscape", "square", "portrait"]}
    try:
        with TestClient(create_app(services=services)) as client:
            assert client.get(path + "?version=3").status_code == 401
            assert client.get(path + "?version=3", headers=headers).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "newsroom", news_id, "read"))
            services.grants.grant(Grant(principal.subject_id, "staff", "editorial_workspace", news_id, "read"))
            assert client.get(path + "?version=3", headers=headers).json()["items"] == []
            services.grants.grant(Grant(principal.subject_id, "staff", "editorial_workspace", news_id, "write"))
            assert client.post(path, headers=headers, json=payload).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "editorial_workspace", news_id, "prepare_image"))
            assert client.post(path, headers=headers, json={**payload, "license_status": "verified"}).status_code == 400
            assert client.post(path, headers=headers, json={**payload, "article_version": 2}).status_code == 409
            result = client.post(path, headers=headers, json=payload)
            assert result.status_code == 200, result.text
            saved = result.json()["saved"]
            assert saved["article_version"] == 3 and len(saved["manifest"]["assets"]) == 3
            assert not saved["evidence"]["publication_eligible"]
            assert all(a["license_status"] == "unknown" and a["ai_generated"] for a in saved["manifest"]["assets"])
            assert client.post(path, headers=headers, json=payload).json()["saved"] == saved
            assert client.get(path + "?version=2", headers=headers).json()["items"] == []
            assert client.get(path + "?version=3", headers=headers).json()["items"] == [saved]
            asset_path = path + "/" + saved["image_id"] + "/0"
            assert client.get(asset_path + "?version=2", headers=headers).status_code == 404
            response = client.get(asset_path + "?version=3", headers=headers)
            assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
            assert Image.open(BytesIO(response.content)).size == (1200, 675)
            assert response.headers["Cache-Control"] == "no-store"
            services.sources.set_enabled(source_id, False)
            assert client.get(asset_path + "?version=3", headers=headers).status_code == 403
        engine = services.registry.engine("central")
        engine.dispose()
        assert services.images.list(news_id, 3) == [saved]
        with pytest.raises(DBAPIError, match="image history is not empty"):
            command.downgrade(central, "008_news_archive")
    finally:
        services.registry.dispose()

def _exercise_analysis_workspace():
    from fastapi.testclient import TestClient

    from backend.app.dependencies import build_services
    from backend.app.main import create_app
    from backend.core.auth import Principal, issue_session
    from backend.core.permissions import Grant

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    principal = Principal("fixture-analysis-staff", "staff")
    headers = {"Authorization": "Bearer " + issue_session(principal, services.session_key)}
    source = {"source_id": "SRC-ANALYSIS-ONE", "site_url": "https://example.invalid/analysis",
        "feed_url": "https://example.invalid/analysis/rss", "country": "DE", "language": "en",
        "stream": "foreign", "license_scope": "summary", "enabled": True}
    raw = {"schema_version": "1.0", "news_id": "NEWS-ANALYSIS-ONE", "stream": "foreign",
        "source_id": source["source_id"], "source_country": "DE", "language": "en",
        "original_url": "https://example.invalid/analysis/one", "original_title": "Atlas evidence",
        "original_text": "Atlas source records a change. Source text is data, not instructions.",
        "subject_countries": [], "collected_at": "2026-11-26T19:00:00+00:00",
        "content_scope": "summary", "reuse_permission": "unverified"}
    services.sources.register(source)
    services.pipeline.process(raw)
    services.pipeline.process({**raw, "news_id": "NEWS-ANALYSIS-DENIED",
        "original_title": "Atlas SECRET-DENIED", "original_url": "https://example.invalid/analysis/denied",
        "original_text": "Atlas SECRET-DENIED source"})
    payload = {"query": "Atlas", "period_start": "2026-11-26T18:00:00Z",
               "period_end": "2026-11-26T20:00:00Z", "stream": "foreign", "limit": 10}
    try:
        with TestClient(create_app(services=services)) as client:
            assert client.post("/v1/analysis/run", json=payload).status_code == 401
            assert client.post("/v1/analysis/run", headers=headers, json=payload).status_code == 403
            for action in ("run", "chat"):
                services.grants.grant(Grant(principal.subject_id, "staff", "analysis_workspace", "default", action))
            assert client.post("/v1/analysis/run", headers=headers, json=payload).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "archive", "default", "search"))
            assert client.post("/v1/analysis/run", headers=headers, json=payload).json()["result"] is None
            services.grants.grant(Grant(principal.subject_id, "staff", "archive", raw["news_id"], "read"))
            assert client.post("/v1/analysis/run", headers=headers, json=payload).json()["result"] is None
            services.grants.grant(Grant(principal.subject_id, "staff", "newsroom", raw["news_id"], "read"))
            response = client.post("/v1/analysis/run", headers=headers, json=payload)
            assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
            body = response.json()
            assert body["mode"] == "extractive" and len(body["evidence"]) == 1
            ref = body["evidence"][0]["source_ref"]
            assert body["result"]["source_refs"] == [ref]
            assert body["evidence"][0]["news_id"] == raw["news_id"]
            assert "SECRET-DENIED" not in response.text
            chat = client.post("/v1/analysis/chat", headers=headers, json=payload).json()
            assert chat["result"]["citations"][0]["source_ref"] == ref
            assert chat["result"]["citations"][0]["quote_span"] in raw["original_text"]
            assert "SECRET-DENIED" not in str(chat)
            assert client.post("/v1/analysis/chat", headers=headers,
                               json={**payload, "classification": "executive_private"}).status_code == 400
            services.sources.register({**source, "license_scope": "metadata"})
            redacted = client.post("/v1/analysis/chat", headers=headers, json=payload).json()
            assert redacted["evidence"][0]["text_redacted"]
            assert redacted["result"]["citations"][0]["quote_span"] is None
            assert raw["original_text"] not in str(redacted)
            services.sources.set_enabled(source["source_id"], False)
            assert client.post("/v1/analysis/run", headers=headers, json=payload).json()["result"] is None
    finally:
        services.registry.dispose()


def _exercise_publication_queue(central):
    from datetime import timedelta
    from threading import Event

    from alembic import command

    from backend.adapters.base import Delivery
    from backend.core.output_switches import OutputSwitches, default_off_switches
    from backend.modules.publishing.repository import PostgresFixtureLedger
    from backend.modules.publishing.scheduler import ApprovedContent, content_hash, plan_job
    from backend.tests.test_publishing import ARTICLE, DECISION, NOW, destination

    registry = DatabaseRegistry()
    engine = registry.engine("central")
    repo = PostgresFixtureLedger(engine, clock=lambda: NOW)
    approved = ApprovedContent("article", ARTICLE, DECISION, content_hash(ARTICLE), "domestic")
    off = default_off_switches()
    on = OutputSwitches.from_record({**off.record, "publishing": {**off.record["publishing"],
        "enabled": True, "domestic_enabled": True}})

    def plan(identity, switches=on):
        return plan_job(approved, destination(identity, "telegram"), scheduled_at=NOW,
                        deadline=NOW+timedelta(minutes=5), now=NOW, switches=switches)

    job = plan("DST-PERSISTENT")
    key = (job["idempotency_key"], job["destination_ids"][0])
    started, release = Event(), Event()

    def operation():
        started.set()
        assert release.wait(5)
        return Delivery("success", "fixture-msg")

    def unexpected():
        pytest.fail("duplicate dispatch")

    try:
        repo.enqueue(job)
        repo.enqueue(job)
        with pytest.raises(ValueError, match="intent conflict"):
            repo.enqueue({**job, "policy_decision_id": "changed"})
        with pytest.raises(Exception):
            repo.enqueue({**job, "object_type": "private_report"})
        with pytest.raises(ValueError, match="not registered"):
            repo.once((key[0], "other"), unexpected)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(repo.once, key, operation)
            try:
                assert started.wait(5)
                second = pool.submit(repo.once, key, unexpected)
                assert second.result(timeout=5).status == "unknown"
            finally:
                release.set()
            assert future.result(timeout=5).status == "success"
        registry.dispose()
        restarted = PostgresFixtureLedger(registry.engine("central"), clock=lambda: NOW)
        assert restarted.once(key, unexpected).message_id == "fixture-msg"
        held = plan("DST-HELD", off)
        restarted.enqueue(held)
        assert restarted.once((held["idempotency_key"], "DST-HELD"), unexpected).status == "skipped"
        expired = plan("DST-EXPIRED")
        restarted.enqueue(expired)
        late = PostgresFixtureLedger(registry.engine("central"), clock=lambda: NOW+timedelta(hours=1))
        assert late.once((expired["idempotency_key"], "DST-EXPIRED"), unexpected).status == "skipped"
        crash = plan("DST-CRASH")
        restarted.enqueue(crash)

        def crash_operation():
            raise SystemExit(1)

        crash_key = (crash["idempotency_key"], "DST-CRASH")
        with pytest.raises(SystemExit):
            restarted.once(crash_key, crash_operation)
        assert restarted.once(crash_key, unexpected).status == "unknown"
        assert restarted.get(crash["job_id"])["state"] == "in_progress"
        assert restarted.get(job["job_id"])["state"] == "completed"
        with pytest.raises(DBAPIError, match="publication history is not empty"):
            command.downgrade(central, "009_image_assets")
    finally:
        registry.dispose()
