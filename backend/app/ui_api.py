"""P27 role-specific dashboard projections with server-side authorization.

No private report payloads, secrets, or cross-role response sections enter
these projections. Private reports continue through the existing P19 API.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import text

from backend.core.ai_cost_ledger import PostgresCostLedger
from backend.core.audit import InMemoryAuditSink, PostgresAuditSink
from backend.core.auth import Principal
from backend.core.errors import APIError, ErrorCode
from backend.core.output_switches import default_off_switches
from backend.core.permissions import decide_access
from backend.core.sources_service import SourceNotAllowed, SourceNotFound
from backend.modules.ui.saved_views import SavedViews, ViewLimit

DESKS = ("politics", "economy", "international", "society", "sports", "culture",
         "technology", "health", "general")
STREAMS = ("all", "domestic", "foreign", "manual")
CONFIG = "output_controls:global:controls-main"
NEWS_FIELDS = ("news_id", "stream", "source_id", "title", "original_url", "collected_at",
               "reuse_permission", "content_scope", "event_id", "desks")
OUTPUT_FIELDS = {
    "public_analysis": ("public_analysis", "enabled"),
    "studio": ("studio", "enabled"),
    "studio_audio": ("studio", "audio_enabled"),
    "studio_video": ("studio", "video_enabled"),
    "publishing": ("publishing", "enabled"),
    "publishing_domestic": ("publishing", "domestic_enabled"),
    "publishing_foreign": ("publishing", "foreign_enabled"),
    "publishing_reports": ("publishing", "public_reports_enabled"),
}


class OutputChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal["public_analysis", "studio", "studio_audio", "studio_video", "publishing",
                 "publishing_domestic", "publishing_foreign", "publishing_reports"]
    enabled: StrictBool
    expected_revision: int = Field(ge=0, strict=True)


class SavedViewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    desk: str = Field(min_length=1, max_length=64)
    stream: Literal["all", "domestic", "foreign", "manual"]


def _allowed(services: Any, p: Principal, kind: str, identity: str, action: str) -> bool:
    return decide_access(p.subject_id, p.role, kind, identity, action, services.grants)["allowed"] is True


def _role(p: Principal, expected: str) -> None:
    if p.role != expected:
        raise APIError(ErrorCode.FORBIDDEN)


def _controls(services: Any, p: Principal) -> dict[str, Any]:
    revision = services.settings.get_latest("output_controls", "controls-main", "global")
    record = revision.value if revision is not None else default_off_switches().record
    return {"revision": revision.revision if revision is not None else 0,
            "values": {k: record[group][field] for k, (group, field) in OUTPUT_FIELDS.items()},
            "can_update": _allowed(services, p, "config", CONFIG, "update")}


def _capabilities(s: Any, p: Principal) -> dict[str, bool]:
    archive = _allowed(s, p, "archive", "default", "search")
    return {
        "archive": archive,
        "analysis": archive and any(_allowed(s, p, "analysis_workspace", "default", action)
                                    for action in ("run", "chat")),
        "reports": archive and _allowed(s, p, "analysis_workspace", "default", "report"),
        "publishing": p.role == "editor_in_chief" and
            _allowed(s, p, "publication_workspace", "default", "read"),
        "preview": _allowed(s, p, "editorial_workspace", "default", "preview"),
    }


def _workbench(s: Any, p: Principal, *, desk: str | None = None,
               stream: str = "all") -> dict[str, Any]:
    assigned = [d for d in DESKS if p.role == "editor_in_chief" or _allowed(s, p, "desk", d, "read")]
    if desk is not None and desk not in assigned:
        raise APIError(ErrorCode.FORBIDDEN)
    items: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    if _allowed(s, p, "newsroom", "feed", "read"):
        # Preserve the existing newsroom read/processing handoff until the
        # independent processing worker owns this trigger.
        s.pipeline.process_pending()
        for item in s.pipeline.feed(limit=200):
            allowed_desks = [d for d in item["desks"] if d in assigned]
            if ((desk is not None and desk not in allowed_desks) or not allowed_desks
                    or (stream != "all" and item["stream"] != stream)
                    or not _allowed(s, p, "newsroom", item["news_id"], "read")):
                continue
            try:
                source = s.sources.require_usable(item["source_id"], stream=item["stream"])
                if source["license_scope"] not in {"metadata", "summary", "full_text"}:
                    continue
            except (SourceNotFound, SourceNotAllowed):
                continue
            projected = {k: item[k] for k in NEWS_FIELDS}
            projected["desks"] = allowed_desks
            items.append(projected)
            if not _allowed(s, p, "editorial_workspace", item["news_id"], "read"):
                continue
            current = s.editorial.get(item["news_id"]) if s.editorial is not None else None
            if current is not None and (current["article"]["desk"] not in allowed_desks
                                        or (desk is not None and current["article"]["desk"] != desk)):
                continue
            tasks.append({
                "news_id": item["news_id"], "title": item["title"], "desk": current["article"]["desk"]
                if current is not None else (desk or allowed_desks[0]), "stream": item["stream"],
                "status": current["article"]["status"] if current is not None else "not_started",
                "decision": current["policy"]["decision"] if current is not None else None,
                "version": current["version"] if current is not None else None,
            })
    return {"desks": assigned, "items": items, "tasks": tasks,
            "scan_limit": 200, "scope": "bounded_authorized_projection",
            "capabilities": _capabilities(s, p)}


def _owner(services: Any, p: Principal, authorize: Callable[..., None],
           request_id: str) -> dict[str, Any]:
    data: dict[str, Any] = {}

    def permitted(kind: str, identity: str, action: str = "read") -> bool:
        if not _allowed(services, p, kind, identity, action):
            return False
        authorize(services, p, resource_type=kind, resource_id=identity,
                  action=action, correlation_id=request_id)
        return True

    if permitted("operations", "default"):
        health = {}
        for role in ("central", "domestic", "foreign"):
            try:
                with services.registry.engine(role).connect() as conn:
                    health[role] = "ok" if conn.execute(text("SELECT 1")).scalar_one() == 1 else "unavailable"
            except Exception:
                health[role] = "unavailable"
        data["health"] = health
        data["workflow_counts"] = services.workflow_jobs.counts() if services.workflow_jobs is not None else None
    if permitted("source_registry", "default"):
        sources = services.sources.list()
        monitoring = {"domestic": "ok", "foreign": "not_authorized"}
        try:
            domestic = {x["source_id"]: x for x in services.pipeline.poll_states.all()}
        except Exception:
            domestic = {}
            monitoring["domestic"] = "unavailable"
        foreign: dict[str, Any] = {}
        foreign_authorized = permitted("foreign_monitor", "default")
        if foreign_authorized:
            if services.foreign_workflow is None:
                monitoring["foreign"] = "unavailable"
            else:
                try:
                    foreign = {
                        x["source_id"]: x
                        for x in services.foreign_workflow.status(limit=200)["polls"]
                    }
                    monitoring["foreign"] = "ok"
                except Exception:
                    monitoring["foreign"] = "unavailable"
        rows = []
        for source in sources[:200]:
            poll = (domestic if source["stream"] == "domestic" else foreign).get(source["source_id"])
            rows.append({"source_id": source["source_id"], "stream": source["stream"],
                         "enabled": source["enabled"],
                         "last_success_at": poll["last_success_at"] if poll else None,
                         "consecutive_failures": poll["consecutive_failures"] if poll else None})
        data["sources"] = {"registered": len(sources), "enabled": sum(x["enabled"] for x in sources),
                           "items": rows, "scan_limit": 200, "monitoring": monitoring}
        data["foreign_monitor"] = foreign_authorized
    if permitted("config", "schedule_config:domestic:SCH-DOMESTIC-INGEST"):
        data["schedule"] = True
    if permitted("config", CONFIG):
        data["controls"] = _controls(services, p)
    if permitted("publication_workspace", "default"):
        data["publishing"] = True
    if permitted("ai_cost", "default"):
        cost = (PostgresCostLedger(services.registry.engine("central")).committed_usd()
                if services.workflow_jobs is not None else None)
        data["ai_cost"] = {"committed_usd": cost,
                           "period": datetime.now(timezone.utc).strftime("%Y-%m"),
                           "basis": "ledger_estimate_utc_month"}
    if permitted("audit", "default"):
        sink = services.audit_sink
        if isinstance(sink, PostgresAuditSink):
            data["audit"] = sink.recent_operations()
        elif isinstance(sink, InMemoryAuditSink):
            data["audit"] = [{"occurred_at": x.occurred_at, "actor_role": x.actor_role,
                              "action": x.action, "decision": x.decision}
                             for x in reversed(sink.entries())
                             if x.resource_type in {"identity", "config", "source_registry",
                                                    "operations", "publication_workspace"}][:20]
    return data

def build_router(*, services_dep: Callable[..., Any], principal_dep: Callable[..., Any],
                 authorize: Callable[..., None], correlation_id: Callable[[Request], str]) -> APIRouter:
    router = APIRouter(prefix="/ui", tags=["role-dashboards"])

    def response_headers(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"

    @router.get("/session")
    def session(response: Response, p: Principal = Depends(principal_dep),
                services: Any = Depends(services_dep)) -> dict[str, Any]:
        response_headers(response)
        return {"schema_version": "1.0", "role": p.role}

    @router.get("/owner-dashboard")
    def owner(request: Request, response: Response, p: Principal = Depends(principal_dep),
              services: Any = Depends(services_dep)) -> dict[str, Any]:
        _role(p, "owner")
        response_headers(response)
        return {"schema_version": "1.0", "role": "owner",
                "operations": _owner(services, p, authorize, correlation_id(request))}

    @router.put("/owner-dashboard/output-controls")
    def output_control(payload: OutputChange, request: Request, response: Response,
                       p: Principal = Depends(principal_dep),
                       services: Any = Depends(services_dep)) -> dict[str, Any]:
        _role(p, "owner")
        for action in ("read", "update"):
            authorize(services, p, resource_type="config", resource_id=CONFIG,
                      action=action, correlation_id=correlation_id(request))
        old = services.settings.get_latest("output_controls", "controls-main", "global")
        if (old.revision if old is not None else 0) != payload.expected_revision:
            raise APIError(ErrorCode.CONFLICT)
        record = deepcopy(dict(old.value if old is not None else default_off_switches().record))
        group, field = OUTPUT_FIELDS[payload.key]
        record[group][field] = payload.enabled
        record.update(revision=payload.expected_revision + 1, updated_by=p.subject_id,
                      updated_at=datetime.now(timezone.utc).isoformat())
        services.settings.update(contract_name="output_controls", config_id="controls-main",
                                 scope="global", value=record, expected_revision=payload.expected_revision,
                                 actor_id=p.subject_id, actor_role=p.role)
        response_headers(response)
        return {"schema_version": "1.0", "controls": _controls(services, p)}

    @router.get("/editor-dashboard")
    def executive(response: Response, p: Principal = Depends(principal_dep),
                  services: Any = Depends(services_dep)) -> dict[str, Any]:
        _role(p, "editor_in_chief")
        data = _workbench(services, p)
        response_headers(response)
        return {"schema_version": "1.0", "role": "editor_in_chief", **data,
                "metrics_snapshot": None,
                "review_count": sum(t["decision"] == "human_review" for t in data["tasks"])}

    @router.get("/staff-dashboard")
    def staff(response: Response, p: Principal = Depends(principal_dep),
              services: Any = Depends(services_dep)) -> dict[str, Any]:
        _role(p, "staff")
        response_headers(response)
        return {"schema_version": "1.0", "role": "staff", **_workbench(services, p)}

    @router.get("/desk/{desk}")
    def workbench(desk: str, response: Response,
                  stream: Literal["all", "domestic", "foreign", "manual"] = Query("all"),
                  p: Principal = Depends(principal_dep), services: Any = Depends(services_dep)) -> dict[str, Any]:
        _role(p, "staff")
        if desk not in DESKS:
            raise APIError(ErrorCode.NOT_FOUND)
        response_headers(response)
        return {"schema_version": "1.0", "role": "staff", **_workbench(services, p, desk=desk, stream=stream)}

    def store(services: Any, p: Principal) -> SavedViews:
        _role(p, "staff")
        if services.workflow_jobs is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        return SavedViews(services.registry.engine("central"))

    @router.get("/saved-views")
    def views(response: Response, p: Principal = Depends(principal_dep),
              services: Any = Depends(services_dep)) -> dict[str, Any]:
        rows = store(services, p).list(p)
        response_headers(response)
        return {"schema_version": "1.0", "items": [
            r for r in rows if r["filters_json"].get("desk") in DESKS
            and r["filters_json"].get("stream") in STREAMS
            and _allowed(services, p, "desk", r["filters_json"]["desk"], "read")]}

    @router.put("/saved-views")
    def save(payload: SavedViewInput, request: Request, response: Response,
             p: Principal = Depends(principal_dep), services: Any = Depends(services_dep)) -> dict[str, Any]:
        repo = store(services, p)
        if payload.desk not in DESKS:
            raise APIError(ErrorCode.CONTRACT_INVALID)
        authorize(services, p, resource_type="desk", resource_id=payload.desk,
                  action="read", correlation_id=correlation_id(request))
        try:
            row = repo.save(p, payload.name.strip(), payload.desk, payload.stream)
        except ViewLimit:
            raise APIError(ErrorCode.CONFLICT) from None
        except ValueError:
            raise APIError(ErrorCode.CONTRACT_INVALID) from None
        response_headers(response)
        return {"schema_version": "1.0", "saved": row}

    @router.delete("/saved-views/{view_id}")
    def delete(view_id: str, response: Response, p: Principal = Depends(principal_dep),
               services: Any = Depends(services_dep)) -> dict[str, Any]:
        repo = store(services, p)
        if len(view_id) != 36 or not repo.delete(p, view_id):
            raise APIError(ErrorCode.NOT_FOUND)
        response_headers(response)
        return {"schema_version": "1.0", "deleted": True}

    return router
