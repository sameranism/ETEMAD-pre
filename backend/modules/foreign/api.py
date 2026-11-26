"""S02-013: permission-scoped foreign monitoring and fixture poll endpoints."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from backend.core.errors import APIError, ErrorCode, correlation_id_for
from backend.core.sources_service import SourceNotAllowed, SourceNotFound
from backend.modules.foreign.bridge import BridgeError
from backend.modules.foreign.feed_client import ForeignFeedError


def build_router(*, services_dep: Callable[..., Any], principal_dep: Callable[..., Any],
                 authorize: Callable[..., None]) -> APIRouter:
    router = APIRouter(prefix="/foreign", tags=["foreign"])

    @router.get("/status")
    def status(request: Request, response: Response, limit: int = Query(50, ge=1, le=200),
               principal: Any = Depends(principal_dep),
               services: Any = Depends(services_dep)) -> dict[str, Any]:
        authorize(services, principal, resource_type="foreign_monitor", resource_id="default",
                  action="read", correlation_id=correlation_id_for(request))
        if services.foreign_workflow is None:
            raise APIError(ErrorCode.NOT_FOUND)
        response.headers["Cache-Control"] = "no-store"
        return services.foreign_workflow.status(limit=limit)

    @router.post("/{source_id}/poll")
    def poll(request: Request, response: Response,
             source_id: str = Path(min_length=1, max_length=64),
             principal: Any = Depends(principal_dep),
             services: Any = Depends(services_dep)) -> dict[str, Any]:
        authorize(services, principal, resource_type="source_registry", resource_id=source_id,
                  action="poll", correlation_id=correlation_id_for(request))
        if services.foreign_workflow is None:
            raise APIError(ErrorCode.NOT_FOUND)
        try:
            result = services.foreign_workflow.poll(source_id)
        except SourceNotFound:
            raise APIError(ErrorCode.NOT_FOUND) from None
        except SourceNotAllowed:
            raise APIError(ErrorCode.FORBIDDEN) from None
        except (BridgeError, ForeignFeedError, ValueError):
            raise APIError(ErrorCode.CONFLICT) from None
        response.headers["Cache-Control"] = "no-store"
        return result

    return router
