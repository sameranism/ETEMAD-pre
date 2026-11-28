# Draft — P19 independent confidential access and staff eligibility

Status: PROPOSED / NOT ACCEPTED / NOT ENACTED. No ADR number assigned until
governance review. Original P19 scope and frozen contracts remain binding.

Owner intent: separately authenticated confidential environment, individual
credential, MFA and per-report explicit grants. Staff eligibility was discussed
but implementation is expressly blocked pending accepted policy. The existing
EIC-only policy is retained for Stage 2, including denial of owner/staff.

Decisions required: who may grant/revoke access, eligible roles and report
scope, independent credential enrollment/reset/recovery, session and audit
boundaries, least privilege, data retention and recovery, contract compatibility
(including private_report.owner_role), and whether owner/staff eligibility
requires a versioned contract or only a distinct access policy. No automatic
grant, inherited privilege, shared password, or private publication/export.

Acceptance before enactment: approved ADR and threat model; authorized schema
and wire changes if necessary; synthetic positive and negative tests per role
and slot, revocation, expiration, replay, independent storage/recovery,
no public leakage, and separate staging authorization. No code change expands
private report eligibility.
