# Case workspace research and decisions

## Purpose and evidence

This artifact fixes the role journeys and terminology for the casework rebuild
tracked by issues #147–#154. It is based on the current Django role policies,
the current React routes and request detail screen, the immutable request and
correspondence models, and the approved product and architecture decisions in
those issue contracts. UX-00 changes no production workflow. It establishes
the baseline that later issues must implement and test.

The fixture and browser flow use synthetic data only. No conclusion here
weakens the statutory lifecycle, requester-PII boundaries, append-only
evidence, or the human-only controls for estimates, denials, clarification
resolution, no-records closure, and camera exclusions.

## Shared terminology

| Term | Meaning |
| --- | --- |
| Case | One `PublicRecordsRequest` and its related legal evidence and work. |
| Case owner | The request's `assigned_officer`; accountable for the whole case. |
| Work item | One projected next action around source evidence. It is not a legal lifecycle state. |
| Work-item assignee | The active authorized staff member responsible for one next action; may differ from the case owner. |
| New / read | Personal receipt state. One person's read action does not change anyone else's state. |
| Needs attention / Waiting / Handled / No action required | Shared team-work disposition categories. They do not change message transport or request status. |
| Legal status | The request lifecycle controlled only by Django's transition service. |
| Transport status | Draft, Approved, Queued, Sent, Failed, Cancelled, Received, Unmatched, or Quarantined correspondence evidence. |
| Global queue | Cross-case work used to manage a function or workload. |
| Case tab | A request-scoped view of the same API primitive and mutation used by a global queue. |

## Role journeys

### Records Officer

#### Current state

1. Sign in and infer urgency from Dashboard, Requests, Correspondence, Tasks,
   Preservations, Record items, Installments, and Audit log.
2. Open a request into one long detail page containing the immutable request,
   lifecycle controls, correspondence, tasks, holds, and other evidence.
3. Move among global resource screens to determine what arrived, who owns the
   case, what failed, and which legally guarded action comes next.
4. For a large case, the request-scoped correspondence wall fetches full
   message detail across every page before the officer selects a message.

#### Target state

1. Land on the **Inbox**, ordered by statutory urgency and next required
   action, with the assigned, unassigned, team, critical, waiting, and recent
   queues grouped under *Your queues*.
2. Open a work item into the exact case and context rather than reconstructing
   the workflow from resource pages.
3. Use a persistent case header and the Overview, Messages, Collection,
   Records, and Releases tabs. The case timeline is the Running log on
   Overview.
4. Read one bounded page of message summaries, load full content only for the
   selected message, and keep personal read state separate from team handling.
5. Complete officer-only actions through the existing guarded REST/service
   paths with required reasons, versions, approvals, and audit evidence.

### Department Liaison

#### Current state

1. Sign in and move between Dashboard, Requests, Correspondence, Tasks,
   Registry, and Time entries.
2. Filter global tasks to find assigned collection work and open the request
   detail page for surrounding context.
3. Prepare eligible drafts or update assigned tasks, then rely on the Records
   Officer for approval and statutory decisions.

#### Target state

1. Land on the **Inbox** with assigned collection/drafting actions and
   explicit waiting or handoff states.
2. Deep-link from a work item to the relevant case Collection or Messages tab.
3. Use the same task/message primitives as global queues, scoped to the case.
4. Hand off approval, queueing, lifecycle, or other officer-only actions
   without acquiring broader requester data or legal authority.

### Reviewer

#### Current state

1. Sign in and move among Dashboard, Requests, Record items, Installments,
   Exemptions, Reporting, and Time entries.
2. Open global record-review work and infer its request context from separate
   screens while requester correspondence remains outside reviewer authority.
3. Complete first/second review using citation-backed redaction evidence.

#### Target state

1. Land on **Review** with authorized record work ordered by urgency.
2. Open directly into the case Records tab with only the role-safe case header
   and review context.
3. Review responsiveness, redactions, exemptions, and second-review work while
   requester PII, correspondence, and legal actions remain unavailable.
4. See review milestones in the role-safe Running log without access to the
   raw global audit log.

### Intake Clerk

#### Current state

1. Sign in to a deliberately narrow shell containing Staff intake and personal
   Agent access.
2. Enter a phone, mail, or in-person request and receive the request number.
3. Cannot browse cases, requester PII, correspondence, or legal actions.

#### Target state

1. Land on **Staff Intake** and submit through the same least-PII canonical
   intake service.
2. Receive a request-number result and remain outside case and Inbox access.
3. Reach personal token self-service under Configuration while identity or
   integration administration and Records Officer authority remain denied.

### Application Admin

#### Current state

1. Sign in to broad operational/configuration screens such as identity,
   registry, message templates, correspondence readiness, and audit-visible
   operations.
2. The model-oriented shell can visually suggest workflow authority that the
   server correctly does not grant.
3. Move among global resource screens to diagnose configuration and transport.

#### Target state

1. Land on **Configuration/operations** with identity, integrations,
   correspondence readiness, templates, registry, and Agent access grouped by
   purpose.
2. Use Oversight and authorized operational queues without gaining Records
   Officer statutory authority.
3. Receive a real Not Authorized screen on prohibited direct routes; navigation
   hiding alone never supplies authorization.

## Approved decisions

### Case ownership versus work-item assignment

`PublicRecordsRequest.assigned_officer` remains the case owner. A work-item
assignee owns one next action and may be a different authorized user. Work
assignment, personal read state, shared disposition, correspondence transport,
and request lifecycle are separate concepts and cannot mutate one another by
implication.

### Inbound disposition authority and categories

In the target model, every nonduplicate, non-DSN inbound message receives one
open or terminal work item. The approved shared categories are **Needs attention** (open),
**Waiting**, **Handled**, and **No action required**. New/read remains a
personal overlay rather than a shared disposition. Duplicate receipts remain
deduplicated evidence and DSNs remain delivery evidence; neither becomes a
normal inbound work item.

**No action required** is a reversible, interactive, Records Officer-only
decision requiring an allowlisted category, a substantive reason, actor,
timestamp, optimistic version, audit entry, and append-only work event. PAT and
shared-agent callers receive a human handoff. The issue contracts do not name
the future reason-code allowlist, so UX-01 must define and validate that bounded
vocabulary before persisting it; this research does not mislabel invented
reason codes as approved.

### Notification channels

The in-application work queue is the operational source for routine casework.
Existing critical Gmail staff alerts remain unchanged and additive. Later
lightweight counts may poll every 30 seconds while the document is visible and
refresh on focus with polite announcements. There is no WebSocket, browser
push, new mail transport, or external automation in the request path.

### Global versus case queues

Global cross-case queues remain available for workload management, oversight,
and operations. Case tabs are request-scoped views of the same REST primitives,
query hooks, components, mutations, authorization, and audit behavior. A scoped
view never creates a second implementation of an action.

### Releases tab label (manual)

The approved user-facing label is **Releases**. It groups installments, fees,
released downloads, exemption-log readiness, and closure readiness without
implying that opening the tab produces records or closes the case. This label
must be reconfirmed during the manual UAT required by UX-06 before rollout.

## Target information architecture

- **Inbox** — the single work surface. Needs attention, Awaiting statutory
  response, New, Unmatched, Quarantined, Delivery exceptions; Assigned to you,
  Unassigned, Team, Critical; Replies waiting, Waiting on others; Received,
  Handled, No action, Recently changed, and All. It absorbed the separate My
  Work screen, whose queues are now the *Your queues* chip group.
- **Cases** — the authorized request index.
- **Review** — authorized record-review work.
- **Staff Intake** — role-scoped nonportal intake.
- **Oversight** — reporting and authorized audit views.
- **Configuration** — Agent access, identity/integrations, registry, templates,
  exemptions, and transport readiness according to role.

Inside a case, the stable tabs are **Overview**, **Messages**, **Collection**,
**Records**, **Releases**, and **Activity**. Only the active tab loads its heavy
data; the header is a bounded role-safe summary.

### Canonical case-action ownership

- **Overview owns** the immutable request and authorized requester contact,
  case-owner assignment, deadlines, initial-response state, lifecycle progress,
  intake-attachment downloads, administrative transitions, and non-request
  closure. Request description remains immutable and every transition keeps its
  existing service boundary.
- **Messages owns** request-scoped message search/detail, drafting, editing,
  preview, approval, queueing, cancellation/retry, attachment downloads,
  work-item handling attached to messages, and interactive clarification
  resolution. Delivery confirmation remains distinct from legal status.
- **Collection owns** request-intent advice, Vault scope/count/export handoff,
  collection-task creation/routing/state, preservation holds, and human-only
  camera exclusion. Advice never applies itself and footage is never auto-denied.
- **Records owns** responsiveness decisions, raster preview, advisory proposals,
  citation-backed withholding/redaction, proposal decisions, second review, and
  immutable redaction rendering. Reviewer access excludes requester contact and
  correspondence.
- **Releases owns** scoped installments, fee assessment/payment, package
  preparation, exemption-log generation, release, downloads, and the read-only
  closure-readiness blocker report. The tab never closes a case.
- **Overview owns the Running log**: the read-only typed lifecycle/work/delivery
  feed. It exposes no mutation and is not a raw `AuditLog` or a substitute work
  queue. It was previously a seventh tab, which meant a case had two histories
  on two screens -- Overview rendered its own StatusEvent list while the tab
  rendered the server-merged feed, and the two could disagree. There is now one
  feed, from `GET /api/staff/requests/{number}/activity/`, in one place.

Global queues remain compatibility and workload surfaces for the same APIs.
Within a case, the nested route named above is the single canonical location for
each action.

### Implemented work-surface inventory and compatibility routes

Unscoped cross-case queues remain available for workload management. Adding a
valid `request` query changes an obsolete resource-scoped bookmark into the
canonical case route with history replacement, preserving only validated
selection and filter state:

| Existing route | Unscoped destination | Request-scoped destination |
| --- | --- | --- |
| `/tasks` | Global collection-task queue | Case **Collection**; `task`, task state, and urgency survive |
| `/preservations` | Global preservation queue | Case **Collection**; preservation state survives |
| `/record-items` | Global record-review queue | Case **Records**; selected item and review state survive |
| `/installments` | Global installment/release queue | Case **Releases** |
| `/time-entries` | Global time-entry queue | Case **Overview**, focused at `section=time-entries` |

Reporting remains under **Oversight**, transport readiness remains under
**Configuration**, and other authorized operational queues remain global.
Global and case variants mount the same React work-surface components and call
the same REST primitives, mutations, role policies, domain services, and audit
paths; scoped APIs add only the request filter.

The former `/correspondence` screen is a redirect contract rather than a
second implementation. Request-scoped correspondence goes to case **Messages**;
explicit inbound receipt review, received-message bookmarks, and
delivery-exception work go to **Inbox**; failed, queued, draft, and
delivery-status-report transport links go to the focused **Configuration →
Correspondence operations** queue for authorized staff.
Quarantined inbound work links use the quarantined Inbox view,
while unmatched, candidate, and ambiguous inbound work links use the unmatched
view. Queued transport state is not mislabeled as a delivery exception.

Redirects use replacement navigation so obsolete URLs do not trap Back/Forward
navigation. Reloading the canonical destination retains validated filters and
selected work; malformed request numbers, identifiers, and filter values are
dropped rather than interpolated into API or case routes.

## Verification baseline and deferred architecture

The `staff-login-open-case` Playwright flow proves that a Records Officer can
sign in with local authentication, find `PRR-E2E-2000`, and verify its request
number, legal status, and immutable description. The fixture deliberately
contains 2,000 messages and realistic evidence shapes.

The implemented workspace keeps its bounded summary cached while mounting only
the active tab. Messages reuse the server-filtered, cursor-paginated summary and
selected-message detail delivered by UX-02; the other tabs reuse their existing
request-filtered REST primitives and guarded mutations.
