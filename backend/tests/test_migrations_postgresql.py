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
            assert len(central_tables) == 27
            assert ("ui", "saved_views") in central_tables
            assert {("orchestration", "outbox"), ("orchestration", "jobs")} <= central_tables
            assert {("insights", "public_report_drafts"), ("executive", "private_reports")} <= central_tables
            assert ("control", "ai_cost_reservations") in central_tables
            assert ("insights", "chat_memory") in central_tables
            assert ("editorial", "image_assets") in central_tables
            assert {("archive", "news_records"), ("archive", "artifact_index")} <= central_tables
            assert {("editorial", "articles"), ("editorial", "translations")} <= central_tables
            assert ("processing", "foreign_inbox") in central_tables
            assert {('sources', 'source_registry'), ('sources', 'source_history')} <= central_tables
            assert ("identity", "login_throttle") in central_tables
            assert ("processing", "artifact_history") in central_tables
            assert {schema for schema, _ in central_tables} == {
                "audit", "control", "identity", "processing", "sources", "editorial", "archive", "publish", "insights", "orchestration", "executive", "ui",
            }
            assert tables("domestic") == tables("foreign") == set()
            command.downgrade(central, "014_durable_workflow")
            with psycopg.connect(conninfo(databases["central"])) as legacy:
                legacy.execute("""
                    INSERT INTO insights.executive_reports (subject_id,report_id,slot,payload,evidence)
                    VALUES ('fixture-legacy','fixture-legacy',5,
                        '{"visibility":"executive_private","report_id":"fixture-legacy",
                          "publishable":false,"owner_role":"editor_in_chief"}'::jsonb,'[]'::jsonb)
                """)
            with pytest.raises(DBAPIError, match="legacy private reports require encrypted migration"):
                command.upgrade(central, "head")
            with psycopg.connect(conninfo(databases["central"])) as legacy:
                assert legacy.execute("SELECT count(*) FROM insights.executive_reports").fetchone()[0] == 1
                legacy.execute("DELETE FROM insights.executive_reports WHERE subject_id='fixture-legacy'")
            command.upgrade(central, "head")
            assert tables("central") == central_tables
            command.downgrade(central, "001_baseline")
            assert tables("central") == {
                row for row in central_tables
                if row[0] not in {"processing", "sources", "editorial", "archive", "publish", "insights", "orchestration", "executive", "ui"} and row not in {("identity", "login_throttle"), ("control", "ai_cost_reservations")}
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
            _exercise_durable_settings()
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
            _exercise_analysis_workspace(central)
            _exercise_publication_queue(central)
            _exercise_ai_cost_ledger()
            _exercise_workflow_handoff(central)
            _exercise_role_dashboards(central)
            assert tables("foreign") == foreign_tables
            assert tables("central") == central_tables
            assert tables("domestic") == domestic_tables
        finally:
            for database in reversed(created):
                control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def _exercise_role_dashboards(central):
    from alembic import command
    from fastapi.testclient import TestClient

    from backend.app.dependencies import build_services
    from backend.app.main import create_app
    from backend.core.auth import Principal, issue_session
    from backend.core.permissions import Grant
    from backend.modules.ui.saved_views import SavedViews

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    source = {"source_id": "SRC-DASHBOARD-TEST", "site_url": "https://example.invalid/dash",
        "feed_url": "https://example.invalid/dash/rss", "country": "IR", "language": "fa",
        "stream": "domestic", "license_scope": "summary", "enabled": True}
    raw = {"schema_version": "1.0", "news_id": "NEWS-DASHBOARD-ECONOMY", "stream": "domestic",
        "source_id": source["source_id"], "original_url": "https://example.invalid/dash/1",
        "original_title": "بورس آزمایشی", "original_text": "بورس و تورم", "language": "fa",
        "subject_countries": [], "collected_at": "2026-11-27T00:00:00Z",
        "content_scope": "summary", "reuse_permission": "unverified"}
    staff = Principal("fixture-dash-staff", "staff")
    other = Principal("fixture-dash-other", "staff")
    eic = Principal("fixture-dash-eic", "editor_in_chief")
    owner = Principal("fixture-dash-owner", "owner")

    def header(p):
        return {"Authorization": "Bearer " + issue_session(p, services.session_key)}

    def grant(p, kind, key, action):
        services.grants.grant(Grant(p.subject_id, p.role, kind, key, action))

    try:
        services.sources.register(source)
        services.pipeline.process(raw)
        sports = {**raw, "news_id": "NEWS-DASHBOARD-SPORTS", "original_title": "فوتبال آزمایشی",
                   "original_text": "لیگ فوتبال", "original_url": "https://example.invalid/dash/2"}
        services.pipeline.process(sports)
        grant(staff, "desk", "economy", "read")
        grant(other, "desk", "economy", "read")
        for p in (staff, eic):
            grant(p, "newsroom", "feed", "read")
            for item in (raw, sports):
                grant(p, "newsroom", item["news_id"], "read")
                grant(p, "editorial_workspace", item["news_id"], "read")
        with TestClient(create_app(services=services)) as client:
            ui = "/v1/ui"
            assert client.get(ui + "/session").status_code == 401
            for p in (staff, eic, owner):
                assert client.get(ui + "/session", headers=header(p)).json()["role"] == p.role
            for p, forbidden in ((staff, "owner-dashboard"), (staff, "editor-dashboard"),
                                 (eic, "owner-dashboard"), (eic, "staff-dashboard"),
                                 (owner, "editor-dashboard"), (owner, "staff-dashboard")):
                assert client.get(ui + "/" + forbidden, headers=header(p)).status_code == 403
            staff_page = client.get(ui + "/staff-dashboard", headers=header(staff))
            assert staff_page.status_code == 200 and staff_page.headers["Cache-Control"] == "no-store"
            body = staff_page.json()
            assert body["desks"] == ["economy"]
            assert [x["news_id"] for x in body["items"]] == [raw["news_id"]]
            assert [x["news_id"] for x in body["tasks"]] == [raw["news_id"]]
            assert "NEWS-DASHBOARD-SPORTS" not in staff_page.text
            assert "metrics_snapshot" not in body and "operations" not in body and "private_reports" not in body
            assert client.get(ui + "/desk/sports", headers=header(staff)).status_code == 403
            assert client.get(ui + "/desk/economy?stream=foreign", headers=header(staff)).json()["items"] == []
            assert [x["news_id"] for x in client.get("/v1/news", headers=header(staff)).json()["items"]] == [raw["news_id"]]
            executive = client.get(ui + "/editor-dashboard", headers=header(eic)).json()
            assert {x["news_id"] for x in executive["items"]} == {raw["news_id"], sports["news_id"]}
            assert executive["metrics_snapshot"] is None
            assert client.get(ui + "/owner-dashboard", headers=header(owner)).json()["operations"] == {}
            for kind, key, action in (("operations","default","read"), ("source_registry","default","read"),
                                       ("config","output_controls:global:controls-main","read"),
                                       ("ai_cost","default","read"), ("audit","default","read")):
                grant(owner, kind, key, action)
            ops = client.get(ui + "/owner-dashboard", headers=header(owner)).json()["operations"]
            assert ops["health"] == {"central":"ok","domestic":"ok","foreign":"ok"}
            assert ops["controls"]["can_update"] is False
            assert ops["ai_cost"]["committed_usd"] >= 0
            assert ops["sources"]["registered"] >= 1
            assert "private_report" not in str(ops)
            saved_path = ui + "/saved-views"
            form = {"name":"آزمایش", "desk":"economy", "stream":"domestic"}
            assert client.put(saved_path, json={**form, "desk":"sports"}, headers=header(staff)).status_code == 403
            saved = client.put(saved_path, json=form, headers=header(staff))
            assert saved.status_code == 200, saved.text
            row = saved.json()["saved"]
            assert client.get(saved_path, headers=header(other)).json()["items"] == []
            assert client.delete(saved_path+"/"+row["view_id"], headers=header(other)).status_code == 404
            second = DatabaseRegistry()
            try:
                assert SavedViews(second.engine("central")).list(staff) == [row]
            finally:
                second.dispose()
            replay = client.put(saved_path, json={**form, "stream":"all"}, headers=header(staff)).json()["saved"]
            assert replay["view_id"] == row["view_id"]
            services.grants.revoke(Grant(staff.subject_id, staff.role, "desk", "economy", "read"))
            assert client.get(saved_path, headers=header(staff)).json()["items"] == []
            assert client.get(ui + "/desk/economy", headers=header(staff)).status_code == 403
            assert client.get("/v1/news", headers=header(staff)).json()["items"] == []
            with pytest.raises(DBAPIError, match="saved views are not empty"):
                command.downgrade(central, "015_executive_storage")
            assert client.delete(saved_path+"/"+row["view_id"], headers=header(staff)).status_code == 200
    finally:
        services.registry.dispose()


def _clear_workflow_test_rows(registry):
    """Only for this disposable test DB: isolate older downgrade guard checks."""
    with registry.engine("central").begin() as conn:
        conn.execute(text("DELETE FROM orchestration.jobs"))
        conn.execute(text("DELETE FROM orchestration.outbox"))


def _exercise_workflow_handoff(central):
    from alembic import command

    from backend.app.dependencies import build_services
    from backend.core.events import ExecutivePrivateEventRoutingError, make_event
    from backend.core.jobs import InvalidTransition, PostgresWorkflowJobs
    from backend.core.orchestrator import ProcessingHandoff
    from backend.core.outbox import PostgresOutbox
    from backend.modules.processing.router import route

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    registry = services.registry
    engine = registry.engine("central")
    second = DatabaseRegistry()
    outbox = PostgresOutbox(engine)
    handoff = ProcessingHandoff(outbox)
    repo = PostgresProcessingRepository(registry, on_revision=handoff.record)
    assert services.workflow_jobs is not None
    raw = {"schema_version": "1.0", "news_id": "NEWS-WORKFLOW-TEST",
        "stream": "domestic", "source_id": "SRC-WORKFLOW-TEST",
        "original_url": "https://example.invalid/workflow/1", "original_title": "Fixture",
        "original_text": "Synthetic workflow content", "language": "fa",
        "subject_countries": [], "collected_at": "2026-11-26T19:00:00Z",
        "content_scope": "summary", "reuse_permission": "unverified"}

    def arguments(record):
        normalized = normalize(record)
        cluster = Clusterer().cluster(normalized)
        classified = classify(normalized, event_id=cluster["event_id"])
        return dict(news=normalized, cluster=cluster, classification=classified,
                    routing=route(classified).as_dict())

    try:
        _clear_workflow_test_rows(registry)
        args = arguments(raw)

        def rollback(conn, classified, routing, revision, digest):
            handoff.record(conn, classified, routing, revision, digest)
            raise RuntimeError("fixture producer rollback")

        failing = PostgresProcessingRepository(registry, on_revision=rollback)
        with pytest.raises(RuntimeError, match="fixture producer rollback"):
            failing.save(**args)
        assert repo.get(raw["news_id"]) is None
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM orchestration.outbox")).scalar_one() == 0
        # Simulate the process stopping between producer commit and queue handoff.
        repo.save(**args)
        repo.save(**args)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM orchestration.outbox WHERE published_at IS NULL")).scalar_one() == 1
        assert services.workflow_jobs.counts() == {}
        restarted_outbox = PostgresOutbox(second.engine("central"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(lambda _: restarted_outbox.dispatch_to_jobs(), range(2))) == 1
        assert restarted_outbox.dispatch_to_jobs() == 0
        queue = PostgresWorkflowJobs(second.engine("central"))
        assert queue.counts() == {"queued": 1}
        assert queue.claim(project="P12", stream="foreign") is None
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: queue.claim(project="P13", stream="domestic"), range(2)))
        assert sum(claim is not None for claim in claims) == 1
        claim = next(claim for claim in claims if claim is not None)
        assert claim["payload_ref"] == "processing/history/NEWS-WORKFLOW-TEST/1"
        assert repo.get_revision(raw["news_id"], 1).news == args["news"]
        with pytest.raises(InvalidTransition, match="stale"):
            queue.finish(job_id=claim["job_id"], lease_token="wrong", success=True)
        with second.engine("central").begin() as conn:
            conn.execute(text("UPDATE orchestration.jobs SET lease_until=now()-interval '1 second' WHERE job_id=:id"),
                         {"id": claim["job_id"]})
        assert queue.claim(project="P13", stream="domestic") is None
        assert queue.get(claim["job_id"])["last_error"] == "lease_expired"
        with pytest.raises(InvalidTransition):
            queue.retry(job_id=claim["job_id"], revalidate=lambda _: False)
        queue.retry(job_id=claim["job_id"], revalidate=lambda _: True)
        queue.hold(job_id=claim["job_id"])
        assert queue.claim(project="P13", stream="domestic") is None
        queue.retry(job_id=claim["job_id"], revalidate=lambda _: True)
        retry = queue.claim(project="P13", stream="domestic")
        assert retry["attempts"] == 2 and retry["lease_token"] != claim["lease_token"]
        queue.finish(job_id=retry["job_id"], lease_token=retry["lease_token"], success=False)
        queue.retry(job_id=retry["job_id"], revalidate=lambda _: True)
        last = queue.claim(project="P13", stream="domestic")
        queue.finish(job_id=last["job_id"], lease_token=last["lease_token"], success=False)
        with pytest.raises(InvalidTransition):
            queue.retry(job_id=last["job_id"], revalidate=lambda _: True)
        assert queue.get(last["job_id"])["attempts"] == 3
        with engine.begin() as conn:
            private = make_event(event_type="news.classified", producer_project="P09",
                object_id="PRIVATE-TEST", correlation_id=str(uuid4()), payload_ref="private/fixture",
                classification="executive_private")
            with pytest.raises(ExecutivePrivateEventRoutingError):
                outbox.append_in_transaction(conn, private, stream="domestic", project="P13")
        # Actual application pipeline uses the same handoff without test injection.
        foreign = {**raw, "news_id": "NEWS-WORKFLOW-FOREIGN", "stream": "foreign",
                   "original_url": "https://example.invalid/workflow/2", "language": "en"}
        services.pipeline.process(foreign)
        assert queue.counts() == {"failed": 1, "queued": 1}
        fjob = queue.claim(project="P12", stream="foreign")
        assert fjob is not None and fjob["payload_ref"] == "processing/history/NEWS-WORKFLOW-FOREIGN/1"
        queue.finish(job_id=fjob["job_id"], lease_token=fjob["lease_token"], success=True)
        assert queue.counts() == {"failed": 1, "done": 1}
        with pytest.raises(DBAPIError, match="workflow history is not empty"):
            command.downgrade(central, "013_daily_reports")
    finally:
        services.registry.dispose()
        second.dispose()


def _exercise_durable_settings():
    import json

    from backend.core.output_switches import default_off_switches
    from backend.core.settings_service import PostgresRevisionStore, RevisionConflict

    registry = DatabaseRegistry()
    second = DatabaseRegistry()
    store = PostgresRevisionStore(registry)
    actor = "fixture-settings-owner"
    examples = BACKEND.parent / "source" / "contracts" / "examples"
    configs = [
        ("output_controls", "controls-main", "global", {
            **default_off_switches().record, "updated_by": actor,
        }),
        ("schedule_config", "SCH-FOREIGN", "foreign", json.loads(
            (examples / "schedule_config.example.json").read_text())),
        ("ai_models_config", "MODELS-01", "global", json.loads(
            (examples / "ai_models_config.example.json").read_text())),
        ("news_framework", "FW-01", "foreign", json.loads(
            (examples / "news_framework.example.json").read_text())),
    ]
    try:
        for contract, identity, scope, value in configs:
            record = store.compare_and_append(contract_name=contract, config_id=identity,
                scope=scope, value=value, actor_id=actor, expected_revision=0)
            assert record.revision == 1
        registry.dispose()
        restarted = PostgresRevisionStore(second)
        for contract, identity, scope, value in configs:
            record = restarted.get(contract, identity, scope)
            assert record is not None and record.value == value and record.updated_by == actor
        assert len([a for a in restarted.audit_entries() if a.actor_id == actor]) == 4
        controls = configs[0][3]
        with pytest.raises(RevisionConflict):
            restarted.compare_and_append(contract_name="output_controls", config_id="controls-main",
                scope="global", value=controls, actor_id=actor, expected_revision=0)
        changed = {**controls, "revision": 2, "publishing": {**controls["publishing"], "enabled": True}}
        def update():
            try:
                restarted.compare_and_append(contract_name="output_controls", config_id="controls-main",
                    scope="global", value=changed, actor_id=actor, expected_revision=1)
                return True
            except RevisionConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: update(), range(2))) == [False, True]
        assert restarted.get("output_controls", "controls-main", "global").value == changed
        # The integration suite uses a default-off runtime after this exercise.
        restarted.compare_and_append(contract_name="output_controls", config_id="controls-main",
            scope="global", value={**controls, "revision": 3}, actor_id=actor, expected_revision=2)
        with pytest.raises(RevisionConflict):
            restarted.compare_and_append(contract_name="output_controls", config_id="MODELS-01",
                scope="global", value=controls, actor_id=actor, expected_revision=0)
        assert restarted.get("output_controls", "MODELS-01", "global") is None
        invalid = deepcopy(configs[2][3])
        invalid["tasks"][0]["credential_ref"] = "not-a-reference"
        with pytest.raises(ValueError):
            restarted.compare_and_append(contract_name="ai_models_config", config_id="MODELS-01",
                scope="global", value=invalid, actor_id=actor, expected_revision=1)
        # Clear only synthetic settings from the disposable integration database.
        with second.engine("central").begin() as conn:
            conn.execute(text("DELETE FROM control.config_revisions WHERE changed_by=:actor"), {"actor": actor})
    finally:
        registry.dispose()
        second.dispose()


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
        _clear_workflow_test_rows(services.registry)
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
            assert client.get("/v1/news", headers=headers).json()["items"] == []
            for item in services.pipeline.feed(limit=200):
                if item["source_id"] == source["source_id"]:
                    services.grants.grant(Grant("fixture-staff", "staff", "newsroom", item["news_id"], "read"))
                    for desk in item["desks"]:
                        services.grants.grant(Grant("fixture-staff", "staff", "desk", desk, "read"))
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
            from backend.app.workflow_worker import WorkflowWorker

            # The worker must consume actual outbox jobs, not an injected fake queue.
            # Isolate this worker run from earlier synthetic processing fixtures.
            _clear_workflow_test_rows(services.registry)
            worker = WorkflowWorker(services, lambda: principal)
            new_raw = {**raw, "news_id": "NEWS-WORKER-PG", "original_url": "https://example.invalid/editorial/worker"}
            denied_raw = {**raw, "news_id": "NEWS-WORKER-DENIED", "original_url": "https://example.invalid/editorial/denied"}
            services.pipeline.process(denied_raw)
            assert worker.run_one(project="P12", stream="foreign") == "held"
            assert services.editorial.get(denied_raw["news_id"]) is None
            assert len(provider.calls) == 2
            services.pipeline.process(new_raw)
            for resource, action in (("newsroom", "read"), ("editorial_workspace", "write"),
                                     ("editorial_workspace", "generate")):
                services.grants.grant(Grant(principal.subject_id, "staff", resource, new_raw["news_id"], action))
            assert worker.run_one(project="P13", stream="domestic") == "empty"
            assert worker.run_one(project="P12", stream="foreign") == "done"
            worker_saved = services.editorial.get(new_raw["news_id"])
            assert worker_saved["version"] == 1
            assert worker_saved["translation"]["model_id"] == "fixture/fixture-translation"
            assert worker_saved["quality"]["generation"]["mode"] == "fixture"
            assert worker_saved["policy"]["decision"] == "human_review"
            assert services.workflow_jobs.counts() == {"held": 1, "done": 1}
            assert worker.run_one(project="P12", stream="foreign") == "empty"
            assert len(provider.calls) == 4
            # Revocation between translation and editorial must prevent the next call.
            revoked_raw = {**raw, "news_id": "NEWS-WORKER-REVOKED", "original_url": "https://example.invalid/editorial/revoked"}
            services.pipeline.process(revoked_raw)
            for resource, action in (("newsroom", "read"), ("editorial_workspace", "write"),
                                     ("editorial_workspace", "generate")):
                services.grants.grant(Grant(principal.subject_id, "staff", resource, revoked_raw["news_id"], action))
            original_complete = provider.complete

            def revoke_after_translation(prompt, *, model, timeout_s):
                result = original_complete(prompt, model=model, timeout_s=timeout_s)
                services.grants.revoke(Grant(principal.subject_id, "staff", "editorial_workspace",
                                             revoked_raw["news_id"], "generate"))
                return result

            provider.complete = revoke_after_translation
            assert worker.run_one(project="P12", stream="foreign") == "failed"
            assert provider.calls[-1] == "fixture-translation" and len(provider.calls) == 5
            assert services.editorial.get(revoked_raw["news_id"]) is None
            assert services.workflow_jobs.counts() == {"held": 1, "done": 1, "failed": 1}
            provider.complete = original_complete
            assert client.get(path, headers=headers).json()["saved"] == generated
            assert client.get(path + "?version=1", headers=headers).json()["saved"] == saved
            services.sources.set_enabled(source["source_id"], False)
            assert client.get(path, headers=headers).status_code == 403
            assert client.put(path, headers={**h, "Idempotency-Key": "revoked"}, json=updated).status_code == 403
            assert client.post(generation_path, headers=gh, json=generate).status_code == 403
            assert len(provider.calls) == 5
        engine = services.registry.engine("central")
        engine.dispose()
        reopened = EditorialRepository(engine)
        assert reopened.get(news_id)["version"] == 3
        assert reopened.get(news_id, 1) == saved
        _clear_workflow_test_rows(services.registry)
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
        _clear_workflow_test_rows(services.registry)
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
        _clear_workflow_test_rows(services.registry)
        with pytest.raises(DBAPIError, match="image history is not empty"):
            command.downgrade(central, "008_news_archive")
    finally:
        services.registry.dispose()

def _private_report_fixture(services):
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from sqlalchemy import create_engine

    from backend.modules.intelligence.report_repository import ReportRepository
    from backend.modules.intelligence.repository import build_executive_store

    admin = make_url(os.environ["ETEMAD_DB_CENTRAL_URL"])
    suffix = uuid4().hex[:12]
    public_role, private_role = f"fixture_public_{suffix}", f"fixture_private_{suffix}"
    password = "fixture-only-isolated-report-test"
    admin_url = admin.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_url) as conn:
        for role in (public_role, private_role):
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA insights TO {}").format(sql.Identifier(public_role)))
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON insights.public_report_drafts TO {}").format(
            sql.Identifier(public_role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA executive TO {}").format(sql.Identifier(private_role)))
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON executive.private_reports TO {}").format(
            sql.Identifier(private_role)))
    public_url = admin.set(username=public_role, password=password)
    private_url = admin.set(username=private_role, password=password)
    public = create_engine(public_url)
    private = create_engine(private_url)
    root = services.objects.root.parent / "executive-fixture"
    encoded = base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()
    env = {"ETEMAD_EXECUTIVE_DB_URL": private_url.render_as_string(hide_password=False),
           "ETEMAD_EXECUTIVE_OBJECT_ROOT": str(root), "ETEMAD_SECRET_EXECUTIVE_REPORTS_KEY": encoded}
    store = build_executive_store(public, services.objects, env)
    assert store is not None
    services.reports = ReportRepository(public, services.grants, private=store)

    # Real application composition must activate private storage only with
    # a distinct, least-privileged connection and disjoint encrypted root.
    from backend.app.dependencies import build_services

    settings = {**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key",
        "ETEMAD_DB_CENTRAL_URL": public_url.render_as_string(hide_password=False)}
    configured = build_services(environ={**settings, **env})
    assert configured.reports is not None and configured.reports.private_available
    configured.reports.close()
    configured.registry.dispose()
    invalid = build_services(environ={**settings, **env, "ETEMAD_SECRET_EXECUTIVE_REPORTS_KEY": "invalid"})
    assert invalid.reports is not None and not invalid.reports.private_available
    invalid.registry.dispose()

    def cleanup():
        store.close()
        public.dispose()
        private.dispose()
        with psycopg.connect(admin_url) as conn:
            for role in (public_role, private_role):
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    return store, public, private, root, env, cleanup


def _exercise_analysis_workspace(central):
    from fastapi.testclient import TestClient

    from backend.app.dependencies import build_services
    from backend.app.main import create_app
    from backend.core.auth import Principal, issue_session
    from backend.core.permissions import Grant

    services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
    private_cleanup = None
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
            memory_path = "/v1/analysis/chat/memory"
            assert client.get(memory_path).status_code == 401
            assert client.get(memory_path, headers=headers).json()["memory"] is None
            memory_input = {"topic": "Atlas", "days": 7, "stream": "foreign"}
            assert client.put(memory_path, headers=headers,
                              json={**memory_input, "subject_id": "another"}).status_code == 400
            saved = client.put(memory_path, headers=headers, json=memory_input)
            assert saved.status_code == 200 and saved.headers["Cache-Control"] == "no-store"
            assert saved.json()["memory"]["topic"] == "Atlas"
            from backend.modules.intelligence.chat_memory import PostgresChatMemory

            restarted = DatabaseRegistry()
            try:
                restored = PostgresChatMemory(restarted.engine("central")).get(principal)
                assert restored is not None and restored.topic == "Atlas"
                assert PostgresChatMemory(restarted.engine("central")).get(
                    Principal(principal.subject_id, "editor_in_chief")) is None
            finally:
                restarted.dispose()
            another = Principal("fixture-memory-other", "staff")
            other_headers = {"Authorization": "Bearer " + issue_session(another, services.session_key)}
            assert client.get(memory_path, headers=other_headers).status_code == 403
            services.grants.grant(Grant(another.subject_id, "staff", "analysis_workspace", "default", "chat"))
            services.grants.grant(Grant(another.subject_id, "staff", "archive", "default", "search"))
            assert client.get(memory_path, headers=other_headers).json()["memory"] is None
            assert client.delete(memory_path, headers=other_headers).status_code == 200
            assert client.get(memory_path, headers=headers).json()["memory"]["topic"] == "Atlas"
            chat_grant = Grant(principal.subject_id, "staff", "analysis_workspace", "default", "chat")
            services.grants.revoke(chat_grant)
            assert client.get(memory_path, headers=headers).status_code == 403
            assert client.delete(memory_path, headers=headers).status_code == 200
            services.grants.grant(chat_grant)
            assert client.get(memory_path, headers=headers).json()["memory"] is None
            from backend.core.output_switches import OutputSwitches, default_off_switches

            report_path = "/v1/analysis/reports/2"
            assert client.post(report_path, headers=headers, json=payload).status_code == 403
            services.grants.grant(Grant(principal.subject_id, "staff", "analysis_workspace", "default", "report"))
            assert client.post(report_path, headers=headers, json=payload).status_code == 422
            off = default_off_switches()
            on = OutputSwitches.from_record({**off.record, "public_analysis": {"enabled": True}})
            services.switches = lambda: on
            report_response = client.post(report_path, headers=headers, json=payload)
            assert report_response.status_code == 200 and report_response.headers["Cache-Control"] == "no-store"
            report = report_response.json()
            assert report["mode"] == "bounded_draft" and report["bounded"]
            assert report["result"]["source_refs"] == [ref]
            assert report["result"]["generation_status"] == "draft"
            assert report["result"]["visibility"] == "public"
            assert "SECRET-DENIED" not in report_response.text
            assert client.post("/v1/analysis/reports/5", headers=headers, json=payload).status_code == 404
            assert client.post("/v1/analysis/reports/1", headers=headers, json=payload).status_code == 400
            daily = "/v1/analysis/daily-reports"
            daily_input = {k: payload[k] for k in ("period_start", "period_end")}
            assert [v["slot"] for v in client.get(daily, headers=headers).json()["slots"]] == [1, 2, 3, 4]
            domestic_source = {**source, "source_id": "SRC-ANALYSIS-DOMESTIC",
                               "stream": "domestic", "country": "IR"}
            services.sources.register(domestic_source)
            domestic = {**raw, "news_id": "NEWS-ANALYSIS-DOMESTIC", "source_id": domestic_source["source_id"],
                        "stream": "domestic", "source_country": "IR", "language": "fa",
                        "original_title": "خبر آزمایشی داخلی",
                        "original_url": "https://example.invalid/analysis/domestic"}
            services.pipeline.process(domestic)
            for resource in ("archive", "newsroom"):
                services.grants.grant(Grant(principal.subject_id, "staff", resource, domestic["news_id"], "read"))
            for slot in (1, 2, 3, 4):
                created = client.post(f"{daily}/{slot}", headers=headers, json=daily_input)
                assert created.status_code == 200 and created.json()["saved"]["payload"]["visibility"] == "public"
                latest = client.get(f"{daily}/{slot}", headers=headers)
                assert latest.json()["saved"] == created.json()["saved"]
                assert "SECRET-DENIED" not in latest.text
            assert client.get(daily + "/5", headers=headers).status_code == 404
            eic = Principal("fixture-daily-eic", "editor_in_chief")
            eic_headers = {"Authorization": "Bearer " + issue_session(eic, services.session_key)}
            services.grants.grant(Grant(eic.subject_id, eic.role, "analysis_workspace", "default", "report"))
            services.grants.grant(Grant(eic.subject_id, eic.role, "archive", "default", "search"))
            for item in (raw, domestic):
                for resource in ("archive", "newsroom"):
                    services.grants.grant(Grant(eic.subject_id, eic.role, resource, item["news_id"], "read"))
            for slot in (5, 6, 7):
                services.grants.grant(Grant(eic.subject_id, eic.role, "private_report", f"slot-{slot}", "read"))
            services.switches = lambda: off
            assert client.get(daily + "/5", headers=eic_headers).status_code == 503
            assert client.post(daily + "/5", headers=eic_headers, json=daily_input).status_code == 503
            assert not any(v["generation_enabled"] for v in client.get(daily, headers=eic_headers).json()["slots"])
            store, public_engine, private_engine, private_root, private_env, private_cleanup = _private_report_fixture(services)
            for slot in (5, 6, 7):
                created = client.post(f"{daily}/{slot}", headers=eic_headers, json=daily_input)
                assert created.status_code == 200 and created.json()["saved"]["payload"]["publishable"] is False
                assert client.get(f"{daily}/{slot}", headers=eic_headers).json()["saved"] == created.json()["saved"]
                assert client.get(f"{daily}/{slot}", headers=headers).status_code == 404
            import base64

            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            from backend.modules.intelligence.report_repository import ReportRepository
            from backend.modules.intelligence.repository import (
                ExecutiveStorageUnavailable,
                build_executive_store,
            )

            with pytest.raises(DBAPIError):
                with public_engine.connect() as conn:
                    conn.execute(text("SELECT * FROM executive.private_reports"))
            with pytest.raises(DBAPIError):
                with private_engine.connect() as conn:
                    conn.execute(text("SELECT * FROM insights.public_report_drafts"))
            with pytest.raises(DBAPIError):
                with private_engine.begin() as conn:
                    conn.execute(text("DELETE FROM executive.private_reports"))
            with pytest.raises(ExecutiveStorageUnavailable):
                build_executive_store(public_engine, services.objects, {
                    **private_env, "ETEMAD_EXECUTIVE_OBJECT_ROOT": str(services.objects.root)})
            with pytest.raises(ExecutiveStorageUnavailable):
                build_executive_store(public_engine, services.objects, {
                    **private_env, "ETEMAD_EXECUTIVE_DB_URL": public_engine.url.render_as_string(hide_password=False)})
            with private_engine.connect() as conn:
                rows = conn.execute(text("SELECT content_ref FROM executive.private_reports")).scalars().all()
                assert len(rows) == 3
            assert not any(p.is_file() for p in services.objects.root.glob("reports/*.bin"))
            for ref in rows:
                data = (private_root / ref).read_bytes()
                assert b"Atlas" not in data and b"publishable" not in data and data.startswith(b"ETEMAD-P19-v1")
            # Verify wrong-key and changed-object rejection, and restart access.
            wrong = build_executive_store(public_engine, services.objects, {
                **private_env, "ETEMAD_SECRET_EXECUTIVE_REPORTS_KEY": base64.b64encode(
                    AESGCM.generate_key(bit_length=256)).decode()})
            assert wrong is not None
            try:
                with pytest.raises(ValueError, match="integrity"):
                    wrong.latest(eic.subject_id, 5)
            finally:
                wrong.close()
            with private_engine.connect() as conn:
                target = conn.execute(text("SELECT content_ref FROM executive.private_reports WHERE slot=5")).scalar_one()
            file = private_root / target
            original = file.read_bytes()
            file.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            assert client.get(daily + "/5", headers=eic_headers).status_code == 422
            file.write_bytes(original)
            reopened = build_executive_store(public_engine, services.objects, private_env)
            assert reopened is not None
            try:
                assert reopened.latest(eic.subject_id, 5) == store.latest(eic.subject_id, 5)
                other = Principal("fixture-other-eic", "editor_in_chief")
                services.grants.grant(Grant(other.subject_id, other.role, "private_report", "slot-5", "read"))
                assert ReportRepository(public_engine, services.grants, private=reopened).latest(other, 5) is None
            finally:
                reopened.close()
            services.grants.revoke(Grant(eic.subject_id, eic.role, "private_report", "slot-5", "read"))
            assert client.get(daily + "/5", headers=eic_headers).status_code == 404
            from alembic import command
            _clear_workflow_test_rows(services.registry)
            with pytest.raises(DBAPIError, match="report history is not empty"):
                command.downgrade(central, "012_ai_cost_ledger")
            # Remove only this disposable test's report fixtures so the
            # independent older publication downgrade check can run.
            with services.registry.engine("central").begin() as conn:
                conn.execute(text("DELETE FROM insights.public_report_drafts WHERE subject_id=:id"),
                             {"id": principal.subject_id})
                conn.execute(text("DELETE FROM executive.private_reports WHERE subject_id=:id"),
                             {"id": eic.subject_id})
            services.switches = lambda: off
            assert client.post(report_path, headers=headers, json=payload).status_code == 422
            services.sources.register({**source, "license_scope": "metadata"})
            redacted = client.post("/v1/analysis/chat", headers=headers, json=payload).json()
            assert redacted["evidence"][0]["text_redacted"]
            assert redacted["result"]["citations"][0]["quote_span"] is None
            assert raw["original_text"] not in str(redacted)
            services.sources.set_enabled(source["source_id"], False)
            assert client.post("/v1/analysis/run", headers=headers, json=payload).json()["result"] is None
    finally:
        if private_cleanup is not None:
            private_cleanup()
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
        manual = plan("DST-MANUAL-HOLD")
        restarted.enqueue(manual)
        assert restarted.hold(manual["job_id"]) == "held"
        assert restarted.hold(manual["job_id"]) == "held"
        assert restarted.once((manual["idempotency_key"], "DST-MANUAL-HOLD"), unexpected).status == "skipped"
        assert restarted.hold("unknown") is None
        with pytest.raises(ValueError, match="already progressed"):
            restarted.hold(job["job_id"])
        overview = restarted.overview(limit=100)
        by_id = {entry["job_id"]: entry for entry in overview["items"]}
        assert by_id[manual["job_id"]]["state"] == "held"
        assert by_id[job["job_id"]]["attempt"]["message_id"] == "fixture-msg"
        assert "idempotency_key" not in by_id[job["job_id"]]

        from fastapi.testclient import TestClient

        from backend.app.dependencies import build_services
        from backend.app.main import create_app
        from backend.core.auth import Principal, issue_session
        from backend.core.permissions import Grant

        services = build_services(environ={**os.environ, "ETEMAD_NEWS_STORAGE": "postgres",
            "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
            "ETEMAD_SOURCE_STORAGE": "postgres", "ETEMAD_SESSION_KEY": "fixture-only-session-key"})
        principal = Principal("fixture-publication-operator", "staff")
        headers = {"Authorization": "Bearer " + issue_session(principal, services.session_key)}
        pending = plan("DST-API-HOLD")
        restarted.enqueue(pending)
        path = "/v1/publishing/queue"
        hold_path = path + "/" + pending["job_id"] + "/hold"
        try:
            with TestClient(create_app(services=services)) as client:
                assert client.get(path).status_code == 401
                assert client.get(path, headers=headers).status_code == 403
                services.grants.grant(Grant(principal.subject_id, principal.role,
                                            "publication_workspace", "default", "read"))
                data = client.get(path, headers=headers)
                assert data.status_code == 200 and data.headers["Cache-Control"] == "no-store"
                assert data.json()["live_dispatch_available"] is False
                assert data.json()["publishing_enabled"] is False
                assert client.post(hold_path, headers=headers).status_code == 403
                services.grants.grant(Grant(principal.subject_id, principal.role,
                                            "publication_workspace", pending["job_id"], "hold"))
                assert client.post(hold_path, headers=headers).json()["state"] == "held"
                assert restarted.get(pending["job_id"])["state"] == "held"
        finally:
            services.registry.dispose()
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
        _clear_workflow_test_rows(registry)
        with pytest.raises(DBAPIError, match="publication history is not empty"):
            command.downgrade(central, "009_image_assets")
    finally:
        registry.dispose()



def _exercise_ai_cost_ledger():
    from backend.core.ai_cost_ledger import PostgresCostLedger
    from backend.core.ai_gateway import BudgetExceeded, FakeProvider, Gateway, ModelBinding

    registry = DatabaseRegistry()
    other_registry = DatabaseRegistry()
    try:
        ledger = PostgresCostLedger(registry.engine("central"))
        other = PostgresCostLedger(other_registry.engine("central"))
        binding = ModelBinding("translation", "fake", "fixture", run_budget_usd=.01, daily_budget_usd=.004)
        calls = []

        class RecordingProvider(FakeProvider):
            def complete(self, prompt, *, model, timeout_s):
                calls.append(1)
                return super().complete(prompt, model=model, timeout_s=timeout_s)

        provider = RecordingProvider(cost_per_call=.002)
        first = Gateway({"translation": binding}, {"fake": provider}, enabled=True,
                        monthly_budget_usd=.006, cost_ledger=ledger)
        restarted = Gateway({"translation": binding}, {"fake": provider}, enabled=True,
                            monthly_budget_usd=.006, cost_ledger=other)
        result = first.call("translation", "fixture prompt")
        assert result.cost_usd == .002
        assert restarted.spent_usd == pytest.approx(.002)
        pending = str(uuid4())
        other.reserve(pending, binding, .002, monthly_budget_usd=.006)
        with pytest.raises(BudgetExceeded):
            restarted.call("translation", "must not dispatch")
        assert calls == [1]
        other.settle(pending, "released")
        assert restarted.call("translation", "second").cost_usd == .002
        task2 = ModelBinding("editorial", "fake", "fixture", run_budget_usd=.01, daily_budget_usd=.01)
        candidates = [str(uuid4()), str(uuid4())]

        def attempt(request_id):
            try:
                PostgresCostLedger(registry.engine("central")).reserve(
                    request_id, task2, .002, monthly_budget_usd=.006)
                return True
            except BudgetExceeded:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, candidates))
        assert outcomes.count(True) == 1
        winner = candidates[outcomes.index(True)]
        ledger.settle(winner, "unknown")
        ledger.settle(winner, "unknown")
        with pytest.raises(ValueError):
            ledger.settle(winner, "released")
        with pytest.raises(BudgetExceeded):
            other.reserve(str(uuid4()), task2, .001, monthly_budget_usd=.006)
        assert ledger.committed_usd() == pytest.approx(.006)
        with registry.engine("central").connect() as conn:
            rows = conn.execute(text("SELECT request_id,state FROM control.ai_cost_reservations")).all()
            assert (result.request_id, "confirmed") in rows
            assert (winner, "unknown") in rows
    finally:
        registry.dispose()
        other_registry.dispose()
