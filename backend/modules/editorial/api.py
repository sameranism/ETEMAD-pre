"""Authenticated P10/P11 editorial preview surface with no persistence or publication."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.core.errors import APIError, ErrorCode
from backend.modules.editorial.glossary import EditorialGlossary
from backend.modules.editorial.quality import assess_quality

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

    return router
