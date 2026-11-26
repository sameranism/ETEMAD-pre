# ADR-018 — Incremental delivery of verified working capabilities

Date: 2026-11-26.
Status: owner-directed delivery policy; effective only after protected merge.
References: ADR-012, ADR-015, ADR-017, AGENTS.md.

## Decision

The owner directs a delivery cycle of implementation, necessary verification,
protected integration, deployment and operational verification, followed by
the next capability. Repeated verification of an unchanged result is not a
development deliverable.

- Deliver a coherent, narrowly bounded vertical slice on a protected PR. Record
  its concrete interfaces, changed paths, migration/rollback, exact-head CI,
  dependency evidence and remaining disabled or incomplete capabilities.
- Do not require unrelated aggregate Gate rows to close before merging a
  default-off, independently verified implementation. The slice must still
  satisfy every applicable contract, security, migration, ownership and
  behavioral dependency. Unmet relevant requirements block that slice; never
  change an acceptance result or describe a Gate as closed merely because
  code was merged. This changes integration scheduling, not security criteria.
- For the already staged domestic-news storage, processing, API and newsroom
  slice, preserve fixture-only defaults, the separate database roles,
  authenticated API access, deny-by-default authorization and disabled
  external outputs. The production implementation may be integrated after
  review of these boundaries and exact-head required CI. Do not merge any
  temporary test-only workflow or private credential. Existing tests provide
  evidence; run only checks affected by changes plus mandatory native CI.
- The owner authorizes a private VPS staging deployment of verified code
  on loopback or an existing authorized private access path, with isolated
  databases and synthetic fixtures, no public listeners, live ingestion,
  external publishing, paid APIs or existing operational data. Validate the
  actual service once and record the release SHA and rollback path. Do not
  present fixture staging as a full operational release.
- Production enablement requires real account provisioning, authorization,
  source approval, applicable security and operational acceptance, backup and
  restore, and any separate authorization for external side effects. Keep
  unready functionality off; do not broaden access to work around blockers.
- Continue outstanding Gate evidence in the owning scope. Never weaken the
  frozen contracts, native required checks, signed and linear protected
  main, CodeQL thresholds, private-data isolation or studio deferral. No
  direct or force push, no unverified deployment, no human review requirement
  for ordinary development.

This supersedes only ADR-012/017's aggregate-Gate integration ordering for
bounded default-off slices and private staging. It does not waive applicable
acceptance, formal Gate closure, or live-release requirements. Revert through
a protected PR; retain previous deployment and data until verification.
