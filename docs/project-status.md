# Project Status Ledger — kickoff-item review, 2026-08-02

> **This ledger is a point-in-time record of the 2026-07-01 kickoff items, not
> a current status page.** The verification counts and gate description below
> describe the 2026-08-02 review and have not been maintained since. For what
> is true now: `bash scripts/verify-fast.sh --full` is the gate, the backend
> suite is ~1,600 tests, and the release-gate machinery this document refers to
> was retired — deployment is the single CDK command in `docs/aws-deploy.md`.
> Off-repo items must be confirmed with the project team rather than inferred
> from this file.

Status of every public-records action item from the 2026-07-01 kickoff
meeting, verified against this codebase (nine-agent code-and-law review,
2026-07-08; all statute citations checked against current text at
apps.leg.wa.gov). Statuses: **Done**, **Partial**, **Not started**,
**Recommend against** (conflicts with statute or the compliance
architecture), **Off-repo** (coordination/people work not visible from the
code).

The kickoff-item classification below is preserved from the original review.
Off-repo work may have advanced and must be confirmed with the project team
rather than inferred from this repository.

The Phase 2 completion branch passed its 2026-08-02 local release gates: 1,298
Django tests (28 expected skips), 28 PostgreSQL legal-workflow race tests, 155
frontend tests plus lint/build, 40 CDK tests plus typecheck/synth, migration
drift and strict OpenAPI checks, the production image build, all three Compose
overlays, ShellCheck, Bandit, the installed Python-environment audit, and the
production frontend dependency audit. CI remains the authoritative Linux
lockfile-environment audit and merge gate. What was implemented as of those gates
is recorded once, in
[Implemented capability snapshot](#implemented-capability-snapshot-verified-2026-08-02)
below.

## A. Process overhaul and automation

| # | Item | Status | Notes |
|---|---|---|---|
| A1 | Public request form and intake | Complete | The accessible `/request` form and anonymous, write-only Django intake primitive are complete: same-origin CSRF, signed form/fill-time/honeypot gates, Redis-backed per-IP/global hard ceilings, bounded quarantined attachments, and a separate idempotency receipt all feed the canonical compliance pipeline. Portal and email-channel intake also queue one explicit non-statutory submission receipt through the durable Gmail delivery machinery without affecting the day-5 response. |
| A1b | "Within ninety days" letter language | Recommend against | No 90-day production statute exists anywhere in RCW 42.56 or WAC 44-14 (verified). WAC 44-14-04003 calls flat form-letter estimates "rarely reasonable." The letter already states a computed per-request estimate; keep that. |
| A2 | Confirm statutory timelines, document findings | Done (research) | See Legal findings below. Findings write-up for the district record still to be authored. |
| A3 | AI intent parsing (search terms and refinement prompts) | Amazon Bedrock implementation selected; deployment verification pending | Async request-intent proposals use keyless Bedrock Nova 2 Lite inference, retain model/input provenance, and are permanently non-applying. The model cannot propose or apply charges, deadlines, status, or requester communications. Representative evaluation remains an operational acceptance step; see `docs/advisory-ai.md`. |
| A4 | Tracking platform decision: Freshservice vs self-hosted | Direction implemented; organizational sign-off pending | This application is the PRA system of record and Freshservice remains IT ticketing; no application integration is exposed. The supporting analysis is in `docs/build-vs-buy.md`, and E2 migration tooling is complete. District ownership/sign-off remains off-repo. |
| A5 | Automated denial for out-of-retention requests | Auto-denial rejected; accountable workflow complete | Automation still only places the preservation hold/checklist. An interactive Records Officer must complete structured reasonable-search evidence, retention/obvious-lead checks when applicable, and a counsel approval reference. Only confirmed delivery of the versioned closure applies `closed_no_records`; see `docs/no-records-closure.md`. |
| A6 | Vault export → EML → Thunderbird review flow | Superseded by in-app pipeline | The resumable Vault pipeline retains export evidence, ingests MBOX/ZIP data into integrity-checked originals and review artifacts, deduplicates, records failures, and creates `RecordItem` review rows. Staff review in the application; Thunderbird is no longer a required legal-record step. Production Google credentials and a representative export rehearsal remain external gates. |
| A7 | Small-model responsiveness filter | Advisory implementation complete; external approval/evaluation pending | OCR-backed proposals are restricted to responsive, non-responsive, or uncertain with confidence/matched terms/rationale. A DB constraint keeps every run `applied=false`; the UI may preselect a completed suggestion, but it never submits it and only a named human confirmation sets `review_state`. Exact hits remain in the queue. |
| A8 | Back-test on prior requests | Harness/tooling ready; external sample pending | The safe migration dry-run/commit path preserves historical dates and provenance without replaying live intake side effects; the fee/JLARC models can retain reviewed classifications and schedule evidence. The district must select a representative benchmark export and adopted schedule. Re-submitting through the live API remains invalid. |
| A9 | Chat group + share notes | Off-repo | |
| A10 | Front-office verbal/paper intake | Complete (code) | The staff intake API and SPA screen call the centralized `submit_intake` service with the original receipt time, bounded quarantined attachments, actor-scoped idempotency, and full deadline/triage/audit behavior. A dedicated Intake Clerk role is write-only for intake and does not inherit request PII reads or legal-officer authority. Training remains off-repo. |

## B. Pilot / six-week cycle infrastructure

| # | Item | Status | Notes |
|---|---|---|---|
| B1 | Chat space PR-# | Off-repo | |
| B2 | Transcript summaries | Off-repo | |
| B3 | Problem-of-practice draft | Off-repo | |
| B4 | AI-generated task list | Partial | This ledger is the raw material. |
| B5 | RCW + State Archivist research | Partial | RCW 42.56 side done and verified. RCW 40.14 / State Archivist LGS retention schedules not yet researched; C1 depends on them. |
| B6 | Task ownership assignment | Off-repo | |
| B7 | Automation-platform access model for non-technical users | Superseded | The application owns its transport edges; no separate workflow platform is in a request or correspondence path. |
| B8 | Team automation-platform dogfooding | Superseded | The external workflow-platform design was retired. |

## C. Data retention

| # | Item | Status | Notes |
|---|---|---|---|
| C1 | New retention schedule (2-year email idea) | Not started | Mostly district/counsel policy work off the LGS schedules. The system stores retention per-system as data. Deliberately no auto-purge code: disposition automation must check for open requests first or it recreates the spoliation risk the footage-hold design exists to prevent. |
| C2 | S3-based legal holds | Not started (code) | Google Vault holds work today (source-system holds). Avigilon Alta Aware bookmark holds are in code but not yet verified against the district tenant (`manage.py verify_alta`); until they are, a footage request whose API hold fails degrades to the urgent manual-hold email to facilities. S3 side is connection config only: no client module, no Object Lock, no hold model. |

## D. Proof of concept and metrics

| # | Item | Status | Notes |
|---|---|---|---|
| D1 | Success metrics | Complete in code; annual human inputs required | Reproducible JLARC reporting combines lifecycle, correspondence, production, redaction, time, fee/payment, and append-only annual supplement evidence. It produces all fifteen metrics, public log data, quality warnings, and a content digest without external submission. Finance/legal annual inputs remain accountable human records. |
| D2 | Trial re-run of an old request | External benchmark pending | Safe NextRequest dry-run/commit tooling and exact-plan cutover checks exist, but the district must select and provide a representative historical export for rehearsal. Live API resubmission remains prohibited. |
| D3 | Full task/process baseline | Partial | Status, delivery, collection, production, fee, and JLARC evidence is now queryable and reportable. The organizational baseline narrative and target measures remain a project-team artifact. |

## E. Other / risk list

| # | Item | Status | Notes |
|---|---|---|---|
| E1 | Weekend autonomous run + review (7/05–7/06) | Off-repo | Dates passed. |
| E2 | NextRequest data migration | Tooling complete; external rehearsal pending | Mapping-driven dry-run/commit, immutable provenance, explicit replay-safe activation, and exact-plan cutover gate are implemented. A representative district export, correspondence/attachment manifests, rehearsal, and final delta remain accountable external handoffs; see `docs/nextrequest-migration.md`. |
| E3 | "Five cents a record" cost language | Misquote rejected; governed fee engine complete | The application supports versioned, counsel-approved schedules, itemized assessments, deposits/payments, work authorization, and release gating. No production fee schedule is inferred or auto-activated; counsel and the district must adopt and enter the applicable RCW 42.56.120 schedule before fee communication. |
| E4 | District data map | Partial (head start) | The records-system registry with custodians, retention, and keywords is a machine-readable seed for whoever owns the district-wide item. |

## Legal findings (verified 2026-07-08 against current statute)

- **Initial response: 5 business days** from receipt (RCW 42.56.520;
  counting starts the day after receipt per RCW 1.12.040). Permissible
  responses: provide records, provide an internet link, acknowledge with a
  reasonable estimate, seek clarification, or deny with written reasons.
- **No Washington statute imposes a 90-day production deadline.** The
  standard is an individualized reasonable estimate (WAC 44-14-04003),
  fullest assistance (RCW 42.56.100), and installment production
  (RCW 42.56.080, .120).
- **Penalties** (RCW 42.56.550): attorney fees and costs plus a
  discretionary penalty up to $100 per day; the agency bears the burden of
  proving any estimate reasonable. *Yousoufian v. Sims*, 168 Wn.2d 444
  (2010) supplies the aggravating/mitigating factors; unjustified fixed
  promises and automated estimate-kicking are aggravators, which is why
  those paths are test-enforced human-only in this codebase.

## Sharpest edges

1. B5 (State Archivist/LGS schedule research) and district/counsel policy still
   block C1; the application deliberately does not infer a retention period.
2. Production cloud, Google, malware-scanner, OCR, and outbound/inbound mail
   credentials and readiness checks must be exercised in the district account.
3. E2's representative export and rehearsal gates block any cutover date until
   the district completes `docs/nextrequest-migration.md`.
4. Policy requires advisory AI to remain disabled until provider/data approval
   and representative evaluation are complete; no model output is ever applied
   automatically. A 2026-08-18 read-only audit found both effective production
   AI switches enabled without that retained evidence in this ledger. Treat
   this as an unresolved configuration/approval mismatch: obtain the named
   record or disable both before the next release. Repository defaults now fail
   closed.
5. Counsel must approve fee schedules and the no-records/out-of-retention
   template/checklist before either is used with requesters.

## Implemented capability snapshot (verified 2026-08-02)

Moved here from `AGENTS.md`, which now links to this ledger instead of carrying
a dated status paragraph, and merged with the status summary that opened this
ledger. Like the rest of this file it is a point-in-time record.

Since the original ledger, direct Google OIDC and group-mapped staff roles,
token-authenticated staff/agent API access, OpenAPI docs, MCP transports, and
the multi-user deploy stack have landed. Hosted `/mcp` exposes exactly four
attributed personal-token draft/proposal tools; standalone stdio and the shared
nonstaff MCP identity remain read-only.

Anonymous write-only public intake, the same canonical intake service every
intake channel uses, runs the compliance pipeline with
structured NextRequest-equivalent requester fields and quarantined,
malware-scanned attachments; deadline engine (WA holidays incl. Juneteenth,
district closures, after-hours rollover, tolling), hourly escalating watchdog,
exemption taxonomy seed, append-only audit, durable requester-correspondence
drafting, approval, queueing, leased delivery, reconciliation, inbound matching,
and day-5 safety net; outbound sending and disabled-by-default inbound Gmail
polling run in-process; Vault client (matters/holds/exports/poll), React staff
workspace over a role-scoped DRF API and production (`prr.psd401.net`) with
direct Google OIDC, plus administrator-configurable Workspace group mappings and
fail-closed session revalidation, least-privilege staff intake for
phone/mail/in-person requests, local developer identity mode, OpenAPI docs,
hosted and standalone MCP transports, superuser-only break-glass admin, and
statutory letter templates.

Phase 2 is implemented in-repository: integrity-checked object storage and
scoped installment downloads; resumable Vault export ingestion; human review,
citation-backed redaction, second review, and raster burn-in; exemption logs,
production manifests, release, and correspondence; JLARC reporting; governed
fees; safe NextRequest migration; advisory-only AI; and human-only no-records
closure.

Backend, frontend, infrastructure, schema, security, Compose, and
production-image checks are development validation and do not control
deployment. The remaining work is operational, not missing application
implementation; it is listed in
[`phase-2-production-handoffs.md`](phase-2-production-handoffs.md).
