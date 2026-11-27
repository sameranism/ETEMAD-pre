"""Compose P12/P13 through the shared gateway with trusted server-side rules.

This boundary does not configure a provider or enable external spending.
An operator-approved composition must supply the gateway and scoped rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from backend.core.ai_gateway import Gateway
from backend.core.contracts import validate_payload
from backend.modules.editorial.editor import ArticleDraft, create_draft
from backend.modules.editorial.glossary import EditorialGlossary
from backend.modules.editorial.translator import (
    TranslationDraft,
    canonical,
    checked_source,
    digest,
    translate,
)


@dataclass(frozen=True)
class EditorialProfile:
    ruleset: Mapping[str, Any]
    framework: Mapping[str, Any]

    @property
    def identity(self) -> str:
        return digest(canonical([self.ruleset, self.framework]))


@dataclass(frozen=True)
class GeneratedDraft:
    article: ArticleDraft
    translation: TranslationDraft | None
    mode: str


class EditorialGenerator:
    def __init__(
        self, gateway: Gateway, profiles: Mapping[tuple[str, str], EditorialProfile],
        *, mode: Literal["fixture", "live"],
    ) -> None:
        import json

        if mode not in {"fixture", "live"}:
            raise ValueError("invalid generation mode")
        self._gateway = gateway
        self.mode = mode
        self._profiles: dict[tuple[str, str], EditorialProfile] = {}
        for (stream, desk), profile in profiles.items():
            rules = json.loads(canonical(profile.ruleset))
            frame = json.loads(canonical(profile.framework))
            validate_payload("news_framework", frame)
            EditorialGlossary(rules).preview("", stream=stream, desk=desk, field="body")
            if frame["stream"] != stream or frame["desk"] != desk:
                raise ValueError("editorial profile scope mismatch")
            self._profiles[(stream, desk)] = EditorialProfile(rules, frame)

    def available(self, raw: Mapping[str, Any], desk: str) -> bool:
        tasks = ["editorial"]
        if raw["stream"] == "foreign" and raw["language"] != "fa":
            tasks.append("translation")
        return ((self.mode != "live" or raw["reuse_permission"] == "licensed")
                and self._gateway.enabled and (raw["stream"], desk) in self._profiles
                and all(task in self._gateway.bindings for task in tasks))

    def fingerprint(self, raw: Mapping[str, Any], desk: str) -> str:
        profile = self._profiles[(raw["stream"], desk)]
        return digest(canonical([self.mode, profile.identity, [
            [task, self._gateway.bindings[task].provider, self._gateway.bindings[task].model]
            for task in ("translation", "editorial") if task in self._gateway.bindings
        ]]))

    def generate(self, raw: Mapping[str, Any], *, desk: str) -> GeneratedDraft:
        source = checked_source(raw)
        if not self.available(source, desk):
            raise ValueError("editorial generation unavailable")
        profile = self._profiles[(source["stream"], desk)]
        tr = None
        if source["stream"] == "foreign" and source["language"] != "fa":
            tr = translate(source, gateway=self._gateway, ruleset=profile.ruleset, desk=desk)
        article = create_draft(source, gateway=self._gateway, ruleset=profile.ruleset,
                               framework=profile.framework, translation=tr)
        return GeneratedDraft(article, tr, self.mode)
