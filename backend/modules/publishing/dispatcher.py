"""P25 fixture dispatcher with explicit unknown handling and independent destinations.

There is no production adapter or public HTTP route in this slice. Results are
always labelled fixture; never feed them into live P17 statistics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Mapping, Protocol

from backend.adapters.base import Delivery
from backend.adapters.registry import AdapterRegistry
from backend.core.contracts import validate_payload
from backend.core.output_switches import OutputSwitches
from backend.modules.publishing.destinations import Destination
from backend.modules.publishing.formatter import format_text
from backend.modules.publishing.scheduler import ApprovedContent, stamp


class AttemptLedger(Protocol):
    def once(self, key: tuple[str, str], operation: Callable[[], Delivery]) -> Delivery: ...


class FixtureLedger:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[tuple[str, str], Delivery] = {}

    def once(self, key: tuple[str, str], operation: Callable[[], Delivery]) -> Delivery:
        with self._lock:
            if key not in self._records:
                # A crash/ambiguous result must not produce an automatic retry.
                self._records[key] = Delivery("unknown", error_code="ATTEMPT_UNCONFIRMED")
                self._records[key] = operation()
            return self._records[key]


def dispatch_fixture(
    job: Mapping[str, Any], *, approval_for: Callable[[], ApprovedContent | None],
    destinations: Mapping[str, Destination], adapters: AdapterRegistry, ledger: AttemptLedger,
    switches: Callable[[], OutputSwitches], now: Callable[[], datetime], dry_run: bool = True,
) -> dict[str, Any]:
    validate_payload("publication_job", job)
    deliveries = []
    for dest_id in job["destination_ids"]:
        instant = now()
        approval = approval_for()
        dest = destinations.get(dest_id)
        reason = None
        if (job["state"] != "scheduled" or job["expires_at"] is None or instant < stamp(job["scheduled_at"])
                or instant >= stamp(job["expires_at"])):
            reason = "HELD_NOT_DUE_OR_EXPIRED"
        elif (approval is None or not approval.valid_at(instant) or approval.object_id != job["object_id"]
              or approval.object_type != job["object_type"] or approval.stream != job["stream"]
              or approval.decision["decision_id"] != job["policy_decision_id"]):
            reason = "APPROVAL_REVOKED"
        elif not approval.switch_enabled(switches()):
            reason = "PUBLISHING_DISABLED"
        elif dest is None or not dest.eligible(stream=approval.stream, desk=approval.record.get("desk")):
            reason = "DESTINATION_UNAVAILABLE"
        if reason:
            outcome = Delivery("skipped", error_code=reason)
        else:
            assert approval is not None and dest is not None
            try:
                adapter = adapters.get(dest.adapter_id, str(dest.config["platform"]))
                adapter.validate_config(dest.config)
                payload = format_text(approval, job, dest, max_chars=adapter.max_text_chars)
                adapter.dry_run(payload)
                if dry_run:
                    outcome = Delivery("skipped", error_code="FIXTURE_DRY_RUN")
                else:
                    def invoke() -> Delivery:
                        current = approval_for()
                        check_time = now()
                        if (current is None or not current.valid_at(check_time)
                                or current.approved_hash != approval.approved_hash
                                or current.decision["decision_id"] != job["policy_decision_id"]
                                or check_time >= stamp(job["expires_at"])
                                or not current.switch_enabled(switches())):
                            return Delivery("skipped", error_code="RECHECK_DENIED")
                        try:
                            return adapter.publish(payload, idempotency_key=job["idempotency_key"] + ":" + dest_id)
                        except Exception:  # noqa: BLE001 - ambiguous fixture delivery is not blindly retried
                            return Delivery("unknown", error_code="ATTEMPT_UNCONFIRMED")
                    outcome = ledger.once((job["idempotency_key"], dest_id), invoke)
            except ValueError:
                outcome = Delivery("failed", error_code="UNSUPPORTED_OR_INVALID_DESTINATION")
        deliveries.append({"destination_id": dest_id, "status": outcome.status,
                           "external_message_id": outcome.message_id, "attempted_at": instant.astimezone(timezone.utc).isoformat(),
                           "error_code": outcome.error_code})
    result = {"schema_version": "1.0", "job_id": job["job_id"], "object_id": job["object_id"],
              "deliveries": deliveries, "completed_at": now().astimezone(timezone.utc).isoformat()}
    validate_payload("publication_result", result)
    return {"mode": "fixture", "dry_run": dry_run, "result": result}
