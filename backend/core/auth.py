"""B01/P28 accounts, password hashing and signed session tokens.

Design constraints from ADR-005:
- individual first-use credentials, mandatory reset, no default or shared password;
- hashes only at rest (scrypt, per-user salt);
- signed, expiring session tokens; no server-side session table needed in phase 1.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Mapping
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.db import DatabaseRegistry

ROLES = frozenset({"owner", "editor_in_chief", "staff"})
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_SALT_BYTES = 16
_TOKEN_VERSION = "v1"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
MAX_ACTIVE_ACCOUNTS = 10


class AuthenticationError(Exception):
    """Generic authentication failure; the message never reveals which check failed."""


class SessionInvalid(AuthenticationError):
    """Token missing, malformed, tampered or expired."""


def generate_first_use_credential(length: int = 24) -> str:
    """High-entropy one-time credential; force a reset at first login."""
    return secrets.token_urlsafe(length)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return ``scrypt$n$r$p$salt$hash``; never store the plaintext."""
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError("password length outside allowed bounds")
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, _b64(salt), _b64(derived)
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check; a malformed hash is a failed login, not a crash."""
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt" or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return False
        if len(password) > MAX_PASSWORD_LENGTH or len(_unb64(salt_b64)) != _SALT_BYTES or len(_unb64(hash_b64)) != 32:
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(hash_b64)),
        )
        return hmac.compare_digest(derived, _unb64(hash_b64))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Principal:
    subject_id: str
    role: str
    must_reset_password: bool = False
    session_marker: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role: {self.role!r}")


def _sign(key: bytes, body: bytes) -> str:
    return _b64(hmac.new(key, body, hashlib.sha256).digest())


def issue_session(principal: Principal, key: bytes, *, ttl_seconds: int = 3600) -> str:
    """Create a signed token: ``v1.<payload>.<signature>``."""
    if not key:
        raise ValueError("session key must not be empty")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    payload = {
        "sub": principal.subject_id,
        "role": principal.role,
        "reset": principal.must_reset_password,
        "exp": int(time.time()) + ttl_seconds,
        "marker": principal.session_marker,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{_TOKEN_VERSION}.{body}.{_sign(key, body.encode('ascii'))}"


def verify_session(token: str, key: bytes, *, now: int | None = None) -> Principal:
    """Validate signature and expiry; raise ``SessionInvalid`` otherwise."""
    try:
        version, body, signature = token.split(".")
    except (AttributeError, ValueError) as exc:
        raise SessionInvalid("malformed token") from exc
    if version != _TOKEN_VERSION or not body or not signature:
        raise SessionInvalid("unsupported token")
    expected = _sign(key, body.encode("ascii"))
    if not hmac.compare_digest(signature, expected):
        raise SessionInvalid("bad signature")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError) as exc:
        raise SessionInvalid("malformed payload") from exc
    if not isinstance(payload, Mapping):
        raise SessionInvalid("malformed payload")
    if int(payload.get("exp", 0)) <= (int(time.time()) if now is None else now):
        raise SessionInvalid("expired token")
    try:
        return Principal(
            subject_id=str(payload["sub"]),
            role=str(payload["role"]),
            must_reset_password=bool(payload.get("reset", False)),
            session_marker=payload.get("marker") if isinstance(payload.get("marker"), str) else None,
        )
    except (KeyError, ValueError) as exc:
        raise SessionInvalid("invalid claims") from exc


def bearer_token(authorization_header: str | None) -> str | None:
    """Extract a bearer token from an Authorization header; None when absent."""
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip() or None


class AccountLimitReached(RuntimeError):
    """The newsroom's active human-account limit has been reached."""


class PostgresAccountStore:
    """P28-owned identity.users storage. No credential values are logged.

    Privileged account login remains disabled until MFA is implemented.
    Provisioning is an explicit trusted operator action, not an HTTP endpoint.
    """

    def __init__(self, registry: DatabaseRegistry, marker_key: bytes) -> None:
        self._registry = registry
        if len(marker_key) < 32:
            raise ValueError("identity signing key must have at least 32 bytes")
        self._marker_key = marker_key

    def _principal(self, user_id: str, role: str, reset: bool, encoded: str) -> Principal:
        marker = hmac.new(self._marker_key, b"account-marker\\0" + encoded.encode(), hashlib.sha256).hexdigest()
        return Principal(user_id, role, reset, marker)

    def _engine(self) -> Engine:
        engine = self._registry.engine("central")
        if engine.dialect.name != "postgresql":
            raise ValueError("account storage requires PostgreSQL")
        return engine

    def consume_attempt(self) -> bool:
        """Shared fixed-window throttle; all attempts count, no account oracle."""
        with self._engine().begin() as conn:
            return conn.execute(text("""
                INSERT INTO identity.login_throttle (bucket, window_started, attempts)
                VALUES (1, now(), 1)
                ON CONFLICT (bucket) DO UPDATE SET
                    window_started = CASE WHEN identity.login_throttle.window_started
                        <= now() - interval '1 minute' THEN now()
                        ELSE identity.login_throttle.window_started END,
                    attempts = CASE WHEN identity.login_throttle.window_started
                        <= now() - interval '1 minute' THEN 1
                        ELSE identity.login_throttle.attempts + 1 END
                WHERE identity.login_throttle.window_started <= now() - interval '1 minute'
                    OR identity.login_throttle.attempts < 30
                RETURNING bucket
            """)).scalar_one_or_none() is not None

    def provision(self, username: str, role: str) -> tuple[str, str]:
        if not username or username != username.strip() or len(username) > 64 or role not in ROLES:
            raise ValueError("invalid account")
        credential = generate_first_use_credential()
        user_id = str(uuid4())
        encoded = hash_password(credential)
        with self._engine().begin() as conn:
            conn.execute(text("LOCK TABLE identity.users IN SHARE ROW EXCLUSIVE MODE"))
            count = conn.execute(text(
                "SELECT count(*) FROM identity.users WHERE is_active"
            )).scalar_one()
            if count >= MAX_ACTIVE_ACCOUNTS:
                raise AccountLimitReached("active account limit")
            conn.execute(text("""
                INSERT INTO identity.users
                    (user_id, username, role_id, password_hash, must_reset, is_active, created_at)
                VALUES (:id, :username, :role, :encoded, true, true, now())
            """), {"id": user_id, "username": username, "role": role, "encoded": encoded})
        return user_id, credential

    def authenticate(self, username: str, password: str) -> Principal:
        with self._engine().connect() as conn:
            row = conn.execute(text("""
                SELECT user_id, role_id, password_hash, must_reset, is_active
                FROM identity.users WHERE username = :username
            """), {"username": username}).mappings().one_or_none()
        if row is None or not row["is_active"] or row["role_id"] != "staff":
            # The same memory-hard operation is performed for missing/disabled
            # accounts and privileged roles; do not reveal account existence.
            verify_password(password, _DUMMY_PASSWORD_HASH)
            raise AuthenticationError("invalid credentials")
        if not verify_password(password, row["password_hash"]):
            raise AuthenticationError("invalid credentials")
        return self._principal(row["user_id"], row["role_id"], row["must_reset"], row["password_hash"])

    def current(self, user_id: str) -> Principal:
        with self._engine().connect() as conn:
            row = conn.execute(text("""
                SELECT role_id, must_reset, password_hash FROM identity.users
                WHERE user_id = :id AND is_active
            """), {"id": user_id}).mappings().one_or_none()
        if row is None or row["role_id"] != "staff":
            raise AuthenticationError("invalid credentials")
        return self._principal(user_id, row["role_id"], row["must_reset"], row["password_hash"])

    def reset_first_use(self, username: str, temporary: str, replacement: str) -> Principal:
        if temporary == replacement:
            raise ValueError("replacement must differ from initial credential")
        encoded = hash_password(replacement)
        with self._engine().begin() as conn:
            row = conn.execute(text("""
                SELECT user_id, role_id, password_hash, must_reset, is_active
                FROM identity.users WHERE username = :username FOR UPDATE
            """), {"username": username}).mappings().one_or_none()
            if row is None or not row["is_active"] or row["role_id"] != "staff" or not row["must_reset"]:
                verify_password(temporary, _DUMMY_PASSWORD_HASH)
                raise AuthenticationError("invalid credentials")
            if not verify_password(temporary, row["password_hash"]):
                raise AuthenticationError("invalid credentials")
            conn.execute(text("""
                UPDATE identity.users SET password_hash = :encoded, must_reset = false
                WHERE user_id = :id
            """), {"encoded": encoded, "id": row["user_id"]})
            return self._principal(row["user_id"], "staff", False, encoded)


# Non-account random value: never a usable account credential.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))
