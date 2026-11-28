# P23 — destination directory integration proposal

Status: partial implementation, migration authorization pending. S06-002/P23,
adjacent S08-017/P27. Baseline ec1fc388. Independent workstream; no dependency
on privileged MFA for development with fixture identities.

Recovered source: [PR #136](https://github.com/sameranism/ETEMAD-pre/pull/136),
head c8813a255c2a9bcb6af6d7ec8860c4c5558efc07; tracked local patch
415696fad37ac04ad944293ef03b931d7ba2ec7a, destination model
b73b3354db6156ebffae0c475bf7f196ec9667ab, archived UI
d20fbacd9f24b3a8abd2abf3ba6a5f9e90c0afa0. Preserve original worktree and
both archive PRs. The archived 014_destination_directory migration forks
013_daily_reports and MUST NOT run. Current main has 018_analyses; MFA
independent PR #137 proposes 019_privileged_mfa. Do not assign an executable
P23 revision until the actual canonical head is confirmed and approved.

This first slice restores the inactive candidate model, redacted directory
projection, deterministic identity, validation, configuration invalidation
and disabling. It does not provide API persistence, a directory UI, or
operational acceptance. No provider verification or delivery.

## Proposed additive central schema (approval required)

P23-owned publish.destinations, per B08 section 4 and database dictionary:
destination_id PK, platform, channel_ref, record JSONB (frozen
destination_config v1.0), created_by, created_at, and integer revision for
optimistic concurrency (not a change to the wire contract). Unique platform
and channel_ref; relational identity matches JSON; no raw credential, only
credential_ref. Candidate initial state unverified/disabled, enabled false.
Verification and enabling are separately authorized and not implemented by
the candidate CRUD slice. Downgrade refuses nonempty table. No replacement
of P02 control.config_revisions, migration 014 or historic revision IDs.

Required work after approval: durable registration/read/update/disable
transaction, workspace grant and per-destination read/update checks,
role-specific owner management and editor scoped read, audit and no-store,
safe concurrent registration, redaction of credential refs, API integration,
migration round-trip plus nonempty downgrade refusal and persistence after
restart. No automatic broad grants; no verification, dispatch or production
calls. Owner UI and editor scope require separate dashboard E2E.

Level A fixture now; Level B masked staging acceptance pending approved
inputs. Level C prohibited. No schema change performed here.
