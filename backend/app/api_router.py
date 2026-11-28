"""B01/P01 versioned API router (``/v1``).

Every privileged route resolves the principal from a signed bearer token and
records an audit entry. Route code never decides access on its own: it asks
``permissions.decide_access`` with a grant lookup from the services container.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

from backend.app.archive_api import build_router as build_archive_router
from backend.app.dependencies import Services, get_services
from backend.app.intelligence_api import build_router as build_intelligence_router
from backend.app.ui_api import build_router as build_ui_router
from backend.core.auth import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    AuthenticationError,
    PostgresAccountStore,
    Principal,
    bearer_token,
    issue_session,
    verify_session,
)
from backend.core.errors import APIError, ErrorCode, correlation_id_for
from backend.core.health import health_report
from backend.core.mfa import CHALLENGE_TTL, MFARateLimited
from backend.core.permissions import decide_access, require_access
from backend.core.sources_service import SourceNotAllowed, SourceNotFound
from backend.modules.domestic.api import build_router as build_domestic_router
from backend.modules.editorial.api import build_router as build_editorial_router
from backend.modules.foreign.api import build_router as build_foreign_router
from backend.modules.publishing.api import build_router as build_publishing_router

router = APIRouter(prefix="/v1")


class SourceInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=64)
    site_url: str = Field(min_length=1, max_length=2048)
    feed_url: str | None = Field(default=None, max_length=2048)
    country: str = Field(min_length=2, max_length=8)
    language: str = Field(min_length=2, max_length=16)
    stream: str = Field(min_length=1, max_length=16)
    license_scope: str = Field(min_length=1, max_length=32)
    enabled: bool = False


class ConfigUpdate(BaseModel):
    value: dict[str, Any]
    scope: str = Field(default="global", max_length=32)
    expected_revision: int = Field(ge=0)


def current_principal(request: Request, services: Services = Depends(get_services)) -> Principal:
    token = bearer_token(request.headers.get("Authorization"))
    if not token:
        raise APIError(ErrorCode.UNAUTHORIZED)
    try:
        if services.mfa is not None and token.startswith("ps."):
            return services.mfa.current(token)
        principal = verify_session(token, services.session_key)
        if principal.must_reset_password:
            raise AuthenticationError("credential reset required")
        if services.accounts is not None:
            if principal.role != "staff":
                raise AuthenticationError("privileged session required")
            current = services.accounts.current(principal.subject_id)
            if current.must_reset_password or not principal.session_marker or current != principal:
                raise AuthenticationError("session is no longer valid")
        return principal
    except AuthenticationError:
        raise APIError(ErrorCode.UNAUTHORIZED) from None


def _correlation_id(request: Request) -> str:
    return correlation_id_for(request)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class FirstResetInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    temporary_password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: SecretStr = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


def _auth_audit(
    services: Services, request: Request, action: str, decision: str,
    principal: Principal | None = None,
) -> None:
    services.auditor.record(
        action=action,
        actor_id=principal.subject_id if principal is not None else "anonymous",
        actor_role=principal.role if principal is not None else "unknown",
        resource_type="identity",
        resource_id=principal.subject_id if principal is not None else "login",
        decision=decision,
        correlation_id=correlation_id_for(request),
    )


def _account_store(services: Services, request: Request, action: str) -> PostgresAccountStore:
    store = services.accounts
    if store is None:
        raise APIError(ErrorCode.NOT_FOUND)
    if not store.consume_attempt():
        _auth_audit(services, request, action, "RATE_LIMITED")
        raise APIError(ErrorCode.RATE_LIMITED)
    return store


def _session_response(principal: Principal, services: Services, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if principal.must_reset_password:
        return {"schema_version": "1.0", "must_reset": True}
    if principal.role != "staff":
        if services.mfa is None:
            raise APIError(ErrorCode.UNAUTHORIZED)
        try:
            challenge = services.mfa.start(principal)
        except AuthenticationError:
            raise APIError(ErrorCode.UNAUTHORIZED) from None
        return {"schema_version": "1.0", "must_reset": False, "mfa_required": True,
                "challenge": challenge, "expires_in": CHALLENGE_TTL}
    return {
        "schema_version": "1.0",
        "must_reset": False,
        "access_token": issue_session(principal, services.session_key, ttl_seconds=900),
        "token_type": "bearer",
        "expires_in": 900,
    }


@router.get("/auth/status", tags=["auth"])
def auth_status(response: Response, services: Services = Depends(get_services)) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "1.0", "mode": "staff" if services.accounts is not None else "fixture"}


@router.post("/auth/login", tags=["auth"])
def login(
    payload: LoginInput, request: Request, response: Response,
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    store = _account_store(services, request, "auth.login")
    try:
        principal = store.authenticate(payload.username, payload.password.get_secret_value())
    except AuthenticationError:
        _auth_audit(services, request, "auth.login", "DENIED")
        raise APIError(ErrorCode.UNAUTHORIZED) from None
    try:
        result = _session_response(principal, services, response)
    except APIError:
        _auth_audit(services, request, "auth.login", "DENIED", principal)
        raise
    decision = "RESET_REQUIRED" if principal.must_reset_password else (
        "MFA_REQUIRED" if principal.role != "staff" else "ALLOWED")
    _auth_audit(services, request, "auth.login", decision, principal)
    return result


@router.post("/auth/first-login/reset", tags=["auth"])
def first_login_reset(
    payload: FirstResetInput, request: Request, response: Response,
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    store = _account_store(services, request, "auth.first_reset")
    try:
        principal = store.reset_first_use(
            payload.username, payload.temporary_password.get_secret_value(),
            payload.new_password.get_secret_value(),
        )
    except AuthenticationError:
        _auth_audit(services, request, "auth.first_reset", "DENIED")
        raise APIError(ErrorCode.UNAUTHORIZED) from None
    except ValueError:
        _auth_audit(services, request, "auth.first_reset", "DENIED")
        raise APIError(ErrorCode.CONTRACT_INVALID) from None
    try:
        result = _session_response(principal, services, response)
    except APIError:
        _auth_audit(services, request, "auth.first_reset", "MFA_UNAVAILABLE", principal)
        raise
    _auth_audit(services, request, "auth.first_reset",
                "MFA_REQUIRED" if principal.role != "staff" else "ALLOWED", principal)
    return result


class MFAInput(BaseModel):
    challenge: SecretStr = Field(min_length=1, max_length=128)
    code: SecretStr = Field(min_length=1, max_length=16)


@router.post("/auth/mfa/verify", tags=["auth"])
def mfa_verify(payload: MFAInput, request: Request, response: Response,
               services: Services = Depends(get_services)) -> dict[str, Any]:
    if services.mfa is None:
        raise APIError(ErrorCode.NOT_FOUND)
    try:
        token, ttl, principal = services.mfa.finish(
            payload.challenge.get_secret_value(), payload.code.get_secret_value())
    except MFARateLimited:
        _auth_audit(services, request, "auth.mfa", "RATE_LIMITED")
        raise APIError(ErrorCode.RATE_LIMITED) from None
    except AuthenticationError:
        _auth_audit(services, request, "auth.mfa", "DENIED")
        raise APIError(ErrorCode.UNAUTHORIZED) from None
    _auth_audit(services, request, "auth.mfa", "ALLOWED", principal)
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "1.0", "must_reset": False, "token_type": "bearer",
            "access_token": token, "expires_in": ttl}


@router.post("/auth/privileged/logout", tags=["auth"])
def privileged_logout(request: Request, response: Response,
                      principal: Principal = Depends(current_principal),
                      services: Services = Depends(get_services)) -> dict[str, Any]:
    token = bearer_token(request.headers.get("Authorization"))
    if services.mfa is None or token is None or not token.startswith("ps."):
        raise APIError(ErrorCode.NOT_FOUND)
    services.mfa.revoke(token)
    _auth_audit(services, request, "auth.privileged.logout", "ALLOWED", principal)
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "1.0", "revoked": True}


@router.post("/auth/p19/challenge", tags=["auth"])
def p19_challenge(request: Request, response: Response,
                  principal: Principal = Depends(current_principal),
                  services: Services = Depends(get_services)) -> dict[str, Any]:
    parent = bearer_token(request.headers.get("Authorization"))
    if services.mfa is None or parent is None or principal.role != "editor_in_chief":
        raise APIError(ErrorCode.NOT_FOUND)
    # This is an infrastructure-only step-up; independent P19 password and UI
    # are not activated under the existing policy.
    if not any(decide_access(principal.subject_id, principal.role, "private_report",
                             f"slot-{slot}", "read", services.grants,
                             classification="executive_private")["allowed"] is True
               for slot in (5, 6, 7)):
        _auth_audit(services, request, "auth.p19.challenge", "DENIED", principal)
        raise APIError(ErrorCode.NOT_FOUND)
    try:
        challenge = services.mfa.start(principal, parent)
    except AuthenticationError:
        _auth_audit(services, request, "auth.p19.challenge", "DENIED", principal)
        raise APIError(ErrorCode.NOT_FOUND) from None
    _auth_audit(services, request, "auth.p19.challenge", "MFA_REQUIRED", principal)
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "1.0", "challenge": challenge, "expires_in": CHALLENGE_TTL}


@router.post("/auth/p19/verify", tags=["auth"])
def p19_verify(payload: MFAInput, request: Request, response: Response,
               principal: Principal = Depends(current_principal),
               services: Services = Depends(get_services)) -> dict[str, Any]:
    parent = bearer_token(request.headers.get("Authorization"))
    if services.mfa is None or parent is None or principal.role != "editor_in_chief":
        raise APIError(ErrorCode.NOT_FOUND)
    try:
        token, ttl, verified = services.mfa.finish(
            payload.challenge.get_secret_value(), payload.code.get_secret_value(), parent)
    except MFARateLimited:
        _auth_audit(services, request, "auth.p19.verify", "RATE_LIMITED", principal)
        raise APIError(ErrorCode.RATE_LIMITED) from None
    except AuthenticationError:
        _auth_audit(services, request, "auth.p19.verify", "DENIED", principal)
        raise APIError(ErrorCode.NOT_FOUND) from None
    _auth_audit(services, request, "auth.p19.verify", "ALLOWED", verified)
    response.headers["Cache-Control"] = "no-store"
    return {"schema_version": "1.0", "step_up_token": token, "expires_in": ttl}


def _authorize(
    services: Services,
    principal: Principal,
    *,
    resource_type: str,
    resource_id: str,
    action: str,
    correlation_id: str,
) -> None:
    decision = decide_access(
        principal.subject_id,
        principal.role,
        resource_type,
        resource_id,
        action,
        services.grants,
        request_id=correlation_id,
    )
    services.auditor.record(
        action=f"{resource_type}.{action}",
        actor_id=principal.subject_id,
        actor_role=principal.role,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=str(decision["reason_code"]),
        correlation_id=correlation_id,
        details={"allowed": decision["allowed"]},
    )
    require_access(decision)


@router.get("/health", tags=["ops"])
def health(services: Services = Depends(get_services)) -> dict[str, Any]:
    """Liveness plus dependency configuration; no secrets, no credentials."""
    return health_report(databases=services.registry)


@router.get("/sources", tags=["sources"])
def list_sources(
    request: Request,
    principal: Principal = Depends(current_principal),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    correlation_id = _correlation_id(request)
    _authorize(
        services,
        principal,
        resource_type="source_registry",
        resource_id="default",
        action="read",
        correlation_id=correlation_id,
    )
    return {"schema_version": "1.0", "sources": services.sources.list()}


@router.post("/sources", status_code=201, tags=["sources"])
def register_source(
    payload: SourceInput,
    request: Request,
    principal: Principal = Depends(current_principal),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    correlation_id = _correlation_id(request)
    _authorize(
        services,
        principal,
        resource_type="source_registry",
        resource_id=payload.source_id,
        action="write",
        correlation_id=correlation_id,
    )
    try:
        source = services.sources.register(payload.model_dump())
    except Exception as exc:  # noqa: BLE001 - contract errors become 400
        raise APIError(ErrorCode.CONTRACT_INVALID) from exc
    return {"schema_version": "1.0", "source": source}


@router.get("/config/{contract_name}/{config_id}", tags=["config"])
def read_config(
    contract_name: str,
    config_id: str,
    request: Request,
    scope: str = "global",
    principal: Principal = Depends(current_principal),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    correlation_id = _correlation_id(request)
    _authorize(
        services,
        principal,
        resource_type="config",
        resource_id=f"{contract_name}:{scope}:{config_id}",
        action="read",
        correlation_id=correlation_id,
    )
    try:
        revision = services.settings.get_latest(contract_name, config_id, scope)
    except Exception as exc:  # noqa: BLE001 - unknown contract must be a 400, not a 500
        raise APIError(ErrorCode.CONTRACT_INVALID) from exc
    if revision is None:
        raise APIError(ErrorCode.NOT_FOUND)
    return {
        "schema_version": "1.0",
        "contract_name": contract_name,
        "config_id": config_id,
        "scope": scope,
        "revision": revision.revision,
        "value": revision.value,
    }


@router.put("/config/{contract_name}/{config_id}", tags=["config"])
def update_config(
    contract_name: str,
    config_id: str,
    payload: ConfigUpdate,
    request: Request,
    principal: Principal = Depends(current_principal),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    correlation_id = _correlation_id(request)
    _authorize(
        services,
        principal,
        resource_type="config",
        resource_id=f"{contract_name}:{payload.scope}:{config_id}",
        action="update",
        correlation_id=correlation_id,
    )
    try:
        revision = services.settings.update(
            contract_name=contract_name,
            config_id=config_id,
            scope=payload.scope,
            value=payload.value,
            expected_revision=payload.expected_revision,
            actor_id=principal.subject_id,
            actor_role=principal.role,
        )
    except (APIError, HTTPException):
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as a safe 400
        raise APIError(ErrorCode.CONTRACT_INVALID) from exc
    return {
        "schema_version": "1.0",
        "contract_name": contract_name,
        "config_id": config_id,
        "scope": payload.scope,
        "revision": revision.revision,
    }


@router.get("/news", tags=["news"])
def news_feed(
    request: Request,
    limit: int = 50,
    principal: Principal = Depends(current_principal),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    """Newsroom feed built from real contract records (no synthetic data)."""
    correlation_id = _correlation_id(request)
    _authorize(
        services,
        principal,
        resource_type="newsroom",
        resource_id="feed",
        action="read",
        correlation_id=correlation_id,
    )
    services.pipeline.process_pending()
    items = services.pipeline.feed(limit=max(1, min(limit, 200)))
    if principal.role == "staff":
        scoped = []
        for item in items:
            if decide_access(principal.subject_id, principal.role, "newsroom", item["news_id"],
                             "read", services.grants)["allowed"] is not True:
                continue
            desks = [desk for desk in item["desks"] if decide_access(
                principal.subject_id, principal.role, "desk", desk, "read", services.grants)["allowed"] is True]
            if not desks:
                continue
            try:
                source = services.sources.require_usable(item["source_id"], stream=item["stream"])
            except (SourceNotFound, SourceNotAllowed):
                continue
            scopes = {"metadata": 0, "summary": 1, "full_text": 2}
            if scopes.get(source["license_scope"], -1) < scopes[item["content_scope"]]:
                continue
            scoped.append({**item, "desks": desks})
        items = scoped
    return {"schema_version": "1.0", "count": len(items), "items": items}


@router.get("/news/{news_id}", tags=["news"])
def news_item(
    news_id: str,
    request: Request,
    principal: Principal = Depends(current_principal),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    correlation_id = _correlation_id(request)
    _authorize(
        services,
        principal,
        resource_type="newsroom",
        resource_id=news_id,
        action="read",
        correlation_id=correlation_id,
    )
    item = services.pipeline.item(news_id)
    if item is None:
        raise APIError(ErrorCode.NOT_FOUND)
    return {"schema_version": "1.0", **item}


# Domestic ingest routes live in the package module; the app supplies auth and audit.
router.include_router(
    build_domestic_router(
        services_dep=get_services,
        principal_dep=current_principal,
        authorize=_authorize,
    )
)

router.include_router(
    build_foreign_router(
        services_dep=get_services,
        principal_dep=current_principal,
        authorize=_authorize,
    )
)

router.include_router(
    build_editorial_router(
        services_dep=get_services,
        principal_dep=current_principal,
        authorize=_authorize,
        correlation_id=_correlation_id,
    )
)

router.include_router(
    build_archive_router(
        services_dep=get_services,
        principal_dep=current_principal,
        authorize=_authorize,
        correlation_id=_correlation_id,
    )
)

router.include_router(
    build_intelligence_router(
        services_dep=get_services,
        principal_dep=current_principal,
        authorize=_authorize,
        correlation_id=_correlation_id,
    )
)


router.include_router(
    build_publishing_router(
        services_dep=get_services,
        principal_dep=current_principal,
        authorize=_authorize,
        correlation_id=_correlation_id,
    )
)

router.include_router(
    build_ui_router(services_dep=get_services, principal_dep=current_principal,
                    authorize=_authorize, correlation_id=_correlation_id)
)
