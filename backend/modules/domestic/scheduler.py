"""P05/S02-003..007 polling state, backoff and failure accounting.

The scheduler never retries in a tight loop: consecutive failures double the
interval up to one hour, and a successful poll resets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine, RowMapping

from backend.core.db import DatabaseRegistry

BASE_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_interval_seconds(consecutive_failures: int) -> int:
    """Exponential backoff, capped; zero failures means the base interval."""
    if consecutive_failures <= 0:
        return BASE_INTERVAL_SECONDS
    return min(BASE_INTERVAL_SECONDS * (2 ** min(consecutive_failures, 5)), MAX_INTERVAL_SECONDS)


@dataclass
class PollState:
    source_id: str
    etag: str | None = None
    last_modified: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    next_poll_at: str | None = None
    _persisted: dict[str, object] | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "next_poll_at": self.next_poll_at,
        }


class PollStateStore:
    """Fixture store; production keeps ``domestic.poll_state`` in PostgreSQL."""

    def __init__(self) -> None:
        self._states: dict[str, PollState] = {}
        self._lock = Lock()

    def get_or_create(self, source_id: str) -> PollState:
        with self._lock:
            state = self._states.get(source_id)
            if state is None:
                # A new source is immediately eligible; the first completed poll
                # establishes its next scheduled time.
                state = PollState(source_id=source_id)
                self._states[source_id] = state
            return state

    def save(self, state: PollState) -> PollState:
        with self._lock:
            self._states[state.source_id] = state
            return state

    def all(self) -> list[dict[str, object]]:
        with self._lock:
            return [state.as_dict() for state in self._states.values()]


class PollStateConflict(RuntimeError):
    """The persisted state changed after this poll loaded its snapshot."""


def _utc_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("poll timestamps require a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _validated_state(state: PollState) -> dict[str, object]:
    values = state.as_dict()
    if not isinstance(state.source_id, str) or not 1 <= len(state.source_id) <= 64:
        raise ValueError("invalid source identifier")
    if type(state.consecutive_failures) is not int or state.consecutive_failures < 0:
        raise ValueError("invalid consecutive failure count")
    for value, maximum in (
        (state.etag, 256), (state.last_modified, 128), (state.last_error, 200),
    ):
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError("invalid poll state field")
    values["last_success_at"] = _utc_timestamp(state.last_success_at)
    values["next_poll_at"] = _utc_timestamp(state.next_poll_at)
    return values


def _from_row(row: RowMapping) -> PollState:
    state = PollState(
        source_id=row["source_id"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        last_success_at=row["last_success_at"].isoformat() if row["last_success_at"] else None,
        last_error=row["last_error"],
        consecutive_failures=row["consecutive_failures"],
        next_poll_at=row["next_poll_at"].isoformat() if row["next_poll_at"] else None,
    )
    state.last_success_at = _utc_timestamp(state.last_success_at)
    state.next_poll_at = _utc_timestamp(state.next_poll_at)
    state._persisted = state.as_dict()
    return state


class PostgresPollStateStore(PollStateStore):
    """Durable P05 state; stale workers cannot overwrite a newer poll result.

    Only the existing domestic table is used. Network requests do not hold
    database transactions open. Concurrent writers use compare-and-swap.
    """

    def __init__(self, registry: DatabaseRegistry) -> None:
        self._registry = registry

    def _engine(self) -> Engine:
        engine = self._registry.engine("domestic")
        if engine.dialect.name != "postgresql":
            raise ValueError("poll state requires PostgreSQL")
        return engine

    def get_or_create(self, source_id: str) -> PollState:
        _validated_state(PollState(source_id=source_id))
        with self._engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO domestic.poll_state (source_id)
                VALUES (:source_id) ON CONFLICT (source_id) DO NOTHING
            """), {"source_id": source_id})
            row = conn.execute(text(_SELECT_STATE + " WHERE source_id = :source_id"),
                               {"source_id": source_id}).mappings().one()
            return _from_row(row)

    def save(self, state: PollState) -> PollState:
        params = _validated_state(state)
        baseline = state._persisted
        if baseline is None or baseline["source_id"] != state.source_id:
            raise ValueError("poll state must be loaded before saving")
        params.update({f"old_{key}": value for key, value in baseline.items()})
        with self._engine().begin() as conn:
            row = conn.execute(text("""
                UPDATE domestic.poll_state SET
                    etag = :etag,
                    last_modified = :last_modified,
                    last_success_at = CAST(:last_success_at AS timestamptz),
                    last_error = :last_error,
                    consecutive_failures = :consecutive_failures,
                    next_poll_at = CAST(:next_poll_at AS timestamptz)
                WHERE source_id = :source_id
                    AND etag IS NOT DISTINCT FROM :old_etag
                    AND last_modified IS NOT DISTINCT FROM :old_last_modified
                    AND last_success_at IS NOT DISTINCT FROM CAST(:old_last_success_at AS timestamptz)
                    AND last_error IS NOT DISTINCT FROM :old_last_error
                    AND consecutive_failures = :old_consecutive_failures
                    AND next_poll_at IS NOT DISTINCT FROM CAST(:old_next_poll_at AS timestamptz)
                RETURNING source_id, etag, last_modified, last_success_at,
                    last_error, consecutive_failures, next_poll_at
            """), params).mappings().one_or_none()
            if row is None:
                raise PollStateConflict("stale poll state; reload before retry")
            persisted = _from_row(row)
        state.last_success_at = persisted.last_success_at
        state.next_poll_at = persisted.next_poll_at
        state._persisted = persisted._persisted
        return state

    def all(self) -> list[dict[str, object]]:
        with self._engine().connect() as conn:
            return [_from_row(row).as_dict() for row in conn.execute(
                text(_SELECT_STATE + " ORDER BY source_id")
            ).mappings()]


_SELECT_STATE = """
SELECT source_id, etag, last_modified, last_success_at, last_error,
       consecutive_failures, next_poll_at
FROM domestic.poll_state
"""


def should_poll(state: PollState, now: datetime | None = None) -> bool:
    """True when the next poll time has arrived (never raises on bad timestamps)."""
    if not state.next_poll_at:
        return True
    try:
        due = datetime.fromisoformat(state.next_poll_at)
    except ValueError:
        return True
    current = now or _now()
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return current >= due


def record_success(
    state: PollState,
    *,
    etag: str | None,
    last_modified: str | None,
    now: datetime | None = None,
) -> PollState:
    moment = now or _now()
    state.etag = etag or state.etag
    state.last_modified = last_modified or state.last_modified
    state.last_success_at = moment.isoformat()
    state.last_error = None
    state.consecutive_failures = 0
    state.next_poll_at = (moment + timedelta(seconds=BASE_INTERVAL_SECONDS)).isoformat()
    return state


def record_failure(
    state: PollState,
    *,
    error: str,
    now: datetime | None = None,
) -> PollState:
    moment = now or _now()
    state.last_error = error[:200]
    state.consecutive_failures += 1
    interval = next_interval_seconds(state.consecutive_failures)
    state.next_poll_at = (moment + timedelta(seconds=interval)).isoformat()
    return state


@dataclass
class PollResult:
    source_id: str
    status: str
    collected: int = 0
    error: str | None = None
    state: Mapping[str, object] = field(default_factory=dict)
