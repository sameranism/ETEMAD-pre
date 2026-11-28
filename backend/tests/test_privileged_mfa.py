"""MFA integration uses disposable databases and synthetic credentials only."""
import base64
import os
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
from backend.core.auth import generate_first_use_credential
from backend.core.mfa import matching_step, totp
from backend.core.permissions import Grant


def test_totp_rfc4226_and_replay():
    secret = b"12345678901234567890"
    assert totp(secret, 0) == "755224"
    assert totp(secret, 1) == "287082"
    assert matching_step(secret, "287082", 30, -1) == 1
    assert matching_step(secret, "287082", 30, 1) is None
    assert matching_step(secret, "not-a-code", 30, -1) is None


@pytest.mark.skipif(not os.environ.get("ETEMAD_TEST_PG_ADMIN_URL"), reason="disposable PostgreSQL unset")
def test_privileged_login_step_up_and_revoke(monkeypatch, tmp_path):
    from alembic import command
    from alembic.config import Config

    admin = make_url(os.environ["ETEMAD_TEST_PG_ADMIN_URL"])
    assert admin.host == "127.0.0.1" and admin.database == "postgres"
    assert admin.username == "fixture_admin" and not admin.query
    suffix = uuid4().hex[:12]
    env = {
        "ETEMAD_ENV": "staging", "ETEMAD_FEED_MODE": "fixture",
        "ETEMAD_NEWS_STORAGE": "postgres", "ETEMAD_IDENTITY_STORAGE": "postgres",
        "ETEMAD_PRIVILEGED_MFA": "totp",
        "ETEMAD_SESSION_KEY": uuid4().hex + uuid4().hex,
        "ETEMAD_OBJECT_ROOT": str(tmp_path / "objects"),
    }
    keys = {role: os.urandom(20) for role in ("owner", "editor_in_chief")}
    for role, key in keys.items():
        env["ETEMAD_SECRET_MFA_" + role.upper()] = base64.b32encode(key).decode()
    created = []
    services = None
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    import backend.core.mfa as mfa_module

    class Clock:
        value = 1700000010

        @classmethod
        def time(cls):
            return cls.value

    monkeypatch.setattr(mfa_module, "time", Clock)
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as control:
        try:
            for role in ("central", "domestic"):
                name = f"etemad_test_{role}_{suffix}"
                control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append(name)
                var = f"ETEMAD_DB_{role.upper()}_URL"
                env[var] = admin.set(drivername="postgresql+psycopg", database=name).render_as_string(
                    hide_password=False)
                monkeypatch.setenv(var, env[var])
            command.upgrade(Config("alembic.ini"), "head")
            command.upgrade(Config("alembic_domestic.ini"), "head")
            services = build_services(env)
            assert services.accounts and services.mfa
            accounts, mfa = services.accounts, services.mfa
            users = {}
            with TestClient(create_app(services=services)) as client:
                for role in ("owner", "editor_in_chief", "staff"):
                    user, initial = accounts.provision("fixture-" + role, role)
                    new = generate_first_use_credential()
                    users[role] = (user, new)
                    if role in keys:
                        mfa.enroll(user, "mfa/" + role, totp(keys[role], Clock.value // 30),
                                   operator_id="fixture-operator")
                    result = client.post("/v1/auth/first-login/reset", json={
                        "username": "fixture-" + role, "temporary_password": initial,
                        "new_password": new})
                    assert result.status_code == 200
                    if role == "staff":
                        staff_token = result.json()["access_token"]
                        assert result.json()["expires_in"] == 900
                        assert client.get("/v1/ui/session", headers={
                            "Authorization": "Bearer " + staff_token}).status_code == 200
                    else:
                        assert "access_token" not in result.json()
                        assert result.json()["mfa_required"] is True
                Clock.value += 60
                tokens = {}
                for role in keys:
                    password = users[role][1]
                    response = client.post("/v1/auth/login", json={
                        "username": "fixture-" + role, "password": password})
                    assert response.status_code == 200
                    challenge = response.json()["challenge"]
                    assert "access_token" not in response.json()
                    assert client.get("/v1/ui/session", headers={
                        "Authorization": "Bearer " + challenge}).status_code == 401
                    code = totp(keys[role], Clock.value // 30)
                    verified = client.post("/v1/auth/mfa/verify", json={
                        "challenge": challenge, "code": code})
                    assert verified.status_code == 200, verified.status_code
                    assert verified.json()["expires_in"] == 600
                    token = verified.json()["access_token"]
                    tokens[role] = token
                    headers = {"Authorization": "Bearer " + token}
                    assert client.get("/v1/ui/session", headers=headers).status_code == 200
                    assert client.post("/v1/auth/mfa/verify", json={
                        "challenge": challenge, "code": code}).status_code == 401
                owner = {"Authorization": "Bearer " + tokens["owner"]}
                editor = {"Authorization": "Bearer " + tokens["editor_in_chief"]}
                staff = {"Authorization": "Bearer " + staff_token}
                editor_id = users["editor_in_chief"][0]
                grant = Grant(editor_id, "editor_in_chief", "private_report", "slot-5", "read")
                services.grants.grant(grant)
                assert client.get("/v1/analysis/daily-reports/5", headers=editor).status_code == 404
                assert client.post("/v1/auth/p19/challenge", headers=owner).status_code == 404
                assert client.post("/v1/auth/p19/challenge", headers=staff).status_code == 404
                challenge = client.post("/v1/auth/p19/challenge", headers=editor)
                assert challenge.status_code == 200
                Clock.value += 60
                step = client.post("/v1/auth/p19/verify", headers=editor, json={
                    "challenge": challenge.json()["challenge"],
                    "code": totp(keys["editor_in_chief"], Clock.value // 30)})
                assert step.status_code == 200
                p19 = step.json()["step_up_token"]
                assert step.json()["expires_in"] == 300
                assert mfa.current(p19, tokens["editor_in_chief"]).subject_id == editor_id
                assert client.get("/v1/ui/session", headers={"Authorization": "Bearer " + p19}).status_code == 401
                assert client.get("/v1/analysis/daily-reports/6", headers={
                    **editor, "X-ETEMAD-P19-Session": p19}).status_code == 404
                assert client.get("/v1/analysis/daily-reports/5", headers={
                    **owner, "X-ETEMAD-P19-Session": p19}).status_code == 404
                # Authorized slot reaches the existing downstream workspace-grant gate.
                assert client.get("/v1/analysis/daily-reports/5", headers={
                    **editor, "X-ETEMAD-P19-Session": p19}).status_code == 403
                services.grants.revoke(grant)
                assert client.get("/v1/analysis/daily-reports/5", headers={
                    **editor, "X-ETEMAD-P19-Session": p19}).status_code == 404
                assert client.post("/v1/auth/privileged/logout", headers=editor).status_code == 200
                assert client.get("/v1/ui/session", headers=editor).status_code == 401
                with pytest.raises(Exception):
                    mfa.current(p19, tokens["editor_in_chief"])
                # Expiry uses the database clock, not the TOTP test clock.
                with services.registry.engine("central").begin() as conn:
                    conn.execute(text("UPDATE identity.privileged_sessions SET expires_at=now()-interval '1 second'"))
                assert client.get("/v1/ui/session", headers=owner).status_code == 401
                challenge = client.post("/v1/auth/login", json={
                    "username": "fixture-owner", "password": users["owner"][1]}).json()["challenge"]
                wrong = "000000" if totp(keys["owner"], Clock.value // 30) != "000000" else "111111"
                observed = []
                for _ in range(7):
                    observed.append(client.post("/v1/auth/mfa/verify", json={
                        "challenge": challenge, "code": wrong}).status_code)
                assert 429 in observed and set(observed) <= {401, 429}
                mfa.revoke_factor(users["owner"][0], operator_id="fixture-operator")
                assert client.post("/v1/auth/login", json={
                    "username": "fixture-owner", "password": users["owner"][1]}).status_code == 401
            with services.registry.engine("central").connect() as conn:
                assert conn.execute(text("SELECT count(*) FROM identity.mfa_factors")).scalar_one() == 2
                assert conn.execute(text("SELECT count(*) FROM audit.audit_entries WHERE action='auth.mfa.enroll'")).scalar_one() == 2
        finally:
            if services is not None:
                services.registry.dispose()
            for name in reversed(created):
                control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
