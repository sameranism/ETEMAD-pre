"""PostgreSQL-backed UTC budget ledger for the shared AI gateway.

All reservations serialize under one database transaction lock. A crash or
uncertain request keeps its reserved amount charged. No prompt, response,
credential or user information is stored. Charges are estimates, not invoices.
"""
from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.ai_gateway import BudgetExceeded, ModelBinding

# Dedicated fixed transaction-lock namespace for the central AI budget.
_LOCK = 1846271309
_SCALE = Decimal(1_000_000)


def _units(value: float, *, ceiling: bool) -> int:
    if not math.isfinite(value) or not 0 <= value <= 1_000_000_000:
        raise ValueError("invalid cost amount")
    return int((Decimal(str(value)) * _SCALE).to_integral_value(
        rounding=ROUND_CEILING if ceiling else ROUND_FLOOR))


class PostgresCostLedger:
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("cost ledger requires PostgreSQL")
        self._engine = engine.execution_options(isolation_level="READ COMMITTED")

    def reserve(self, request_id: str, binding: ModelBinding, cost_usd: float,
                *, monthly_budget_usd: float) -> None:
        if str(UUID(request_id)) != request_id:
            raise ValueError("invalid request identity")
        if binding.daily_budget_usd is None:
            raise BudgetExceeded("daily task budget must be explicit")
        requested = _units(cost_usd, ceiling=True)
        monthly = _units(monthly_budget_usd, ceiling=False)
        daily = _units(binding.daily_budget_usd, ceiling=False)
        run = _units(binding.run_budget_usd, ceiling=False)
        if requested > run:
            raise BudgetExceeded("run cost exceeds budget")
        with self._engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": _LOCK})
            today = conn.execute(text("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date")).scalar_one()
            row = conn.execute(text("""
                SELECT COALESCE(SUM(charged_microusd),0) AS month_total,
                       COALESCE(SUM(charged_microusd) FILTER
                           (WHERE budget_day=:day AND task=:task),0) AS day_total
                FROM control.ai_cost_reservations
                WHERE budget_day >= :month_start AND budget_day <= :day
            """), {"day": today, "month_start": today.replace(day=1), "task": binding.task}).one()
            if row.month_total + requested > monthly or row.day_total + requested > daily:
                raise BudgetExceeded("daily or monthly AI budget exhausted")
            conn.execute(text("""
                INSERT INTO control.ai_cost_reservations
                    (request_id,task,provider,model,budget_day,reserved_microusd,charged_microusd,state)
                VALUES (:id,:task,:provider,:model,:day,:amount,:amount,'reserved')
            """), {"id": request_id, "task": binding.task, "provider": binding.provider,
                   "model": binding.model, "day": today, "amount": requested})

    def settle(self, request_id: str, state: Literal["confirmed", "unknown", "released"],
               *, cost_usd: float | None = None) -> None:
        if state not in {"confirmed", "unknown", "released"} or (state == "confirmed") != (cost_usd is not None):
            raise ValueError("invalid settlement")
        amount = _units(cost_usd, ceiling=True) if cost_usd is not None else 0
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                SELECT state,reserved_microusd,charged_microusd
                FROM control.ai_cost_reservations WHERE request_id=:id FOR UPDATE
            """), {"id": request_id}).one()
            if state == "unknown":
                amount = row.reserved_microusd
            if amount > row.reserved_microusd:
                raise BudgetExceeded("reported cost exceeds reservation")
            if row.state != "reserved":
                if row.state == state and row.charged_microusd == amount:
                    return
                raise ValueError("conflicting cost settlement")
            conn.execute(text("""
                UPDATE control.ai_cost_reservations
                SET charged_microusd=:amount,state=:state,settled_at=clock_timestamp()
                WHERE request_id=:id
            """), {"amount": amount, "state": state, "id": request_id})

    def committed_usd(self) -> float:
        with self._engine.connect() as conn:
            amount = conn.execute(text("""
                SELECT COALESCE(SUM(charged_microusd),0)
                FROM control.ai_cost_reservations
                WHERE budget_day >= date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC')::date
                  AND budget_day <= (clock_timestamp() AT TIME ZONE 'UTC')::date
            """)).scalar_one()
        return float(amount / _SCALE)
