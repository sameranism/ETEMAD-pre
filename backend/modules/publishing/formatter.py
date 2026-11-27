"""P24 deterministic text rendition from one approved immutable object."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.core.contracts import validate_payload
from backend.modules.publishing.destinations import Destination
from backend.modules.publishing.scheduler import ApprovedContent, canonical, content_hash


def format_text(approval: ApprovedContent, job: Mapping[str, Any], destination: Destination,
                *, max_chars: int) -> dict[str, Any]:
    validate_payload("publication_job", job)
    if (job["object_type"] != approval.object_type or job["object_id"] != approval.object_id
            or job["stream"] != approval.stream or content_hash(approval.record) != approval.approved_hash
            or job["policy_decision_id"] != approval.decision["decision_id"]
            or destination.identity not in job["destination_ids"] or not 1 <= max_chars <= 100_000):
        raise ValueError("publication payload identity mismatch")
    record = approval.record
    if approval.object_type == "article":
        material = str(record["title"]) + "\n\n" + str(record["short_summary"])
    else:
        material = str(record["title"]) + "\n\n" + "\n\n".join(
            section["heading"] + "\n" + section["body"] for section in record["sections"])
    if not material.strip() or len(material) > max_chars:
        raise ValueError("destination text exceeds configured limit; no silent truncation")
    identity = hashlib.sha256(canonical([job["idempotency_key"], destination.identity, material]).encode()).hexdigest()
    payload = {"schema_version": "1.0", "payload_id": "PL-" + identity[:32], "job_id": job["job_id"],
               "destination_id": destination.identity, "content_type": "text", "text": material,
               "asset_uris": [], "source_refs": list(record["source_refs"]),
               "prepared_at": datetime.now(timezone.utc).isoformat()}
    validate_payload("destination_payload", payload)
    return payload
