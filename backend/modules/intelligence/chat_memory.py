"""P16 opt-in durable topic context; no stored archive text, answers or citations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.core.auth import Principal


@dataclass(frozen=True)
class TopicMemory:
    topic: str
    days: int
    stream: str
    updated_at: datetime

    def as_record(self) -> dict[str, Any]:
        return {"topic": self.topic, "days": self.days, "stream": self.stream,
                "updated_at": self.updated_at.isoformat()}


class PostgresChatMemory:
    """One explicit saved topic per authenticated subject and role.

    Role changes do not expose topics saved under a previous role. The repository
    has no arbitrary-subject lookup and returns no cached retrieval evidence.
    """

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("chat memory requires PostgreSQL")
        self._engine = engine

    def get(self, principal: Principal) -> TopicMemory | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT topic, days, stream, updated_at FROM insights.chat_memory
                WHERE subject_id=:subject AND role=:role
            """), {"subject": principal.subject_id, "role": principal.role}).mappings().one_or_none()
        return TopicMemory(**dict(row)) if row is not None else None

    def save(self, principal: Principal, *, topic: str, days: int, stream: str) -> TopicMemory:
        topic = topic.strip()
        if (not topic or len(topic) > 500 or "\x00" in topic
                or days not in {1, 7, 30} or stream not in {"all", "domestic", "foreign"}):
            raise ValueError("invalid topic memory")
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO insights.chat_memory (subject_id,role,topic,days,stream)
                VALUES (:subject,:role,:topic,:days,:stream)
                ON CONFLICT (subject_id,role) DO UPDATE SET topic=EXCLUDED.topic,
                    days=EXCLUDED.days, stream=EXCLUDED.stream, updated_at=now()
                RETURNING topic,days,stream,updated_at
            """), {"subject": principal.subject_id, "role": principal.role,
                   "topic": topic, "days": days, "stream": stream}).mappings().one()
            return TopicMemory(**dict(row))

    def clear(self, principal: Principal) -> None:
        # Authenticated users can remove all their own saved role contexts,
        # even after a chat grant has been revoked.
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM insights.chat_memory WHERE subject_id=:subject"),
                         {"subject": principal.subject_id})
