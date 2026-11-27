"""Authenticated editorial preview and durable draft workspace; no publication."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from backend.core.errors import APIError, ErrorCode
from backend.core.sources_service import SourceNotAllowed, SourceNotFound
from backend.modules.editorial.glossary import EditorialGlossary
from backend.modules.editorial.quality import assess_quality
from backend.modules.editorial.repository import FIELDS, RevisionConflict
from backend.modules.editorial.translator import canonical, checked_source, digest

Authorize = Callable[..., None]


class EditorialPreviewInput(BaseModel):
    ruleset: dict[str, Any]
    source_text: str = Field(min_length=1, max_length=100_000)
    draft_text: str = Field(min_length=1, max_length=100_000)
    stream: Literal["domestic", "foreign", "mixed", "manual"]
    desk: str = Field(min_length=1, max_length=64)
    field: Literal["body", "lead", "title", "headline"]
    source_refs: list[str] = Field(min_length=1, max_length=20)
    attributed_refs: list[str] = Field(default_factory=list, max_length=20)
    required_names: list[str] = Field(default_factory=list, max_length=50)
    claim_evidence: dict[str, list[str]] = Field(default_factory=dict, max_length=50)


class ManualDraftInput(BaseModel):
    expected_version: int = Field(ge=0, lt=2**31-1)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    desk: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=60_000)
    lead: str = Field(min_length=1, max_length=60_000)
    body: str = Field(min_length=1, max_length=60_000)
    short_summary: str = Field(min_length=1, max_length=60_000)
    translated_text: str | None = Field(default=None, max_length=60_000)


def _source(services: Any, news_id: str) -> tuple[dict[str, Any], list[str]]:
    item = services.pipeline.item(news_id)
    if item is None:
        raise APIError(ErrorCode.NOT_FOUND)
    raw = {k: v for k, v in item["news"].items()
           if k not in {"normalized_text", "fingerprint", "normalization_version"}}
    try:
        raw = checked_source(raw)
        registered = services.sources.require_usable(raw["source_id"], stream=raw["stream"])
        scopes = {"metadata": 0, "summary": 1, "full_text": 2}
        if (registered["license_scope"] not in scopes
                or scopes[registered["license_scope"]] < scopes[raw["content_scope"]]):
            raise APIError(ErrorCode.FORBIDDEN)
    except (SourceNotFound, SourceNotAllowed, ValueError):
        raise APIError(ErrorCode.FORBIDDEN) from None
    desks = list(dict.fromkeys(item["classification"]["desks"]))
    return raw, desks


def build_router(
    *,
    services_dep: Callable[..., Any],
    principal_dep: Callable[..., Any],
    authorize: Authorize,
    correlation_id: Callable[[Request], str],
) -> APIRouter:
    """Build a router that delegates access decisions to the application boundary."""
    router = APIRouter(prefix="/editorial", tags=["editorial"])

    @router.post("/rules/preview")
    def preview_rules(
        payload: EditorialPreviewInput,
        request: Request,
        principal: Any = Depends(principal_dep),
        services: Any = Depends(services_dep),
    ) -> dict[str, Any]:
        authorize(
            services,
            principal,
            resource_type="editorial_workspace",
            resource_id="default",
            action="preview",
            correlation_id=correlation_id(request),
        )
        if any(not ref.strip() for ref in payload.source_refs + payload.attributed_refs):
            raise APIError(ErrorCode.CONTRACT_INVALID)
        if any(len(ref) > 128 for ref in payload.source_refs + payload.attributed_refs):
            raise APIError(ErrorCode.CONTRACT_INVALID)
        if not set(payload.attributed_refs).issubset(payload.source_refs):
            raise APIError(ErrorCode.CONTRACT_INVALID)
        if any(not name.strip() or len(name) > 256 for name in payload.required_names):
            raise APIError(ErrorCode.CONTRACT_INVALID)
        if any(
            not claim.strip()
            or len(claim) > 1000
            or len(evidence) > 20
            or any(not item.strip() or len(item) > 1000 for item in evidence)
            for claim, evidence in payload.claim_evidence.items()
        ):
            raise APIError(ErrorCode.CONTRACT_INVALID)
        try:
            glossary = EditorialGlossary(payload.ruleset)
            preview = glossary.preview(
                payload.draft_text,
                stream=payload.stream,
                desk=payload.desk,
                field=payload.field,
            )
            quality = assess_quality(
                payload.source_text,
                preview.proposed,
                source_refs=payload.source_refs,
                attributed_refs=payload.attributed_refs,
                required_names=payload.required_names,
                claim_evidence=payload.claim_evidence,
            )
        except (TypeError, ValueError) as exc:
            raise APIError(ErrorCode.CONTRACT_INVALID) from exc
        return {
            "schema_version": "1.0",
            "rules_version": glossary.version,
            "original": preview.original,
            "proposed": preview.proposed,
            "replacements": [asdict(item) for item in preview.replacements],
            "quality": asdict(quality),
        }

    def _authorize_workspace(services: Any, principal: Any, request: Request,
                             news_id: str, action: str) -> None:
        if not 1 <= len(news_id) <= 64:
            raise APIError(ErrorCode.NOT_FOUND)
        corr = correlation_id(request)
        authorize(services, principal, resource_type="newsroom", resource_id=news_id,
                  action="read", correlation_id=corr)
        authorize(services, principal, resource_type="editorial_workspace", resource_id=news_id,
                  action=action, correlation_id=corr)

    @router.get("/news/{news_id}/draft")
    def get_draft(
        news_id: str, request: Request, response: Response,
        version: int | None = Query(default=None, ge=1, lt=2**31),
        before: int | None = Query(default=None, ge=1, lt=2**31),
        principal: Any = Depends(principal_dep), services: Any = Depends(services_dep),
    ) -> dict[str, Any]:
        _authorize_workspace(services, principal, request, news_id, "read")
        if services.editorial is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        raw, desks = _source(services, news_id)
        saved = services.editorial.get(news_id, version)
        if version is not None and saved is None:
            raise APIError(ErrorCode.NOT_FOUND)
        # Rechecking current rights also protects historical source snapshots.
        if saved is not None:
            previous = saved["source_snapshot"]
            if previous["source_id"] != raw["source_id"] or previous["content_scope"] != raw["content_scope"]:
                raise APIError(ErrorCode.FORBIDDEN)
        latest = services.editorial.get(news_id)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "source": raw, "source_hash": digest(canonical(raw)),
                "desks": desks, "latest_version": latest["version"] if latest else 0,
                "saved": saved, "history": services.editorial.history(news_id, before=before),
                "mode": "manual", "ai_available": False}

    @router.put("/news/{news_id}/draft")
    def put_draft(
        news_id: str, payload: ManualDraftInput, request: Request, response: Response,
        request_id: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        principal: Any = Depends(principal_dep), services: Any = Depends(services_dep),
    ) -> dict[str, Any]:
        _authorize_workspace(services, principal, request, news_id, "write")
        if services.editorial is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        raw, desks = _source(services, news_id)
        if payload.desk not in desks:
            raise APIError(ErrorCode.CONTRACT_INVALID)
        try:
            result = services.editorial.save_manual(
                raw, desk=payload.desk, fields={k: getattr(payload, k) for k in FIELDS},
                translated_text=payload.translated_text, expected_version=payload.expected_version,
                expected_source_hash=payload.source_hash, request_id=request_id,
                actor_id=principal.subject_id,
            )
        except RevisionConflict:
            raise APIError(ErrorCode.CONFLICT) from None
        except ValueError:
            raise APIError(ErrorCode.CONTRACT_INVALID) from None
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "saved": result}

    return router
