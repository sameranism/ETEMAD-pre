"""S02-011: signed foreign-to-central transfer protocol.

No database connection, network client or credentials are created here.
The sender and receiving application are composed independently. The signed
wire body contains the existing event envelope and its referenced raw record.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from backend.core.contracts import validate_payload
from backend.core.events import make_event

MAX_TRANSFER_BYTES = 3 * 1024 * 1024
MAX_CLOCK_SKEW_SECONDS = 300
EVENT_TYPE = "news.raw.foreign"
_SENDER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_HEX = re.compile(r"[0-9a-f]{64}")


class BridgeError(ValueError):
    """Rejected transfer; messages do not expose payloads or keys."""


@dataclass(frozen=True)
class SignedTransfer:
    sender: str
    sent_at: int
    body: bytes = field(repr=False)
    signature: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedTransfer:
    sender: str
    envelope: dict[str, Any]
    raw: dict[str, Any]
    digest: str


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise BridgeError("invalid transfer JSON") from None


def _raw_record(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = json.loads(_canonical(record))
    validate_payload("raw_news", raw)
    if raw["stream"] != "foreign" or raw.get("source_country") is None:
        raise BridgeError("foreign raw record required")
    # This release transfers metadata or a supplied summary, not full text or
    # content with unknown/blocked rights.
    if raw["content_scope"] == "metadata":
        if raw["original_text"] is not None or raw["reuse_permission"] != "link_only":
            raise BridgeError("metadata record exceeds its rights scope")
    elif raw["content_scope"] != "summary" or raw["reuse_permission"] != "unverified":
        raise BridgeError("unsupported foreign reuse scope")
    return raw


def _envelope(raw: Mapping[str, Any], digest: str) -> dict[str, Any]:
    object_id = str(raw["news_id"])
    identity = hashlib.sha256(("P06:raw:" + object_id).encode("utf-8")).hexdigest()
    return make_event(
        event_type=EVENT_TYPE, producer_project="P06", object_id=object_id,
        correlation_id="CORR-" + identity[:32], event_id="EVT-" + identity,
        idempotency_key="P06:raw:" + identity, classification="internal",
        occurred_at=str(raw["collected_at"]), payload_ref="sha256:" + digest,
    )


def _mac(sender: str, sent_at: int, body: bytes, key: bytes) -> str:
    prefix = f"etemad-foreign-bridge-v1\n{sender}\n{sent_at}\n".encode("ascii")
    return hmac.new(key, prefix + body, hashlib.sha256).hexdigest()


def sign_transfer(raw: Mapping[str, Any], *, sender: str, key: bytes,
                  sent_at: int | None = None) -> SignedTransfer:
    if not _SENDER.fullmatch(sender) or len(key) < 32:
        raise BridgeError("invalid sender configuration")
    record = _raw_record(raw)
    digest = hashlib.sha256(_canonical(record)).hexdigest()
    body = _canonical({"envelope": _envelope(record, digest), "raw": record})
    if len(body) > MAX_TRANSFER_BYTES:
        raise BridgeError("transfer exceeds size limit")
    stamp = int(time.time()) if sent_at is None else sent_at
    if type(stamp) is not int or stamp < 0:
        raise BridgeError("invalid transfer timestamp")
    return SignedTransfer(sender, stamp, body, _mac(sender, stamp, body, key))


class BridgeVerifier:
    """Receiver's explicit service allowlist, separate from staff sessions."""

    def __init__(self, trusted_senders: Mapping[str, bytes]) -> None:
        if not trusted_senders or any(
            not _SENDER.fullmatch(sender) or not isinstance(key, bytes) or len(key) < 32
            for sender, key in trusted_senders.items()
        ):
            raise BridgeError("invalid bridge trust configuration")
        self._keys = dict(trusted_senders)

    def verify(self, transfer: SignedTransfer, *, now: int | None = None) -> VerifiedTransfer:
        clock = int(time.time()) if now is None else now
        key = self._keys.get(transfer.sender)
        if (key is None or type(transfer.sent_at) is not int
                or abs(clock - transfer.sent_at) > MAX_CLOCK_SKEW_SECONDS
                or not 0 < len(transfer.body) <= MAX_TRANSFER_BYTES
                or not _HEX.fullmatch(transfer.signature)):
            raise BridgeError("unauthorized or expired transfer")
        expected = _mac(transfer.sender, transfer.sent_at, transfer.body, key)
        if not hmac.compare_digest(expected, transfer.signature):
            raise BridgeError("invalid transfer signature")
        try:
            body = json.loads(transfer.body)
            if (not isinstance(body, dict) or set(body) != {"envelope", "raw"}
                    or _canonical(body) != transfer.body):
                raise BridgeError("invalid transfer body")
            record = _raw_record(body["raw"])
            envelope = body["envelope"]
            validate_payload("event_envelope", envelope)
            digest = hashlib.sha256(_canonical(record)).hexdigest()
            if envelope != _envelope(record, digest):
                raise BridgeError("event identity or payload reference mismatch")
        except (TypeError, ValueError, UnicodeError, AttributeError):
            raise BridgeError("invalid signed contract") from None
        return VerifiedTransfer(transfer.sender, dict(envelope), record, digest)
