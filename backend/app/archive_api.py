"""P26 archive search and retrieval: explicit grants, current rights, no private reports."""
from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from backend.app.dependencies import Services
from backend.core.archive import ArchiveIntegrityError, PostgresNewsArchive
from backend.core.auth import Principal
from backend.core.errors import APIError, ErrorCode
from backend.core.permissions import decide_access
from backend.core.sources_service import SourceNotAllowed, SourceNotFound


def build_router(*, services_dep: Callable[..., Any], principal_dep: Callable[..., Any],
                 authorize: Callable[..., None], correlation_id: Callable[[Request], str]) -> APIRouter:
    router = APIRouter(prefix="/archive", tags=["archive"])

    def store(services: Services) -> PostgresNewsArchive:
        if services.news_archive is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        return services.news_archive

    def current_rights(services: Services, item: dict[str, Any]) -> dict[str, Any]:
        try:
            source = services.sources.require_usable(item["source_id"], stream=item["stream"])
        except (SourceNotFound, SourceNotAllowed):
            raise APIError(ErrorCode.FORBIDDEN) from None
        if source["license_scope"] not in {"metadata", "summary", "full_text"}:
            raise APIError(ErrorCode.FORBIDDEN)
        return source

    def allowed(services: Services, principal: Principal, news_id: str) -> bool:
        return all(decide_access(principal.subject_id, principal.role, kind, news_id, "read",
                                 services.grants)["allowed"] is True
                   for kind in ("archive", "newsroom"))

    def authorize_item(services: Services, principal: Principal, request: Request, news_id: str) -> None:
        if not 1 <= len(news_id) <= 128:
            raise APIError(ErrorCode.NOT_FOUND)
        for kind in ("archive", "newsroom"):
            authorize(services, principal, resource_type=kind, resource_id=news_id,
                      action="read", correlation_id=correlation_id(request))

    @router.get("/news")
    def search(request: Request, response: Response,
               q: str = Query(default="", max_length=200),
               stream: Literal["domestic", "foreign", "manual"] | None = None,
               offset: int = Query(default=0, ge=0, le=5000),
               principal: Principal = Depends(principal_dep),
               services: Services = Depends(services_dep)) -> dict[str, Any]:
        authorize(services, principal, resource_type="archive", resource_id="default",
                  action="search", correlation_id=correlation_id(request))
        results = []
        # The bounded candidate page is filtered before any title, count or identifier leaves the server.
        for item in store(services).search(q.strip(), stream=stream, limit=100, offset=offset):
            if not allowed(services, principal, item["news_id"]):
                continue
            try:
                current_rights(services, item)
            except APIError:
                continue
            results.append(item)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "items": results, "count": len(results)}

    @router.get("/news/{news_id}/versions")
    def versions(news_id: str, request: Request, response: Response,
                 principal: Principal = Depends(principal_dep),
                 services: Services = Depends(services_dep)) -> dict[str, Any]:
        authorize_item(services, principal, request, news_id)
        repo = store(services)
        result = repo.versions(news_id)
        if not result:
            raise APIError(ErrorCode.NOT_FOUND)
        try:
            original = repo.original(news_id, result[0]["artifact_id"])
        except ArchiveIntegrityError:
            raise APIError(ErrorCode.INVALID_STATE) from None
        if original is None:
            raise APIError(ErrorCode.NOT_FOUND)
        current_rights(services, original)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "news_id": news_id, "versions": result}

    @router.get("/news/{news_id}/versions/{artifact_id}")
    def read_original(news_id: str, artifact_id: str, request: Request, response: Response,
                      principal: Principal = Depends(principal_dep),
                      services: Services = Depends(services_dep)) -> dict[str, Any]:
        authorize_item(services, principal, request, news_id)
        if not 1 <= len(artifact_id) <= 80:
            raise APIError(ErrorCode.NOT_FOUND)
        try:
            raw = store(services).original(news_id, artifact_id)
        except ArchiveIntegrityError:
            raise APIError(ErrorCode.INVALID_STATE) from None
        if raw is None:
            raise APIError(ErrorCode.NOT_FOUND)
        source = current_rights(services, raw)
        scope = {"metadata": 0, "summary": 1, "full_text": 2}
        redacted = (scope[source["license_scope"]] < scope[raw["content_scope"]]
                    or raw["content_scope"] == "metadata"
                    or raw["reuse_permission"] in {"link_only", "blocked"})
        if redacted:
            raw["original_text"] = None
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "artifact_id": artifact_id,
                "source": raw, "text_redacted": redacted}

    return router
