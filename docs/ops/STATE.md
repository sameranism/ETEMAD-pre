# ETEMAD execution state

## Verified checkpoint — 2026-11-26 (Asia/Tehran), baseline `d1d773db8038ab3ac4b06f6c54ac450a2158ced7`

- [PR #58](https://github.com/sameranism/ETEMAD-pre/pull/58) is merged at [signed main commit](https://github.com/sameranism/ETEMAD-pre/commit/d1d773db8038ab3ac4b06f6c54ac450a2158ced7). The exact-head [bootstrap check](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713010755), [backend](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713010676), [contracts](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713010784) and [CodeQL](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713010804) report success.
- On this exact main SHA, [backend CI](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211748/job/139552603583) reports 142 passed, 15 warnings, PostgreSQL 18.7, including the migration round-trip/database-separation test. Lint, type checking and contract validation steps succeeded. [Contract CI](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211853/job/139552603853) reports 62 task preflights, 336 negative contract cases and passing boundary checks. Its separate [unit job](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211853/job/139552604220) reports 141 passed, 1 skipped (no PostgreSQL URL), 1 warning. [Main Guardian audit](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211848) and [CodeQL](https://github.com/sameranism/ETEMAD-pre/actions/runs/45713211725) succeeded.
- Reconciled the 223 rows in manifests/tasks.csv against the complete recursive Git tree at this SHA. Seven studio tasks remain deferred; 216 active, 111 active target paths present (51.39%; 111/223 = 49.78% overall). All 62 S00 and 30 S01 targets exist. Presence is not acceptance: the 62 S00 rows remain blocked; a complete per-task acceptance percentage cannot yet be verified.
- [Ruleset 28111497](https://github.com/sameranism/ETEMAD-pre/rules/28111497) is active with zero ordinary approvals, no bypass actors, strict four required status checks, native CodeQL, signed squash PRs, linear history, deletion and force-push prevention. The extra approval for unattributed changes remains true, contrary to ADR-012. The current connector exposes no ruleset write action; configuration requires an authorized administrative interface. No protection was changed.
- Gate-00 remains OPEN/BLOCKED. The frozen schema accepts restricted, but backend/core/events.py CLASSIFICATIONS omits it. The shared event behavior/routing and authorization decision requires explicit owner approval before code changes in this execution. Do not silently narrow or alter the frozen schema. See the gate register for missing semantic, privacy, secret and permission evidence.
- Gate-01: no acceptance register found in docs/gates at this revision. The coding path requires Gate-01 PASS and allowed source list for S03-002, S03-003 and S03-006. Verify these, and the task-specific acceptance logs for S03-002/003, before S03-006. No production integrations authorized.
- At the observed baseline there were zero open PRs and zero open issues. This checkpoint is documentation-only evidence maintenance; the old checkpoints and failed runs remain below. Rollback via a protected revert PR.

## Current checkpoint — 2026-11-26 (Asia/Tehran)

- [PR #56](https://github.com/sameranism/ETEMAD-pre/pull/56) merged
  `887e83f3609da993e75725538f5a93255031de30` as
  `e297b5f9de301e4d07ac108d808615e6e14a6cb7`, adopting ADR-012 and
  automatic development acceptance. [Post-merge Guardian](https://github.com/sameranism/ETEMAD-pre/actions/runs/45711967375)
  and [CodeQL on main](https://github.com/sameranism/ETEMAD-pre/actions/runs/45711967167)
  passed. Personal review for new development is NOT_REQUIRED under repository policy.
- [PR #57](https://github.com/sameranism/ETEMAD-pre/pull/57) merged
  `fabfcf8e3aacd0e8d0c18caaa774dd15e17548ba` as
  `58c2b53bf2e53234d42e7c9123a1a83c84cc91fc`. Its exact-head
  [backend CI](https://github.com/sameranism/ETEMAD-pre/actions/runs/45712445039)
  passed 140 tests on PostgreSQL 18 including separate central and domestic
  migration round trips. [Trusted Guardian](https://github.com/sameranism/ETEMAD-pre/actions/runs/45712444735)
  and [CodeQL](https://github.com/sameranism/ETEMAD-pre/actions/runs/45712445030)
  passed. The foreign fixture remained untouched; there is no foreign migration.
- The [first main audit attempt for #57](https://github.com/sameranism/ETEMAD-pre/actions/runs/45712663422/attempts/1)
  failed while the merge-to-PR association was unavailable. The GitHub API now
  maps the exact commit to merged PR #57. A bounded association retry preserves a
  failed audit when GitHub does not return the matching PR.
- The Gate-00 semantic audit reproduced a v1.0 producer mismatch for
  `event_envelope.classification=restricted`. Record exact contract
  and routing evidence before changing producer behavior. The approved
  source list remains a downstream prerequisite for S03-006.

- Canonical repository: [sameranism/ETEMAD-pre](https://github.com/sameranism/ETEMAD-pre), protected `main`.
- Security maintenance [PR #55](https://github.com/sameranism/ETEMAD-pre/pull/55)
  squash merged head `66c40b443413926a90feccac9d4d627fd1516644` as
  `48d0e77c1092a20150f5f000b5630d99bab2aab6`. All four native status contexts
  and CodeQL passed on its head. CodeQL also passed on
  [main run 45710579171](https://github.com/sameranism/ETEMAD-pre/actions/runs/45710579171).
- The prior Merge Guardian returned failure for missing sensitive-change approval on
  [PR #55](https://github.com/sameranism/ETEMAD-pre/actions/runs/45709344599)
  and its [main push](https://github.com/sameranism/ETEMAD-pre/actions/runs/45710579289).
  Both are historical results under the old policy. The status is outside the
  four required native status contexts for this merge.
- [Ruleset 28111497](https://github.com/sameranism/ETEMAD-pre/rules/28111497):
  active, no bypass actors, four strict CI requirements, CodeQL, signed squash-only
  PRs, deletion and force-push blocked. Unattributed-change approval is still on.
- S09-023: the exact-head [backend run](https://github.com/sameranism/ETEMAD-pre/actions/runs/45709344460)
  passed Ruff, Mypy, contract validation and Pytest and saved its test summary.
  Prior failed attempts remain in the history below.
- Gate-00: `OPEN / BLOCKED`. [Evidence and exact gaps](../gates/GATE-00.md).
  Gate-01 acceptance evidence has not been verified on the canonical repository.
- Next product task: `S03-006` after Gate-00, Gate-01, source scope and S03-002/S03-003
  acceptance. Local preflight passed for all three. The current processing repository
  is fixture-backed. The queue uses `canonical_url` and `content_hash`, while the
  existing migration uses `(source_id, original_url)` and an indexed
  `fingerprint`. Resolve the storage uniqueness and history criteria within the
  processing ownership boundary before wiring PostgreSQL.
- Inventory baseline: 111 of 223 target paths present (49.8% path coverage).
  Accepted-task progress is unmeasured because the per-task evidence is incomplete.
  Seven studio tasks remain deferred by the owner.
- Rollback: revert an accepted change through a protected PR, with the applicable
  checks and native CodeQL scanning still active.

## Historical checkpoint — previous repository

## Current task

- Task: `S09-023`
- Branch: `feat/s09-023-backend-ci`
- PR: [#45](https://github.com/etemadion/ETEMAD/pull/45) (draft)
- State: CI failed after two debug attempts. The remaining findings require changes to existing backend files outside the workflow target.

## Review and evidence

- Second agent reviewed the proposal locally. Its review does not constitute GitHub approval.
- Local checks passed: 62 manifest targets, relative links, 30 schemas and examples, and package boundaries including two negative fixtures.
- [OPS-001 PR #43](https://github.com/etemadion/ETEMAD/pull/43) was merged. The ADR remains proposed and Gate-00 remains open.

## Review debt

- OPS-002 / PR #44: self-review with evidence. Diff contains the workflow, ADR and state record. Read-only replay detected the unreviewed PR #43 and the eight reviewer-selection fixtures passed. No independent GitHub approval recorded.
- OPS-001 / PR #43: GitHub reports merged with no recorded reviews. Gate-00 remains open.

## Isolated follow-on work

- OPS-002 [CI](https://github.com/etemadion/ETEMAD/actions/runs/45277350200) passed. A read-only replay of main commit 7756736 reported PR #43 missing independent approval and returned the expected failure.
- S09-023: [attempt 1](https://github.com/etemadion/ETEMAD/actions/runs/45279433244) added jsonschema stubs (16 Ruff / 4 mypy findings). [Attempt 2](https://github.com/etemadion/ETEMAD/actions/runs/45279556236) set the repository import root (7 Ruff / 4 mypy findings). Both CI runs failed. Contract validation and all 106 backend tests passed.
- GitHub listed one collaborator, the PR author's account. The branch protection API returned 403.

## Baseline

- `main`: `2c60ac932c9675801614172992dbcc5e110bab03`
- [Baseline CI](https://github.com/samram-shipt-it/ETEMAD/actions/runs/45272942280): 106 passed, 1 warning.
- Gate-00: open, pending independent PASS.
- `main` branch protection: inactive at the last GitHub API check.
- Next isolated product task in [QUEUE.md](QUEUE.md): `S03-006`, subject to its repository and PostgreSQL prerequisites.

See the [Gate register](../gates/GATE-00.md) for task evidence and the
[ADR proposal](../decisions/ADR-010-gate00-conditional-acceptance.md) for closure conditions.
