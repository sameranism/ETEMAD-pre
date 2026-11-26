# ADR-019 — Product-first delivery and risk-based verification

Date: 2026-11-26.
Status: owner-directed proposal; effective after protected merge.
References: ADR-012, ADR-015, ADR-017, ADR-018, AGENTS.md, manifests/tasks.csv, manifests/coding_path.csv, docs/ops/QUEUE.md.

## Decision
The owner's current priority is to finish the media product using the original 29 project IDs, nine package IDs, 223 task IDs, prompts, attachments, frozen contracts and functional acceptance criteria. Change execution order, not product scope or security requirements. P20/P21 and S08-018 remain deferred. Defer additional P28 account administration and privileged MFA development to release readiness; retain existing identity, authorization, privacy, audit, and denied privileged login. No public or live production activation before applicable security and operational requirements are met.

Use the current ADR-018 incremental delivery policy: implement one coherent product slice, run its necessary functional and relevant negative/security/migration checks and mandatory exact-head CI, integrate via protected PR, deploy privately where safe, verify the changed behavior once, record the result, and move on. Do not repeat successful checks on unchanged code outside required CI. No independent human development review or separate manual acceptance is required for ordinary changes. Unrelated open Gate rows, complex browser suites, load tests, broad audit campaigns, and final acceptance documentation do not block independently safe default-off product development. Schedule them at the owning integration/release milestone; never fabricate their results, disable existing mandatory CI, or waive a relevant safety dependency. Missing contract, relevant security, data-integrity or migration prerequisite blocks only the affected slice; record why and choose the next independent eligible product slice. Preserve one active implementation writer and one integration PR for each slice.

## Execution overlay (authoritative for scheduling after merge)
Keep original manifests and prompts intact. Interpret historical aggregate Gate entry language in accordance with ADR-018 and this decision; retain actual dependencies and all task-specific functional and safety criteria. The queue is the execution overlay; do not mark Gate or task acceptance merely from file presence.

1. R01 — P01/P02/P03/P05/P29: complete durable source registry (S01-005), settings, authorized stream-scoped snapshots and internal polling/scheduling (S02-001..006), durable coordination where needed. Existing code is extended, not rewritten.
2. R02 — P04/P06: S02-008..014 country discovery, disabled candidates, foreign collection, foreign-owned persistence, authenticated bridge and API.
3. R03 — P07/P08/P09: S03-001..007 complete shared normalization, durable duplicate reconstruction, classification and routing; reuse existing repositories.
4. R04 — P10/P11/P12/P13/P14: S04-001..008 glossary, rights/quality, independent translation, editing, image, durable artifacts. Model calls default to fixture/mock until separately authorized.
5. R05 — P26: S01-010 persistent archive, provenance and authorized search/retrieval, immutable originals.
6. R06 — P15..P19: S05-001..009 evidence-backed analysis, retrieval, chat, metrics, four public and three executive-private reports. Keep private data isolated; no public routing.
7. R07 — P22..P25: S06-001..014 scheduling, destinations, formatting and official adapter interfaces, dry-run delivery; real publishing requires separate authorization.
8. R08 — P27: implement relevant S02/S08 user-facing screens alongside each slice; finish shared design and dashboards here. Studio remains deferred.
9. R09 — S09/S10: complete applicable integration, backup/restore, advanced security (P28 lifecycle/MFA), end-to-end/load and operational acceptance; only then authorize actual production enablement separately.

## First slice
S01-005 is first. Read its prompt and all references, inspect existing source service and migration chain. Source persistence belongs to P03 central schema. Authorize a narrowly scoped additive migration (next unique revision based on actual chain), repository/service, application wiring under S01-025, and focused regression tests in their owning tasks as one bounded slice. Do not change frozen v1.0 or existing migrations. Maintain explicit source approval, stream separation, rights, authorization, no credentials in records, and fixture-only staging; version snapshots for ingestion instead of direct cross-package DB queries. No external sources enabled by this ADR. If required schema or ownership is uncertain, stage implementation safely and report the exact dependency.

## Verification and rollback
Preserve protected main, required status checks and CodeQL, signed squash PRs, no direct/force push, three DB roles, separate private reports, official APIs only. VPS ai remains the private execution environment; GitHub remains the version/CI authority. For each delivery report exact task IDs, changed paths, commit, actual essential checks, deploy state and rollback; keep previous release and data. Revert via protected PR. Record remaining advanced controls and release prerequisites without blocking unrelated implementation.
