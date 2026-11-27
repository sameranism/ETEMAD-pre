# ADR-020 — Operational product delivery / تحویل محصول عملیاتی

Date: 2026-11-27
Status: owner-directed decision; effective on protected merge.
Supersedes execution sequencing and acceptance shortcuts in ADR-019 and
historical QUEUE/coding_path entries; preserves their evidence and all applicable
functional, contract, security, privacy, migration and release requirements.

## Owner's policy / سیاست مالک

Tests are tools for confidence in product behavior, not the objective of the
project. A fixture/demo release is not the final product. The deliverable is
a usable, stable, secure, extensible ETEMAD newsroom meeting the approved
functional scope. A file, passing test, completed ticket, dry run, merged PR,
or green CI alone is not delivery. Optimize for verified user workflows and
operational outcomes, not test/PR counts, documentation volume or elapsed hours.

## Active task plan

`manifests/product_delivery_tasks.csv` is the authoritative execution assignment
for ALL 223 original task IDs. Join on task_id to `manifests/tasks.csv` and
`tasks.json` for the original target, prompt, references and acceptance criterion.
Original manifests, generated coding_path, prompts, 30 v1.0 contracts and 29
project / 9 package IDs are retained as the baseline for traceability. The new
plan changes execution grouping, priority, scheduling and evidence of delivery,
not product requirements or source contracts. No task is marked complete by
this decision. Seven existing studio tasks stay deferred until the owner
explicitly reopens them; final acceptance must disclose that scope exclusion.
Do not declare the original unrestricted 29-project scope delivered.

For executable project/PR planning, use [the 29-project roadmap](../ops/PROJECT_PR_ROADMAP.md) and [the exact task-to-PR-unit assignment](../../manifests/project_pr_tasks.csv). Unit IDs are planning scopes, not real PR numbers; retain actual dependencies and record actual PR/commit evidence in Issue #78.

## Product roadmap / نقشه راه

The rows below are workstreams, NOT serial gates. Choose eligible slices by
dependency, user value and remaining integration gap. Develop independent
workstreams concurrently when real execution resources exist: one writer per
owned capability, disjoint paths or agreed merge order, no conflicting DB
migration IDs, serialized runtime deployments. Never describe concurrency or
background work not actually running. Reuse functioning code; integrate before
rewriting. Group coherent repository/migration/service/API/UI/test changes
under their owning task IDs into one protected PR and deploy a usable release
bundle rather than each tiny commit. Each row has a concrete user outcome.

| Workstream | Ownership | Usable outcome and acceptance scenario | Dependencies |
|---|---|---|---|
| D01 Foundation | R01 / B01 P01/P02/P03/P28/P29; shared P27 | Authorized staff use versioned settings, source registry, role-scoped access, audit and reliable jobs; revocation denies access; persistent settings survive restart. | Contract/ownership and security baseline; advanced identity needed before privileged operation |
| D02 Iran | R01 / B02 P05 + P27 | Approved domestic feeds poll on independent schedule, preserve originals, resume after outage, deduplicate and show real status/news in dashboard. | D01 source approval/rights/settings, domestic DB, D04 processing interface |
| D03 World | R02 / B03 P04/P06 + P27 | Country/language discovery creates disabled candidates, authorized activation enables independent foreign polling; authenticated durable bridge sends validated news to central, dashboard exposes source/status/news. Tamper/replay rejected; restart does not duplicate. | D01 source approval, foreign DB, D04 contract; authorized live feed only later |
| D04 Processing | R03 / B04 P07/P08/P09 + P27 | From either stream, preserve source, normalize, reconstruct duplicate/event state after restart, classify, route and show priority/topic/source in newsroom. | Raw contract from D02/D03 (fixtures permit independent work) |
| D05 Editorial | R04 / B05 P10..P14 + P27 | Open a stored news item, translate foreign text, generate headline/lead/body/summary, revise/save/reopen, prepare rights-cleared images, show provenance and validation; no invented quote/number, immutable original, unverified rights hold. | D04 records, D01 model/rule configs and gateway, D06 archive interface |
| D06 Archive | R05 / B01 P26 + P27 | Retrieve/search originals, versions, citations, policy and publication history through authorized queries; restart preserves records; restricted content never leaks. | D01 persistence/roles; independent fixture records from producers |
| D07 Intelligence | R06 / B06 P15..P19 + P27 | Evidence-based analysis, source-linked chat, metrics, 4 public and 3 executive-private reports, appropriate role dashboard and independent output switches; unsupported claims and unauthorized access refused. | D04/D06 retrieval and provenance; model interface; private isolation prior to sensitive data |
| D08 Publishing | R07 / B08 P22..P25 + P27 | From approved content, configure destinations/independent schedules, render once per destination, dispatch idempotently and show results/retries; off switch/held/expired/private records cannot send; other destination survives failure. | D05 P11 decision, D06 audit/archive, D01 output controls and credential refs; official API rights and authorization for each live destination |
| D09 Dashboard | R08 / B09 P27 | Shared Persian RTL role navigation, filters, states/errors, citations, safe settings, editor/translator/analysis/reports/publishing views, responsive design; user workflows invoke actual authorized backend. UI ships with D01..D08, not postponed to the end. | Each owning feature's API; no duplicate credit for a dashboard shell |
| D10 Operational release | R09 / S09/S10 + advanced P28 | Repeatable deploy, actual backup restore, health/alerting, stable jobs/retry, capacity and security/privacy checks, authorized pilot with real sources/models/destinations, actual observed end-to-end operation, operator handoff. | Relevant D01..D09 requirements and approved external activation |
| HOLD Studio | B07 P20/P21 and S08-018 | Existing explicit deferral; no active studio implementation or activation. Keep output-off/privacy protections. | Explicit owner decision to reopen |

## Execution classes and task acceptance

Every task row has a delivery_id and execution_class:
- FEATURE: implement or connect real functional behavior, original task
  acceptance plus demonstrated workflow in its delivery group.
- FOUNDATION: prerequisite/control implemented when needed for a feature or
  operational release; do not make unrelated governance an aggregate blocker.
- VERIFY: run proportionate checks at the owning feature/integration milestone.
  Required CI always runs. Do not rerun an unchanged passing optional check.
- RELEASE: operational control or evidence needed before the affected live
  capability/production release, not a blanket prerequisite for unrelated
  safe default-off development.
- DEFERRED: the seven explicitly deferred studio tasks only.

Original task acceptance criteria are mandatory for the applicable scope; new
workflow acceptance is additional. Missing required references or an actual
dependency, failing relevant tests, security/data-integrity regression, failed
mandatory CI or migration blocks the affected delivery. Fix the issue or advance
another eligible workstream. No human development review required for ordinary
changes. No bypass of protected main, CI, privacy/security or contractual limits.
External activation and formal release still require applicable owner approvals;
unavailable real credentials, rights, official APIs or brand assets are explicit
dependencies, not reasons to halt independent implementation. Do not substitute
made-up official APIs, a paid model or an unauthorized channel. Unknown CMS stays
unactivated. Maintain the prior scope/deferrals and official adapter contracts.

## Evidence states and definition of done

Record per capability and task (not only per PR) using Issue #78 and STATE:
1. PLANNED / IN_PROGRESS: requirement mapped, or actual implementation underway.
2. IMPLEMENTED: real module/contract and needed behavior exist; remaining wiring
   or verification explicitly listed.
3. INTEGRATED: required CI passed on exact head, protected main contains change,
   dependencies/API/UI connected for the delivered scope.
4. VERIFIED_STAGING: exact private deployed release, durable data and authorized
   end-to-end workflow exercised with controlled fixtures, negatives as applicable.
5. READY_FOR_LIVE: applicable operational/security/rights/config/restore checks
   met, real integration requirements identified and approved; not a claim of live use.
6. LIVE_VERIFIED: authorized actual newsroom workflow exercised against real
   approved services, observable successful output, failures/retry and recovery
   evidenced; scope and environment recorded.
7. ACCEPTED: all applicable original acceptance and production exit criteria for
   this capability satisfied; actual operational evidence available, not only staging.

BLOCKED and DEFERRED are explicit flags with reason, dependency and next eligible
work; never disguise them as complete. No stage is inferred from file existence
or number of tests. For each transition record task IDs, capability, exact SHA,
PR and CI, environment, actual input/action/output, changed behavior and negative
check, known gaps, rollback and next work. Evidence can be reused for unchanged
behavior without another deployment/report. The existing five-distinct-B dashboard
milestone is an intermediate report (any five, P27 alone does not count): require
authorized deployed functional path to real backend state with fixture or approved
real data. It is not full package completion or production acceptance.

## Risk-based verification

Before ordinary merge: affected functional checks, negative cases where behavior
can fail, contract compatibility and mandatory exact-head CI. If changing auth,
privacy, cross-package boundary, output authorization, money/cost, or data
migration, run focused corresponding negative/security and durability checks.
Before private release: exact-main required checks, build once when changed,
backup/rollback for affected data, health and changed user workflow once. Reuse
unchanged artifacts when source trees match. Broad browser/load/soak and disaster
recovery are scheduled at integration/operational release, except when a changed
critical path requires focused testing immediately. Tests provide evidence, not
feature output; do not count test success as progress toward a missing capability.
Keep source contracts, audit, separate DB roles, private reports and output-off
controls intact. Prefer controlled pilot and observe actual outputs; no public
listener, live source, external provider, real credential, billing or publishing
until separately authorized. Do not remove legal/editorial source approvals.

## Production exit / معیار تحویل نهایی

All active approved workflows must be delivered and live-verified in the intended
newsroom environment, including authorized source lists, translation/editing with
the configured real model, archive/search, analysis/chat, 4 public and 3 private
reports, independent official destination adapters and schedules, role-based RTL
dashboard, and administrator/operator maintenance. The owner must authorize real
activation; record legitimate restrictions for channels and deferred studio rather
than claiming coverage. Verify relevant capacity from the original 500–5000
news/day scenario, agreed operating thresholds, observed data freshness/error
rates, duplicate behavior, access isolation, tested backup restore, rollback,
monitoring and actionable recovery. Do not invent numerical SLA/accuracy targets
not agreed; record measured values and any unmet baseline. Fix material defects
and document remaining accepted limitations. The final acceptance report must
map every active task and approved capability to evidence, not a percentage
computed from code paths or passed tests.

## Start from current evidence, not a restart

Issue #78 is the live ledger. As of the current review, private runtime
b8e5ebdfd0e4af0748a0440e3332e8fd9bcee1e5 includes B01/B02/B03/B04 and B05
preview slices, with verified synthetic backend behavior but incomplete
authorized interactive dashboard evidence. PR #100 translation/editor modules
merged as 92be0c55b611740d977b6af97dcda7d4083d670c; no deployed API/UI or
durable revisions for these modules yet. Next coherent slice: D05 storage,
revision, API/UI and source-linked workflows; independent D04/D06/D07/D08
implementation may advance as actual dependencies permit. Complete remaining
D02/D03 source/schedule/activation readiness in parallel where safe. Do not
redeploy unchanged code just for this roadmap. Preserve #79 exclusion.
