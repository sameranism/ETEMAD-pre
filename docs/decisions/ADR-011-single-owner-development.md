# ADR-011 — Single-owner development with evidence-based acceptance

Status: Proposed (owner requested on 2026-11-26; not yet effective)
Baseline: 6c79c9bc6171ac870f89d45924eaf3adc21559b0

## Decision for review
Separate routine development from formal release/security acceptance. Permit routine, scoped development on task branches with fixture data and automated verification; preserve the existing independent review and Gate requirements for integration/release and changes affecting security, contracts, permissions, private data, publication or deployment. Do not represent automated tests as independent human approval or retrospectively mark Gate-00 PASS.

Use a separate governance change to reconcile AGENTS.md, the gate procedures and merge-guardian. Until that change is accepted, the existing policy remains effective.

## Repository rules proposal
Consolidate the two currently active rulesets (28107297 and 28107358) into one explicitly targeting the default branch, with no bypass actors. Require pull requests, successful applicable CI checks, prevent force pushes and branch deletion. Retain review requirements for sensitive changes; verify the exact supported check names and configuration before applying. Do not require unavailable CODEOWNERS teams or unconfigured deployment checks. Keep the existing rules until the replacement is verified.

## Acceptance evidence
- Verify all affected policy references agree; retain task scope, rollback and honest test reporting.
- Require passing contract validation, architecture boundaries, backend tests, lint and type checking; add applicable frontend/security checks.
- Test negative cases: missing or failed checks, unauthorized private data access, contract incompatibility, direct pushes and force pushes.
- Record actual branch protection, CI URLs and reviewed revision; missing checks remain NOT_RUN/BLOCKED.
- Existing backend CI has 7 lint and 4 type errors; fix them separately.
- No live credentials, publishing, deployment, schema or application changes are authorized by this ADR.

## Rollback
Close the proposal PR, or revert the accepted governance change through the normal reviewed process. Preserve original rule configuration until a verified replacement exists.
