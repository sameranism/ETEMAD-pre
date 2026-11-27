"""P25 adapter boundary. This initial implementation supports fixtures only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol


@dataclass(frozen=True)
class Delivery:
    status: Literal["success", "failed", "unknown", "skipped"]
    message_id: str | None = None
    error_code: str | None = None


class PublicationAdapter(Protocol):
    adapter_id: str
    platform: str
    mode: str
    max_text_chars: int

    def validate_config(self, config: Mapping[str, Any]) -> None: ...
    def dry_run(self, payload: Mapping[str, Any]) -> None: ...
    def publish(self, payload: Mapping[str, Any], *, idempotency_key: str) -> Delivery: ...
    def status(self, idempotency_key: str) -> Delivery | None: ...


class FixtureAdapter:
    """Explicit synthetic adapter; never opens a network connection."""

    mode = "fixture"

    def __init__(self, adapter_id: str, platform: str, *, max_text_chars: int = 1000,
                 outcome: Literal["success", "failed", "unknown"] = "success") -> None:
        self.adapter_id = adapter_id
        self.platform = platform
        self.max_text_chars = max_text_chars
        self.outcome = outcome
        self.calls: dict[str, Delivery] = {}

    def validate_config(self, config: Mapping[str, Any]) -> None:
        if config["platform"] != self.platform:
            raise ValueError("adapter platform mismatch")

    def dry_run(self, payload: Mapping[str, Any]) -> None:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > self.max_text_chars:
            raise ValueError("text exceeds adapter profile")

    def publish(self, payload: Mapping[str, Any], *, idempotency_key: str) -> Delivery:
        self.dry_run(payload)
        if idempotency_key not in self.calls:
            self.calls[idempotency_key] = (
                Delivery("success", "fixture-" + idempotency_key[:24])
                if self.outcome == "success" else Delivery(self.outcome, error_code="FIXTURE_" + self.outcome.upper())
            )
        return self.calls[idempotency_key]

    def status(self, idempotency_key: str) -> Delivery | None:
        return self.calls.get(idempotency_key)
