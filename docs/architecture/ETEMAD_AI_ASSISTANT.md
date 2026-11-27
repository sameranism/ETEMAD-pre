# ETEMAD AI Assistant — دستیار هوشمند اعتماد

Owner-approved product direction, 2026-11-27. P16 remains in B06;
P20/P21 remain in B07. This is an additive product specification, not a
change to project IDs, scheduling, governance, frozen contracts or release rules.

## Product boundaries

* Internal assistant: authorized ETEMAD newsroom employees; organizational
  identity, editorial knowledge, private per-user memory and shared approved
  organizational knowledge. Use existing P02/P15/P16/P17/P18/P19/P26/P27
  capabilities via authorized versioned APIs; do not duplicate them.
* Public Studio: independent commercial subscription product, public customer
  identities and isolated customer workspaces, never newsroom identities/grants
  or private archive/report data. Only explicitly cleared public or user-owned
  content may cross the boundary. Shared infrastructure does not imply shared
  authorization, storage namespace, budget or credentials. Public signup,
  billing and actual Studio generation are not enabled by this slice.
* No verified newspaper history, staff details, branding or editorial rules
  were supplied. Use approved organizational documents as the source for those
  claims; do not invent them.

## First slice: existing functionality, new internal assistant surface

Base 16062d943eff2d5dd3773d7e0f7680eb5c4f28ae. Existing S05-003/S05-004
and P27 analysis workspace, plus owner-approved expansion scope.
GET /v1/analysis/assistant returns version 1.0 identity and grant-filtered
tool catalog. POST accepts an explicit action (research, analysis, public_report) or auto,
the existing evidence request and an optional public slot 1–4 (required only
for report). It delegates to existing handlers, retaining their response
contracts, source ACLs, rights, audit and output switches. No arbitrary tool
names, URLs, external calls, publication, or Studio dispatch. Auto selects only research or analysis from local user-text
intent rules; the selected handler checks current grants. The response adds
assistant_action outside the frozen result. UI defaults to auto and labels
current extractive mode honestly.
Existing chat context opt-in and delete behavior are unchanged.

## Remaining operational increments

* Grounded generative answers and bounded tool selection through P02's
  provider routing after authorized model configuration; cite permitted
  evidence, treat source text as untrusted, validate output and budgets.
* Durable multi-turn sessions with per-user/role ownership and current ACL
  revalidation, retention/deletion; approved organization knowledge ingestion.
* Additional typed adapters for editorial drafting, translation, editing,
  metrics, private reports (EIC only), and policy-gated publication.
* Text, table, chart, Word and PDF exports with rights checks; no private
  exports through public Studio.
* Commercial Studio identities, tenant isolation, quotas, subscriptions,
  payment authorization, synthetic-media disclosure and rights. Separate
  release; no live billing, public deployment or paid model calls here.

Rollback: revert this additive PR. No new database migrations or dependencies.
