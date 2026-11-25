# Gate-00 evidence register

**Decision: OPEN / BLOCKED.** See the [automated acceptance procedure](../../prompts/GATES/GATE-00-REVIEW.md)
and [ADR-012](../decisions/ADR-012-automated-acceptance.md).

## ADR-013 scoped corrective maintenance — 2026-11-26

[ADR-013](../decisions/ADR-013-restricted-event-routing.md) was merged in [PR #60](https://github.com/sameranism/ETEMAD-pre/pull/60) at `514c5d6f1be87ac58537fb3b0c1d16a592827599`. This corrective change adds constructor support for restricted while refusing it on the unauthenticated shared fixture bus and generic outbox. The frozen schema is unchanged. New positive and negative tests are present; their results require exact-head CI and are not asserted by this change. The historical mismatch below refers to its recorded baseline. Full semantic/security acceptance, executive-private delivery, authenticated restricted transport, permission/secret evidence and the wider prose/wire mismatch remain open. Gate-00 remains OPEN/BLOCKED; downstream entry conditions are unchanged.

## Exact-main evidence refresh — 2026-11-26

Baseline: [`d1d773db8038ab3ac4b06f6c54ac450a2158ced7`](https://github.com/sameranism/ETEMAD-pre/commit/d1d773db8038ab3ac4b06f6c54ac450a2158ced7), merged via [PR #58](https://github.com/sameranism/ETEMAD-pre/pull/58).

| Check | Result | Evidence |
| --- | --- | --- |
| Backend, Ruff, Mypy, contracts, migration and database separation | PASS (142 passed, 15 warnings; PostgreSQL 18.7) | [Job 139552603583](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211748/job/139552603583) |
| Foundation inventory, negative contract cases and package boundaries | PASS (62 preflights, 336 negative cases) | [Job 139552603853](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211853/job/139552603853) |
| Separate unit job | PASS (141 passed, 1 skipped because PostgreSQL URL unset, 1 warning) | [Job 139552604220](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211853/job/139552604220) |
| Main provenance audit and native scanning | PASS | [Guardian](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211848), [CodeQL](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211725) |

Static inspection at this SHA confirms the event mismatch: the [frozen schema](https://github.com/sameranism/ETEMAD-pre/blob/d1d773db8038ab3ac4b06f6c54ac450a2158ced7/source/contracts/schemas/event_envelope.schema.json) includes restricted, but the [producer](https://github.com/sameranism/ETEMAD-pre/blob/d1d773db8038ab3ac4b06f6c54ac450a2158ced7/backend/core/events.py) excludes it. Before changing shared event behavior, obtain explicit authorization for an ADR and scoped implementation/consumer tests; do not edit the frozen wire enum. The semantic and privacy tests, secret/permission scan evidence, and per-task acceptance evidence remain open. A later trusted-base PR check is still required. Gate-00 stays OPEN/BLOCKED; all 62 existing task rows below retain their blocked disposition. Gate-01 and allowed-source evidence have not been established; S03-006 must not start.

## Verified foundation evidence

| Check | Result | Evidence |
| --- | --- | --- |
| Exact-head CI and CodeQL on security maintenance | PASS | [PR #55](https://github.com/sameranism/ETEMAD-pre/pull/55), head `66c40b443413926a90feccac9d4d627fd1516644`; [backend](https://github.com/sameranism/ETEMAD-pre/actions/runs/45709344460), [contracts and boundaries](https://github.com/sameranism/ETEMAD-pre/actions/runs/45709344427), [bootstrap](https://github.com/sameranism/ETEMAD-pre/actions/runs/45709344753), [CodeQL](https://github.com/sameranism/ETEMAD-pre/actions/runs/45709344618) |
| Native default-branch protection | PASS for CI, signed squash PRs, CodeQL, deletion and force-push prevention | [Active Ruleset 28111497](https://github.com/sameranism/ETEMAD-pre/rules/28111497); extra unattributed-change approval remains configured at this observation |
| Phase-zero inventory and preflight | PASS locally | `python tools/check_gate00.py`, exit 0, 62/62 tracked targets, prompts and preflights |
| Frozen schema hashes and positive and negative schema checks | PASS locally | Same command, 30 positive schema/example pairs and 336 rejected negative cases |
| Automated policy and exact-head CI | PASS | [PR #56](https://github.com/sameranism/ETEMAD-pre/pull/56), head `887e83f3609da993e75725538f5a93255031de30`; [backend](https://github.com/sameranism/ETEMAD-pre/actions/runs/45711659707), [contract and foundation checks](https://github.com/sameranism/ETEMAD-pre/actions/runs/45711659482), [CodeQL](https://github.com/sameranism/ETEMAD-pre/actions/runs/45711659447), [main provenance audit](https://github.com/sameranism/ETEMAD-pre/actions/runs/45711967375) |
| Fixture authorization and orchestration | PASS locally | `python -m pytest -q backend/tests/test_security.py backend/tests/test_orchestration.py`, exit 0, 41 passed |
| Central and domestic migration round trip | PASS on local PostgreSQL 19.0 | `python -m pytest -q backend/tests` with ephemeral loopback `ETEMAD_TEST_PG_ADMIN_URL`, 140 passed including the migration check. Separate foreign database verified untouched; no foreign migration was run |
| Frontend build | PASS locally | `npm run build`. `npm test` exited 1 because the repository had no matching tests; frontend behavior remains unverified |
| Import boundaries | PASS locally and in PR #55 CI | `python tools/check_boundaries.py --self-test` and `python tools/check_boundaries.py`, both exit 0; two deliberately invalid fixture dependencies detected |

The phase-zero table below records what the foundation check exercised. Historical
links are retained as evidence of earlier work. The earlier independent acceptance
column was pending for every task.

## Evidence required for closure

- Finish the contract-level semantic audits, including consumer/producer mapping
  for the frozen wire format in [event_envelope](../contract-audits/event_envelope.md)
  and workflow state transitions in [workflow_status](../contract-audits/workflow_status.md).
  The example and negative schema check covers structure. Cross-record uniqueness,
  legal state transitions and privacy across downstream services require separate tests.
  On the accepted baseline `e297b5f9`, a local fixture call to `backend.core.events.make_event`
  with `classification="restricted"` raised `UnknownClassification` (exit 1).
  The frozen `event_envelope` enum includes `restricted`. Record a compatible
  producer/consumer policy and test routing and authorization before marking S00-035 complete.
- Record applicable secret and permission scan evidence, and run failure-path tests
  for unauthorized private data access across the backend, storage and retrieval boundaries.
- Use the new main-push audit recorded above. Verify the trusted base check
  on a subsequent PR. The old runs retain their original conclusions.

## Downstream entry criteria

- Verify Gate-01, the approved-source prerequisite, and the S03-002/S03-003
  acceptance evidence before S03-006 integration proceeds.

## Task inventory

| Task | Target | Foundation check | Historical link | Acceptance |
| --- | --- | --- | --- | --- |
| S00-001 | `AGENTS.md` | Target and preflight PASS | [PR #41](https://github.com/samram-shipt-it/ETEMAD/pull/41) | BLOCKED pending complete evidence |
| S00-002 | `.cursor/rules/architecture.mdc` | Target and preflight PASS | [PR #42](https://github.com/samram-shipt-it/ETEMAD/pull/42) | BLOCKED pending complete evidence |
| S00-003 | `.cursor/rules/contracts.mdc` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-004 | `.cursor/rules/security.mdc` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-005 | `.cursor/rules/frontend.mdc` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-006 | `.cursor/rules/testing.mdc` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-007 | `.cursor/rules/cloud-agent.mdc` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-008 | `docs/decisions/ADR-001-architecture.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-009 | `docs/decisions/ADR-002-ui-design-system.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-010 | `docs/decisions/ADR-003-storage-ownership.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-011 | `docs/decisions/ADR-004-publishing-gates.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-012 | `docs/decisions/ADR-005-auth-roles.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-013 | `docs/decisions/ADR-006-ai-provider-gateway.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-014 | `docs/architecture/SYSTEM_MAP.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-015 | `docs/architecture/CONTRACT_OWNERSHIP.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-016 | `docs/architecture/DATABASE_REVIEW.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-017 | `tools/validate_contracts.py` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-018 | `.github/workflows/contract-check.yml` | Target and preflight PASS | [CI run 45272942280](https://github.com/samram-shipt-it/ETEMAD/actions/runs/45272942280) | BLOCKED pending complete evidence |
| S00-019 | `backend/pyproject.toml` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-020 | `frontend/package.json` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-021 | `README.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-022 | `docs/contract-audits/access_decision.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-023 | `docs/contract-audits/ai_models_config.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-024 | `docs/contract-audits/analysis_chat_response.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-025 | `docs/contract-audits/analysis_result.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-026 | `docs/contract-audits/archive_record.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-027 | `docs/contract-audits/article.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-028 | `docs/contract-audits/audio_manifest.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-029 | `docs/contract-audits/classified_news.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-030 | `docs/contract-audits/cluster_result.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-031 | `docs/contract-audits/country_source_pack.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-032 | `docs/contract-audits/destination_config.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-033 | `docs/contract-audits/destination_payload.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-034 | `docs/contract-audits/editorial_rules.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-035 | `docs/contract-audits/event_envelope.md` | Target and preflight PASS | [PR #40](https://github.com/samram-shipt-it/ETEMAD/pull/40) | BLOCKED pending complete evidence |
| S00-036 | `docs/contract-audits/image_manifest.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-037 | `docs/contract-audits/metrics_snapshot.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-038 | `docs/contract-audits/news_framework.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-039 | `docs/contract-audits/normalized_news.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-040 | `docs/contract-audits/output_controls.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-041 | `docs/contract-audits/private_report.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-042 | `docs/contract-audits/public_report.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-043 | `docs/contract-audits/publication_job.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-044 | `docs/contract-audits/publication_result.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-045 | `docs/contract-audits/raw_news.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-046 | `docs/contract-audits/schedule_config.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-047 | `docs/contract-audits/source_registry.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-048 | `docs/contract-audits/translation.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-049 | `docs/contract-audits/validation_decision.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-050 | `docs/contract-audits/video_manifest.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-051 | `docs/contract-audits/workflow_status.md` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-052 | `.gitignore` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-053 | `.editorconfig` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-054 | `.github/CODEOWNERS` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-055 | `tools/check_boundaries.py` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-056 | `frontend/tsconfig.json` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-057 | `frontend/vite.config.ts` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-058 | `frontend/index.html` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-059 | `frontend/src/main.tsx` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-060 | `frontend/components.json` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-061 | `frontend/eslint.config.js` | Target and preflight PASS | — | BLOCKED pending complete evidence |
| S00-062 | `frontend/playwright.config.ts` | Target and preflight PASS | — | BLOCKED pending complete evidence |
