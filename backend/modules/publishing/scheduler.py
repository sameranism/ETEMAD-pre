"""P22 immutable content approval and per-destination schedule, no outbound I/O."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.core.contracts import validate_payload
from backend.core.output_switches import OutputSwitches
from backend.modules.publishing.destinations import Destination


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class ApprovedContent:
    """Trusted P11 handoff. No client-facing approval creation is provided."""

    object_type: str
    record: Mapping[str, Any]
    decision: Mapping[str, Any]
    approved_hash: str
    stream: str
    _decision_hash: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.object_type not in {"article", "public_report"}:
            raise ValueError("private or unsupported publication object")
        obj = json.loads(canonical(self.record))
        decision = json.loads(canonical(self.decision))
        validate_payload(self.object_type, obj)
        validate_payload("validation_decision", decision)
        identity = obj["article_id"] if self.object_type == "article" else obj["report_id"]
        state = obj["status"] if self.object_type == "article" else obj["generation_status"]
        if (state != "ready_for_policy" or decision["decision"] != "auto_allowed"
                or decision["object_type"] != self.object_type or decision["object_id"] != identity
                or decision["expires_at"] is None or content_hash(obj) != self.approved_hash
                or not set(obj["source_refs"]) <= set(decision["evidence_refs"])
                or self.stream not in {"domestic", "foreign", "mixed", "manual"}
                or (self.object_type == "article" and self.stream != obj["stream"])):
            raise ValueError("content has no matching valid approval")
        object.__setattr__(self, "record", obj)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "_decision_hash", content_hash(decision))

    @property
    def object_id(self) -> str:
        return str(self.record["article_id"] if self.object_type == "article" else self.record["report_id"])

    def valid_at(self, now: datetime) -> bool:
        return (content_hash(self.record) == self.approved_hash
                and content_hash(self.decision) == self._decision_hash
                and self.decision["decision"] == "auto_allowed"
                and stamp(self.decision["decided_at"]) <= now < stamp(self.decision["expires_at"]))

    def switch_enabled(self, switches: OutputSwitches) -> bool:
        stream = "public_report" if self.object_type == "public_report" else self.stream
        return stream in {"domestic", "foreign", "public_report"} and switches.publishing_stream_enabled(stream)


def plan_job(approval: ApprovedContent, destination: Destination, *, scheduled_at: datetime,
             deadline: datetime, now: datetime, switches: OutputSwitches) -> dict[str, Any]:
    if any(value.tzinfo is None or value.utcoffset() is None for value in (scheduled_at, deadline, now)):
        raise ValueError("publication times require timezone")
    expires = min(deadline, stamp(approval.decision["expires_at"]))
    if not approval.valid_at(now) or not now <= scheduled_at < expires:
        raise ValueError("publication approval or deadline expired")
    if not destination.eligible(stream=approval.stream, desk=approval.record.get("desk")):
        raise ValueError("destination not eligible")
    identity = hashlib.sha256(canonical([approval.object_type, approval.object_id,
                                         approval.approved_hash, destination.identity]).encode()).hexdigest()
    job = {"schema_version": "1.0", "job_id": "JOB-" + identity[:32], "object_type": approval.object_type,
           "object_id": approval.object_id, "stream": approval.stream, "destination_ids": [destination.identity],
           "scheduled_at": scheduled_at.astimezone(timezone.utc).isoformat(),
           "expires_at": expires.astimezone(timezone.utc).isoformat(),
           "policy_decision_id": approval.decision["decision_id"], "idempotency_key": "pub-" + identity,
           "state": "scheduled" if approval.switch_enabled(switches) else "held",
           "created_at": now.astimezone(timezone.utc).isoformat()}
    validate_payload("publication_job", job)
    return job
