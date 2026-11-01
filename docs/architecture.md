# PSD401 Public Records Request System — Architecture & Framework

**Status:** Draft v0.1 — Planning
**Context:** Custom-built, Google Workspace–integrated system for managing Washington Public Records Act (RCW 42.56) requests end-to-end.

---

## 1. Legal & Compliance Requirements (Design Drivers)

These are the non-negotiables the system must encode. Verify each against current statute with your district counsel — the PRA gets amended regularly.

| Requirement | Source | System Implication |
|---|---|---|
| Respond within 5 business days (acknowledge, fulfill, seek clarification, or provide reasonable time estimate) | RCW 42.56.520 | Automatic deadline clock starting at receipt; business-day calendar aware of district/state holidays |
| Provide fullest assistance and prompt responses | RCW 42.56.100 | Status transparency for requesters; no dead-air states |
| Installment production allowed | RCW 42.56.080/.120 | Installment tracking per request, per-installment fee and delivery records |
| Exemption log: identify withheld/redacted records with statutory citation | RCW 42.56.210(3) | Structured exemption tagging on every redaction; auto-generated withholding index per production |
| Fee schedule (per-page copies, electronic per-GB/per-file caps, customized service charges) | RCW 42.56.120 | Configurable fee engine matching the district's adopted fee schedule |
| JLARC annual reporting (agencies >$100k staff/legal PRA costs) | RCW 40.14.026 | Capture all JLARC metrics natively: request counts, response times, staff hours, costs, litigation |
| Records retention | WA Secretary of State LGS retention schedules | Retention flags on request files themselves (the request file is itself a public record) |
| Student records privacy | FERPA (20 U.S.C. §1232g) + RCW 28A.605 | FERPA exemption category in redaction workflow; extra review gate on anything touching student PII |
| Employee privacy, security exemptions, etc. | RCW 42.56.230–.480 | Exemption taxonomy seeded with the full RCW 42.56 exemption list |

## 2. High-Level Architecture

```
┌─────────────────┐     ┌──────────────────────────────────────┐
│ Public request  │     │        Staff Application (SSO)        │
│ form            │     │  Dashboard · Case Mgmt · Redaction    │
└───────┬─────────┘     └───────────────┬──────────────────────┘
        │        HTTPS / REST+JSON       │
┌───────▼────────────────────────────────▼──────────────────────┐
│                     Core API (Django + DRF)                    │
│  Request lifecycle · Deadline engine · Fee engine · Audit log  │
└──┬───────────┬───────────────┬───────────────┬───────────────┘
   │           │               │               │
┌──▼───┐  ┌────▼─────┐  ┌──────▼───────┐  ┌────▼─────────────┐
│ PG 16 │  │ Redis +  │  │ Object Store │  │ Google Workspace │
│  DB   │  │ Celery   │  │ S3 / MinIO   │  │ Vault·Drive·Gmail│
└───────┘  │ (timers, │  │ (records +   │  │ Admin SDK · SSO  │
           │ exports) │  │ productions) │  └──────────────────┘
           └──────────┘  └──────────────┘
```

**Isolated front doors, one backend:**
- **Public intake** — the same-origin, anonymous, write-only endpoint feeds
  `submit_intake` directly after CSRF, signed-form, timing, honeypot, hard-rate,
  attachment, and idempotency controls. It has no read surface and returns only
  the request number.
- **Gmail transport** — migration-provisioned non-login Django identities run
  outbound delivery and disabled-by-default inbound polling as in-process
  Celery work. Django owns rendering, leases, raw evidence, MIME safety,
  matching, and audit; Gmail is only the mailbox transport. See
  [`gmail-transport.md`](gmail-transport.md). A future requester
  tracking/download portal uses opaque magic-link authorization, never a
  predictable request identifier alone.
- **Staff app** — direct Google Workspace OIDC. The immutable Google subject
  identifies the account; existing district groups map to Records Officer,
  Intake Clerk, Department Liaison, Reviewer, and Application Admin through audited runtime
  configuration, so no group-name prefix is hardcoded. Application Admin and
  Records Officer remain distinct roles and require separate explicit mapping
  rows. The application allows one Workspace group to be assigned to both;
  group separation is an operational governance decision.

## 3. Recommended Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python 3.12 / Django 6 + Django REST Framework | Batteries-included admin, mature auth, your team can move fast; huge FOSS ecosystem for docs/OCR |
| Frontend | React (Vite) or Django templates + HTMX for v1 | HTMX keeps v1 simple; React if you want a richer redaction UI later |
| Database | PostgreSQL 16 | Full-text search (pg_trgm/tsvector), JSONB for flexible metadata, row-level security |
| Queue/scheduler | Celery + Redis | Deadline timers, Vault export polling, OCR jobs, email ingestion |
| Object storage | S3 for production; MinIO for isolated storage-contract tests through the same API | Exercises the portable integrity boundary without creating another deployment target; use S3 encryption/versioning in production, apply legal-hold object lock only where it cannot block verified cleanup, and govern adopted raw-message retention from database evidence rather than a blanket quarantine lifecycle |
| Email in/out | Gmail API (ingest a records@psd401.net inbox) + transactional out | Requests arrive by email constantly; auto-create cases from inbox |
| OCR | ocrmypdf (Tesseract) | Makes scanned docs searchable pre-review |
| Redaction | pikepdf/PyMuPDF pipeline that **burns redactions in** (rasterize redacted pages) + exemption metadata sidecar | Never ship "black box overlay" redactions — flatten to image so text is unrecoverable |
| Auth | Direct Google OIDC (staff), local-only developer auth, magic links (future requesters) | Zero new production staff passwords; developers need no cloud access |
| Infra | Docker Compose locally and on one production EC2 host defined in `infra/cdk` (TypeScript CDK) | Local/CI validation precedes one serialized deployment to the production host; ~$59/month (t3a.large, sized for ClamAV's in-memory signature database) beats ECS/RDS at this volume |

**FOSS components worth pulling in rather than building:** ocrmypdf, Tesseract, Apache Tika (text extraction from odd formats), ClamAV (scan uploads), MinIO, Keycloak (only if you outgrow plain Google OIDC).

## 4. Core Data Model (v1 entities)

- **Request** — id (public-facing like `PRR-2026-0142`), requester contact, description, received_at, channel (portal/email/mail/in-person), status, assigned_officer, statutory_due_date, estimated_completion, tolling periods (clarification pauses)
- **Requester** — minimal PII by design
- **StatusEvent** — every transition, timestamped, actor, note (feeds requester-visible timeline + audit)
- **CollectionTask** — per-department/system search assignment ("HR: search personnel file for X", "Vault export: mailbox Y, terms Z, date range")
- **RecordItem** — individual responsive record: source, custodian, hash, OCR text, review status (unreviewed / responsive / non-responsive / duplicate)
- **Redaction** — page/region coordinates + **ExemptionCitation** (FK to seeded RCW 42.56 / FERPA taxonomy) + brief explanation
- **Production/Installment** — set of released RecordItems, exemption log document, released_at, delivery method
- **FeeEstimate / Payment**
- **AuditLog** — append-only (separate table, no updates/deletes at app layer; consider pgaudit too)
- **TimeEntry** — staff hours per request (JLARC needs this)

Requester correspondence is a separate bounded context: immutable messages and
recipient snapshots are distinct from lease-controlled delivery jobs, inbound
transport receipts, and clarification-cycle state. See
[`correspondence.md`](correspondence.md) for the ownership boundary, lifecycle,
and invariants. Request status effects still go through
`apps.records.services.transition()` only after confirmed delivery.

Inbound object storage also has an explicit database boundary. A durable
pre-I/O quarantine reservation allocates each generated raw-message or
attachment key, byte count, and digest before any external write; an immutable
transport-alias ledger binds hashed idempotency/provider identities to that
canonical evidence across credential rotations and reservation generations.
Each upload is one bounded S3 `PutObject`, never a multipart upload. The
durable processing lease is committed before each storage I/O. Django
reacquires and rechecks the writer lock before the PUT. It holds that lock
through the PUT and marks the generation stored in the same transaction. The
lease is refreshed after parsing, after each attachment stage, and immediately
before adoption. This order prevents cleanup from racing a writer after
database-lock loss or a late PUT. The stored generation is atomically adopted
by its receipt or attachment with the domain commit. If the process disappears
first, a leased recovery sweep waits for both the created-at grace period and
processing-lease expiry, then
cross-checks the exact domain references, heals a proven reference, or
physically deletes the orphan with persisted backoff. This separates
unadopted staging from preserved evidence. Adopted raw RFC822 is retained legal
evidence and must never be lifecycle-expired. For raw messages, only
application-verified unadopted reservation generations are purgeable. Adopted
and unadopted raw objects use the shared
`quarantine/correspondence/raw/` prefix, so prefix-wide object lock cannot
distinguish adopted evidence from unadopted generations and would block
required orphan cleanup. Never add a lifecycle expiration to that prefix.
Govern raw-message retention from the durable receipt/adoption evidence; the
application's reference-checked cleanup is the only deletion path for an
unadopted raw generation. Legal hold for responsive-record storage remains a
separate policy.

## 5. Google Workspace Integration

This is your biggest lever — most responsive records live in Gmail/Drive.

1. **Vault API** (service account, domain-wide delegation, Vault admin privilege):
   - Create a **matter** per request, apply **holds** on relevant custodians immediately (preservation!), run searches, create **exports**.
   - Celery job polls export status → downloads to object store → unpacks (Vault exports are mbox/zip) → converts messages to PDF → OCR → into review queue with chain-of-custody metadata.
2. **Drive API** — targeted file retrieval once reviewers identify responsive Drive docs.
3. **Gmail API** — in-process Celery workers send approved correspondence and
   poll original RFC822 requester replies under separate tokenless system
   identities. Django alone reserves raw storage, parses MIME, scans
   attachments, matches conversations, and records audits.
4. **Admin SDK Directory** — map staff to departments for collection task routing.

⚠️ Scope the service account tightly, log every delegated call to the audit trail, and document this in your data governance review — domain-wide delegation is powerful.

## 6. Key Workflows

**Intake → Acknowledge (Day 0–5):**
signed public form → canonical idempotent submission → request created →
officer assigned → 5-day clock starts → templated acknowledgment (fulfill now / need
clarification / time estimate). Clarification requests pause the clock (tolling
recorded).

**Collect:** officer defines search scope → Vault holds + searches fire → department liaisons get manual collection tasks with due dates → everything lands in the review queue, deduplicated by content hash.

**Review & Redact:** page-by-page review UI → mark responsive/non-responsive → draw redactions → every redaction requires an exemption citation (enforced) → second-reviewer gate for anything flagged FERPA → burn-in on production.

**Produce:** assemble installment → auto-generate exemption/withholding log → fee calculation → requester notified → secure download (expiring links) → repeat until closed.

**Close & Report:** closure letter, retention clock starts, JLARC counters already populated.

## 7. Phased Roadmap

**Phase 1 — Tracking core (4–8 wks):** Request/Requester/StatusEvent models,
staff authentication, deadline engine + notifications, public intake,
in-process Gmail transport, and audit log. Requester tracking is a later
magic-link surface.
**Phase 2 — Collection (4–6 wks):** Vault matters/holds/searches/exports, collection tasks, object storage, OCR pipeline, review queue.
**Phase 3 — Redaction & production (6–8 wks):** redaction UI, exemption taxonomy, burn-in pipeline, exemption logs, installments, requester download portal.
**Phase 4 — Fees, reporting, polish:** fee engine, JLARC export, dashboards, time tracking, retention automation.

## 8. Security & Risk Notes

- Encrypt at rest (KMS/SSE) and in transit; records under review are often *more* sensitive than what gets released.
- Append-only audit log; the system will be scrutinized in PRA litigation — assume every action gets deposed.
- Backups with tested restore; a lost request file is itself a PRA violation risk.
- Penalty exposure under RCW 42.56.550 is per-day — the deadline engine and notification failsafes (escalate to supervisor if officer misses acknowledgment) are your cheapest insurance.
- Get counsel review of: fee schedule config, exemption taxonomy, acknowledgment letter templates, retention settings.

## 9. Open Questions

1. Annual request volume? (Shapes how much workflow automation pays off in Phase 1 vs later.)
2. Does the district already use Google Vault (license tier — Vault needs Workspace for Education Plus or add-on)?
3. Payment processing: online payments needed, or invoice/check via existing district finance flow?
4. Any records in non-Google systems (SIS like PowerSchool/Skyward, HR/finance ERP, security camera footage)? Manual-upload path covers these in v1.
5. ~~Hosting call~~ — superseded 2026-08-18: production is the sole deployment destination, on one dedicated EC2 host in the district's AWS account, defined in CDK TypeScript (`infra/cdk`) with records in S3. Local/CI validation replaces the retired district-hosted dev promotion target. See [ADR 0002](decisions/0002-production-only-deployments.md) and the deployment target in the README.

## 10. Repository layout

`AGENTS.md` carries the one-line version of this table; the annotated detail
lives here.

- `config/` — settings, celery (beat schedule lives in `config/celery.py`).
  Settings the district can change do NOT live here: see `apps/configuration/`.
  What stays in `config/settings.py` is credentials, identity, infrastructure
  addresses, and the eleven Phase A switches kept as a bootstrap seed. A bound
  belongs in the configuration registry, enforced on an administrator's save,
  not in `config/settings.py` raising at import and crash-looping a container.
- `apps/configuration/` — the in-app settings store. `registry.py` is the typed
  list of every district-managed setting with its safe default, its bounds, and
  the consequence text an admin reads; `values.py` is the one place a value is
  parsed and validated; `prerequisites.py` holds the checks that span more than
  one setting; `runtime.get(key)` is the single read path (never
  `settings.PRR_*` for a registered key, and never a module-level constant or
  default argument — both are evaluated once at import, which is the
  restart-bound behaviour this replaces) and takes effect without a restart in
  web, worker, and beat; `services.set_value()` is the only write path and
  requires an Application Admin, a substantive reason, and appends to an
  append-only history plus the audit log. Every district-tunable value lives
  here: the on/off switches, and every limit, rate, timeout, DPI, domain list,
  address list, and the advisory model. Read a value once before a loop, not per
  iteration: the cache is shared, so a read per item is a round trip per item,
  and every item in one batch should be judged against the same limit.
- `apps/records/` — core domain: `models.py`, `deadlines.py` (RCW 42.56.520
  business-day engine — READ ITS DOCSTRING before touching), `services.py`
  (lifecycle), `tasks.py` (watchdog + Vault polling), scoped intake API.
- `apps/records/handling.py` — which review path a record's media type allows;
  `derivatives.py` (PDF renditions), `media_review.py` (audio/video timelines),
  `opaque_review.py` (records nothing can render). READ
  [`record-formats.md`](record-formats.md) before adding a format or a converter.
- `apps/api/` — staff DRF API, session/token auth, SPA serving, and the
  machine-readable GUI/agent capability contract (`capabilities.py`).
- `apps/audit/` — append-only audit log, `audit()` helper.
- `apps/integrations/gmail/` — keyless Gmail API client used by in-process
  outbound and inbound Celery work.
- `apps/integrations/vault/client.py` — Google Vault (matters/holds/exports).
- `frontend/` — React/Vite/TypeScript staff workspace; compliance logic stays
  in Django.
- `integrations/mcp/` — hosted and standalone MCP facades over the staff REST
  API.
- `templates/correspondence/` — versioned requester correspondence templates
  (COUNSEL REVIEW REQUIRED markers).
- `templates/sops/` — SOP templates (`personal-device-search.md` = the Nissen
  procedure; counsel review required).
- `docs/` — development.md, architecture.md, build-vs-buy.md, record-formats.md,
  compliance-rules.md, and custodian-gap-analysis.md (the design-review record:
  every record location, custodian, and why — READ THIS before changing registry
  or triage scope). [`roadmap-usability.md`](roadmap-usability.md) is the phased
  plan for the next work (2026-09-21) with the decisions that bound it; tracked
  as one GitHub issue per phase.
- `openwiki/` — auto-generated repo wiki (OpenWiki, refreshed by the scheduled
  GitHub Action; start at `openwiki/quickstart.md`; don't hand-edit).
- `infra/cdk/` — AWS CDK (TypeScript, bun): `psd401-prr-prod` hosts production.
  `psd401-prr-terraform-state` holds the Google identity root's Terraform state.
  `infra/aws/` — the app-backup timer and isolated restore
  verifier; `infra/terraform/google-identity/` — the Google side. AWS is CDK
  only: no console or CLI provisioning, and no Terraform.
