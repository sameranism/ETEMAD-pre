"""P05/S02-003..007 polling state, backoff and failure accounting.

The scheduler never retries in a tight loop: consecutive failures double the
interval up to one hour, and a successful poll resets it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.engine import Engine, RowMapping

from backend.core.contracts import validate_payload
from backend.core.db import DatabaseRegistry

BASE_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_interval(seconds: int) -> int:
    if type(seconds) is not int or not 60 <= seconds <= 86400:
        raise ValueError("poll interval must be between one minute and one day")
    return seconds


@dataclass(frozen=True)
class DomesticPollPlan:
    """A P02-provided interval schedule for the domestic ingestion worker only."""

    enabled: bool
    interval_seconds: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> DomesticPollPlan:
        validate_payload("schedule_config", record)
        if (record["stream"] != "domestic" or record["desk"] != "all"
                or record["mode"] != "interval" or record["fixed_times_local"]
                or type(record["interval_minutes"]) is not int):
            raise ValueError("domestic ingest requires a domestic all-desk interval schedule")
        try:
            ZoneInfo(record["timezone"])
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid schedule timezone") from exc
        return cls(record["enabled"], _validate_interval(record["interval_minutes"] * 60))


def next_interval_seconds(
    consecutive_failures: int, *, base_interval_seconds: int = BASE_INTERVAL_SECONDS,
) -> int:
    """Back off without ever shortening the configured interval."""
    base = _validate_interval(base_interval_seconds)
    if consecutive_failures <= 0:
        return base
    return min(base * (2 ** min(consecutive_failures, 5)), max(base, MAX_INTERVAL_SECONDS))


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


@dataclass(frozen=True)
class PollClaim:
    source_id: str
    snapshot_id: str
    claim_token: str
    worker_id: str
    claimed_until: str
    source: Mapping[str, Any]


def _snapshot_payload(document: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    record = dict(document)
    validate_payload("source_registry", record)
    snapshot_id = str(record["registry_id"])
    sources = [dict(source) for source in record["sources"]]
    if not snapshot_id or any(
        source["stream"] != "domestic"
        or source["enabled"] is not True
        or source["license_scope"] == "unknown"
        for source in sources
    ):
        raise ValueError("domestic snapshot must contain only enabled rights-cleared sources")
    source_content = json.dumps(
        {"stream": "domestic", "sources": sources},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    expected_suffix = hashlib.sha256(source_content).hexdigest()
    if snapshot_id.rsplit(":", 1)[-1] != expected_suffix:
        raise ValueError("domestic snapshot identifier is not content-addressed")
    canonical = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return snapshot_id, hashlib.sha256(canonical).hexdigest(), sources


class ScheduleStore:
    """Fixture snapshot and lease store; PostgreSQL is authoritative in staging."""

    def __init__(
        self, poll_states: PollStateStore, *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._poll_states = poll_states
        self._clock = clock or _now
        self._snapshot_id: str | None = None
        self._sources: dict[str, dict[str, Any]] = {}
        self._claims: dict[str, tuple[str, datetime]] = {}
        self._schedule_lock = Lock()

    def apply_snapshot(self, document: Mapping[str, Any]) -> str:
        snapshot_id, _digest, sources = _snapshot_payload(document)
        with self._schedule_lock:
            self._snapshot_id = snapshot_id
            self._sources = {str(source["source_id"]): source for source in sources}
            for source_id in self._sources:
                self._poll_states.get_or_create(source_id)
        return snapshot_id

    def claim_due(
        self, *, worker_id: str, limit: int = 10, lease_seconds: int = 120,
    ) -> list[PollClaim]:
        if not worker_id or not 1 <= limit <= 100 or not 30 <= lease_seconds <= 900:
            raise ValueError("invalid poll claim")
        now = self._clock().astimezone(timezone.utc)
        with self._schedule_lock:
            if self._snapshot_id is None:
                return []
            claims: list[PollClaim] = []
            for source_id in sorted(self._sources):
                if len(claims) >= limit:
                    break
                existing = self._claims.get(source_id)
                if existing is not None and existing[1] > now:
                    continue
                state = self._poll_states.get_or_create(source_id)
                if not should_poll(state, now):
                    continue
                token = str(uuid4())
                expiry = now + timedelta(seconds=lease_seconds)
                self._claims[source_id] = (token, expiry)
                claims.append(PollClaim(
                    source_id=source_id,
                    snapshot_id=self._snapshot_id,
                    claim_token=token,
                    worker_id=worker_id,
                    claimed_until=expiry.isoformat(),
                    source=dict(self._sources[source_id]),
                ))
            return claims

    def is_current(self, claim: PollClaim) -> bool:
        now = self._clock().astimezone(timezone.utc)
        with self._schedule_lock:
            existing = self._claims.get(claim.source_id)
            return bool(
                existing is not None and existing[0] == claim.claim_token and existing[1] > now
                and self._snapshot_id == claim.snapshot_id
                and self._sources.get(claim.source_id) == dict(claim.source)
            )

    def release(self, claim: PollClaim) -> None:
        with self._schedule_lock:
            existing = self._claims.get(claim.source_id)
            if existing is None or existing[0] != claim.claim_token:
                raise PollStateConflict("poll claim is no longer current")
            del self._claims[claim.source_id]


class PostgresScheduleStore(ScheduleStore):
    """P05-owned durable source snapshots and crash-safe polling leases."""

    def __init__(self, registry: DatabaseRegistry, poll_states: PostgresPollStateStore) -> None:
        self._registry = registry
        self._poll_states = poll_states

    def _engine(self) -> Engine:
        engine = self._registry.engine("domestic")
        if engine.dialect.name != "postgresql":
            raise ValueError("poll scheduling requires PostgreSQL")
        return engine

    def apply_snapshot(self, document: Mapping[str, Any]) -> str:
        snapshot_id, digest, sources = _snapshot_payload(document)
        payload = json.dumps(dict(document), ensure_ascii=False, sort_keys=True)
        with self._engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO domestic.source_snapshots
                    (snapshot_id, stream, content_hash, payload)
                VALUES (:snapshot_id, 'domestic', :digest, CAST(:payload AS jsonb))
                ON CONFLICT (snapshot_id) DO NOTHING
            """), {"snapshot_id": snapshot_id, "digest": digest, "payload": payload})
            stored = conn.execute(text("""
                SELECT content_hash FROM domestic.source_snapshots
                WHERE snapshot_id = :snapshot_id
            """), {"snapshot_id": snapshot_id}).scalar_one()
            if stored != digest:
                raise ValueError("snapshot identifier conflicts with stored content")
            for source in sources:
                conn.execute(text("""
                    INSERT INTO domestic.source_snapshot_entries
                        (snapshot_id, source_id, config)
                    VALUES (:snapshot_id, :source_id, CAST(:config AS jsonb))
                    ON CONFLICT (snapshot_id, source_id) DO NOTHING
                """), {
                    "snapshot_id": snapshot_id,
                    "source_id": source["source_id"],
                    "config": json.dumps(source, ensure_ascii=False, sort_keys=True),
                })
                conn.execute(text("""
                    INSERT INTO domestic.poll_state (source_id)
                    VALUES (:source_id) ON CONFLICT (source_id) DO NOTHING
                """), {"source_id": source["source_id"]})
            conn.execute(text("""
                INSERT INTO domestic.current_source_snapshot (stream, snapshot_id)
                VALUES ('domestic', :snapshot_id)
                ON CONFLICT (stream) DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id,
                    activated_at = now()
            """), {"snapshot_id": snapshot_id})
        return snapshot_id

    def claim_due(
        self, *, worker_id: str, limit: int = 10, lease_seconds: int = 120,
    ) -> list[PollClaim]:
        if not worker_id or len(worker_id) > 128 or not 1 <= limit <= 100 or not 30 <= lease_seconds <= 900:
            raise ValueError("invalid poll claim")
        with self._engine().begin() as conn:
            rows = conn.execute(text("""
                SELECT p.source_id, current.snapshot_id, entry.config
                FROM domestic.current_source_snapshot AS current
                JOIN domestic.source_snapshot_entries AS entry
                  ON entry.snapshot_id = current.snapshot_id
                JOIN domestic.poll_state AS p ON p.source_id = entry.source_id
                LEFT JOIN domestic.poll_claims AS claim ON claim.source_id = p.source_id
                WHERE current.stream = 'domestic'
                  AND (p.next_poll_at IS NULL OR p.next_poll_at <= now())
                  AND (claim.source_id IS NULL OR claim.claimed_until <= now())
                ORDER BY p.next_poll_at NULLS FIRST, p.source_id
                FOR UPDATE OF p SKIP LOCKED
                LIMIT :limit
            """), {"limit": limit}).mappings().all()
            claims: list[PollClaim] = []
            for row in rows:
                token = str(uuid4())
                expiry = conn.execute(text("""
                    INSERT INTO domestic.poll_claims
                        (source_id, snapshot_id, claim_token, worker_id, claimed_until)
                    VALUES (
                        :source_id, :snapshot_id, :token, :worker_id,
                        now() + make_interval(secs => :lease_seconds)
                    )
                    ON CONFLICT (source_id) DO UPDATE SET
                        snapshot_id = EXCLUDED.snapshot_id,
                        claim_token = EXCLUDED.claim_token,
                        worker_id = EXCLUDED.worker_id,
                        claimed_at = now(),
                        claimed_until = EXCLUDED.claimed_until
                    WHERE domestic.poll_claims.claimed_until <= now()
                    RETURNING claimed_until
                """), {
                    "source_id": row["source_id"],
                    "snapshot_id": row["snapshot_id"],
                    "token": token,
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds,
                }).scalar_one_or_none()
                if expiry is None:
                    continue
                claims.append(PollClaim(
                    source_id=row["source_id"],
                    snapshot_id=row["snapshot_id"],
                    claim_token=token,
                    worker_id=worker_id,
                    claimed_until=expiry.astimezone(timezone.utc).isoformat(),
                    source=dict(row["config"]),
                ))
            return claims

    def is_current(self, claim: PollClaim) -> bool:
        with self._engine().connect() as conn:
            config = conn.execute(text("""
                SELECT entry.config
                FROM domestic.poll_claims AS claim
                JOIN domestic.current_source_snapshot AS current
                  ON current.snapshot_id = claim.snapshot_id AND current.stream = 'domestic'
                JOIN domestic.source_snapshot_entries AS entry
                  ON entry.snapshot_id = claim.snapshot_id AND entry.source_id = claim.source_id
                WHERE claim.source_id = :source_id AND claim.claim_token = :token
                  AND claim.worker_id = :worker_id AND claim.claimed_until > now()
                  AND claim.snapshot_id = :snapshot_id
            """), {
                "source_id": claim.source_id, "token": claim.claim_token,
                "worker_id": claim.worker_id, "snapshot_id": claim.snapshot_id,
            }).scalar_one_or_none()
        return config is not None and config == dict(claim.source)

    def release(self, claim: PollClaim) -> None:
        with self._engine().begin() as conn:
            result = conn.execute(text("""
                DELETE FROM domestic.poll_claims
                WHERE source_id = :source_id AND claim_token = :token
            """), {"source_id": claim.source_id, "token": claim.claim_token})
            if result.rowcount != 1:
                raise PollStateConflict("poll claim is no longer current")


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
    interval_seconds: int = BASE_INTERVAL_SECONDS,
) -> PollState:
    interval = _validate_interval(interval_seconds)
    moment = now or _now()
    state.etag = etag or state.etag
    state.last_modified = last_modified or state.last_modified
    state.last_success_at = moment.isoformat()
    state.last_error = None
    state.consecutive_failures = 0
    state.next_poll_at = (moment + timedelta(seconds=interval)).isoformat()
    return state


def record_failure(
    state: PollState,
    *,
    error: str,
    now: datetime | None = None,
    interval_seconds: int = BASE_INTERVAL_SECONDS,
) -> PollState:
    base = _validate_interval(interval_seconds)
    moment = now or _now()
    state.last_error = error[:200]
    state.consecutive_failures += 1
    interval = next_interval_seconds(state.consecutive_failures, base_interval_seconds=base)
    state.next_poll_at = (moment + timedelta(seconds=interval)).isoformat()
    return state


@dataclass
class PollResult:
    source_id: str
    status: str
    collected: int = 0
    error: str | None = None
    state: Mapping[str, object] = field(default_factory=dict)
