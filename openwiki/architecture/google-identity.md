---
type: Architecture
title: Google Workspace Identity and RBAC
description: Direct Google OIDC authentication for PSD401 staff with role-based access control mapped through existing Workspace groups.
tags: [architecture, authentication, sso, google, rbac, security]
---

# Google Workspace Identity and RBAC

Direct Google Workspace OIDC authentication for staff with passwordless login
and role-based access control through existing district groups.

## Architecture Decision

The system uses **Direct Google Workspace OIDC** for staff authentication,
moving authorization into the application where administrators map existing
Workspace groups to stable application roles. See [ADR 0001](../../docs/decisions/0001-google-workspace-identity.md) for the full rationale.

Key decision: **No Cognito** — the district has one identity provider, all
accounts already use Google 2FA, and adding a second control plane would add
complexity without capability.

## Roles

Five stable roles map to existing Workspace groups through administrator
configuration. The enum is `StaffRole` in `apps/api/models.py`:

| Role | Permissions |
|------|-------------|
| **Records Officer** | Transition requests, revise estimates, legal authority |
| **Intake Clerk** | Submit staff intake (`intake` action on `RequestsViewSet`, shared with the Records Officer). Not in `WORKFLOW_STAFF_ROLES` (`apps/api/identity.py`), so it cannot browse request PII after submission |
| **Department Liaison** | Work assigned cases: collection tasks, correspondence drafts, and time entries on requests they are assigned to (see [liaison case scope](#liaison-case-scope)) |
| **Reviewer** | Review/redact records, apply exemption citations |
| **Application Admin** | Configure group mappings and direct grants, district settings, and the identity configuration |

Roles are mutually independent. Application Admin controls configuration but
cannot perform statutory actions. Records Officer holds legal authority.

### Shared Role Groups

The application permits a single Workspace group to be explicitly mapped to
multiple roles. Group separation is an operational governance decision, not an
application constraint. Each role still requires its own audited mapping row,
so Application Admin authority never implicitly grants Records Officer authority.

During pre-launch testing, a single pre-launch test group may be mapped to several
roles. Replacing that with permanent role groups
before live casework is an operational decision. The application enforces the
audit trail and role independence regardless of how many groups hold each role.

### Liaison case scope

A Department Liaison is limited to cases represented by their own work.
`scope_case_queryset()` in `apps/api/case_scope.py` filters a case queryset to
requests that have either a `collection_tasks__assigned_to` row for the user or a
`WorkItem` assigned to the user with a work kind in `LIAISON_CASE_WORK_KINDS`
(`collection` and `correspondence_draft`). Users who also hold the Records Officer
role are not narrowed. The request, correspondence, and work-item resources
(`apps/api/resources/requests.py`, `correspondence.py`, `work_items.py`) apply it
when they build their querysets.

Invariants and focused checks:

- A liaison sees only assigned requests and correspondence:
  `apps/api/tests/test_correspondence.py::test_liaison_sees_only_assigned_request_correspondence`.
- A liaison can read and write notes on an assigned case, and cannot on an
  unassigned one: `apps/api/tests/test_case_notes_api.py`
  (`test_a_liaison_reads_notes_on_an_assigned_case`,
  `test_a_liaison_cannot_read_notes_on_an_unassigned_case`).
- Assigned draft work can open its case workspace:
  `apps/api/tests/test_case_workspace.py::test_liaison_with_assigned_draft_work_can_open_its_case_workspace`.
- Role-scoped task and request visibility:
  `apps/api/tests/test_staff_roles.py::test_liaison_only_sees_tasks_and_requests_assigned_to_them`.

Liaison assignment for work items is further limited by
`apps/work_queue/services.py` (`Liaison access requires a current assignment to this work item.`);
see [work queue](../domain/work-queue.md#mutations-and-idempotency).

### Locked authority checks

Human-only actions do not trust the role data in the request session alone. The
service layer re-reads the actor and their group membership under a row lock, and
refuses unless the actor is still an active staff user with the required role.
Two helpers implement this:

- `lock_authorized_staff()` in `apps/work_queue/authorization.py` locks each
  actor's `User` row and their group-membership rows (`select_for_update`), then
  checks the current roles for every requirement. The work-queue mutation services
  in `apps/work_queue/services.py` call it, so a role removed mid-request cannot
  complete an assignment or disposition.
- `require_current_human_staff()` in `apps/correspondence/authorization.py` does the
  same for correspondence and inbound actions. Its callers are the approval and
  queue helpers in `apps/correspondence/services.py`, three Records Officer
  helpers in `apps/correspondence/inbound.py` for inbound receipt actions, and the
  email-intake redirect in `apps/records/email_intake.py`. Its `roles=` argument
  restricts a check to one role, for example Records Officer approval.

The invariant is that the check and the write happen in one transaction. A
session that was valid earlier does not authorize a write after the group
membership has been removed; `test_attributed_staff_token_cannot_outlive_google_group_removal`
in `apps/api/tests/test_staff_identity.py` covers the token path. The focused checks are
`apps/work_queue/tests/test_concurrency.py` (which patches `lock_authorized_staff`
to observe the lock) and
`apps/api/tests/test_security_remediations.py::test_nonterminal_transition_rechecks_current_officer_authority`.
Run `PYTHON_DOTENV_DISABLED=1 python manage.py test apps.work_queue.tests.test_concurrency apps.api.tests.test_security_remediations --settings=config.test_settings`.

## Authentication Flow

```
┌──────────────┐    1. Initiate     ┌──────────────────┐
│ Staff Browser │ ─────────────────▶ │ Google OAuth     │
└──────────────┘                    │ (accounts.google) │
       ▲                            └──────────────────┘
       │                                     │
       │                            2. Authorize (openid email profile)
       │                                     │
       │                            ┌────────▼──────────┐
       │                            │ Django OIDC       │
       │                            │ Callback          │
       │                            └────────┬──────────┘
       │                                     │
       │                            3. Validate claims
       │                               - issuer/audience
       │                               - hosted domain
       │                               - verified email
       │                               - immutable sub
       │                                     │
       │                            ┌────────▼──────────┐
       │                            │ Group Directory   │
       │                            │ Lookup            │
       │                            └────────┬──────────┘
       │                                     │
       │                            4. Check membership
       │                               in configured groups
       │                                     │
       └──────────────────────────── Session created with roles
```

### Claim Validation

The OIDC backend validates:

- **Issuer** — Must be `accounts.google.com`
- **Audience** — Must match configured client ID
- **Authorized presenter** — Must match client ID (prevent token reuse)
- **Hosted domain** — Must match `PRR_GOOGLE_HOSTED_DOMAIN`
- **Email verification** — Must be verified by Google
- **Subject** — Immutable identifier, never email

Email is never the identity key. Existing local accounts require one-time
administrator approval before linking to a Google subject.

### Role Refresh

Roles are refreshed during active sessions. Both intervals are Application Admin
settings, read through `runtime.get()`, so they change without a redeploy
([configuration.md](configuration.md)):

- `PRR_GOOGLE_GROUP_RECHECK_SECONDS` (default 900): how often the revalidation
  middleware (`apps/api/middleware.py`) re-reads a signed-in person's direct group
  membership
- `PRR_GOOGLE_GROUP_MAX_STALE_SECONDS` (default 1800): how old a membership answer
  may be before the session is ended rather than trusted; a Google outage therefore
  signs staff out instead of keeping stale roles
- Only direct group membership counts; nested membership is intentionally rejected
- A role mapping or direct grant change calls `expire_google_authorizations()`
  (`apps/api/identity.py`). It clears every Google principal's cached check, so roles
  are recomputed on the next request, and it ends the sessions of every other Google
  principal. The administrator who made the change keeps their session. Ending it
  mid-save broke multi-role saves before commit 8f3fb0a.

### Direct role grants

A `DirectStaffRoleGrant` (`apps/api/models.py`) gives a named address one role
when it belongs to no syncable group. It is an Application Admin action with a
reason, audited, and managed through `apps/api/resources/identity_config.py`.
Grants are additive: they union with group-derived roles and never subtract one,
and they cannot bypass the last-admin guard. Deactivating a grant removes the role
at the holder's next refresh. Grants keep working during a Google outage because
they are application records rather than Google facts.

## Components

### Backend

| File | Purpose |
|------|---------|
| [`apps/api/oidc.py`](../../apps/api/oidc.py) | OIDC backend with claim validation |
| [`apps/api/identity.py`](../../apps/api/identity.py) | Group lookup, role sync, session revocation |
| [`apps/api/models.py`](../../apps/api/models.py) | `StaffIdentity`, `GoogleGroupRoleMapping` models |
| [`apps/api/resources/identity_config.py`](../../apps/api/resources/identity_config.py) | Admin CRUD for group mappings and direct grants |
| [`apps/api/middleware.py`](../../apps/api/middleware.py) | Per-request role revalidation and fail-closed staleness |
| [`apps/api/auth_backends.py`](../../apps/api/auth_backends.py) | Password backend gated by the settings store (break-glass and local mode) |
| [`apps/configuration/signin.py`](../../apps/configuration/signin.py) | Refuses a sign-in change that would leave no Application Admin able to sign in |

### Frontend

| File | Purpose |
|------|---------|
| [`frontend/src/pages/IdentitySettings.tsx`](../../frontend/src/pages/IdentitySettings.tsx) | Admin UI for group-role mappings |
| [`frontend/src/pages/Login.tsx`](../../frontend/src/pages/Login.tsx) | Google OIDC login initiation |

### Infrastructure

| Path | Purpose |
|------|---------|
| [`infra/terraform/google-identity/`](../../infra/terraform/google-identity/) | Terraform root for the Google project, APIs, service account |
| [`infra/cdk/lib/terraform-state-stack.ts`](../../infra/cdk/lib/terraform-state-stack.ts) | S3 bucket for shared, versioned Terraform remote state |
| `docs/google-identity-bootstrap.md` | One-time setup guide (operational document outside this wiki) |

## Configuration

### Required Settings

Google sign-in is configured in **Configuration > District settings** (categories
"Staff sign-in" and "Google identities"), not in `.env.app`. Group-to-role mappings
and direct grants live on **Configuration > Identity** (`/configuration/identity`). `PRR_AUTH_MODE` (default `google`), `PRR_GOOGLE_HOSTED_DOMAIN`
(default `psd401.net`), `PRR_GOOGLE_OIDC_CLIENT_ID`, and the write-only
`PRR_GOOGLE_OIDC_CLIENT_SECRET` are registry entries in
[`apps/configuration/registry.py`](../../apps/configuration/registry.py). The
client secret is encrypted at rest and never read back. The group-reader identity
is configured by `PRR_GOOGLE_GROUP_CREDENTIALS_FILE`. Operator-held bootstrap
values only seed these settings on first import
(`manage.py import_environment_settings`). See [configuration.md](configuration.md).

### Local Development

Developers use local mode without any Google access:

```bash
PRR_AUTH_MODE=local
PRR_LOCAL_IMPLICIT_STAFF_ROLES=records_officer,liaison,reviewer,admin
```

Local mode creates ordinary Django users with the same stable roles. No OAuth,
no Google Admin, no AWS credentials required.

## Bootstrap Process

1. **Apply Terraform** — Create Google project, enable APIs, create service account
2. **Create OAuth client** — One Google console task to create web client with exact callback URI
3. **Configure group visibility** — Give service account read access to role groups
4. **Deploy with break-glass** — Enable first admin, configure mappings, test login
5. **Disable break-glass** — Remove password route, require Google SSO

The complete guide is the operational document `docs/google-identity-bootstrap.md`, which
is outside this wiki.

## Security Properties

- **Passwordless** — No password login route in Google mode
- **No domain-wide delegation** — Group reader uses minimal scope, not admin delegation
- **No group enumeration** — Checks only the named user's direct membership
- **No nested membership** — Only direct group membership counts
- **Session revocation** — Mapping and grant changes end every other Google principal's sessions and force a fresh role check for all
- **Audit trail** — All mapping changes recorded with actor and timestamp
- **Fail closed** — Directory unavailability denies access, grants nothing

Related: [Security Model](../../docs/security.md), [ADR 0001](../../docs/decisions/0001-google-workspace-identity.md)
: [Security Model](../../docs/security.md), [ADR 0001](../../docs/decisions/0001-google-workspace-identity.md)
ecisions/0001-google-workspace-identity.md)
: [Security Model](../../docs/security.md), [ADR 0001](../../docs/decisions/0001-google-workspace-identity.md)
