# PSD401 Public Records Request System

Custom-built Public Records Act request management for Peninsula School
District No. 401 (WA), replacing commercial tools like NextRequest/GovQA.
Governing law: **RCW 42.56** (Washington PRA) and **FERPA**. Compliance is
the product: deadline math, exemption handling, and the audit trail are
legal artifacts, so they live in code where they can be enforced at the
database level and proven by tests.

**Owner:** Kris Hagel (@krishagel), CIO. Tier A (production) under the
district's [development standards](https://github.com/psd401/psd-dev-standards).

**Status (verified 2026-08-02):** Phase 1, the React staff workspace, direct
Google Workspace OIDC, group-mapped staff roles, the staff/agent API, and both
MCP transports are implemented. Hosted `/mcp` adds exactly five personal-token
draft/proposal tools; standalone stdio and the dedicated nonstaff MCP identity
remain read-only. Durable requester correspondence now owns drafting, approval,
queueing, delivery evidence, reconciliation, inbound MIME/DSN matching, and the
day-5 safety net. Outbound correspondence sends in-process through a lease-safe
Celery Gmail worker, and the disabled-by-default in-process Gmail poller feeds
replies into the same durable inbound pipeline. The former integration-specific
HTTP intake and correspondence routes and their tokens have been removed.
The anonymous, write-only public intake API accepts the complete legacy
requester-information set and malware-scanned file attachments through the
compliance pipeline. Least-privilege staff intake logs phone, paper, and
in-person requests through that same path. Phase 2 is implemented in the
repository: integrity storage and scoped releases, resumable Vault ingest,
human review/redaction and raster burn-in, exemption/production packages,
JLARC reporting, governed fees, safe migration, advisory AI, and accountable
no-records closure. Production credentials, approvals, representative-data
rehearsals, and cutover remain explicit external handoffs. See
[docs/project-status.md](docs/project-status.md) for the kickoff
ledger and [docs/development.md](docs/development.md) for the current developer
starting point.

![Staff dashboard](docs/screenshots/dashboard.png)

## Why a custom build

The 2026 build/buy/FOSS analysis ([docs/build-vs-buy.md](docs/build-vs-buy.md))
surveyed the commercial market (NextRequest ~$1k–5k/month, GovQA and
FOIAXpress at enterprise pricing) and the FOSS field (ArkCase CE is the only
credible agency-side option, and it is a heavyweight Java/Alfresco stack with
no Google Vault integration). The district's records live in Google
Workspace, its engineering capacity is in-house, and the statutory logic has
to be testable. A purpose-built Django system with FOSS components (OCR,
redaction, storage) won on all three, and the avoided license cost funds the
build.

A general ticketing platform (Freshservice, etc.) can hold a queue, but the
properties below become conventions there: fields someone can skip, rules an
admin can edit. Here they are constraints.

## Compliance guarantees

Enforced by the schema and the test suite, not by policy:

| Guarantee | Mechanism |
|---|---|
| 5-business-day response clock (RCW 42.56.520) | `apps/records/deadlines.py`: WA holidays (incl. Juneteenth), district closures, after-hours receipt rollover, day-after-receipt counting, clarification tolling. Tested against real calendar dates. |
| One lifecycle path | Every status change goes through `services.transition()`, which atomically writes the timeline event, recomputes the deadline, and appends to the audit log. No code path skips it. |
| Every redaction cites an exemption | `Redaction.exemption` is a non-nullable protected FK into the seeded RCW 42.56/FERPA taxonomy; the production pipeline generates a manifest-bound RCW 42.56.210(3) exemption log from that evidence. |
| Append-only audit | `apps/audit` has no update or delete code path. |
| Human-only statutory actions | Estimate revisions and camera-hold exclusions require a human actor and a documented substantive reason; tests parse the background-task module's AST to prove no automated caller exists (`test_compliance.py`, `test_footage.py`). Denials have no automated send path: expired-footage triage queues only a human-review and adequate-search checklist, and an interactive Records Officer must separately draft any legally supported response. |
| Delivery evidence drives legal state | Queueing never marks correspondence sent or advances a communication-dependent request state. Only the first valid Gmail success/reconciliation callback applies the approved legal effect; duplicate callbacks are idempotent. |
| Footage: auto-hold always, auto-deny never | Intake triage detects camera requests and immediately bookmarks every camera at each site the request names, or every camera district-wide when it names none, using each camera's own retention in Aware. A named site with no cameras, or a window no camera retains, queues only an adequate-search review. No response is drafted, queued, or sent until a human completes that review, and releasing a hold is human-only. |
| Requester PII minimized | The PRA forbids requiring purpose or identity beyond what is needed to respond. Intake collects name, contact, and description. |
| The request text is immutable | `request.description` is never edited after intake; it is the legal record of what was asked. Clarifications are new timeline events. |

## What's built

**Request intake**
- Anonymous write-only public intake with same-origin CSRF, a signed HttpOnly
  form token, timing/honeypot checks, Redis-backed per-IP/global hard ceilings,
  one-request idempotency, and a request-number-only response
- Actor-scoped, idempotent staff intake for phone, mail, and in-person
  requests, with a dedicated Intake Clerk role that can submit but cannot
  browse request PII or inherit Records Officer/Application Admin authority
- Structured name, email, phone, street, city, state, postal code, and company
  fields matching the legacy form, plus bounded multipart attachments that
  must pass file-signature validation and remain in private object-storage
  quarantine until ClamAV clears them
- No anonymous read surface; public intake is the one reviewed anonymous write
  operation and every other business/data API requires an explicit identity
- Durable requester correspondence: versioned drafting and preview, human
  approval, immutable content/recipient snapshots, day-5 automated
  acknowledgment queueing, leased delivery, deterministic Message-ID
  reconciliation, safe attachments, inbound MIME matching, privacy-minimized
  append-only DSN evidence, and operational stuck-delivery recovery. DSNs
  never retry a delivery or reverse legal state. The durable acknowledgment
  queue is complete;
  automatic send-on-receipt is intentionally prohibited so a Records Officer
  can choose the legally appropriate initial response. Queueing alone never
  claims a message was sent.

**Compliance core**
- Deadline engine, lifecycle service, append-only audit, and seeded RCW
  42.56/FERPA exemption taxonomy
- Records-system registry mapping district systems to custodians, retention
  profiles, triage keywords, and adequate-search checklists;
  intake auto-routes matching requests into per-system collection tasks
- Camera-footage triage with site-scoped Avigilon Alta Aware bookmark holds, urgent
  preservation emails, and a human-only adequate-search/response path
- Hourly escalating deadline watchdog, estimate watchdog, stall detector

**Staff application**
- DRF API resources for requests, tasks, preservations, registry,
  exemptions, requesters, record items, installments, time entries, audit log, intake
  attachments, and Google role mappings, role- and object-scoped with a
  separate read-only agent gate
- Direct Google Workspace OIDC using immutable Google subjects, exact hosted
  domain validation, administrator-configurable group-to-role mappings, and
  periodic fail-closed group revalidation; local developers need no Google or
  AWS credentials
- React SPA on the PSD brand system. Django admin is a disabled-by-default,
  superuser-only break-glass surface rather than a daily workspace

**Integrations**
- Google Vault client: matters, holds, Gmail/Drive exports, export polling
- Keyless, mailbox-pinned Gmail API client with in-process outbound delivery
  and disabled-by-default inbound polling; see
  [docs/gmail-transport.md](docs/gmail-transport.md)
- Migration-provisioned exact-scope, tokenless system identities for those
  Gmail workers, plus a separately provisioned dedicated
  read-only MCP identity; generated OpenAPI docs; PII-free correspondence
  operations/readiness; and hosted plus stdio MCP transports over the staff API

![Request detail](docs/screenshots/request-detail.png)
![System registry](docs/screenshots/registry.png)
![Footage preservations](docs/screenshots/preservations.png)

## What's next

The in-repository Phase 2 workflow is implemented: staff intake, integrity
storage and scoped installment downloads, resumable Vault ingest, review and
burn-in redaction, exemption logs and production, correspondence delivery,
JLARC export, counsel-controlled fees, safe NextRequest migration tooling,
human-only no-records closure, and permanently non-applying advisory AI.

Production readiness now depends on the accountable external handoffs in
[docs/project-status.md](docs/project-status.md): counsel approval of legal
templates, fee and retention policy; district cloud and Google credentials;
representative migration rehearsals; advisory-provider approval/evaluation;
staff training; and operational cutover sign-off.

## Architecture

```
┌─────────────────┐      ┌──────────────────────────────────────┐
│ Public request  │      │      Staff SPA (React + PSD brand)    │
│ signed form +   │      │  Dashboard · Requests · Tasks ·       │
│ idempotent POST │      │  Registry · Preservations · Audit     │
└───────┬─────────┘      └───────────────┬──────────────────────┘
        │            REST + JSON          │ (Google OIDC → Django session)
┌───────▼─────────────────────────────────▼─────────────────────┐
│                   Django + DRF (compliance core)               │
│  deadline engine · lifecycle · exemptions · audit · letters    │
└──┬───────────┬───────────────┬──────────────┬────────────────┘
   │           │               │              │
┌──▼───┐  ┌────▼─────┐  ┌──────▼───────┐  ┌───▼──────────────┐
│ PG 16 │  │ Redis +  │  │ Object store │  │ Google Gmail ·   │
│       │  │ Celery   │  │ (S3/MinIO)   │  │ Vault · Alta     │
└───────┘  └──────────┘  └──────────────┘  └──────────────────┘
```

**Django owns compliance and its own transport edges.** Deadline math, request
state, exemptions, public intake, in-process Gmail correspondence transport,
and the audit trail live here and are unit tested. No external automation
platform sits in a request or requester-correspondence path.

**Agent-native surfaces.** Following Every's
[Agent-native Architectures](https://every.to/guides/agent-native), anything
the staff GUI can do must have an agent-accessible atomic REST primitive. The
SPA and token-authenticated agents share the same API; `/api/staff/capabilities/`
publishes the machine-readable parity and risk contract, and a test scans the
production frontend so new GUI operations cannot land without a registered
agent path. The OpenAPI schema, management commands, and MCP transports provide
additional agent surfaces. Hosted MCP adds only five gated
personal-token draft/proposal tools to the shared reads; it does not erase legal
accountability. Denials, estimate changes, and camera exclusions remain explicit
handoffs to a human under the compliance rules below.

## Repository layout

| Path | Contents |
|---|---|
| `config/` | Settings, Celery app and beat schedule |
| `apps/records/` | Core domain: models, deadline engine, lifecycle service, triage, tasks, letters, public intake API |
| `apps/api/` | Staff DRF API, GUI/agent capability registry, and SPA serving |
| `apps/audit/` | Append-only audit log |
| `apps/integrations/` | Google Gmail, Vault, and Avigilon Alta clients |
| `frontend/` | Staff SPA (bun + Vite + React + TypeScript) |
| `integrations/mcp/` | Hosted and stdio MCP facades over the staff REST API; stdio is read-only, while hosted adds four role-gated record and decision-batch reads plus five gated personal-token draft/proposal tools |
| `templates/correspondence/` | Versioned requester correspondence templates (counsel review required) |
| `templates/sops/` | SOP templates (personal-device search procedure) |
| `docs/` | Architecture, build-vs-buy, custodian gap analysis, status ledger |
| `openwiki/` | Generated, agent-navigable repository wiki; never hand-edit except `openwiki/INSTRUCTIONS.md`, the wiki brief |

## Getting started (local dev)

Services run in Docker; Django and Celery run on the host.

```bash
# 1. Services
docker compose up -d db redis minio
# Create the MinIO bucket `prr-records` (console at :9001, prr / prr-dev-only)

# 2. Python 3.12 env, installed from the committed uv.lock (uv >= 0.13.1)
uv sync --locked
source .venv/bin/activate

# 3. Config: seed only a missing local .env; never overwrite an existing one
cp -n .env.example .env
export DJANGO_DEBUG=true  # also selects local sign-in
export DATABASE_URL=postgres://prr:prr-dev-only@localhost:5432/prr
export REDIS_URL=redis://localhost:6379/0
export OBJECT_STORE_ENDPOINT=http://localhost:9000

# 4. Migrate and seed
python manage.py migrate
python manage.py seed_exemptions   # RCW 42.56 / FERPA taxonomy
python manage.py seed_systems      # district records-system registry
python manage.py createsuperuser

# 5. Frontend (built SPA is served by Django at /)
cd frontend && bun install && bun run build && cd ..

# 6. Run
python manage.py runserver         # Django routes at :8000; admin at /admin
celery -A config worker -l info    # separate terminal
celery -A config beat -l info      # watchdogs, correspondence recovery, safety net
```

Use `uvicorn config.asgi:application --host 127.0.0.1 --port 8000` instead
when locally testing the hosted `/mcp` route.

For frontend development, `bun run dev` serves Vite on :5173 with `/api`
proxied to :8000. Local mode uses Django users and the same four application
roles; it never calls Google or AWS. Developers do not need access to either
cloud, production identity groups, or production secrets.


## Production deployment

Production is `prr.psd401.net`. After changes merge to `main`, run:

```bash
cd infra/cdk && bunx cdk deploy psd401-prr-prod --require-approval never --profile psd401-prr-prod
```

Nothing runs between merge and that command. CDK packages the repository,
builds the application image on the existing host, creates an application
backup, applies Django migrations, replaces the application containers, and
automatically restores the previous image if replacement fails. The host's
private `.env` and `.env.app` files remain in place and are not included in
the source asset.

Production is the only environment; the retired `dev-prr.psd401.net` box was
decommissioned on 2026-10-13.
See [docs/aws-deploy.md](docs/aws-deploy.md).

## API, MCP & agent integration

Beyond the staff SPA and public request form, this system exposes a staff REST
API with OpenAPI docs and hosted plus stdio MCP transports for agents. Both
transports share eight read tools; hosted
`/mcp` adds exactly five gated draft/proposal tools for personal tokens. See
[docs/agentic-integration.md](docs/agentic-integration.md) for the full
reference: endpoints, the session vs. token auth model, and the guardrails
section defining what an agent may and must never do against a live instance.
The statutory day-5 safety net and the non-statutory submission receipt are the
only permitted requester-facing automations; estimate revisions, denials, and
camera exclusions require the documented human authority by design
(AGENTS.md rules 6 and 7), and no agent tooling may create a path around that.

Quick starts:

- MCP server (hosted `/mcp` with eight reads, four role-gated reads, and five personal-token
  draft/proposal tools; read-only stdio and service identity):
  [integrations/mcp/README.md](integrations/mcp/README.md)
- Coding agents working on this repository: [AGENTS.md](AGENTS.md)

## Testing

```bash
python manage.py test apps --settings=config.test_settings   # SQLite, no services needed
cd frontend && bun run test                                   # SPA tests
```

Conventions: deadline and tolling changes require tests with real WA
calendar dates (Juneteenth and observed July 4th trip naive
implementations). Runtime is Postgres 16; timezone is America/Los_Angeles and the
deadline math depends on it.

The repository's GitHub Actions run code-quality, dependency, OpenWiki, and
review checks. They do not control or authorize production deployment.

## Operational setup

- [ ] Counsel review: exemption seed list, letter templates, fee schedule,
      close-of-business receipt policy
- [ ] Apply the Google identity Terraform, complete the one-time Auth Platform
      and Workspace Groups Reader bootstrap, configure role mappings, and run
      the Google login/offboarding acceptance checks in
      `docs/google-identity-bootstrap.md`
- [ ] Vault service account with domain-wide delegation (scopes in
      `apps/integrations/vault/client.py` docstring)
- [ ] Run `manage.py verify_alta` against the district Avigilon tenant
      (endpoint paths carry VERIFY markers)
- [ ] Verify bus-video vendor retention window (see
      `docs/custodian-gap-analysis.md`), assign custodian emails in the
      registry
- [x] Automated PostgreSQL/audit backup timer installed and manifest-marked
      uploads observed through 2026-08-18
- [ ] DB trigger or pgaudit reinforcing audit-log immutability

## Documentation

- [openwiki/quickstart.md](openwiki/quickstart.md): generated, agent-oriented
  repository map; refreshed through a rolling PR on every `main` push and a
  weekly backstop
- [docs/development.md](docs/development.md): local setup, quality checks,
  documentation ownership, and the recommended next workstream
- [docs/agentic-integration.md](docs/agentic-integration.md): API surface, MCP
  server, and agentic guardrails
- [docs/security.md](docs/security.md): reviewed attack surface, deployment
  boundary, automated controls, and known production security work
- [docs/architecture.md](docs/architecture.md): legal design drivers, data
  model, workflows, phased roadmap
- [docs/build-vs-buy.md](docs/build-vs-buy.md): market and FOSS research
- [docs/custodian-gap-analysis.md](docs/custodian-gap-analysis.md): every
  record location, custodian, and retention risk (read before changing
  registry or triage scope)
- [docs/project-status.md](docs/project-status.md): dated status ledger
  against the 2026-07-01 project kickoff items
- [docs/gmail-transport.md](docs/gmail-transport.md): keyless in-process Gmail
  sender/poller configuration and operations
- [AGENTS.md](AGENTS.md): the rule index, repository map, and working context
  for AI coding agents
- [docs/compliance-rules.md](docs/compliance-rules.md): architecture rules 1–13
  in full, with every enforcement anchor

Peninsula School District project, released under the [MIT License](LICENSE).
