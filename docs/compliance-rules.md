# Architecture and compliance rules (full text)

These thirteen rules are the non-negotiable invariants of the PSD401 Public
Records Request system. [`AGENTS.md`](../AGENTS.md) carries a one-line index of
them and links here; this file is the source of truth for their full text.
Rule numbers are stable and are cited from code comments, tests, ADRs, and
`openwiki/` — never renumber them.

Compliance is the product: treat deadline math and exemption logic as legal
artifacts, not just features. Governing law is **RCW 42.56** (WA PRA) and
**FERPA** (student records).

## Rule 1 — Django owns compliance and its own transport edges

Deadline math, request state, exemptions, redactions, public intake, Gmail
send/poll, and the audit trail live in this codebase and are unit tested.
In-process Celery work owns email transport; no external automation platform
sits in a request or requester-correspondence path.

## Rule 2 — All status changes go through `services.transition()`

All status changes go through `apps/records/services.transition()` — never set
`PublicRecordsRequest.status` directly. The service atomically writes
StatusEvent, handles clarification tolling, recomputes the deadline,
audit-logs, and emits webhooks.

## Rule 3 — Every redaction or withholding requires an ExemptionCitation

Every redaction/withholding requires an ExemptionCitation FK (DB-level). The
RCW 42.56.210(3) exemption log is generated from these rows.

## Rule 4 — AuditLog and StatusEvent are append-only

No update/delete code paths.

## Rule 5 — Requester PII is minimized

The PRA forbids requiring purpose/identity beyond what's needed to respond.
Don't add required intake fields.

## Rule 6 — Automated requester-facing messages are limited to two paths

The safety-net acknowledgment (`tasks.auto_acknowledge_safety_net`) is the only
automated *statutory* response; late acknowledgment is strict liability. Its
switch lives in the app, not in `.env.app`: an Application Admin turns
`AUTO_ACK_SAFETY_NET` on or off from Configuration > District settings with a
written reason, the change takes effect without a redeploy, and the Settings
page states the consequence in the district's own terms — off means no
automatic day-5 acknowledgment, and a late acknowledgment is strict liability
under RCW 42.56.520. Moving the switch changed nothing about who may send: the
task still reads one value (`apps.configuration.runtime.get`) and the AST guard
below still forbids a second automated statutory path.

The only other permitted automation is the submission receipt: it is explicitly
non-statutory, cannot set `initial_response_delivered`, cannot transition
request status, and cannot satisfy or defer the day-5 response.
`test_compliance.test_no_automated_path_exists` AST-enforces the current
safety-net/estimate boundary; any submission-receipt implementation must add and
retain the companion `receipt-ast-guard` for statutory effects.

Estimate revisions still require a human and a substantive reason through
`apps/records/letters.revise_estimate()`; no background task may call it.
Automated estimate-kicking is a Yousoufian penalty aggravator. Never invert this
acknowledgment/estimate asymmetry.

The `scope_clarification` letter ("your request matched N items, here are three
narrower options") is human-only for the same reason, AST-guarded by
`scope-ast-guard` in `test_compliance`: its clarification form tolls the
RCW 42.56.520(3) clock on delivery and its informational form explicitly does
not, and choosing between them is a judgment with an accountable name on it.
Only that clarification form and the original `clarification_request` may open
the tolled CLARIFICATION status.

The interpretation-and-estimate letter (#42) is drafted by the system and sent
only by a person. `apps/records/estimate_letters.prepare_estimate_letters`
(`PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED`, an in-app setting, off by default)
computes a `RecordsEstimate` and drafts the letter as a *system-prepared*
draft: no author, never `automated`, and never approved, queued, or sent by the
system. `SYSTEM_PREPARED_DRAFT_KINDS` holds only that kind, the database
constraint `correspondence_system_prepared_kind_allowed` holds the same line,
and `estimate-letter-ast-guard` in `test_compliance` forbids the drafting path
from calling any approve, queue, send, transition, estimate-revision, or
deposit function. A Records Officer approves and sends it
(`correspondence.approve_and_send`); its delivery records the stated date as an
`EstimateRevision` revised by that officer, whose reason names the computed
estimate holding every input. The acknowledgment/estimate asymmetry above is
unchanged: no estimate reaches a requester, or moves `estimated_completion`,
without a named person's approval.

The intake-time intent advisory and the Vault count it enables
(`PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED`, an in-app setting, off by default)
are not exceptions to this rule: a count estimates a search universe and reaches
no requester.

A first response that went out through a channel this system never delivered
(the retired NextRequest tenant, district email, paper) is recorded only by
`services.record_external_initial_response()`: a named Records Officer's written
statement with channel, date when known, and evidence, appended as an
`InitialResponseAttestation`. It sends nothing, and no task may call it
(AST-guarded in `test_compliance`). `manage.py
attest_external_initial_responses` backfills the migrated set through the same
service.

## Rule 7 — Footage: auto-hold always, auto-deny never

Intake triage (`apps/records/triage.py`) detects camera-footage requests and
auto-holds always: every camera at each site the request names
(`apps/records/footage_sites.py` maps school names, abbreviations, and site
codes to Aware camera groups), or every camera district-wide when no site is
identified. Each in-scope camera gets an Avigilon Alta Aware bookmark with no
expiry (`apps/integrations/avigilon/client.py`), starting at the later of the
requested window's start and that camera's oldest retained footage. Requested
dates are whole Pacific calendar days. Retention is per camera, read from
Aware. `CAMERA_RETENTION_DAYS` only sizes the window when the request has no
parseable date. When Aware is unreachable the hold is an URGENT manual hold
whatever the window's age, because only Aware knows which cameras keep footage
past the setting.
A camera whose bookmark covers less than 95% of the window, or whose coverage
Aware did not report, is recorded as a footage gap, and the preservation is
then PARTIAL, never HELD. PARTIAL sends an URGENT notice naming each failed,
gap, or unverified camera.

A request whose dates span more than `CAMERA_HOLD_MAX_SPAN_DAYS` (default 31)
is held for that many days only. When the days inside `CAMERA_RETENTION_DAYS`
alone exceed the cap, the oldest of them are held, because they are pruned
first. Otherwise the hold keeps every retained day and reaches back from the
last requested date, since some cameras keep footage longer than the setting.
A hold narrowed this way,
or narrowed to the sites the request names, writes a StatusEvent and sends one
URGENT scope notice to facilities, the PRA supervisor, and the Records
Officers. A Records Officer widens a site-narrowed hold to every camera
district-wide through `triage.widen_preservation()` (`POST
/api/staff/preservations/{id}/widen/`) with a substantive reason. Widening
bookmarks only cameras with no hold yet, so a camera already tried keeps its
outcome. It only preserves more, so it is a direct action rather than a
human-only handoff. A widen is refused while the first run is still PENDING.
A widen that cannot list Aware's cameras, or finds none, sends an URGENT
manual notice and leaves the hold's state, held counts, scope and confirmation
text as they were. A widen's completion notice and not-retained review cover
only the cameras it added. A widen that stops partway sends an URGENT manual
notice.

Zero cameras at a named site goes to human adequate-search review
(`NO_CAMERAS`) and an URGENT manual notice, never "held": footage may sit on
another recorder. Where no in-scope camera retains the window, and for each
camera that no longer retains it inside a partly held preservation: queue only
a human-review + adequate-search checklist (Neighborhood Alliance standard) —
no requester response is drafted or queued automatically. A human must complete the search,
choose the legally supported response, draft, approve, and send; there is no
auto-denial code path and none may be added. The preservation confirmation
text (HELD/PARTIAL only) is shown to staff for correspondence they write; it is
never sent automatically.

Release and exclusion are human-only with a reason. Camera exclusions go
through `triage.exclude_camera()`. Deleting a preservation's bookmarks goes
through `triage.release_preservation()`, which requires a closed request (and,
for a merged duplicate, a closed case of record), current Records Officer
authority, a substantive reason, and an attestation that no litigation hold,
pending lawsuit, or reasonably anticipated litigation covers the footage. Both
are AST-guarded like `revise_estimate`. Release and exclusion mark each
bookmark `RELEASE_FAILED` (not yet confirmed deleted) inside their
transaction, and flip it to `RELEASED` only after Aware confirms the delete (or
reports the bookmark already gone). A refused delete, or a crash partway
through, therefore leaves the camera retryable and logged; posting the release
again retries only those cameras, excluded ones included. Release is refused
while a hold run (a widen included) is still bookmarking; if the sweeper
cleared a stalled run that then makes one more bookmark after a release, that
bookmark is marked `RELEASE_FAILED` so the retry deletes it. A camera whose
create Aware never confirmed has no bookmark id to delete by, so the release's
timeline entry (or, for a create made after the release, its own timeline
entry) names the bookmark for a human to find and delete in Aware.
The confirmation text says when footage recorded after the hold was placed is
not yet confirmed preserved.

Intake enqueues the hold task only after its transaction commits. Each run
claims a camera's row before calling Aware, so two runs never bookmark the
same camera; the claim is cleared only when Aware definitely made no bookmark.
Coverage is scored against the time each camera's bookmark was made. If the
Aware API is down or unconfigured, or a hold task crashes, holds degrade to
URGENT manual instructions to facilities — never to silence. Every run (intake
and widen) marks itself in progress and heartbeats per camera. A beat sweeper
runs every 5 minutes: a PENDING hold whose heartbeat is 15 minutes old moves
to URGENT manual, and a stalled widen sends the URGENT notice but keeps the
hold's state. A run's final state save is conditional on the state it started
from, so it never overwrites what the sweeper set; a lost save is audited.

## Rule 8 — The system registry routes collection

The system registry (`RecordSystem`, seeded by `manage.py seed_systems`) maps
every district system to a custodian title, retention profile, triage keywords,
and an adequate-search checklist. Intake routing (`triage.route_to_systems`)
creates a manual CollectionTask per matching system; urgent_hold systems (bus
video!) also fire an immediate preservation email. Re-seeding preserves
district-entered custodian and retention edits — only checklists/keywords
refresh.

District's remaining setup: assign `custodian_email` per system, verify
⚠️-marked retention windows (bus video vendor+retention FIRST), clear
`needs_verification`.

## Rule 9 — `request.description` is never edited after intake

`request.description` is never edited after intake (it's the legal record of
what was asked). Clarifications are new StatusEvents.

Two numbers for one request are resolved only by `services.merge_requests()`:
every matched inbound email is rematched to the case of record, the duplicate
closes as `CLOSED_DUPLICATE` with `merged_into` set, and the target keeps its own
`received_at` and deadline. `CLOSED_DUPLICATE` is unreachable through
`transition_manually` and a merged duplicate cannot be reopened. Closing any
case retires its requester-email Inbox rows
(`work_queue.services.handle_case_inbox_items`), attributed to the closing
officer.

## Rule 10 — GUI/agent parity is mandatory

This follows Every's [Agent-native
Architectures](https://every.to/guides/agent-native): every outcome available in
the staff UI must have an agent-accessible atomic REST primitive. Register it in
`apps/api/capabilities.py` in the same change; `test_agent_capabilities` scans
production frontend API calls and fails if the GUI and capability catalog drift.

Agent parity does not weaken rules 6 or 7: estimate revisions, denials, and
camera exclusions remain accountable-human handoffs rather than autonomous agent
actions.

## Rule 11 — Every integration fails closed and uses least privilege

The sole anonymous application operation is `POST /api/public/requests/`:
write-only, CSRF-protected, signed-form and fill-time gated, per-IP and globally
rate-limited, idempotent, and limited to a request-number response with no PII
echo. Every other business/data API requires an explicit identity.

Shared stdio MCP uses a separate nonstaff token with only
`records.read_staff_api`; hosted MCP uses attributed personal staff tokens and
never that shared identity for its gated tools. Hosted `/mcp` exposes exactly
five attributed personal-token draft/proposal tools -- an acknowledgment draft,
a clarification draft, two proposals that apply nothing, and a decision-batch
proposal (#43, rule 11 amendment) that records a batch for a person to approve;
standalone stdio and the shared nonstaff MCP identity remain read-only. Drafting
is not delivery: every one of them stops short of approving, queueing, or
sending, so the surface never widens rule 6's two automated requester-facing
paths.

Decision batches (#43): only a person with the batch kind's role approves or
rejects a batch, in the staff application (`decision_batches.approve`/`reject`
are human-only; a token gets `human_approval_required`), and approval runs the
same service the staff UI runs. Proposal and decision are append-only rows. For
token callers, the `human_confirmation` endpoints a batch replaces -- record
review and id-list bulk review, collection supersede, Vault collection start and
re-scope, case merge, and non-request closure -- now answer 403
`human_approval_required`; browser sessions are unchanged, and other
`human_confirmation` endpoints keep accepting tokens. Never put tokens in source or
`.env.example`.

The one unattended Vault action is authorized by district decision
(2026-09-23): with the in-app `PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED` setting
on, a completed automatic intent proposal that maps a request to an active
Vault-backed system runs Gmail *counts* with the proposed query, no accountable
actor, and `trigger=automatic`: the first across the entire organization, or
only the district mailboxes the proposal names (#36); then, under iterative
scoping (`apps/records/scoping.py`), at most `PRR_INTENT_MAX_COUNT_ITERATIONS`
(in-app, default 4, 1 turns refinement off) counts in all, each a tighter query
the agent proposed after reading the previous total and custodian breakdown.
Every one is still a count.

Collection from the approved scope (#37, rule 11 amendment, district decision
of the epic #27 plan). A Records Officer approves the proposed scope and
starts the Vault collection in one action from the Scope stage
(`requests.vault_collection.start`). With the in-app `PRR_TRUST_SCOPE_MODE`
set to `auto` (default `suggest`), the export also starts unattended -- and
only then: from the final completed count of a finished *automatic* scoping
session, on a request with no Vault collection yet, with no actor and
`CollectionTask.trigger=automatic`, which the Collect stage shows. It is the
same collection an officer would start: the same deposit gate, the same
targeted hold for named district mailboxes and no organization-wide hold, the
same export into review. An officer's re-run never collects by itself;
re-scoping (`requests.rescope`) is an officer's approval with a reason that
collects the new scope and supersedes the previous finished collection's
unreviewed records. `test_compliance` pins the only caller of the automatic
path. It may create the count's
Vault matter; it never holds, exports, collects, changes status, or reaches the
requester. It has no volume cap of its own, also by district decision: web-form
requests are bounded only by the public-intake throttles, and emailed requests
by nothing beyond one request per Gmail message and the blocked-sender-domain
list. Do not widen it to any other Vault operation.

Web-form files use bounded multipart `attachments` parts, generated object keys,
private quarantine, and ClamAV. They are `IntakeAttachment`s, never responsive
`RecordItem`s; no pending/infected/error file may be downloaded.

## Rule 12 — Google group authorization stays keyless and read-only

The dedicated group-reader service account receives the Workspace Groups Reader
role once so application administrators can map existing groups without
per-group Google Admin work. Application code still performs only named-user
direct membership lookups against configured groups. Never add the reader to
every role group, reuse the Vault identity, or grant Groups Editor, Groups
Administrator, another admin privilege, a private key, or domain-wide delegation.

Groups are not the only way in: an Application Admin may record a reasoned
`DirectStaffRoleGrant` for a named address that belongs to no syncable group.
Grants only ever union with group-derived roles, are audited, expire sessions on
change, and keep working through a Google outage precisely because they are
application records rather than Google facts. Never let a grant subtract a role
or bypass the last-admin guard.

## Rule 13 — A case's matter kind decides which law it answers to

`PublicRecordsRequest.matter_kind` is `public_records`, `litigation_discovery`,
or `ferpa_parent` (ADR [`docs/decisions/0004-matter-kinds.md`](decisions/0004-matter-kinds.md)),
fixed at intake and immutable in the ORM and at the database. Collection,
review, redaction, production, and the audit trail are shared.

The RCW 42.56.520 clock, the day-5 safety net, JLARC, and the PRA letter
instruments belong to `public_records` alone: a litigation matter sends no
requester correspondence at all and a FERPA matter sends none that asserts
RCW 42.56, carries the 34 CFR 99.10(b) 45-**calendar**-day `ferpa_access_due`
instead, and discloses nothing until a named officer records the 34 CFR 99.10
identity verification.

Nothing a requester reaches — the public form or the intake mailbox — may open
anything but a public records request, and cases governed by different law never
merge. Any new statutory query, watchdog, report, or letter kind must say which
kinds it applies to.

## Operating against a deployed instance

If you're calling this system over HTTP instead of editing its code (a script,
an MCP client, or an agent), start at
[`agentic-integration.md`](agentic-integration.md). It documents the API
surface, the MCP server, and, most importantly, the guardrails section: what
agents may do against a live instance versus what they must never do. Rules 6
and 7 apply just as much to an agent calling the API as they do to code running
inside this repository.
