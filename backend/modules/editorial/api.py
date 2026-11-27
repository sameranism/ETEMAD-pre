"""Authenticated editorial preview and durable draft workspace; no publication."""
from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.core.ai_gateway import BeforeProviderCall, BudgetExceeded, GatewayDisabled, UnknownTask
from backend.core.chat_provider import ProviderResponseError
from backend.core.errors import APIError, ErrorCode
from backend.core.sources_service import SourceNotAllowed, SourceNotFound
from backend.modules.editorial.glossary import EditorialGlossary
from backend.modules.editorial.image_processor import (
    MAX_IMAGE_BYTES,
    ImageRights,
    ImageUnavailable,
    prepare_image,
)
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


class GenerateDraftInput(BaseModel):
    expected_version: int = Field(ge=0, lt=2**31-1)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    desk: str = Field(min_length=1, max_length=64)


class ManualDraftInput(GenerateDraftInput):
    title: str = Field(min_length=1, max_length=60_000)
    lead: str = Field(min_length=1, max_length=60_000)
    body: str = Field(min_length=1, max_length=60_000)
    short_summary: str = Field(min_length=1, max_length=60_000)
    translated_text: str | None = Field(default=None, max_length=60_000)


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    article_version: int = Field(ge=1, lt=2**31)
    image_data: str = Field(min_length=4, max_length=4*((MAX_IMAGE_BYTES+2)//3))
    alt_text: str = Field(min_length=1, max_length=1000)
    source_ref: str = Field(min_length=1, max_length=1024)
    rights_ref: str | None = Field(default=None, max_length=1024)
    origin: Literal["ai_declared", "non_ai_declared"]
    profiles: list[Literal["landscape", "square", "portrait"]] = Field(min_length=1, max_length=3)
    output_format: Literal["jpg", "png", "webp"] = "webp"
    fit: Literal["contain", "crop"] = "contain"


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
        # For the editable latest view, the concurrency token must come from
        # the exact revision returned to the editor.  A second read here can
        # race with another writer and would let an unseen revision be
        # overwritten. Historical views are read-only, so they may report the
        # current head separately.
        latest = saved if version is None else services.editorial.get(news_id)
        generator = services.editorial_generator
        ai_desks = [desk for desk in desks if generator is not None and generator.available(raw, desk)]
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "source": raw, "source_hash": digest(canonical(raw)),
                "desks": desks, "latest_version": latest["version"] if latest else 0,
                "saved": saved, "history": services.editorial.history(news_id, before=before),
                "mode": "manual", "ai_available": bool(ai_desks), "ai_desks": ai_desks,
                "generation_mode": generator.mode if ai_desks else None}

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

    @router.post("/news/{news_id}/generate")
    def generate_draft(
        news_id: str, payload: GenerateDraftInput, request: Request, response: Response,
        request_id: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        principal: Any = Depends(principal_dep), services: Any = Depends(services_dep),
    ) -> dict[str, Any]:
        _authorize_workspace(services, principal, request, news_id, "write")
        _authorize_workspace(services, principal, request, news_id, "generate")
        raw, desks = _source(services, news_id)
        generator = services.editorial_generator
        if payload.desk not in desks:
            raise APIError(ErrorCode.CONTRACT_INVALID)
        if services.editorial is None or generator is None or not generator.available(raw, payload.desk):
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)

        def revalidate() -> None:
            _authorize_workspace(services, principal, request, news_id, "write")
            _authorize_workspace(services, principal, request, news_id, "generate")
            current, current_desks = _source(services, news_id)
            if digest(canonical(current)) != payload.source_hash or payload.desk not in current_desks:
                raise RevisionConflict("source changed")

        try:
            result = services.editorial.save_generated(
                raw, desk=payload.desk, generator=generator, expected_version=payload.expected_version,
                expected_source_hash=payload.source_hash, request_id=request_id,
                actor_id=principal.subject_id, revalidate=revalidate,
            )
        except RevisionConflict:
            raise APIError(ErrorCode.CONFLICT) from None
        except BudgetExceeded:
            raise APIError(ErrorCode.RATE_LIMITED) from None
        except (BeforeProviderCall, GatewayDisabled, UnknownTask, ProviderResponseError):
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
        except ValueError:
            raise APIError(ErrorCode.CONTRACT_INVALID) from None
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "saved": result}

    def _image_revision(services: Any, news_id: str, version: int) -> dict[str, Any]:
        if services.editorial is None or services.images is None:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        raw, _ = _source(services, news_id)
        saved = services.editorial.get(news_id, version)
        if saved is None:
            raise APIError(ErrorCode.NOT_FOUND)
        previous = saved["source_snapshot"]
        if previous["source_id"] != raw["source_id"] or previous["content_scope"] != raw["content_scope"]:
            raise APIError(ErrorCode.FORBIDDEN)
        return saved

    @router.get("/news/{news_id}/images")
    def list_images(
        news_id: str, request: Request, response: Response,
        version: int = Query(ge=1, lt=2**31),
        principal: Any = Depends(principal_dep), services: Any = Depends(services_dep),
    ) -> dict[str, Any]:
        _authorize_workspace(services, principal, request, news_id, "read")
        _image_revision(services, news_id, version)
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "article_version": version, "items": services.images.list(news_id, version)}

    @router.post("/news/{news_id}/images")
    def create_images(
        news_id: str, payload: ImageInput, request: Request, response: Response,
        principal: Any = Depends(principal_dep), services: Any = Depends(services_dep),
    ) -> dict[str, Any]:
        _authorize_workspace(services, principal, request, news_id, "write")
        _authorize_workspace(services, principal, request, news_id, "prepare_image")
        saved = _image_revision(services, news_id, payload.article_version)
        latest = services.editorial.get(news_id)
        if latest is None or latest["version"] != saved["version"]:
            raise APIError(ErrorCode.CONFLICT)
        try:
            data = base64.b64decode(payload.image_data, validate=True)
            # Client-supplied rights references are claims, not verified legal evidence.
            rights = ImageRights(payload.source_ref, "unknown", payload.rights_ref,
                                 payload.origin == "ai_declared")
            prepared = prepare_image(saved["article"], data, objects=services.objects, rights=rights,
                                     alt_text=payload.alt_text, profiles=tuple(payload.profiles),
                                     output_format=payload.output_format, fit=payload.fit)
            _authorize_workspace(services, principal, request, news_id, "write")
            _authorize_workspace(services, principal, request, news_id, "prepare_image")
            _image_revision(services, news_id, payload.article_version)
            latest = services.editorial.get(news_id)
            if latest is None or latest["version"] != saved["version"]:
                raise APIError(ErrorCode.CONFLICT)
            result = services.images.save(saved["article"], saved["version"], prepared,
                                          actor_id=principal.subject_id)
        except ImageUnavailable:
            raise APIError(ErrorCode.DEPENDENCY_UNAVAILABLE) from None
        except (ValueError, binascii.Error):
            raise APIError(ErrorCode.CONTRACT_INVALID) from None
        response.headers["Cache-Control"] = "no-store"
        return {"schema_version": "1.0", "saved": result}

    @router.get("/news/{news_id}/images/{image_id}/{index}")
    def read_image(
        news_id: str, image_id: str, index: int, request: Request,
        version: int = Query(ge=1, lt=2**31),
        principal: Any = Depends(principal_dep), services: Any = Depends(services_dep),
    ) -> Response:
        _authorize_workspace(services, principal, request, news_id, "read")
        _image_revision(services, news_id, version)
        if not 1 <= len(image_id) <= 64 or not 0 <= index < 3:
            raise APIError(ErrorCode.NOT_FOUND)
        try:
            result = services.images.read(news_id, version, image_id, index)
        except ValueError:
            raise APIError(ErrorCode.INVALID_STATE) from None
        if result is None:
            raise APIError(ErrorCode.NOT_FOUND)
        data, mime = result
        return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store",
                                                                "X-Content-Type-Options": "nosniff"})

    return router
