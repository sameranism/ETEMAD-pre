"""S01-020: authentication, authorization, audit and configuration safety."""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from backend.app.dependencies import build_services
from backend.app.main import create_app
from backend.core.audit import REDACTED, Auditor, InMemoryAuditSink, sanitize
from backend.core.auth import (
    MIN_PASSWORD_LENGTH,
    AccountLimitReached,
    AuthenticationError,
    Principal,
    SessionInvalid,
    bearer_token,
    generate_first_use_credential,
    hash_password,
    issue_session,
    verify_password,
    verify_session,
)
from backend.core.db import DatabaseNotConfigured, DatabaseRegistry, configured_roles, redact_url
from backend.core.permissions import Grant, InMemoryGrantStore, decide_access, require_access
from backend.core.secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    InvalidSecretReference,
    SecretUnavailable,
    require_secret,
)

KEY = b"test-session-key-not-a-real-secret"


# --- passwords and credentials -------------------------------------------------


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("scrypt$")
    assert "correct horse" not in encoded
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password entirely", encoded)


def test_short_password_is_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))


def test_same_password_has_different_hashes() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first != second


def test_first_use_credential_is_long_and_unique() -> None:
    values = {generate_first_use_credential() for _ in range(20)}
    assert len(values) == 20
    assert all(len(value) >= 30 for value in values)


# --- sessions ------------------------------------------------------------------


def test_session_roundtrip_preserves_principal() -> None:
    principal = Principal("user-001", "editor_in_chief", must_reset_password=True)
    token = issue_session(principal, KEY, ttl_seconds=60)
    restored = verify_session(token, KEY)
    assert restored == principal


def test_tampered_token_is_rejected() -> None:
    token = issue_session(Principal("user-001", "staff"), KEY)
    head, body, signature = token.split(".")
    forged = f"{head}.{body[:-2]}AA.{signature}"
    with pytest.raises(SessionInvalid):
        verify_session(forged, KEY)


def test_expired_token_is_rejected() -> None:
    token = issue_session(Principal("user-001", "staff"), KEY, ttl_seconds=1)
    with pytest.raises(SessionInvalid):
        verify_session(token, KEY, now=int(time.time()) + 10)


def test_unknown_role_cannot_be_a_principal() -> None:
    with pytest.raises(ValueError):
        Principal("user-001", "superuser")


def test_bearer_extraction() -> None:
    assert bearer_token("Bearer abc.def") == "abc.def"
    assert bearer_token("Basic abc") is None
    assert bearer_token(None) is None


# --- authorization -------------------------------------------------------------


def test_default_is_deny() -> None:
    decision = decide_access("user-001", "staff", "article", "a-1", "read", lambda *args: False)
    assert decision["allowed"] is False
    assert decision["reason_code"] == "DENY_DEFAULT"


def test_grant_allows_the_named_action_only() -> None:
    store = InMemoryGrantStore([Grant("user-001", "staff", "article", "a-1", "read")])
    allowed = decide_access("user-001", "staff", "article", "a-1", "read", store)
    denied = decide_access("user-001", "staff", "article", "a-1", "publish", store)
    assert allowed["allowed"] is True
    assert denied["allowed"] is False


def test_private_report_requires_explicit_editor_in_chief_grant() -> None:
    store = InMemoryGrantStore()
    without_grant = decide_access(
        "eic-1", "editor_in_chief", "private_report", "r-9", "read", store
    )
    assert without_grant["allowed"] is False
    assert without_grant["reason_code"] == "GRANT_REQUIRED"

    store.grant(Grant("eic-1", "editor_in_chief", "private_report", "r-9", "read"))
    with_grant = decide_access("eic-1", "editor_in_chief", "private_report", "r-9", "read", store)
    assert with_grant["allowed"] is True


def test_owner_and_staff_never_reach_private_reports() -> None:
    store = InMemoryGrantStore()
    for role, actor in (("owner", "owner-1"), ("staff", "staff-1")):
        decision = decide_access(actor, role, "private_report", "r-9", "read", store)
        assert decision["allowed"] is False
        assert decision["reason_code"] == "EXECUTIVE_PRIVATE"


def test_require_access_hides_private_objects() -> None:
    denied_private = decide_access("s", "staff", "private_report", "r-9", "read", lambda *a: False)
    denied_public = decide_access("s", "staff", "article", "a-1", "read", lambda *a: False)
    with pytest.raises(Exception) as private:
        require_access(denied_private)
    with pytest.raises(Exception) as public:
        require_access(denied_public)
    assert getattr(private.value, "status_code", None) == 404
    assert getattr(public.value, "status_code", None) == 403


# --- audit ---------------------------------------------------------------------


def test_audit_sanitizes_secrets_and_bounds_values() -> None:
    sink = InMemoryAuditSink()
    auditor = Auditor(sink)
    entry = auditor.record(
        action="config.update",
        actor_id="user-001",
        actor_role="editor_in_chief",
        resource_type="output_controls",
        resource_id="controls-main",
        decision="allow",
        correlation_id="req-1",
        details={
            "password": "hunter2",
            "credential_ref": "dest/a",
            "note": "x" * 500,
        },
    )
    assert entry.details["password"] == REDACTED
    assert entry.details["credential_ref"] == REDACTED
    assert entry.details["note"].endswith("...")
    assert len(entry.details["note"]) <= 203
    assert sink.entries() == (entry,)


def test_audit_requires_complete_identity() -> None:
    with pytest.raises(ValueError):
        Auditor(InMemoryAuditSink()).record(
            action="x",
            actor_id="",
            actor_role="staff",
            resource_type="article",
            resource_id="a-1",
            decision="deny",
            correlation_id="req-1",
        )


def test_sanitize_keeps_ids_and_neutralizes_objects() -> None:
    clean = sanitize({"resource_id": "a-1", "payload": {"a": 1}})
    assert clean["resource_id"] == "a-1"
    assert clean["payload"] == "<dict>"


# --- database configuration ----------------------------------------------------


def test_database_roles_are_optional_and_redacted() -> None:
    registry = DatabaseRegistry(environ={})
    with pytest.raises(DatabaseNotConfigured):
        registry.engine("central")
    assert registry.describe()["central"] == "not_configured"
    assert configured_roles({}) == []


def test_database_url_password_is_redacted() -> None:
    url = "postgresql+psycopg://etemad:s3cr3t@db:5432/etemad_central"
    redacted = redact_url(url)
    assert "s3cr3t" not in redacted
    assert redacted.endswith("/etemad_central")


def test_engine_is_created_lazily_for_a_configured_role(tmp_path) -> None:
    registry = DatabaseRegistry(environ={"ETEMAD_DB_CENTRAL_URL": "sqlite://"})
    engine = registry.engine("central")
    assert engine.dialect.name == "sqlite"
    registry.dispose()


def test_unknown_database_role_is_rejected() -> None:
    with pytest.raises(KeyError):
        DatabaseRegistry(environ={}).url_for("staging")


# --- secrets -------------------------------------------------------------------


def test_env_secret_store_maps_reference_to_variable() -> None:
    store = EnvSecretStore(environ={"ETEMAD_SECRET_DEST_TELEGRAM": "abc"})
    assert require_secret(store, "dest/telegram") == "abc"
    with pytest.raises(SecretUnavailable):
        require_secret(store, "dest/missing")


def test_secret_reference_format_is_enforced() -> None:
    with pytest.raises(InvalidSecretReference):
        InMemorySecretStore({"Bad Ref!": "x"})


def test_authentication_error_is_the_common_base() -> None:
    assert issubclass(SessionInvalid, AuthenticationError)


# --- PostgreSQL identity slice (disposable loopback databases only) ------------


def test_persistent_identity_is_explicit_and_default_off() -> None:
    services = build_services({})
    assert services.accounts is None
    with TestClient(create_app(services=services)) as client:
        assert client.get("/v1/auth/status").json()["mode"] == "fixture"
        result = client.post("/v1/auth/login", json={"username": "fixture", "password": "invalid"})
        assert result.status_code == 404
    with pytest.raises(ValueError, match="private staging"):
        build_services({"ETEMAD_IDENTITY_STORAGE": "postgres"})


@pytest.mark.skipif(not os.environ.get("ETEMAD_TEST_PG_ADMIN_URL"), reason="disposable PostgreSQL unset")
def test_staff_identity_reset_permissions_and_limits(monkeypatch, tmp_path) -> None:
    from alembic import command
    from alembic.config import Config

    admin = make_url(os.environ["ETEMAD_TEST_PG_ADMIN_URL"])
    assert admin.host == "127.0.0.1" and admin.database == "postgres"
    assert admin.username == "fixture_admin" and not admin.query
    suffix = uuid4().hex[:12]
    env = {
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_NEWS_STORAGE": "postgres", "ETEMAD_IDENTITY_STORAGE": "postgres",
        "ETEMAD_SESSION_KEY": uuid4().hex + uuid4().hex,
        "ETEMAD_OBJECT_ROOT": str(tmp_path / "objects"),
    }
    created = []
    services_list = []
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as control:
        try:
            for role in ("central", "domestic"):
                name = f"etemad_test_{role}_{suffix}"
                control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append(name)
                key = f"ETEMAD_DB_{role.upper()}_URL"
                env[key] = admin.set(drivername="postgresql+psycopg", database=name).render_as_string(
                    hide_password=False
                )
                monkeypatch.setenv(key, env[key])
            command.upgrade(Config("alembic.ini"), "head")
            command.upgrade(Config("alembic_domestic.ini"), "head")
            services = build_services(env)
            services_list.append(services)
            store = services.accounts
            assert store is not None
            staff_id, initial = store.provision("fixture-staff", "staff")
            _, privileged_initial = store.provision("fixture-owner", "owner")
            _, other_initial = store.provision("fixture-other", "staff")
            replacement = generate_first_use_credential()
            grant = Grant(staff_id, "staff", "newsroom", "feed", "read")
            services.grants.grant(grant)
            assert not services.grants.lookup(staff_id, "staff", "newsroom", "feed", "read")

            with TestClient(create_app(services=services)) as client:
                assert client.get("/v1/auth/status").json()["mode"] == "staff"
                login_path = "/v1/auth/login"
                reset_path = "/v1/auth/first-login/reset"
                assert client.post(login_path, json={
                    "username": "fixture-owner", "password": privileged_initial,
                }).status_code == 401
                assert client.post(login_path, json={
                    "username": "fixture-staff", "password": "incorrect",
                }).status_code == 401
                first = client.post(login_path, json={"username": "fixture-staff", "password": initial})
                assert first.status_code == 200 and first.json()["must_reset"] is True
                assert "access_token" not in first.json()
                assert first.headers["Cache-Control"] == "no-store"
                assert client.get("/v1/news").status_code == 401
                fixture_token = issue_session(Principal(staff_id, "staff"), env["ETEMAD_SESSION_KEY"].encode())
                assert client.get("/v1/news", headers={"Authorization": f"Bearer {fixture_token}"}).status_code == 401
                reset = client.post(reset_path, json={
                    "username": "fixture-staff", "temporary_password": initial,
                    "new_password": replacement,
                })
                assert reset.status_code == 200
                token = reset.json()["access_token"]
                assert reset.json()["expires_in"] == 900
                headers = {"Authorization": f"Bearer {token}"}
                assert client.get("/v1/news", headers=headers).status_code == 200
                assert client.post(login_path, json={
                    "username": "fixture-staff", "password": initial,
                }).status_code == 401
                assert client.post(reset_path, json={
                    "username": "fixture-other", "temporary_password": other_initial,
                    "new_password": replacement,
                }).status_code == 400
                assert client.post(reset_path, json={
                    "username": "fixture-staff", "temporary_password": initial,
                    "new_password": generate_first_use_credential(),
                }).status_code == 401
            services.registry.dispose()

            restarted = build_services(env)
            services_list.append(restarted)
            assert restarted.accounts is not None
            with TestClient(create_app(services=restarted)) as client:
                assert client.get("/v1/news", headers=headers).status_code == 200
                restarted.grants.revoke(grant)
                assert client.get("/v1/news", headers=headers).status_code == 403
                restarted.grants.grant(grant)
                assert client.get("/v1/news", headers=headers).status_code == 200
                for index in range(7):
                    restarted.accounts.provision(f"fixture-extra-{index}", "staff")
                with pytest.raises(AccountLimitReached):
                    restarted.accounts.provision("fixture-eleventh", "staff")
                engine = restarted.registry.engine("central")
                with engine.begin() as conn:
                    assert conn.execute(text("SELECT count(*) FROM identity.users WHERE is_active")).scalar_one() == 10
                    rows = conn.execute(text("SELECT password_hash FROM identity.users")).scalars().all()
                    assert all(initial not in value and replacement not in value for value in rows)
                    records = conn.execute(text("SELECT action, decision, details_json FROM audit.audit_entries")).all()
                    assert any(row.action == "auth.first_reset" and row.decision == "ALLOWED" for row in records)
                    assert any(row.action == "auth.login" and row.decision == "DENIED" for row in records)
                    assert initial not in str(records) and replacement not in str(records)
                    conn.execute(text("UPDATE identity.users SET is_active = false WHERE user_id = :id"), {"id": staff_id})
                assert client.get("/v1/news", headers=headers).status_code == 401
                for _ in range(30):
                    if not restarted.accounts.consume_attempt():
                        break
                assert client.post(login_path, json={
                    "username": "fixture-staff", "password": replacement,
                }).status_code == 429
        finally:
            for service in services_list:
                service.registry.dispose()
            for name in reversed(created):
                control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
