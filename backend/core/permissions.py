"""B01/P28 server-side access decision; no role implies a broad grant."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.db import DatabaseRegistry

Role = str
GrantLookup = Callable[[str, Role, str, str, str], bool]
_ROLES = frozenset({"owner", "editor_in_chief", "staff"})


@dataclass(frozen=True)
class Grant:
    principal_id: str
    role: str
    resource_type: str
    resource_id: str
    action: str


class InMemoryGrantStore:
    """Fixture grant store; phase 1 replaces it with authoritative P28 storage.

    Grants are explicit and additive: absence of a row is a deny.
    """

    def __init__(self, grants: list[Grant] | None = None) -> None:
        self._lock = Lock()
        self._grants: set[Grant] = set(grants or [])

    def grant(self, grant: Grant) -> None:
        with self._lock:
            self._grants.add(grant)

    def revoke(self, grant: Grant) -> None:
        with self._lock:
            self._grants.discard(grant)

    def lookup(
        self,
        principal_id: str,
        role: Role,
        resource_type: str,
        resource_id: str,
        action: str,
    ) -> bool:
        with self._lock:
            return Grant(principal_id, role, resource_type, resource_id, action) in self._grants

    def __call__(
        self,
        principal_id: str,
        role: Role,
        resource_type: str,
        resource_id: str,
        action: str,
    ) -> bool:
        return self.lookup(principal_id, role, resource_type, resource_id, action)


class PostgresGrantStore(InMemoryGrantStore):
    """Authoritative grants in P28's central schema, with explicit revocation."""

    def __init__(self, registry: DatabaseRegistry) -> None:
        self._registry = registry

    def _engine(self) -> Engine:
        engine = self._registry.engine("central")
        if engine.dialect.name != "postgresql":
            raise ValueError("grant storage requires PostgreSQL")
        return engine

    @staticmethod
    def _params(grant: Grant) -> dict[str, str]:
        return {
            "principal_id": grant.principal_id, "role": grant.role,
            "resource_type": grant.resource_type, "resource_id": grant.resource_id,
            "action": grant.action,
        }

    def grant(self, grant: Grant) -> None:
        if grant.role not in _ROLES or not all(self._params(grant).values()):
            raise ValueError("invalid grant")
        with self._engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO identity.grants
                    (grant_id, principal_id, role, resource_type, resource_id, action, granted_at)
                VALUES (:id, :principal_id, :role, :resource_type, :resource_id, :action, now())
                ON CONFLICT (principal_id, role, resource_type, resource_id, action)
                DO UPDATE SET revoked_at = NULL, granted_at = now()
            """), {**self._params(grant), "id": str(uuid4())})

    def revoke(self, grant: Grant) -> None:
        with self._engine().begin() as conn:
            conn.execute(text("""
                UPDATE identity.grants SET revoked_at = now()
                WHERE principal_id = :principal_id AND role = :role
                  AND resource_type = :resource_type AND resource_id = :resource_id
                  AND action = :action AND revoked_at IS NULL
            """), self._params(grant))

    def lookup(
        self, principal_id: str, role: Role, resource_type: str, resource_id: str, action: str,
    ) -> bool:
        with self._engine().connect() as conn:
            return bool(conn.execute(text("""
                SELECT EXISTS (
                    SELECT 1 FROM identity.grants g
                    JOIN identity.users u ON u.user_id = g.principal_id AND u.role_id = g.role
                    WHERE g.principal_id = :principal_id AND g.role = :role
                      AND g.resource_type = :resource_type AND g.resource_id = :resource_id
                      AND g.action = :action AND g.revoked_at IS NULL
                      AND u.is_active AND NOT u.must_reset
                )
            """), self._params(Grant(principal_id, role, resource_type, resource_id, action))).scalar_one())


def decide_access(
    principal_id: str,
    role: Role,
    resource_type: str,
    resource_id: str,
    action: str,
    grant_lookup: GrantLookup,
    *,
    classification: str = "internal",
    request_id: str | None = None,
) -> dict[str, object]:
    """Return the access_decision v1.0 shape.

    The caller must obtain role and classification from trusted server state,
    never request parameters. Grant lookup must use authoritative P28 storage.
    """
    allowed = False
    reason = "DENY_DEFAULT"
    if not all((principal_id, resource_type, resource_id, action)) or role not in _ROLES:
        reason = "INVALID_CONTEXT"
    elif classification == "executive_private" or resource_type == "private_report":
        if role != "editor_in_chief" or action != "read" or resource_type != "private_report":
            reason = "EXECUTIVE_PRIVATE"
        elif grant_lookup(principal_id, role, resource_type, resource_id, action):
            allowed, reason = True, "ROLE_ALLOWED"
        else:
            reason = "GRANT_REQUIRED"
    elif grant_lookup(principal_id, role, resource_type, resource_id, action):
        allowed, reason = True, "ROLE_ALLOWED"

    return {
        "schema_version": "1.0",
        "request_id": request_id or str(uuid4()),
        "principal_id": principal_id or "anonymous",
        "role": role or "unknown",
        "resource_type": resource_type or "unknown",
        "resource_id": resource_id or "unknown",
        "action": action or "unknown",
        "allowed": allowed,
        "reason_code": reason,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


def require_access(decision: dict[str, object]) -> None:
    """Reject a direct API request; conceal private object existence."""
    if decision.get("allowed") is True:
        return
    private = decision.get("resource_type") == "private_report"
    raise HTTPException(status_code=404 if private else 403, detail="Access denied")
