"""P28 durable TOTP authentication. No raw bearer or TOTP secret in PostgreSQL.

Factor enrollment/recovery is a trusted operator operation using an independently
provisioned secret reference; no public enrollment or recovery bypass.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from backend.core.auth import AuthenticationError, PostgresAccountStore, Principal
from backend.core.secrets import SecretStore, validate_ref

PRIVILEGED = frozenset({"owner", "editor_in_chief"})
LOGIN_TTL = 600
PRIVATE_TTL = 300
CHALLENGE_TTL = 120
_CODE = re.compile(r"[0-9]{6}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class MFARateLimited(AuthenticationError):
    """Bounded MFA attempts exceeded."""


def totp(secret: bytes, step: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff
    return f"{value % 1000000:06d}"


def matching_step(secret: bytes, code: str, now: int, last_step: int) -> int | None:
    if not _CODE.fullmatch(code):
        return None
    step = now // 30
    matches = [
        candidate for candidate in (step - 1, step, step + 1)
        if candidate >= 0 and hmac.compare_digest(totp(secret, candidate), code)
        and candidate > last_step
    ]
    return max(matches) if matches else None


def _digest(token: str, prefix: str) -> str:
    if not token.startswith(prefix) or not _TOKEN.fullmatch(token[len(prefix):]):
        raise AuthenticationError("invalid authentication")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _token(prefix: str) -> tuple[str, str]:
    token = prefix + secrets.token_urlsafe(32)
    return token, _digest(token, prefix)


class PostgresMFA:
    def __init__(self, engine: Engine, accounts: PostgresAccountStore, store: SecretStore):
        self.engine = engine
        self.accounts = accounts
        self.store = store

    def _secret(self, ref: str) -> bytes:
        try:
            value = self.store.get(ref)
            if value is None or len(value) > 128:
                raise ValueError
            key = base64.b32decode(value.upper(), casefold=True)
            if not 20 <= len(key) <= 64:
                raise ValueError
            return key
        except (ValueError, TypeError):
            raise AuthenticationError("invalid authentication") from None

    def _factor(self, conn: Connection, user: str) -> Any:
        factor = conn.execute(text("""
            SELECT * FROM identity.mfa_factors WHERE user_id=:u FOR UPDATE
        """), {"u": user}).mappings().one_or_none()
        if factor is None:
            raise AuthenticationError("invalid authentication")
        return factor

    def _consume(self, bucket: str, limit: int) -> None:
        with self.engine.begin() as conn:
            allowed = conn.execute(text("""
                INSERT INTO identity.mfa_throttle(bucket, window_started, attempts)
                VALUES (:b, now(), 1)
                ON CONFLICT (bucket) DO UPDATE SET
                    window_started=CASE WHEN identity.mfa_throttle.window_started
                        <= now()-interval '1 minute' THEN now()
                        ELSE identity.mfa_throttle.window_started END,
                    attempts=CASE WHEN identity.mfa_throttle.window_started
                        <= now()-interval '1 minute' THEN 1 ELSE identity.mfa_throttle.attempts+1 END
                WHERE identity.mfa_throttle.window_started <= now()-interval '1 minute'
                      OR identity.mfa_throttle.attempts < :lim
                RETURNING bucket
            """), {"b": bucket, "lim": limit}).scalar_one_or_none()
        if allowed is None:
            raise MFARateLimited("invalid authentication")

    def enroll(self, user_id: str, ref: str, code: str, *, operator_id: str) -> None:
        """Trusted operator only. Confirm possession before activating new factor."""
        principal = self.accounts.current(user_id)
        if principal.role not in PRIVILEGED:
            raise AuthenticationError("invalid authentication")
        if not operator_id or len(operator_id) > 64:
            raise ValueError("trusted operator identity required")
        validate_ref(ref)
        self._consume("global", 30)
        self._consume("user:" + user_id, 5)
        key = self._secret(ref)
        step = matching_step(key, code, int(time.time()), -1)
        if step is None:
            raise AuthenticationError("invalid authentication")
        with self.engine.begin() as conn:
            conn.execute(text("SELECT user_id FROM identity.users WHERE user_id=:u FOR NO KEY UPDATE"),
                         {"u": user_id})
            previous = conn.execute(text("""
                SELECT credential_ref FROM identity.mfa_factors WHERE user_id=:u
            """), {"u": user_id}).scalar_one_or_none()
            if previous == ref:
                raise AuthenticationError("use a new factor reference")
            if previous is not None:
                try:
                    old_key = self._secret(previous)
                except AuthenticationError:
                    old_key = None
                if old_key is not None and hmac.compare_digest(key, old_key):
                    raise AuthenticationError("use a new factor")
            conn.execute(text("""
                INSERT INTO identity.mfa_factors(user_id, credential_ref, generation, confirmed, last_step)
                VALUES (:u, :r, :g, true, :s)
                ON CONFLICT(user_id) DO UPDATE SET credential_ref=:r, generation=:g,
                    confirmed=true, last_step=:s
            """), {"u": user_id, "r": ref, "g": uuid4().hex, "s": step})
            self._revoke_all(conn, user_id)
            self._operator_audit(conn, operator_id, user_id, "auth.mfa.enroll")

    @staticmethod
    def _operator_audit(conn: Connection, operator_id: str, user_id: str, action: str) -> None:
        conn.execute(text("""
            INSERT INTO audit.audit_entries
              (audit_id,occurred_at,actor_id,actor_role,action,resource_type,resource_id,
               decision,correlation_id,details_json)
            VALUES (:id,now(),:actor,'operator',:action,'identity',:subject,
                    'ALLOWED',:correlation,CAST('{}' AS json))
        """), {"id": uuid4().hex, "actor": operator_id, "action": action,
               "subject": user_id, "correlation": uuid4().hex})

    @staticmethod
    def _revoke_all(conn: Connection, user_id: str) -> None:
        conn.execute(text("""
            UPDATE identity.privileged_sessions SET revoked_at=now()
            WHERE user_id=:u AND revoked_at IS NULL
        """), {"u": user_id})
        conn.execute(text("""
            UPDATE identity.auth_challenges SET consumed_at=now()
            WHERE user_id=:u AND consumed_at IS NULL
        """), {"u": user_id})

    def revoke_factor(self, user_id: str, *, operator_id: str) -> None:
        """Trusted operator recovery: invalidate, never fall back to password."""
        if not operator_id or len(operator_id) > 64:
            raise ValueError("trusted operator identity required")
        with self.engine.begin() as conn:
            self._factor(conn, user_id)
            conn.execute(text("""
                UPDATE identity.mfa_factors SET confirmed=false, generation=:g WHERE user_id=:u
            """), {"u": user_id, "g": uuid4().hex})
            self._revoke_all(conn, user_id)
            self._operator_audit(conn, operator_id, user_id, "auth.mfa.revoke")

    def start(self, principal: Principal, parent: str | None = None) -> str:
        if (principal.role not in PRIVILEGED or principal.must_reset_password
                or not principal.session_marker or self.accounts.current(principal.subject_id) != principal):
            raise AuthenticationError("invalid authentication")
        parent_hash = _digest(parent, "ps.") if parent else None
        if parent is not None and (principal.role != "editor_in_chief" or self.current(parent) != principal):
            raise AuthenticationError("invalid authentication")
        token, digest = _token("mc.")
        with self.engine.begin() as conn:
            factor = self._factor(conn, principal.subject_id)
            if not factor["confirmed"]:
                raise AuthenticationError("invalid authentication")
            conn.execute(text("""
                DELETE FROM identity.auth_challenges WHERE user_id=:u AND expires_at <= now()
            """), {"u": principal.subject_id})
            conn.execute(text("""
                UPDATE identity.auth_challenges SET consumed_at=now()
                WHERE user_id=:u AND purpose=:p AND consumed_at IS NULL
            """), {"u": principal.subject_id, "p": "p19" if parent else "login"})
            conn.execute(text("""
                INSERT INTO identity.auth_challenges
                (token_hash,user_id,marker,generation,purpose,parent_hash,expires_at)
                VALUES (:h,:u,:m,:g,:p,:parent,:e)
            """), {"h": digest, "u": principal.subject_id, "m": principal.session_marker,
                   "g": factor["generation"], "p": "p19" if parent else "login",
                   "parent": parent_hash,
                   "e": datetime.now(timezone.utc) + timedelta(seconds=CHALLENGE_TTL)})
        return token

    def finish(self, challenge: str, code: str, parent: str | None = None) -> tuple[str, int, Principal]:
        self._consume("global", 30)
        digest = _digest(challenge, "mc.")
        with self.engine.connect() as conn:
            user = conn.execute(text("""
                SELECT user_id FROM identity.auth_challenges WHERE token_hash=:h
            """), {"h": digest}).scalar_one_or_none()
        if user is None:
            raise AuthenticationError("invalid authentication")
        self._consume("user:" + user, 5)
        principal = self.accounts.current(user)
        if principal.role not in PRIVILEGED or principal.must_reset_password:
            raise AuthenticationError("invalid authentication")
        parent_hash = _digest(parent, "ps.") if parent else None
        if parent is not None and (principal.role != "editor_in_chief" or self.current(parent) != principal):
            raise AuthenticationError("invalid authentication")
        purpose = "p19" if parent else "login"
        ttl = PRIVATE_TTL if parent else LOGIN_TTL
        token, session_hash = _token("p19." if parent else "ps.")
        with self.engine.begin() as conn:
            factor = self._factor(conn, user)
            row = conn.execute(text("""
                SELECT * FROM identity.auth_challenges
                WHERE token_hash=:h AND consumed_at IS NULL AND expires_at > now() FOR UPDATE
            """), {"h": digest}).mappings().one_or_none()
            if (row is None or not factor["confirmed"] or row["generation"] != factor["generation"]
                    or row["marker"] != principal.session_marker or row["purpose"] != purpose
                    or row["parent_hash"] != parent_hash):
                raise AuthenticationError("invalid authentication")
            step = matching_step(self._secret(factor["credential_ref"]), code, int(time.time()),
                                 factor["last_step"])
            if step is None:
                raise AuthenticationError("invalid authentication")
            conn.execute(text("UPDATE identity.mfa_factors SET last_step=:s WHERE user_id=:u"),
                         {"s": step, "u": user})
            conn.execute(text("UPDATE identity.auth_challenges SET consumed_at=now() WHERE token_hash=:h"),
                         {"h": digest})
            conn.execute(text("""
                DELETE FROM identity.privileged_sessions WHERE user_id=:u AND expires_at <= now()
            """), {"u": user})
            conn.execute(text("""
                INSERT INTO identity.privileged_sessions
                (token_hash,user_id,marker,generation,purpose,parent_hash,expires_at)
                VALUES (:h,:u,:m,:g,:p,:parent,:e)
            """), {"h": session_hash, "u": user, "m": principal.session_marker,
                   "g": factor["generation"], "p": purpose, "parent": parent_hash,
                   "e": datetime.now(timezone.utc) + timedelta(seconds=ttl)})
        return token, ttl, principal

    def current(self, token: str, parent: str | None = None) -> Principal:
        digest = _digest(token, "p19." if parent else "ps.")
        parent_hash = _digest(parent, "ps.") if parent else None
        with self.engine.connect() as conn:
            row = conn.execute(text("""
                SELECT s.user_id,s.marker FROM identity.privileged_sessions s
                JOIN identity.mfa_factors f ON f.user_id=s.user_id AND f.generation=s.generation
                WHERE s.token_hash=:h AND s.purpose=:p AND s.revoked_at IS NULL
                  AND s.expires_at > now() AND f.confirmed
                  AND s.parent_hash IS NOT DISTINCT FROM :parent
            """), {"h": digest, "p": "p19" if parent else "login",
                   "parent": parent_hash}).mappings().one_or_none()
        if row is None:
            raise AuthenticationError("invalid authentication")
        principal = self.accounts.current(row["user_id"])
        if (principal.role not in PRIVILEGED or principal.must_reset_password
                or principal.session_marker != row["marker"]):
            raise AuthenticationError("invalid authentication")
        if parent and (principal.role != "editor_in_chief" or self.current(parent) != principal):
            raise AuthenticationError("invalid authentication")
        return principal

    def revoke(self, token: str) -> None:
        digest = _digest(token, "ps.")
        with self.engine.begin() as conn:
            conn.execute(text("""
                UPDATE identity.privileged_sessions SET revoked_at=now()
                WHERE (token_hash=:h OR parent_hash=:h) AND revoked_at IS NULL
            """), {"h": digest})
