"""P29 -> P12/P13 internal worker; bounded, explicit opt-in, no publishing.

The operator selects an already-authorized staff identity in protected runtime
configuration. Never grant permissions or create identities here. The normal
editorial generator, budget ledger, source checks and repository are reused.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import unquote

from sqlalchemy.engine import Connection

from backend.app.dependencies import Services, build_services
from backend.app.editorial_runtime import build_workflow_fixture_runtime
from backend.core.auth import AuthenticationError, Principal
from backend.core.jobs import InvalidTransition
from backend.core.permissions import decide_access
from backend.core.sources_service import PostgresSourceService, SourceNotAllowed, SourceNotFound
from backend.modules.editorial.repository import RevisionConflict
from backend.modules.editorial.translator import canonical, checked_source, digest
from backend.modules.processing.repository import PostgresProcessingRepository

SCOPES = (("P12", "foreign"), ("P13", "domestic"), ("P13", "manual"))


class WorkHeld(RuntimeError):
    """No eligible operation may start on the current claim."""


@dataclass
class WorkflowWorker:
    services: Services
    actor: Callable[[], Principal]

    def _source(self, claim: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        repo = self.services.pipeline.processed
        if not isinstance(repo, PostgresProcessingRepository):
            raise WorkHeld("persistent producer required")
        parts = str(claim["payload_ref"]).split("/")
        if (len(parts) != 4 or parts[:2] != ["processing", "history"]
                or not parts[3].isdigit() or len(parts[3]) > 10):
            raise WorkHeld("invalid producer reference")
        news_id = unquote(parts[2])
        if not 1 <= len(news_id) <= 64:
            raise WorkHeld("invalid producer identity")
        item = repo.get_revision(news_id, int(parts[3]))
        current = repo.get(news_id)
        if item is None or current is None or item.as_dict() != current.as_dict():
            raise WorkHeld("producer revision changed")
        if (item.news["stream"] != claim["stream"]
                or item.routing.get("target_project") != claim["project_id"]
                or item.routing.get("news_id") != news_id):
            raise WorkHeld("producer route changed")
        raw = checked_source({k: v for k, v in item.news.items()
                              if k not in {"normalized_text", "fingerprint", "normalization_version"}})
        registered = self.services.sources.require_usable(raw["source_id"], stream=raw["stream"])
        scopes = {"metadata": 0, "summary": 1, "full_text": 2}
        if scopes.get(registered["license_scope"], -1) < scopes[raw["content_scope"]]:
            raise WorkHeld("source rights changed")
        return raw, list(dict.fromkeys(item.classification["desks"]))

    def _principal(self, news_id: str, job_id: str) -> Principal:
        principal = self.actor()
        correlation_id = "workflow:" + job_id
        if principal.role != "staff" or principal.must_reset_password:
            self.services.auditor.record(
                action="workflow_worker.identity",
                actor_id=principal.subject_id,
                actor_role=principal.role,
                resource_type="workflow_job",
                resource_id=job_id,
                decision="INVALID_CONTEXT",
                correlation_id=correlation_id,
                details={"allowed": False},
            )
            raise WorkHeld("invalid worker identity")
        for resource_type, action in (
            ("newsroom", "read"),
            ("editorial_workspace", "write"),
            ("editorial_workspace", "generate"),
        ):
            decision = decide_access(
                principal.subject_id,
                principal.role,
                resource_type,
                news_id,
                action,
                self.services.grants,
                request_id=correlation_id,
            )
            # Authorization evidence must be durable before model work starts.
            # Audit persistence failure propagates and therefore fails closed.
            self.services.auditor.record(
                action=f"{resource_type}.{action}",
                actor_id=principal.subject_id,
                actor_role=principal.role,
                resource_type=resource_type,
                resource_id=news_id,
                decision=str(decision["reason_code"]),
                correlation_id=correlation_id,
                details={"allowed": decision["allowed"]},
            )
            if decision["allowed"] is not True:
                raise WorkHeld("worker access denied")
        return principal

    def run_one(self, *, project: str, stream: str) -> str:
        queue = self.services.workflow_jobs
        generator = self.services.editorial_generator
        editorial = self.services.editorial
        if queue is None or generator is None or editorial is None:
            return "unavailable"
        # Verify the actor before claiming any work. The grant store checks
        # active identity again for every news item.
        try:
            principal = self.actor()
            if principal.role != "staff" or principal.must_reset_password:
                return "unavailable"
        except AuthenticationError:
            return "unavailable"
        self.services.pipeline.flush_handoff()
        claim = queue.claim(project=project, stream=stream, lease_seconds=600)
        if claim is None:
            return "empty"
        job_id, lease = claim["job_id"], claim["lease_token"]
        started = False
        try:
            raw, desks = self._source(claim)
            principal = self._principal(raw["news_id"], job_id)
            desk = next((d for d in desks if generator.available(raw, d)), None)
            if desk is None:
                raise WorkHeld("no eligible editorial profile")
            source_hash = digest(canonical(raw))
            # Never overwrite an existing human or independently generated draft.
            # The same request id and version zero allow only an exact replay.
            if editorial.get(raw["news_id"]) is not None:
                raise WorkHeld("editorial revision already exists")

            def revalidate() -> None:
                latest, current_desks = self._source(claim)
                if (self._principal(raw["news_id"], job_id) != principal
                        or desk not in current_desks or digest(canonical(latest)) != source_hash
                        or not generator.available(latest, desk)
                        or not queue.lease_is_current(job_id=job_id, lease_token=lease)):
                    raise WorkHeld("workflow authorization changed")

            def acknowledge(conn: Connection) -> None:
                queue.complete_in_transaction(conn, job_id=job_id, lease_token=lease)

            revalidate()
            started = True
            editorial.save_generated(raw, desk=desk, generator=generator, expected_version=0,
                                      expected_source_hash=source_hash, request_id="workflow:"+job_id,
                                      actor_id=principal.subject_id, revalidate=revalidate,
                                      on_commit=acknowledge)
            return "done"
        except (WorkHeld, AuthenticationError, SourceNotAllowed, SourceNotFound, RevisionConflict, ValueError):
            # Once generation might have begun, its external completion/cost
            # is uncertain. Retain failure, not an automatically resumable hold.
            try:
                if started:
                    queue.finish(job_id=job_id, lease_token=lease, success=False)
                else:
                    queue.hold_claim(job_id=job_id, lease_token=lease)
            except InvalidTransition:
                return "stale"
            return "failed" if started else "held"
        except Exception:
            # No exception text or source content in logs. No blind retry.
            try:
                queue.finish(job_id=job_id, lease_token=lease, success=False)
            except InvalidTransition:
                return "stale"
            return "failed"

    def tick(self, *, limit_per_stream: int = 5) -> dict[str, int]:
        if not 1 <= limit_per_stream <= 20:
            raise ValueError("invalid worker batch limit")
        counts: dict[str, int] = {}
        for project, stream in SCOPES:
            for _ in range(limit_per_stream):
                state = self.run_one(project=project, stream=stream)
                counts[state] = counts.get(state, 0) + 1
                if state in {"empty", "unavailable"}:
                    break
        return counts


def main() -> None:
    mode = os.environ.get("ETEMAD_WORKFLOW_WORKER_MODE", "off")
    if mode == "off":
        print(json.dumps({"mode": "off"}))
        return
    if mode not in {"authorized", "fixture"}:
        raise SystemExit("invalid workflow worker mode")
    actor_id = os.environ.get("ETEMAD_WORKFLOW_ACTOR_ID", "")
    if not 1 <= len(actor_id) <= 64:
        raise SystemExit("authorized staff identity required")
    services = build_services()
    try:
        if services.accounts is None:
            raise SystemExit("persistent identity required")
        if mode == "fixture":
            if services.editorial_generator is not None:
                raise SystemExit("fixture worker cannot replace a configured editorial runtime")
            try:
                services.editorial_generator = build_workflow_fixture_runtime(
                    os.environ,
                    private_identity=True,
                    persistent_sources=isinstance(services.sources, PostgresSourceService),
                )
            except ValueError:
                raise SystemExit("authorized private workflow fixture required") from None
        elif (services.editorial_generator is None
              or services.editorial_generator.mode != "live"):
            raise SystemExit("configured runtime is not an authorized live generator")
        accounts = services.accounts
        counts = WorkflowWorker(services, lambda: accounts.current(actor_id)).tick()
        print(json.dumps(counts, sort_keys=True))
    finally:
        if services.reports is not None:
            services.reports.close()
        services.registry.dispose()


if __name__ == "__main__":
    main()
