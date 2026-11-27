"""Authenticated P27 queue view and manual hold; no external send endpoint."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query, Request, Response

from backend.core.errors import APIError, ErrorCode
from backend.core.output_switches import PUBLISHING
from backend.core.permissions import decide_access


def build_router(*, services_dep: Callable[..., Any], principal_dep: Callable[..., Any],
                 authorize: Callable[..., None], correlation_id: Callable[[Request], str]) -> APIRouter:
    router = APIRouter(prefix="/publishing", tags=["publishing"])

    @router.get("/queue")
    def overview(request: Request, response: Response, limit: int = Query(default=100, ge=1, le=100),
                 principal: Any = Depends(principal_dep), services: Any = Depends(services_dep)) -> dict[str, Any]:
        authorize(services, principal, resource_type="publication_workspace", resource_id="default",
                  action="read", correlation_id=correlation_id(request))
        if services.publication_ledger is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        data = services.publication_ledger.overview(limit=limit)
        for item in data["items"]:
            item["can_hold"] = bool(item["can_hold"] and decide_access(
                principal.subject_id, principal.role, "publication_workspace", item["job_id"],
                "hold", services.grants)["allowed"] is True)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "mode": "fixture_history",
                "publishing_enabled": services.switches().enabled(PUBLISHING),
                "live_dispatch_available": False, **data}

    @router.post("/queue/{job_id}/hold")
    def hold(job_id: str, request: Request, response: Response,
             principal: Any = Depends(principal_dep), services: Any = Depends(services_dep)) -> dict[str, Any]:
        if not 1 <= len(job_id) <= 128:
            raise APIError(ErrorCode.NOT_FOUND)
        corr = correlation_id(request)
        authorize(services, principal, resource_type="publication_workspace", resource_id="default",
                  action="read", correlation_id=corr)
        authorize(services, principal, resource_type="publication_workspace", resource_id=job_id,
                  action="hold", correlation_id=corr)
        if services.publication_ledger is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        try:
            state = services.publication_ledger.hold(job_id)
        except ValueError:
            raise APIError(ErrorCode.CONFLICT) from None
        if state is None:
            raise APIError(ErrorCode.NOT_FOUND)
        services.auditor.record(action="publication.hold", actor_id=principal.subject_id,
                                actor_role=principal.role, resource_type="publication_workspace",
                                resource_id=job_id, decision="held", correlation_id=corr)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "job_id": job_id, "state": state}

    return router
