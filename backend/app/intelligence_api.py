"""Authenticated P15/P16 archive-backed analysis and chat.

Only P26's internal raw-news references are consumed; private reports are not
a retrieval source. No network or AI provider is enabled by this composition.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Literal, Mapping

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.app.dependencies import Services
from backend.core.archive import ArchiveIntegrityError
from backend.core.auth import Principal
from backend.core.errors import APIError, ErrorCode
from backend.core.output_switches import PUBLIC_ANALYSIS
from backend.core.permissions import decide_access
from backend.core.sources_service import SourceNotAllowed, SourceNotFound
from backend.modules.intelligence.analysis import analyze
from backend.modules.intelligence.chat import answer_question
from backend.modules.intelligence.chat_memory import PostgresChatMemory
from backend.modules.intelligence.evidence import Evidence, EvidenceBundle, Period
from backend.modules.intelligence.private_reports import PRIVATE_SLOTS, build_private_report
from backend.modules.intelligence.public_reports import PUBLIC_SLOTS, build_public_report
from backend.modules.intelligence.retrieval import retrieve


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    period_start: datetime
    period_end: datetime
    stream: Literal["domestic", "foreign", "manual"] | None = None
    limit: int = Field(default=12, ge=1, le=20)


class DailyReportInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    period_start: datetime
    period_end: datetime


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=500)
    days: Literal[1, 7, 30]
    stream: Literal["all", "domestic", "foreign"]


def build_router(*, services_dep: Callable[..., Any], principal_dep: Callable[..., Any],
                 authorize: Callable[..., None], correlation_id: Callable[[Request], str]) -> APIRouter:
    router = APIRouter(prefix="/analysis", tags=["analysis"])

    def bundle_for(payload: EvidenceInput, request: Request, principal: Principal,
                   services: Services, action: str, *, all_news: bool = False) -> EvidenceBundle:
        corr = correlation_id(request)
        authorize(services, principal, resource_type="analysis_workspace", resource_id="default",
                  action=action, correlation_id=corr)
        authorize(services, principal, resource_type="archive", resource_id="default",
                  action="search", correlation_id=corr)
        repo = services.news_archive
        if repo is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        try:
            period = Period(payload.period_start, payload.period_end)
            candidates = repo.candidates_in_period(start=period.start, end=period.end, stream=payload.stream)
        except ValueError:
            raise APIError(ErrorCode.CONTRACT_INVALID) from None

        def scope_for(candidate: Mapping[str, Any]) -> str | None:
            if not all(decide_access(principal.subject_id, principal.role, kind, candidate["news_id"],
                                     "read", services.grants)["allowed"] is True
                       for kind in ("archive", "newsroom")):
                return None
            try:
                source = services.sources.require_usable(candidate["source_id"], stream=candidate["stream"])
            except (SourceNotFound, SourceNotAllowed):
                return None
            scope = source["license_scope"]
            return str(scope) if scope in {"metadata", "summary", "full_text"} else None

        def load(candidate: Mapping[str, Any], scope: str) -> Evidence | None:
            # No object is loaded unless its news-level access and current rights are known.
            versions = repo.versions(candidate["news_id"])
            if not versions:
                return None
            latest = versions[0]
            raw = repo.original(candidate["news_id"], latest["artifact_id"])
            if raw is None:
                return None
            if (raw["source_id"] != candidate["source_id"] or raw["stream"] != candidate["stream"]):
                raise ArchiveIntegrityError("source reference mismatch")
            return Evidence.from_raw(raw, latest["artifact_id"], latest["sha256"], allowed_scope=scope)

        try:
            if all_news:
                selected: list[Evidence] = []
                for candidate in candidates:
                    scope = scope_for(candidate)
                    if scope is None:
                        continue
                    item = load(candidate, scope)
                    if item is not None and period.start <= item.collected_at < period.end:
                        selected.append(item)
                    if len(selected) >= payload.limit:
                        break
                result = EvidenceBundle(period, tuple(selected), True)
            else:
                result = retrieve(payload.query, period, candidates=candidates, authorized=scope_for,
                                  load=load, limit=payload.limit, bounded=True)
        except ArchiveIntegrityError:
            raise APIError(ErrorCode.INVALID_STATE) from None
        except ValueError:
            raise APIError(ErrorCode.CONTRACT_INVALID) from None
        # Recheck source rights and audit every returned evidence reference.
        for item in result.evidence:
            for kind in ("archive", "newsroom"):
                authorize(services, principal, resource_type=kind, resource_id=item.news_id,
                          action="read", correlation_id=corr)
            scope = scope_for({"news_id": item.news_id, "source_id": item.source_id, "stream": item.stream})
            if scope is None or (not item.redacted and
                                  {"metadata": 0, "summary": 1, "full_text": 2}[scope]
                                  < {"metadata": 0, "summary": 1, "full_text": 2}[item.scope]):
                raise APIError(ErrorCode.FORBIDDEN)
        return result

    @router.post("/run")
    def run(payload: EvidenceInput, request: Request, response: Response,
            principal: Principal = Depends(principal_dep),
            services: Services = Depends(services_dep)) -> dict[str, Any]:
        bundle = bundle_for(payload, request, principal, services, "run")
        result = analyze(payload.query, bundle)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "mode": "extractive", "result": result,
                "evidence": [item.reference() for item in bundle.evidence], "bounded": bundle.bounded}

    @router.post("/chat")
    def chat(payload: EvidenceInput, request: Request, response: Response,
             principal: Principal = Depends(principal_dep),
             services: Services = Depends(services_dep)) -> dict[str, Any]:
        bundle = bundle_for(payload, request, principal, services, "chat")
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "mode": "extractive", "result": answer_question(payload.query, bundle),
                "evidence": [item.reference() for item in bundle.evidence], "bounded": bundle.bounded}

    def memory_store(services: Services) -> PostgresChatMemory:
        if services.chat_memory is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        return services.chat_memory

    def authorize_memory(request: Request, principal: Principal, services: Services) -> None:
        corr = correlation_id(request)
        authorize(services, principal, resource_type="analysis_workspace", resource_id="default",
                  action="chat", correlation_id=corr)
        authorize(services, principal, resource_type="archive", resource_id="default",
                  action="search", correlation_id=corr)

    @router.get("/chat/memory")
    def read_memory(request: Request, response: Response,
                    principal: Principal = Depends(principal_dep),
                    services: Services = Depends(services_dep)) -> dict[str, Any]:
        authorize_memory(request, principal, services)
        item = memory_store(services).get(principal)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "memory": item.as_record() if item else None}

    @router.put("/chat/memory")
    def save_memory(payload: MemoryInput, request: Request, response: Response,
                    principal: Principal = Depends(principal_dep),
                    services: Services = Depends(services_dep)) -> dict[str, Any]:
        authorize_memory(request, principal, services)
        try:
            item = memory_store(services).save(principal, topic=payload.topic,
                                               days=payload.days, stream=payload.stream)
        except ValueError:
            raise APIError(ErrorCode.CONTRACT_INVALID) from None
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "memory": item.as_record()}

    @router.delete("/chat/memory")
    def clear_memory(request: Request, response: Response,
                     principal: Principal = Depends(principal_dep),
                     services: Services = Depends(services_dep)) -> dict[str, Any]:
        # A revoked chat grant must not prevent self-deletion. The subject
        # comes only from the authenticated principal, never from input.
        services.auditor.record(action="chat_memory.clear", actor_id=principal.subject_id,
                                actor_role=principal.role, resource_type="chat_memory",
                                resource_id="self", decision="self_delete",
                                correlation_id=correlation_id(request))
        memory_store(services).clear(principal)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "memory": None}

    @router.post("/reports/{slot}")
    def report(slot: int, payload: EvidenceInput, request: Request, response: Response,
               principal: Principal = Depends(principal_dep),
               services: Services = Depends(services_dep)) -> dict[str, Any]:
        # Only the four PUBLIC slots are exposed here, never private reports.
        if slot not in PUBLIC_SLOTS:
            raise APIError(ErrorCode.NOT_FOUND)
        authorize(services, principal, resource_type="analysis_workspace", resource_id="default",
                  action="report", correlation_id=correlation_id(request))
        if not services.switches().enabled(PUBLIC_ANALYSIS):
            raise APIError(ErrorCode.INVALID_STATE)
        stream = {1: "domestic", 2: "foreign"}.get(slot)
        if stream is not None and payload.stream not in (None, stream):
            raise APIError(ErrorCode.CONTRACT_INVALID)
        scoped = payload.model_copy(update={"stream": stream}) if stream else payload
        bundle = bundle_for(scoped, request, principal, services, "report")
        switches = services.switches()
        if not switches.enabled(PUBLIC_ANALYSIS):
            raise APIError(ErrorCode.INVALID_STATE)
        result = build_public_report(slot, bundle, switches=switches)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "mode": "bounded_draft", "result": result,
                "evidence": [item.reference() for item in bundle.evidence], "bounded": bundle.bounded}

    def report_access(slot: int, request: Request, principal: Principal, services: Services) -> None:
        corr = correlation_id(request)
        if slot in PRIVATE_SLOTS:
            authorize(services, principal, resource_type="private_report", resource_id=f"slot-{slot}",
                      action="read", correlation_id=corr)
        elif slot not in PUBLIC_SLOTS:
            raise APIError(ErrorCode.NOT_FOUND)
        authorize(services, principal, resource_type="analysis_workspace", resource_id="default",
                  action="report", correlation_id=corr)
        authorize(services, principal, resource_type="archive", resource_id="default",
                  action="search", correlation_id=corr)

    @router.get("/daily-reports")
    def daily_slots(response: Response, request: Request,
                    principal: Principal = Depends(principal_dep),
                    services: Services = Depends(services_dep)) -> dict[str, Any]:
        authorize(services, principal, resource_type="analysis_workspace", resource_id="default",
                  action="report", correlation_id=correlation_id(request))
        slots = [{"slot": i, "title": title, "private": False,
                  "generation_enabled": services.switches().enabled(PUBLIC_ANALYSIS)}
                 for i, (_, title) in PUBLIC_SLOTS.items()]
        slots += [{"slot": i, "title": title, "private": True, "generation_enabled": True}
                  for i, (_, title) in PRIVATE_SLOTS.items()
                  if decide_access(principal.subject_id, principal.role, "private_report", f"slot-{i}",
                                   "read", services.grants, classification="executive_private")["allowed"] is True]
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "slots": slots}

    @router.post("/daily-reports/{slot}")
    def generate_daily(slot: int, payload: DailyReportInput, request: Request, response: Response,
                       principal: Principal = Depends(principal_dep),
                       services: Services = Depends(services_dep)) -> dict[str, Any]:
        report_access(slot, request, principal, services)
        if services.reports is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        if slot in PUBLIC_SLOTS and not services.switches().enabled(PUBLIC_ANALYSIS):
            raise APIError(ErrorCode.INVALID_STATE)
        scoped = EvidenceInput(query="daily", period_start=payload.period_start, period_end=payload.period_end,
                               stream="domestic" if slot == 1 else "foreign" if slot == 2 else None, limit=20)
        bundle = bundle_for(scoped, request, principal, services, "report", all_news=True)
        try:
            if slot in PRIVATE_SLOTS:
                result = build_private_report(slot, bundle, principal=principal, grants=services.grants)
            else:
                result = build_public_report(slot, bundle, switches=services.switches())
            if result is None and slot in PUBLIC_SLOTS and not services.switches().enabled(PUBLIC_ANALYSIS):
                raise APIError(ErrorCode.INVALID_STATE)
            evidence = [item.reference() for item in bundle.evidence
                        if result is not None and item.artifact_id in result["source_refs"]]
            report_access(slot, request, principal, services)
            saved = services.reports.save(principal, slot, result, evidence) if result is not None else None
        except PermissionError:
            raise APIError(ErrorCode.NOT_FOUND) from None
        except ValueError:
            raise APIError(ErrorCode.INVALID_STATE) from None
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "slot": slot, "mode": "bounded_daily_draft",
                "saved": saved, "bounded": True}

    @router.get("/daily-reports/{slot}")
    def read_daily(slot: int, request: Request, response: Response,
                   principal: Principal = Depends(principal_dep),
                   services: Services = Depends(services_dep)) -> dict[str, Any]:
        report_access(slot, request, principal, services)
        if services.reports is None or services.news_archive is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        try:
            saved = services.reports.latest(principal, slot)
            if saved is not None:
                # The stored report contains metadata only. Every underlying
                # source and archive reference must still be readable now.
                for item in saved["evidence"]:
                    for kind in ("archive", "newsroom"):
                        authorize(services, principal, resource_type=kind, resource_id=item["news_id"],
                                  action="read", correlation_id=correlation_id(request))
                    source = services.sources.require_usable(item["source_id"], stream=item["stream"])
                    versions = services.news_archive.versions(item["news_id"])
                    if not any(v["artifact_id"] == item["source_ref"] and v["sha256"] == item["sha256"]
                               for v in versions) or source["license_scope"] not in {"metadata", "summary", "full_text"}:
                        raise APIError(ErrorCode.FORBIDDEN)
                report_access(slot, request, principal, services)
        except (PermissionError, SourceNotAllowed, SourceNotFound):
            raise APIError(ErrorCode.NOT_FOUND) from None
        except ValueError:
            raise APIError(ErrorCode.INVALID_STATE) from None
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "slot": slot, "mode": "bounded_daily_draft",
                "saved": saved, "bounded": True}

    return router
