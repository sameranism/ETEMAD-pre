# ETEMAD execution state

## Current checkpoint — 2026-11-26 (Asia/Tehran)

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
