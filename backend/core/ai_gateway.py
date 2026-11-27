"""B01/P19 AI gateway: one doorway to every model provider (ADR-006).

Feature code never imports a provider SDK. The gateway enforces the configured
binding, a budget ceiling and provenance, and it is **off by default**: without
an explicit enable plus a configured binding it makes no external call.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Literal, Mapping, Protocol
from uuid import uuid4


class GatewayDisabled(RuntimeError):
    """No external model call is allowed in this process."""


class BeforeProviderCall(RuntimeError):
    """No external request was attempted; reservation can be released."""


class BudgetExceeded(RuntimeError):
    """The per-run or monthly budget would be exceeded."""


class UnknownTask(RuntimeError):
    """No binding exists for the requested task."""


@dataclass(frozen=True)
class ModelBinding:
    task: str
    provider: str
    model: str
    timeout_s: float = 30.0
    run_budget_usd: float = 0.50
    daily_budget_usd: float | None = None


@dataclass(frozen=True)
class AiResult:
    text: str
    task: str
    provider: str
    model: str
    prompt_sha256: str
    request_id: str
    occurred_at: str
    cost_usd: float

    def provenance(self) -> dict[str, object]:
        """What gets stored next to a product record; no prompt, no secret."""
        return {
            "task": self.task,
            "provider": self.provider,
            "model": self.model,
            "prompt_sha256": self.prompt_sha256,
            "request_id": self.request_id,
            "occurred_at": self.occurred_at,
            "cost_usd": round(self.cost_usd, 6),
        }


class Provider(Protocol):
    name: str

    def complete(self, prompt: str, *, model: str, timeout_s: float) -> tuple[str, float]:
        """Return (text, cost_usd); implementations must not log the prompt."""


class CostLedger(Protocol):
    def reserve(self, request_id: str, binding: ModelBinding, cost_usd: float,
                *, monthly_budget_usd: float) -> None: ...

    def settle(self, request_id: str, state: Literal["confirmed", "unknown", "released"],
               *, cost_usd: float | None = None) -> None: ...

    def committed_usd(self) -> float: ...


@dataclass
class FakeProvider:
    """Deterministic fixture provider; performs no network I/O."""

    name: str = "fake"
    reply: str = "fixture-output"
    cost_per_call: float = 0.001

    def maximum_cost(self, prompt: str, *, model: str) -> float:
        return self.cost_per_call

    def complete(self, prompt: str, *, model: str, timeout_s: float) -> tuple[str, float]:
        return f"{self.reply}:{model}:{len(prompt)}", self.cost_per_call


@dataclass
class Gateway:
    bindings: Mapping[str, ModelBinding]
    providers: Mapping[str, Provider]
    enabled: bool = False
    monthly_budget_usd: float = 25.0
    cost_ledger: CostLedger | None = None
    _spent_usd: float = field(default=0.0, init=False, repr=False)
    _reserved_usd: float = field(default=0.0, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        for binding in self.bindings.values():
            if binding.provider not in self.providers:
                raise ValueError(f"binding for {binding.task!r} names unknown provider {binding.provider!r}")

    @property
    def spent_usd(self) -> float:
        if self.cost_ledger is not None:
            return self.cost_ledger.committed_usd()
        with self._lock:
            return round(self._spent_usd, 6)

    def call(self, task: str, prompt: str) -> AiResult:
        if not self.enabled:
            raise GatewayDisabled("AI gateway is disabled; no external call is permitted")
        binding = self.bindings.get(task)
        if binding is None:
            raise UnknownTask(f"no model binding for task {task!r}")
        if not prompt or len(prompt) > 200_000:
            raise ValueError("prompt must be non-empty and bounded")
        provider = self.providers[binding.provider]
        # Older fixture adapters retain the existing Provider interface. Live
        # routes supply an explicit maximum_cost based on approved token limits.
        maximum_cost = getattr(provider, "maximum_cost", None)
        reserve = maximum_cost(prompt, model=binding.model) if callable(maximum_cost) else binding.run_budget_usd
        if (not math.isfinite(reserve) or reserve < 0
                or not math.isfinite(binding.run_budget_usd) or binding.run_budget_usd < reserve
                or not math.isfinite(self.monthly_budget_usd) or self.monthly_budget_usd < 0):
            raise BudgetExceeded("run cost exceeds its binding budget")
        request_id = str(uuid4())
        if self.cost_ledger is not None:
            # The reservation must commit before the external call. If the
            # database is unavailable, the request must not reach the provider.
            self.cost_ledger.reserve(request_id, binding, reserve, monthly_budget_usd=self.monthly_budget_usd)
            try:
                text, cost = provider.complete(prompt, model=binding.model, timeout_s=binding.timeout_s)
                if not math.isfinite(cost) or cost < 0 or cost > reserve + 1e-12:
                    raise BudgetExceeded("provider returned an unconfirmed cost")
            except BeforeProviderCall:
                self.cost_ledger.settle(request_id, "released")
                raise
            except BaseException:
                self.cost_ledger.settle(request_id, "unknown")
                raise
            # If settlement fails, the committed reservation survives; never
            # return an apparently successful result without durable evidence.
            self.cost_ledger.settle(request_id, "confirmed", cost_usd=cost)
            return self._result(binding, prompt, text, cost, request_id)
        with self._lock:
            if self._spent_usd + self._reserved_usd + reserve > self.monthly_budget_usd + 1e-12:
                raise BudgetExceeded("monthly AI budget is exhausted")
            self._reserved_usd += reserve
        try:
            text, cost = provider.complete(prompt, model=binding.model, timeout_s=binding.timeout_s)
            if not math.isfinite(cost) or cost < 0 or cost > reserve + 1e-12:
                raise BudgetExceeded("provider returned an unconfirmed cost")
        except BeforeProviderCall:
            with self._lock:
                self._reserved_usd -= reserve
            raise
        except BaseException:
            # Unknown delivery may still be billed: retain the conservative
            # charge and never silently retry or fall back.
            with self._lock:
                self._reserved_usd -= reserve
                self._spent_usd += reserve
            raise
        with self._lock:
            self._reserved_usd -= reserve
            self._spent_usd += cost
        return self._result(binding, prompt, text, cost, request_id)

    @staticmethod
    def _result(binding: ModelBinding, prompt: str, text: str, cost: float, request_id: str) -> AiResult:
        return AiResult(
            text=text,
            task=binding.task,
            provider=binding.provider,
            model=binding.model,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            request_id=request_id,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            cost_usd=cost,
        )
