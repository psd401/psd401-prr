# PRR Staff Web UI — Design

Date: 2026-07-07. Approved by Reese in session (design sections 1-4); Reese
waived the spec-review and plan-review gates ("just get it built so I have a
demo"). This doc records the approved design for the build. Implementation
update (2026-07-22): the planned Google identity phase is complete using direct
Google OIDC, configurable Workspace-group role mappings, and local-only
developer authentication; see
`docs/decisions/0001-google-workspace-identity.md`.
Subsequent implementation brought public intake and Gmail transport into the
application and removed the retired external workflow and webhook surfaces.

## Decisions (from brainstorming Q&A)

- **Audience**: staff workspace plus a bounded public request form. Inbound
  requester email is handled by the application's Gmail poller.
- **Scope**: full admin replacement — every model registered in Django admin
  gets a branded screen. Django admin remains a local/explicitly enabled escape
  hatch; Django users/groups management stays admin-only.
- **Auth**: Django sessions remain the application session layer. Deployed
  sessions start through Google OIDC; local development retains the branded
  password login without cloud credentials.
- **Architecture**: React SPA + DRF (Reese's call over the server-rendered
  recommendation). Compliance logic stays server-side.

## Shape: one repo, one deploy

- `frontend/` in this repo: Vite + React + TypeScript, bun tooling.
- Django serves the built SPA: Vite `base: "/static/app/"`, `outDir:
  frontend/dist`; `STATICFILES_DIRS` maps it under the `app/` prefix; a
  catch-all view (everything not `api/`, `admin/`, `static/`) returns
  `index.html`. `/` is the staff app.
- Same-origin session-cookie auth + CSRF. No JWT, no CORS.
- Dev: Vite dev server proxies `/api` to :8000 for hot reload.

## Staff API: `apps/api`

- One module per resource in `apps/api/resources/<name>.py` (serializer +
  viewset together); `apps/api/urls.py` registers them all on one router under
  `/api/staff/`. Tests in `apps/api/tests/test_<name>.py`.
- Session auth + staff-only permission on every staff endpoint (DRF defaults
  set accordingly). Explicitly scoped nonstaff service identities may use safe
  read methods; public intake is the sole anonymous write-only exception.
- Auth endpoints: `POST /api/staff/auth/login`, `POST /api/staff/auth/logout`,
  `GET /api/staff/auth/me`, plus a CSRF bootstrap GET.
- Resources: requests (+ nested status events, estimate revisions,
  transition + revise-estimate actions), collection-tasks, record-systems,
  footage-preservations (+ camera holds, exclude-camera action), exemptions,
  requesters, record-items, redactions, installments, time-entries, and audit
  log (read-only).
- **Compliance rules bind the API layer**: status changes only via
  `services.transition()`; estimate revisions only via
  `letters.revise_estimate()` (reason passes through, server rejects thin
  reasons); camera exclusions only via `triage.exclude_camera()`;
  `description` immutable; AuditLog/StatusEvent read-only endpoints only.
- drf-spectacular serves the OpenAPI schema at `/api/schema/`; frontend types
  generated from it at integration time.

## Frontend

- Stack: react-router-dom, TanStack Query, Tailwind (v4, tokens as CSS
  variables), hand-rolled UI kit (Button/Card/Table/Badge/Modal/Input/Toast)
  styled with PSD tokens. Fonts self-hosted via @fontsource.
- PSD brand (from psd-brand-guidelines skill): Pacific #25424C (nav/text),
  Sea Glass #6CA18A (primary buttons/accents), Whulge #346780 (secondary/
  links), Sea Foam #EEEBE4 / Skylight #FFFAEC (backgrounds), Driftwood
  #D7CDBE, Cedar #466857, Meadow #5D9068, Ocean #7396A9 (accents). Headings
  and operational body text use Source Sans 3; restrained supporting copy may
  use Source Serif 4. Real logo PNGs copied from the skill assets (white
  horizontal in the dark sidebar, 2-color emblem on login);
  never AI-generate logos.
- Layout: dark Pacific sidebar nav + light content area. Screens:
  - **Dashboard**: stat tiles (open, due this week, overdue, urgent tasks),
    deadline-sorted queue with overdue/urgent badges.
  - **Request detail**: description (read-only, marked as legal record),
    deadline panel (server-computed only), status timeline, transition modal,
    estimate-revision modal (reason required, min length enforced server-side),
    collection tasks, preservation summary, time entries.
  - **Tasks**: board/list by state with assign + state changes.
  - **Registry**: table with inline custodian-email editing,
    needs-verification punch list surfaced at top.
  - **Preservations**: list + detail with camera holds, exclude-with-reason.
  - **Exemptions**: CRUD table.
  - **Requesters / Record items / Redactions / Installments / Time entries**:
    simple list/edit screens.
  - **Audit**: read-only filterable browser.
- Compliance affordances: deadlines never computed client-side; reason-gated
  modals refuse empty submit; description never editable.

## Errors & testing

- DRF standard error shape; service-layer rejections surface as 400s with the
  service message verbatim; UI shows inline field errors or toasts.
- Backend: DRF endpoint tests in the main suite (TDD; tests before viewsets).
- Frontend: vitest for compliance-critical widgets (reason-gated modals);
  Playwright smoke (login → dashboard → request detail → transition) against
  the dev stack at the end.

## Build orchestration

Foundation (settings, auth, SPA serving, frontend scaffold + shell + UI kit +
exemplar exemptions vertical) built inline; remaining verticals fan out as
parallel backend→frontend agent pipelines (claude-sonnet-5[1m]), each agent
constrained to its own files; integration (router/nav/routes, schema types,
full suite, bun build, smoke) inline afterward.
