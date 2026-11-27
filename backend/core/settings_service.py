"""B01/P02 versioned settings, with an explicit mock revision store.

The persistence boundary must atomically append both a revision and its audit
record. ``InMemoryRevisionStore`` is for local fixtures only; it is not durable.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from backend.core.contracts import ContractError, validate_payload
from backend.core.db import DatabaseRegistry
from backend.core.permissions import GrantLookup, decide_access, require_access
from backend.core.secrets import validate_ref

_SUPPORTED = frozenset(
    {"output_controls", "schedule_config", "ai_models_config", "news_framework"}
)


@dataclass(frozen=True)
class ConfigRevision:
    contract_name: str
    config_id: str
    scope: str
    revision: int
    value: dict[str, Any]
    updated_by: str


@dataclass(frozen=True)
class ConfigAudit:
    contract_name: str
    config_id: str
    scope: str
    revision: int
    actor_id: str
    occurred_at: str
    action: str = "config.update"


class RevisionConflict(Exception):
    """Expected revision did not match authoritative storage."""


class RevisionStore(Protocol):
    def get(self, contract_name: str, config_id: str, scope: str) -> ConfigRevision | None:
        """Return the latest revision without exposing internal mutable state."""

    def compare_and_append(
        self,
        *,
        contract_name: str,
        config_id: str,
        scope: str,
        expected_revision: int,
        value: Mapping[str, Any],
        actor_id: str,
    ) -> ConfigRevision:
        """Atomically compare, append an immutable revision, and append audit."""


class InMemoryRevisionStore:
    """Thread-safe fixture store; never use as production persistence."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._revisions: dict[tuple[str, str, str], list[ConfigRevision]] = {}
        self._audit: list[ConfigAudit] = []

    def get(self, contract_name: str, config_id: str, scope: str) -> ConfigRevision | None:
        key = (contract_name, config_id, scope)
        with self._lock:
            records = self._revisions.get(key, [])
            return deepcopy(records[-1]) if records else None

    def compare_and_append(
        self,
        *,
        contract_name: str,
        config_id: str,
        scope: str,
        expected_revision: int,
        value: Mapping[str, Any],
        actor_id: str,
    ) -> ConfigRevision:
        key = (contract_name, config_id, scope)
        with self._lock:
            records = self._revisions.setdefault(key, [])
            current = records[-1].revision if records else 0
            if current != expected_revision:
                raise RevisionConflict
            revision = current + 1
            record = ConfigRevision(
                contract_name, config_id, scope, revision, deepcopy(dict(value)), actor_id
            )
            audit = ConfigAudit(
                contract_name,
                config_id,
                scope,
                revision,
                actor_id,
                datetime.now(timezone.utc).isoformat(),
            )
            records.append(record)
            self._audit.append(audit)
            return deepcopy(record)

    def audit_entries(self) -> tuple[ConfigAudit, ...]:
        with self._lock:
            return tuple(self._audit)


DOMESTIC_INGEST_CONFIG_ID = "SCH-DOMESTIC-INGEST"
DOMESTIC_INGEST_SCOPE = "domestic"


def _validate_ingest_schedule(value: Mapping[str, Any]) -> None:
    validate_payload("schedule_config", value)
    if (value["config_id"] != DOMESTIC_INGEST_CONFIG_ID
            or value["stream"] != "domestic" or value["desk"] != "all"
            or value["mode"] != "interval" or value["fixed_times_local"]
            or type(value["interval_minutes"]) is not int
            or not 1 <= value["interval_minutes"] <= 1440):
        raise ValueError("invalid domestic ingestion interval configuration")
    try:
        ZoneInfo(value["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid schedule timezone") from exc


class PostgresRevisionStore:
    """P02: authoritative immutable revisions for all supported settings.

    Reuse the existing control.config_revisions table and its audit metadata.
    Keep the original table's (config_id, scope, version_no) identity: a key
    cannot be reassigned to another contract. No credential values are stored.
    """

    def __init__(self, registry: DatabaseRegistry) -> None:
        self._registry = registry

    def _engine(self) -> Engine:
        engine = self._registry.engine("central")
        if engine.dialect.name != "postgresql":
            raise ValueError("settings persistence requires PostgreSQL")
        return engine

    @staticmethod
    def _check_key(contract_name: str, config_id: str, scope: str) -> None:
        if (contract_name not in _SUPPORTED or not 1 <= len(config_id) <= 64
                or not 1 <= len(scope) <= 32):
            raise ValueError("invalid persistent configuration key")

    @staticmethod
    def _latest(conn: Connection, contract_name: str, config_id: str,
                scope: str) -> ConfigRevision | None:
        row = conn.execute(text("""
            SELECT version_no, value_json, changed_by, contract_name
            FROM control.config_revisions
            WHERE config_id = :id AND scope = :scope
            ORDER BY version_no DESC LIMIT 1
        """), {"id": config_id, "scope": scope}).mappings().one_or_none()
        if row is None or row["contract_name"] != contract_name:
            return None
        validate_payload(contract_name, row["value_json"])
        return ConfigRevision(contract_name, config_id, scope, row["version_no"],
                              deepcopy(row["value_json"]), row["changed_by"])

    def get(self, contract_name: str, config_id: str, scope: str) -> ConfigRevision | None:
        self._check_key(contract_name, config_id, scope)
        with self._engine().connect() as conn:
            return self._latest(conn, contract_name, config_id, scope)

    def compare_and_append(
        self, *, contract_name: str, config_id: str, scope: str,
        expected_revision: int, value: Mapping[str, Any], actor_id: str,
    ) -> ConfigRevision:
        self._check_key(contract_name, config_id, scope)
        if not 1 <= len(actor_id) <= 64 or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid persistent configuration revision")
        validate_payload(contract_name, value)
        if config_id == DOMESTIC_INGEST_CONFIG_ID:
            if contract_name != "schedule_config" or scope != DOMESTIC_INGEST_SCOPE:
                raise ValueError("invalid domestic schedule scope")
            _validate_ingest_schedule(value)
        if contract_name == "ai_models_config":
            for task in value["tasks"]:
                validate_ref(task["credential_ref"])
        if contract_name == "output_controls" and (
                value["revision"] != expected_revision + 1 or value["updated_by"] != actor_id):
            raise ValueError("invalid output control revision")
        with self._engine().begin() as conn:
            # Preserve the existing serialization strategy and PK, including
            # competing first revisions using different contract names.
            conn.execute(text("LOCK TABLE control.config_revisions IN SHARE ROW EXCLUSIVE MODE"))
            latest = self._latest(conn, contract_name, config_id, scope)
            if latest is None and conn.execute(text("""
                SELECT EXISTS(SELECT 1 FROM control.config_revisions
                WHERE config_id=:id AND scope=:scope)
            """), {"id": config_id, "scope": scope}).scalar_one():
                raise RevisionConflict
            if (latest.revision if latest else 0) != expected_revision:
                raise RevisionConflict
            revision = expected_revision + 1
            conn.execute(text("""
                INSERT INTO control.config_revisions
                    (config_id, version_no, scope, contract_name, value_json, changed_by, effective_at)
                VALUES (:id, :rev, :scope, :contract, CAST(:value AS json), :actor, now())
            """), {
                "id": config_id, "rev": revision, "scope": scope, "contract": contract_name,
                "value": json.dumps(dict(value), ensure_ascii=False, allow_nan=False),
                "actor": actor_id,
            })
        return ConfigRevision(contract_name, config_id, scope, revision, deepcopy(dict(value)), actor_id)

    def audit_entries(self) -> tuple[ConfigAudit, ...]:
        with self._engine().connect() as conn:
            rows = conn.execute(text("""
                SELECT config_id,scope,contract_name,version_no,changed_by,effective_at
                FROM control.config_revisions ORDER BY effective_at,config_id,scope,version_no
            """)).mappings().all()
        return tuple(ConfigAudit(row["contract_name"], row["config_id"], row["scope"],
                                 row["version_no"], row["changed_by"], row["effective_at"].isoformat())
                     for row in rows)


# Compatibility for existing imports; the selected PostgreSQL store no longer
# falls back to process-local state for output controls or other settings.
PostgresIngestScheduleStore = PostgresRevisionStore


CredentialRefCheck = Callable[[str], bool]


class SettingsService:
    """Authorize and validate a scoped revision before committing it."""

    def __init__(
        self,
        store: RevisionStore,
        grant_lookup: GrantLookup,
        *,
        credential_ref_check: CredentialRefCheck | None = None,
    ) -> None:
        self._store = store
        self._grant_lookup = grant_lookup
        self._credential_ref_check = credential_ref_check

    def get_latest(
        self, contract_name: str, config_id: str, scope: str
    ) -> ConfigRevision | None:
        """Return the latest revision for a supported contract, or None."""
        if contract_name not in _SUPPORTED:
            raise HTTPException(status_code=400, detail="Unknown config contract")
        if not all((contract_name, config_id, scope)):
            raise HTTPException(status_code=400, detail="Invalid config key")
        return self._store.get(contract_name, config_id, scope)

    def update(
        self,
        *,
        contract_name: str,
        config_id: str,
        scope: str,
        value: Mapping[str, Any],
        expected_revision: int,
        actor_id: str,
        actor_role: str,
    ) -> ConfigRevision:
        if (
            not all((contract_name, config_id, scope, actor_id))
            or type(expected_revision) is not int
            or expected_revision < 0
        ):
            raise HTTPException(status_code=400, detail="Invalid config key or revision")
        decision = decide_access(
            actor_id,
            actor_role,
            "config",
            f"{contract_name}:{scope}:{config_id}",
            "update",
            self._grant_lookup,
        )
        require_access(decision)
        if contract_name not in _SUPPORTED:
            raise HTTPException(status_code=400, detail="Unknown config contract")
        try:
            validate_payload(contract_name, value)
        except ContractError as exc:
            raise HTTPException(status_code=400, detail="Invalid config contract") from exc
        if contract_name == "output_controls":
            if value["revision"] != expected_revision + 1 or value["updated_by"] != actor_id:
                raise HTTPException(status_code=400, detail="Invalid output control revision")
        elif contract_name == "schedule_config" and value["config_id"] != config_id:
            raise HTTPException(status_code=400, detail="Config ID mismatch")
        elif contract_name == "ai_models_config":
            if value["configuration_id"] != config_id:
                raise HTTPException(status_code=400, detail="Config ID mismatch")
            refs = [task["credential_ref"] for task in value["tasks"]]
            if refs and (
                self._credential_ref_check is None
                or not all(self._credential_ref_check(ref) for ref in refs)
            ):
                raise HTTPException(status_code=400, detail="Invalid credential reference")
        elif contract_name == "news_framework" and value["framework_id"] != config_id:
            raise HTTPException(status_code=400, detail="Config ID mismatch")
        if config_id == DOMESTIC_INGEST_CONFIG_ID:
            if contract_name != "schedule_config" or scope != DOMESTIC_INGEST_SCOPE:
                raise HTTPException(status_code=400, detail="Invalid domestic schedule scope")
            try:
                _validate_ingest_schedule(value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid domestic interval schedule") from exc
        try:
            return self._store.compare_and_append(
                contract_name=contract_name,
                config_id=config_id,
                scope=scope,
                expected_revision=expected_revision,
                value=value,
                actor_id=actor_id,
            )
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail="Revision conflict") from exc
