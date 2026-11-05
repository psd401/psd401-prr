<!-- OPENWIKI:START -->

## OpenWiki

This repository uses OpenWiki for recurring code documentation. Start with `openwiki/quickstart.md`, then follow its links to architecture, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

OpenWiki deployment pages use the same direct production CDK command documented
below.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

# AGENTS.md — PSD401 Public Records Request System

`CLAUDE.md` imports this file; edit this one.

Public Records Act request management for Peninsula School District No. 401 (WA).
Governing law: **RCW 42.56** (WA PRA) + **FERPA** (student records). Treat deadline
math and exemption logic as legal artifacts, not just features.

## Architecture rules (do not violate)

Full text, with every enforcement anchor, is
[`docs/compliance-rules.md`](docs/compliance-rules.md). Rule numbers are stable
and cited from code, tests, and ADRs; never renumber them.

1. **Django owns compliance and its own transport edges** — deadline math,
   request state, exemptions, redactions, intake, Gmail send/poll, and the audit
   trail live here and are unit tested. No external automation platform sits in a
   request or requester-correspondence path.
2. **All status changes go through `apps/records/services.transition()`** — never
   set `PublicRecordsRequest.status` directly.
3. **Every redaction/withholding requires an `ExemptionCitation` FK** (DB-level);
   the RCW 42.56.210(3) exemption log is generated from those rows.
4. **`AuditLog` and `StatusEvent` are append-only** — no update/delete paths.
5. **Requester PII is minimized** — the PRA forbids requiring purpose or identity
   beyond what's needed to respond; don't add required intake fields.
6. **Only two automated requester-facing paths** — the day-5 safety-net
   acknowledgment (`tasks.auto_acknowledge_safety_net`, in-app
   `AUTO_ACK_SAFETY_NET` switch) and the explicitly non-statutory submission
   receipt. Estimate revisions, `scope_clarification`, and external-response
   attestations are human-only. The interpretation-and-estimate letter may be
   *drafted* by the system (`SYSTEM_PREPARED_DRAFT_KINDS`, never automated)
   but only a Records Officer approves and sends it. Guards:
   `test_compliance.test_no_automated_path_exists`, `receipt-ast-guard`,
   `scope-ast-guard`, `estimate-letter-ast-guard`.
7. **Footage: auto-hold always, auto-deny never** — `apps/records/triage.py` holds
   every camera inside retention via `apps/integrations/avigilon/client.py`;
   outside retention a human completes the search and chooses the response. There
   is no auto-denial code path and none may be added; exclusions need a human and
   a reason through `triage.exclude_camera()`. If Alta is down or unconfigured,
   holds degrade to URGENT manual instructions to facilities — never to silence.
8. **The system registry routes collection** — `RecordSystem`, seeded by
   `manage.py seed_systems`; `triage.route_to_systems` opens a CollectionTask per
   match and urgent_hold systems (bus video!) fire a preservation email.
9. **`request.description` is never edited after intake** — it is the legal record
   of what was asked. Clarifications are new StatusEvents; two numbers for one
   request merge only through `services.merge_requests()`.
10. **GUI/agent parity is mandatory** — every staff-UI outcome needs an atomic
    REST primitive registered in `apps/api/capabilities.py` in the same change;
    `test_agent_capabilities` fails on drift. Parity never weakens rules 6 or 7.
11. **Every integration fails closed and uses least privilege** — `POST
    /api/public/requests/` is the sole anonymous operation (write-only,
    rate-limited, no PII echo). Shared stdio MCP holds only
    `records.read_staff_api`; hosted MCP uses attributed personal staff tokens,
    never that shared identity for its gated tools, and exposes exactly four
    gated draft/proposal tools. Never put tokens in source or `.env.example`.
    Unattended Vault actions are exactly two: the automatic intent Gmail
    *counts* (iterative scoping), which never hold, export, collect, change
    status, or reach the requester; and, only with the in-app
    `PRR_TRUST_SCOPE_MODE=auto`, the export of a finished automatic scoping
    session's final count on a request with no Vault collection yet, recorded
    `trigger=automatic` (`vault_collections.queue_automatic_vault_collection`,
    holds unchanged) — do not widen either. Web-form files are scan-gated
    `IntakeAttachment`s, never `RecordItem`s; no unscanned file is downloadable.
12. **Google group authorization stays keyless and read-only** — a dedicated
    group-reader service account plus named-user membership lookups. No private
    key, domain-wide delegation, Vault-identity reuse, or Groups Editor/Admin
    role. A `DirectStaffRoleGrant` only adds roles; it never bypasses the last-admin guard.
13. **A case's matter kind decides which law it answers to** —
    `PublicRecordsRequest.matter_kind` is fixed at intake and immutable in ORM and
    database ([ADR 0004](docs/decisions/0004-matter-kinds.md)). The public form
    and intake mailbox open only `public_records`; kinds never merge. RCW 42.56
    clocks and letters are `public_records`-only — litigation sends no requester
    mail, FERPA none asserting RCW 42.56. New statutory queries, watchdogs,
    reports, and letter kinds must say which kinds they apply to.

## Layout

Full annotated map: [`docs/architecture.md`](docs/architecture.md) §10. What it won't tell you:

- `config/` holds credentials, identity, infrastructure addresses, and the Phase A
  bootstrap switches only. District-tunable values live in `apps/configuration/`:
  read via `runtime.get()`, never `settings.PRR_*`; write via `services.set_value()`.
- `apps/records/deadlines.py` is the RCW 42.56.520 engine — read its docstring first.
- Read [`docs/record-formats.md`](docs/record-formats.md) before adding a media
  format (`apps/records/handling.py`).
- `templates/correspondence/` holds versioned requester templates with COUNSEL
  REVIEW REQUIRED markers.
- `infra/cdk/` (TypeScript, bun) is the only AWS provisioning path — no console,
  CLI, or Terraform. Stack `psd401-prr-prod` hosts production.
- `openwiki/` is generated, except `openwiki/INSTRUCTIONS.md` (the wiki brief) and
  `.openwikiignore`, which are hand-written.

## Commands

```bash
PYTHON_DOTENV_DISABLED=1 uv run python manage.py test apps --settings=config.test_settings   # run everything
uv run ruff check . && uv run python scripts/pyright_baseline.py  # lint; typecheck ratchet
uv run python manage.py makemigrations --settings=config.test_settings
uv run python manage.py seed_exemptions                       # exemption taxonomy
uv sync --locked && docker compose up -d db redis minio      # deps from uv.lock; dev services
uv run celery -A config worker -l info && uv run celery -A config beat -l info
cd infra/cdk && bunx cdk deploy psd401-prr-prod --require-approval never --profile psd401-prr-prod
```

Tests use SQLite via `config/test_settings.py`; runtime is Postgres 16. Timezone is
America/Los_Angeles and deadline math depends on it.

Production is the sole deployed target (`prr.psd401.net`). After merging to
`main`, the single CDK deploy command above — documented in
[`docs/aws-deploy.md`](docs/aws-deploy.md) — is the whole deployment path; do not
add a step between merge and that command, and do not treat local or CI tests as
prerequisites. No dev environment (`dev-prr.psd401.net`, decommissioned 2026-10-13).

## Evidence a change must carry

- TDD: a failing test first, then the minimal code to pass, for every feature and
  bugfix. Deadline/tolling changes require tests with real WA calendar dates.
- The test suite passes, plus the frontend and `infra/cdk` gates in
  [`docs/development.md`](docs/development.md), and ≥80% patch coverage on added
  lines (`verify-fast.sh --patch-coverage`) — never a project-coverage floor. A
  test you cannot make reliable is quarantined through [`FLAKY.md`](FLAKY.md):
  owner, issue, four-week expiry, never skipped.
- A new or changed staff-UI outcome has its registered capability (rule 10).
- The PR follows `.github/pull_request_template.md`, including the human-handoff
  checks for rules 6 and 7.
- Never read, write, or commit `.env`; secrets live there.

## Where the rest lives

- Calling a deployed instance, not editing code: [`docs/agentic-integration.md`](docs/agentic-integration.md)
- Readiness and accounts still needed: [`docs/phase-2-production-handoffs.md`](docs/phase-2-production-handoffs.md)
- Record locations and custodians — READ before changing registry or triage scope: [`docs/custodian-gap-analysis.md`](docs/custodian-gap-analysis.md)
