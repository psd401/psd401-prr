---
type: Architecture
title: System Architecture Overview
description: PSD401 PRR system architecture including Django backend, React frontend, Postgres/Redis/MinIO infrastructure, and Google Workspace integration.
tags: [architecture, infrastructure, technology-stack]
---

# System Architecture Overview

High-level architecture for the PSD401 Public Records Request System, a
custom-built PRA request management system for Peninsula School District 401.

## Architecture Diagram

```
┌─────────────────┐     ┌──────────────────────────────────────┐
│  Public Portal   │     │        Staff Application (SSO)        │
│  (POST intake)   │     │  Dashboard · Case Mgmt · Redaction    │
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

## Public Request Intake

- **Accessible submission form** at `/request` with CSRF protection, honeypot, and rate limiting
- **Write-only endpoint** — `POST /api/public/requests/` accepts multipart form data
- **No requester tracking route yet** — a future portal must use opaque magic-link authorization rather than a predictable request number

## Staff Application

- **React SPA** — staff workspace routes defined in `frontend/src/App.tsx`, including Identity Settings for Application Admins; the route guard, navigation roles, and API client are described in [staff frontend](staff-frontend.md)
- **Session-authenticated** — Production uses Google OIDC with PKCE and exact callback URI; local mode uses Django session auth
- **Token-authenticated** — narrowly scoped DRF tokens for agents/scripts
- **Staff/scoped-read gated** — staff have normal access; the nonstaff MCP identity can use safe read methods only
- **SSO implemented** — Direct Google OIDC with passwordless login, hosted domain verification, and role refresh through workspace groups

## Technology Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Backend | Python 3.12 / Django 6.0 (`Django>=6.0.7,<6.1`) + DRF | Core API, admin, auth |
| Frontend | React 19 + TypeScript + Vite | Staff workspace SPA |
| Database | PostgreSQL 16 | Full-text search, JSONB |
| Queue | Celery + Redis | Deadline timers, Vault polling |
| Object Storage | MinIO (dev) / S3 (prod) | Records, productions, presigned downloads |
| Email | Gmail API | Intake from the delegated intake mailbox, transactional out |
| OCR | ocrmypdf (Phase 2) | Searchable scanned docs |
| Auth | Google OIDC (staff), Token (agents) | Passwordless SSO for staff |

## Django Applications

### Core Apps

| App | Purpose | Key Files |
|-----|---------|-----------|
| [`apps/records`](../../apps/records/) | Core PRA domain: requests, requesters, deadlines, triage, collection, review, advisory AI | `models.py`, `services.py`, `deadlines.py`, `triage.py`, `vault_*.py`, `advisory_ai.py` |
| [`apps/correspondence`](../../apps/correspondence/) | Requester correspondence, Gmail transport, inbound mail | `services.py`, `inbound.py`, `gmail_sync.py`, `operations.py` |
| [`apps/work_queue`](../../apps/work_queue/) | Current next-action ownership, separate from legal status | `models.py`, `services.py`, `source_reconciliation.py` |
| [`apps/configuration`](../../apps/configuration/) | District-managed settings store, secrets, and sign-in guards | `registry.py`, `runtime.py`, `services.py` |
| [`apps/api`](../../apps/api/) | Staff REST API, SPA serving, auth, agent capability contract | `auth.py`, `identity.py`, `capabilities.py`, `resources/*.py` |
| [`apps/audit`](../../apps/audit/) | Append-only audit log | `models.py` |

### Integration Apps

| App | Purpose | Key Files |
|-----|---------|-----------|
| [`apps/integrations/vault`](../../apps/integrations/vault/) | Google Vault API client | `client.py` |
| [`apps/integrations/avigilon`](../../apps/integrations/avigilon/) | Avigilon Alta camera hold API | `client.py` |
| [`apps/integrations/gmail`](../../apps/integrations/gmail/) | Gmail API transport client | `client.py` |

### Config

| Module | Purpose |
|--------|---------|
| [`config/settings.py`](../../config/settings.py) | Django settings, DRF config, database; credentials, identity, and infrastructure addresses |
| [`config/celery.py`](../../config/celery.py) | Celery beat schedule, periodic tasks ([background jobs](background-jobs.md)) |
| [`config/urls.py`](../../config/urls.py) | URL routing |
| [`config/asgi.py`](../../config/asgi.py) | ASGI composition: Django plus hosted `/mcp` ([MCP](../integrations/mcp.md)) |

## Data Model (v1 Entities)

### Core Domain

- **PublicRecordsRequest** — Central request record. Its public number is prefixed by matter kind (`PRR-`, `LIT-`, `FER-`, then the year and sequence; see [matter kinds](../domain/matter-kinds.md))
- **Requester** — Minimal-PII requester (email required)
- **RequestStatus** — Lifecycle enum that includes clarification, installment, closed, and denied states alongside received through fulfilled. The full transition rules are in [requests](../domain/requests.md)
- **StatusEvent** — Append-only lifecycle log

### Collection & Review

- **CollectionTask** — Search assignments (Vault or manual)
- **RecordSystem** — District system registry with triage keywords
- **RecordItem** — Responsive records in review queue
- **Redaction** — Redaction regions with required exemption FK

### Exemptions & Production

- **ExemptionCitation** — RCW 42.56 / FERPA exemption taxonomy
- **FootagePreservation** — Surveillance footage holds
- **Installment** — Record productions to requester, packaged and released behind an expiring link ([installments and release](../domain/installments-and-release.md))
- **EstimateRevision** — Deadline change audit trail

See [domain/requests.md](../domain/requests.md) for the full data model.

## Key Workflows

### Intake → Acknowledge (Day 0–5)

```
portal/email → request created → dedupe check → officer assigned
  → 5-day clock starts → templated acknowledgment
  → clarification requests pause the clock (tolling)
```

### Collect

```
officer reviews a Vault count (estimate only, no hold or export)
  → officer confirms the collection on the reviewed scope
  → Vault hold and export run; the export is ingested into record items
  → department liaisons get manual collection tasks
  → everything lands in review queue, deduplicated by content hash
```

See [collection-and-vault.md](../domain/collection-and-vault.md).

### Review & Redact (Phase 2)

```
page-by-page review UI → mark responsive/non-responsive
  → draw redactions → every redaction requires exemption citation
  → second-reviewer gate for FERPA-flagged items → burn-in on production
```

Phase 2 is implemented in the repository: review and redaction, the Vault ingest
pipeline, OCR, and integrity-checked object storage are in code. What remains is
operational (credentials, approvals, rehearsals, and cutover), tracked in the Phase 2
production handoff document (an operational document outside this wiki).

## Google Workspace Integration

### Vault API

The application impersonates a delegated Vault identity (`PRR_VAULT_IMPERSONATION_TARGET`;
deployed access is keyless, see [aws-deployment.md](../operations/aws-deployment.md)) for:

1. **Matters** — Created per request, reused if it exists
2. **Counts** — An entire-organization or account-scoped Gmail count that creates a matter but no hold
3. **Holds and exports** — Started from a reviewed, completed count, or, only with `PRR_TRUST_SCOPE_MODE=auto`, from a finished automatic scoping session's final count
4. **Ingest** — Celery polls exports, downloads them to the object store, and converts messages into record items

Details are in [collection-and-vault.md](../domain/collection-and-vault.md).

### Gmail API

- **Inbound** — The minute-scheduled poller reads the intake mailbox and hands each message to the receiving pipeline ([correspondence](../domain/correspondence.md#gmail-transport))
- **Outbound** — Approved correspondence, including acknowledgments, is sent through Gmail by a Celery worker ([correspondence](../domain/correspondence.md))

The repository has no Google Drive or Admin SDK integration. Staff roles come from
Google groups and direct grants, not from directory lookups ([Google identity](google-identity.md)).

## Security & Compliance

### Audit Trail

- **Append-only** — `AuditLog` and `StatusEvent` have no update/delete paths
- **Attribution** — `audit()` records the action, target, and actor, plus the client IP when the caller supplies one (`apps/audit/models.py`)

### Deadline Engine

- **Business-day aware** — WA holidays, district closures
- **After-hours rollover** — Receipts after close-of-business roll to next day
- **Tolling** — Clarification requests pause the clock
- **Watchdogs** — Escalating deadline notifications

### Preservation

- **Auto-hold always** — Camera footage in retention window held immediately
- **Auto-deny never** — No automated denial path; human attestation required
- **Source-system holds** — Google Vault and Avigilon Alta preservation

## Related Documentation

- [architecture/google-identity.md](google-identity.md) — Google Workspace SSO and RBAC
- [architecture/configuration.md](configuration.md) — District-managed settings store
- [architecture/agent-parity.md](agent-parity.md) — GUI/agent capability contract and human handoffs
- [domain/requests.md](../domain/requests.md) — Core request lifecycle
- [domain/matter-kinds.md](../domain/matter-kinds.md) — Public records, litigation, and FERPA matters
- [domain/work-queue.md](../domain/work-queue.md) — Next-action ownership and the Inbox
- [domain/case-workspace.md](../domain/case-workspace.md) — Case notes, record uploads, and next response due
- [domain/collection-and-vault.md](../domain/collection-and-vault.md) — Collection tasks, record systems, Vault
- [domain/advisory-ai.md](../domain/advisory-ai.md) — Bounded AI proposals
- [domain/knowledge-pack.md](../domain/knowledge-pack.md) — Officer-maintained knowledge the intent advisory reads
- [domain/advisory-evaluation.md](../domain/advisory-evaluation.md) — Offline scoring of candidate advisory models
- [domain/compliance.md](../domain/compliance.md) — Deadline engine, exemptions
- [integrations/mcp.md](../integrations/mcp.md) — MCP server for agents
- [domain/correspondence.md](../domain/correspondence.md) — Requester correspondence and Gmail transport
- [domain/message-templates.md](../domain/message-templates.md) — District-editable, versioned wording for requester messages
- [operations/deployment.md](../operations/deployment.md) — Local development and production deployment
- [operations/backups.md](../operations/backups.md) — Application backup and restore
- [integrations/cli.md](../integrations/cli.md) — Token-authenticated staff command-line client
- [background-jobs.md](background-jobs.md) — Celery beat schedule, watchdogs, and staff alerts
- [domain/nextrequest-migration.md](../domain/nextrequest-migration.md) — Legacy NextRequest import and cutover
- [`docs/security.md`](../../docs/security.md) — Security model, attack surface, controls
