"""Central receiving boundary for signed foreign events.

The sender calls a receiver interface, never a central database. The receiving
application owns its inbox and checks the current P03 source approval again.
A receipt and original raw record commit atomically before acknowledging.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.modules.foreign.bridge import BridgeError, BridgeVerifier, SignedTransfer


@dataclass(frozen=True)
class BridgeReceipt:
    receipt_id: str
    duplicate: bool


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


class ForeignInbox:
    def __init__(self, central_engine: Engine, verifier: BridgeVerifier,
                 source_lookup: Callable[[str], Mapping[str, Any]]) -> None:
        if central_engine.dialect.name != "postgresql":
            raise ValueError("inbox requires central PostgreSQL")
        self._engine = central_engine
        self._verifier = verifier
        self._source = source_lookup

    def _require_source(self, raw: Mapping[str, Any]) -> None:
        if len(raw["news_id"]) > 64 or len(raw["source_id"]) > 64:
            raise BridgeError("invalid foreign identity")
        try:
            source = self._source(raw["source_id"])
        except KeyError:
            raise BridgeError("source not currently approved") from None
        if (source.get("source_id") != raw["source_id"]
                or source.get("enabled") is not True or source.get("stream") != "foreign"
                or source.get("license_scope") not in {"metadata", "summary", "full_text"}
                or source.get("country") != raw["source_country"]
                or source.get("language") != raw["language"]
                or (source["license_scope"] == "metadata" and raw["content_scope"] != "metadata")):
            raise BridgeError("source not currently approved")

    def receive(self, transfer: SignedTransfer, *, now: int | None = None) -> BridgeReceipt:
        verified = self._verifier.verify(transfer, now=now)
        raw, event = verified.raw, verified.envelope
        self._require_source(raw)
        params = {"news_id": raw["news_id"], "event_id": event["event_id"],
                  "idem": event["idempotency_key"], "sender": verified.sender,
                  "digest": verified.digest, "envelope": _json(event), "raw": _json(raw)}
        with self._engine.begin() as conn:
            inserted = conn.execute(text("""
                INSERT INTO processing.foreign_inbox
                    (news_id,event_id,idempotency_key,sender,payload_hash,envelope,raw)
                VALUES (:news_id,:event_id,:idem,:sender,:digest,CAST(:envelope AS jsonb),CAST(:raw AS jsonb))
                ON CONFLICT DO NOTHING RETURNING event_id
            """), params).scalar_one_or_none()
            if inserted is None:
                existing = conn.execute(text("""
                    SELECT event_id,idempotency_key,sender,payload_hash FROM processing.foreign_inbox
                    WHERE news_id=:news_id
                """), params).mappings().one_or_none()
                if existing is None or any(existing[k] != v for k, v in (
                    ("event_id", params["event_id"]), ("idempotency_key", params["idem"]),
                    ("sender", params["sender"]), ("payload_hash", params["digest"]),
                )):
                    raise BridgeError("conflicting foreign replay")
            return BridgeReceipt(event["event_id"], inserted is None)

    def process_one(self, consume: Callable[[dict[str, Any]], None]) -> bool:
        """Process oldest pending receipt under a row lock; errors keep it pending.

        Consumer must be idempotent by raw news identity, including after a crash
        between the processing commit and the inbox acknowledgment.
        """
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                SELECT news_id,raw FROM processing.foreign_inbox
                WHERE processed_at IS NULL ORDER BY received_at,news_id
                LIMIT 1 FOR UPDATE SKIP LOCKED
            """)).mappings().one_or_none()
            if row is None:
                return False
            raw = dict(row["raw"])
            self._require_source(raw)
            consume(raw)
            conn.execute(text("""
                UPDATE processing.foreign_inbox SET processed_at=now()
                WHERE news_id=:news_id
            """), {"news_id": row["news_id"]})
            return True
