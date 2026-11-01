# ADR 0003: Cross-domain work queue ownership

- Status: Accepted
- Date: 2026-08-23

## Decision

Create a dedicated `apps.work_queue` domain for current next-action metadata.
`PublicRecordsRequest.assigned_officer` remains the **case owner**, accountable
for the whole case. A `WorkItem.assignee` owns **one next action** and may be a
different active staff member whose current application role authorizes that
work kind. Case ownership, work assignment, personal read state, shared team
handling, legal status, and correspondence transport state are independent.

The source request, correspondence, inbound receipt, collection task, or record
review evidence remains authoritative. `WorkItem` is a current projection;
`WorkItemEvent` is its append-only attributed history; `WorkItemReceipt` is one
user's mutable read state. These records are **not a legal lifecycle** and do
not reuse `StatusEvent`. Work services cannot transition a request, change a
deadline or clarification cycle, rewrite correspondence, or alter raw receipt
evidence.

Sources use a bounded `source_type` vocabulary and normalized string ID rather
than an unrestricted Django content-type relation. The database permits exactly
one projection for each `(source_type, source_id, work_kind)`. Inbound routing
uses one receipt source across its match states, reclassifying the same work
item when the authoritative receipt changes rather than creating competing
items. Duplicate receipts and successful DSNs create no normal work; failed or
delayed DSNs create delivery-exception work.

All shared mutations use an optimistic `WorkItem.version` and an actor-scoped
idempotency key recorded on the resulting event. An exact retry returns that
event before version validation. Assignment, reassignment, unassignment,
Waiting, Handled, No action required, and reopen append attributed events and
audit entries. Personal read/unread actions update only the caller's receipt,
append their own event/audit evidence, and do not increment the shared version.

`No action required` is a reversible interactive Records Officer decision. Its
allowlist is `courtesy_copy`, `duplicate_notification`, `informational_only`,
`resolved_elsewhere`, and `superseded`; none is a request closure or legal
finding. The full 10–2000 character reason stays in the role-gated work event.
The global audit row stores only category, reason length, and SHA-256. Personal
tokens and the shared read-only agent receive an explicit human handoff before
object lookup.

Bulk workflow-metadata mutation is atomic and limited to **50** distinct work
items. Each item supplies its own authorization, expected version, and
idempotency key. Only read/unread, assignment/unassignment, Waiting, Handled,
and reopen are accepted. No-action decisions, legal transitions,
correspondence actions, review outcomes, and content changes are excluded.

## Reconciliation and failure behavior

Normal source processing schedules reconciliation only after source evidence
commits. The robust callback only enqueues a bounded Celery task; projection
locks and reconciliation work never run on the Gmail intake or legal-write
path, and an enqueue failure cannot roll back or mask authoritative evidence.
A bounded idempotent Celery backfill scans source IDs in stable order, repairs
missing or stale projections as unassigned work, and never writes a
source-domain field. If authoritative
evidence proves a stale projection is ineligible, reconciliation records the
distinct system-derived `Source excluded` state; it never fabricates an
attributed human `Handled` decision. Disposed items leave unresolved counts but
remain directly retrievable and searchable with complete history.

Recurring reconciliation never derives or overwrites an existing work
assignee. The one explicit source-to-work handoff is an Officer changing a
collection task's `assigned_to` value: that request uses the same locked,
versioned, audited work-assignment service in the source update transaction.
If the projection does not exist yet, its post-commit creation seeds the newly
selected authorized assignee instead.

The schema is additive. Historical eligible evidence backfills as unassigned
Needs attention; source transport or legal state is never treated as proof that
the team handled it. Once any work item, event, or receipt exists, migration
reversal is blocked. Production correction must use a **forward fix** so durable
work history cannot be discarded.

## Consequences

- Records Officer remains the only role for inbound casework and No action
  required. Liaison assignment is limited to collection/drafting work; Reviewer
  assignment is limited to review work. Application Admin receives only the
  explicitly operational delivery-exception kind and no implied legal power.
- The work queue can be rebuilt after missed callbacks without deriving legal
  outcomes or rewriting immutable evidence.
- My Work, Inbox, and Review use the same role-scoped REST projection and
  mutations as case-scoped views. Capability rows identify their canonical UI
  routes so GUI/agent parity cannot drift.

Amendment: My Work was later merged into the Inbox as its *Your queues* chip
group, and `/my-work` redirects there. The projection, the mutations, and the
parity contract above are unchanged; only the number of screens reading them
changed.
