"""Approved, text-only OpenAI-format provider routes; no automatic paid calls.

Official endpoints: AvalAI, DeepSeek, Google Gemini and OpenAI. Routes, model
IDs, token ceilings and price ceilings are explicit operator-supplied values.
Usage-derived cost is conservative (uncached rates); reconcile provider invoices
before production. No tool calls, redirects, proxy inheritance or auto retry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping

import httpx

from backend.core.ai_gateway import BeforeProviderCall
from backend.core.secrets import SecretStore, validate_ref

_ENDPOINTS = {
    "avalai": "https://api.avalai.ir/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
}
_MAX_RESPONSE = 2_000_000
Transport = Callable[[str, str, Mapping[str, Any], float], Mapping[str, Any]]


class ProviderResponseError(RuntimeError):
    """No trustworthy completed text response; request may have been billed."""


@dataclass(frozen=True)
class ChatRoute:
    model: str
    credential_ref: str
    input_price_per_million: float
    output_price_per_million: float
    price_valid_until: datetime
    max_input_tokens: int
    max_output_tokens: int
    output_limit_field: Literal["max_tokens", "max_completion_tokens"]

    def __post_init__(self) -> None:
        validate_ref(self.credential_ref)
        prices = (self.input_price_per_million, self.output_price_per_million)
        if (not self.model.strip() or len(self.model) > 160
                or not all(math.isfinite(x) and x >= 0 for x in prices)
                or self.price_valid_until.tzinfo is None
                or not 512 <= self.max_input_tokens <= 200_000
                or not 1 <= self.max_output_tokens <= 16_384
                or self.output_limit_field not in {"max_tokens", "max_completion_tokens"}):
            raise ValueError("invalid bounded chat route")

    def cost(self, inputs: int, outputs: int) -> float:
        return (inputs * self.input_price_per_million + outputs * self.output_price_per_million) / 1_000_000


def _send(url: str, secret: str, payload: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    import json

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False,
                          transport=httpx.HTTPTransport(retries=0)) as client:
            with client.stream("POST", url, headers={"Authorization": "Bearer " + secret},
                               json=dict(payload)) as response:
                if response.status_code != 200:
                    raise ProviderResponseError("provider did not return a completed response")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE:
                        raise ProviderResponseError("provider response exceeds bound")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("invalid response")
        return data
    except Exception:
        # Never echo HTTP headers, body, original prompt, URL exceptions or key.
        raise ProviderResponseError("provider completion unavailable or unconfirmed") from None


class ChatProvider:
    """Gateway Provider implementation; one instance per approved provider."""

    def __init__(self, name: str, routes: Mapping[str, ChatRoute], secrets: SecretStore,
                 *, authorized: Callable[[], bool] | None = None,
                 transport: Transport = _send) -> None:
        if name not in _ENDPOINTS or not routes or any(k != r.model for k, r in routes.items()):
            raise ValueError("unknown provider or model route")
        if name == "deepseek" and any(r.output_limit_field != "max_tokens" for r in routes.values()):
            raise ValueError("DeepSeek requires max_tokens")
        self.name = name
        self._routes = dict(routes)
        self._secrets = secrets
        self._authorized = authorized or (lambda: False)
        self._transport = transport

    def _route(self, prompt: str, model: str) -> ChatRoute:
        route = self._routes.get(model)
        if (route is None or self._authorized() is not True
                or route.price_valid_until <= datetime.now(timezone.utc)):
            raise BeforeProviderCall("model route, price or authorization unavailable")
        # UTF-8 bytes + framing margin: conservative preflight, not a provider
        # tokenizer or contractual billing guarantee. Reserve the entire ceiling.
        if not prompt.strip() or len(prompt.encode("utf-8")) + 256 > route.max_input_tokens:
            raise BeforeProviderCall("prompt exceeds approved input envelope")
        return route

    def maximum_cost(self, prompt: str, *, model: str) -> float:
        route = self._route(prompt, model)
        return route.cost(route.max_input_tokens, route.max_output_tokens)

    def complete(self, prompt: str, *, model: str, timeout_s: float) -> tuple[str, float]:
        route = self._route(prompt, model)
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 120:
            raise BeforeProviderCall("invalid timeout")
        secret = self._secrets.get(route.credential_ref)
        if not secret or self._authorized() is not True:
            raise BeforeProviderCall("credential or authorization unavailable")
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "stream": False, route.output_limit_field: route.max_output_tokens}
        try:
            result = self._transport(_ENDPOINTS[self.name], secret, payload, timeout_s)
            usage = result["usage"]
            inputs, outputs = usage["prompt_tokens"], usage["completion_tokens"]
            choice = result["choices"][0]
            message = choice["message"]
            answer = message["content"]
            if (type(inputs) is not int or type(outputs) is not int
                    or not 0 <= inputs <= route.max_input_tokens
                    or not 0 <= outputs <= route.max_output_tokens
                    or choice["finish_reason"] != "stop" or not isinstance(answer, str)
                    or not answer.strip() or message.get("tool_calls")):
                raise ValueError("unconfirmed or out-of-envelope result")
            return answer, route.cost(inputs, outputs)
        except Exception:
            raise ProviderResponseError("provider completion unavailable or unconfirmed") from None


@dataclass(frozen=True)
class ModelEvaluation:
    """Recorded results for an identical task and versioned evaluation corpus."""
    provider: str
    model: str
    task: str
    corpus_id: str
    samples: int
    accepted: int
    critical_failures: int
    cost_usd: float
    p95_latency_s: float

    def __post_init__(self) -> None:
        if (self.provider not in _ENDPOINTS or not self.model or not self.task or not self.corpus_id
                or type(self.samples) is not int or type(self.accepted) is not int
                or type(self.critical_failures) is not int
                or not 0 <= self.accepted <= self.samples
                or not 0 <= self.critical_failures <= self.samples - self.accepted
                or not all(math.isfinite(x) and x >= 0 for x in (self.cost_usd, self.p95_latency_s))):
            raise ValueError("invalid evaluation evidence")


def choose_evaluated_model(
    evaluations: list[ModelEvaluation], *, task: str, corpus_id: str,
    approved_routes: set[tuple[str, str]], min_samples: int,
    min_acceptance: float, max_p95_s: float,
) -> ModelEvaluation | None:
    """Choose lowest measured cost per accepted item, subject to quality/SLA.

    No invented scores, network, billing or silent fallback. The caller must
    supply reviewed results measured on the same corpus and approved routes.
    The returned recommendation does not modify production configuration.
    """
    if (not task or not corpus_id or min_samples < 1
            or not math.isfinite(min_acceptance) or not 0 < min_acceptance <= 1
            or not math.isfinite(max_p95_s) or max_p95_s <= 0):
        raise ValueError("invalid evaluation requirements")
    candidates = [
        item for item in evaluations
        if item.task == task and item.corpus_id == corpus_id
        and (item.provider, item.model) in approved_routes
        and item.samples >= min_samples and item.accepted > 0
        and item.accepted / item.samples >= min_acceptance
        and item.critical_failures == 0 and item.p95_latency_s <= max_p95_s
    ]
    return min(candidates, key=lambda x: (x.cost_usd / x.accepted, x.p95_latency_s,
                                          x.provider, x.model), default=None)
