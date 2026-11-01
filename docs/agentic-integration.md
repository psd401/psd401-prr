# Agentic & API Integration

This is the hub document for anything that talks to this system over HTTP
instead of through the Django code directly: scripts, MCP clients, hosted
tools, and LLM agents (including Codex sessions working on or against this
codebase).
For the architecture rules this document has to stay consistent with, see
[`AGENTS.md`](../AGENTS.md) for the index and
[`compliance-rules.md`](compliance-rules.md) for their full text.

Contents:
- [Agent-native foundation](#agent-native-foundation)
- (a) [API surface](#a-api-surface)
- (b) [Removed webhook event bus](#b-removed-webhook-event-bus)
- (c) [MCP server](#c-mcp-server)
- (c.1) [Command-line client](#c1-command-line-client)
- (d) [Agentic guardrails](#d-agentic-guardrails)

## Agent-native foundation

This project adopts the parity principle from Every's
[Agent-native Architectures](https://every.to/guides/agent-native): whatever a
staff user can accomplish through the GUI must have an agent-accessible atomic
primitive. The React SPA and agents therefore operate on the same Django/DRF
domain API; UI-only business logic is an architecture defect.

`GET /api/staff/capabilities/` is the machine-readable contract. Each row
provides a stable capability id, user outcome, REST method/path, GUI routes,
risk, MCP discovery name when one exists, constraint definitions, and one of
three execution policies:

| Policy | Meaning |
|---|---|
| `direct` | A token-authenticated agent may execute the REST primitive. |
| `human_confirmation` | The agent may prepare the action, but a human must explicitly approve it before execution. |
| `human_only` | The agent must hand off; the accountable human performs the action in the staff UI. |

The last category is deliberate, not an accidental missing tool. Estimate
revisions, correspondence approval/queueing, clarification-response
association and resolution, denials, and camera exclusions carry accountable
human boundaries protected by `AGENTS.md` and the capability contract.
Agent-native parity means the agent can discover the boundary, prepare the
work, and produce a precise handoff; it does not mean disguising an automated
caller as a person.

Human-only staff endpoints enforce that distinction at runtime. A DRF token
caller receives `403` with:

```json
{
  "code": "human_approval_required",
  "detail": "This legal action requires the accountable human to execute it in the staff application.",
  "handoff": {
    "capability_id": "correspondence.approve",
    "action": "Approve the immutable correspondence snapshot.",
    "required_role": "records_officer"
  }
}
```

The same role-mapped person may complete the action through the
CSRF-protected staff session. This is intentionally not a challenge that a
token caller can satisfy by adding another request field.

`apps/api/tests/test_agent_capabilities.py` scans every production frontend
`api()` call and compares its method/path to `apps/api/capabilities.py`. A new
GUI operation fails the backend suite until its agent primitive and policy are
registered. Browser session login/logout/me are the only excluded calls because
agents authenticate and identify themselves through DRF tokens instead. Agents
can discover the same catalog through the MCP `psd_prr_capabilities_list` tool.

## (a) API surface

### Public request intake

The sole anonymous business operation is the write-only public intake API:

- First `GET /request` from the same origin. The shell sets the normal CSRF
  cookie and an HttpOnly `prr_public_intake` signed form cookie. The latter is
  accepted only between `PRR_PUBLIC_INTAKE_MIN_FILL_SECONDS` (default 3) and
  `PRR_PUBLIC_INTAKE_TOKEN_MAX_AGE_SECONDS` (default 86400).
- **`POST /api/public/requests/`** accepts JSON or multipart data with the CSRF
  cookie plus matching `X-CSRFToken` header. `email` and `description` are the
  only required requester fields; `name`, `phone`, `street_address`, `city`,
  `state`, `postal_code`, and `company` are optional. `website` is the hidden
  honeypot and must remain empty. Multipart submissions repeat `attachments`.
- The endpoint has no authentication scope because it is deliberately public,
  but it is bounded by Redis-backed per-IP (`10/hour`) and global (`500/day`)
  hard ceilings. A successful first submission returns `201 {"number":
  "PRR-YYYY-NNNN"}` with no PII or internal ID. An identical signed-form replay
  returns the same number with `200`; conflicting reuse or in-progress work
  returns `409`, upload-boundary rejection returns `413`, throttling returns
  `429`, and partial processing failure returns `503`.
- Honeypot, too-fast, expired, missing, and invalid signed-form rejections use
  the same generic `400` body while the server logs the specific reason without
  requester content. `PublicIntakeSubmission` stores only token/payload hashes,
  state, source IP, and the linked request.

A hard process termination can leave a public receipt in `processing`. After
`PRR_PUBLIC_INTAKE_PROCESSING_STALE_MINUTES` (15 minutes by default), an
accountable staff operator must confirm that the original process is gone and
run the break-glass recovery command documented below.

```bash
python manage.py recover_stale_intake RECEIPT_ID \
  --operator STAFF_USERNAME \
  --reason "Host terminated after public intake; original process is gone." \
  --confirm
```

The command never creates or retries a request. If a request was already
linked, it remains authoritative and a same-form replay returns the failed
state with that request number; an unlinked receipt additionally requires
`--confirm-unlinked` after reconciliation against the request queue.

The public primitive is registered as capability `requests.submit_public` with
the explicit `anonymous` execution policy and is served by the accessible
`/request` form. It is not an MCP tool: no MCP transport exposes request intake.

### Outbound correspondence transport

Requester email delivery is an in-process Gmail transport boundary. A
migration-provisioned, non-login system principal with exactly
`correspondence.deliver_outbound_correspondence` attributes transport work;
it has no API token and cannot be provisioned through the integration identity
console. The worker calls the claim and result services directly.

Claiming returns either an immutable `send` payload
(subject, body, To/Cc/Bcc, deterministic Message-ID, tagged Reply-To, and safe
attachment descriptors) or a `reconcile` instruction containing only the
exact Gmail RFC822 Message-ID query. An active lease is a deterministic `409`
with `Retry-After`. A callback at or after lease expiry receives stable `409
delivery_claim_expired`. The minute delivery watchdog finalizes the expired
attempt as `lease_expired`, moves the same delivery to
`awaiting_reconciliation`, and audits the change; a later claim can return only
reconciliation. An uncertain provider outcome, including a timeout, must be
`unknown`, never a definitive failure category, and also requires
reconciliation before any resend.

Result callbacks are attempt-bound. Equivalent normalized durable evidence is
idempotent: timestamp offsets normalize to one UTC instant, raw provider errors
are discarded, and `sent`/`reconciled` are aliases on reconciliation claims.
Different provider ids, timestamp instants, categories, or safe metadata for a
completed attempt are a `409` conflict. A successful first result
stores provider ids/timestamp and then applies the already-approved legal
effect once. Categorized failures store only allowlisted descriptions and
bounded provider codes; raw provider errors are discarded. A reconciliation
`not_found` result requires a Records Officer to use the staff Retry action
before the worker can receive another send claim. The same delivery and Message-ID are
always reused.

The provider timestamp in successful evidence is the authoritative
communication instant. Django uses it, rather than callback or reconciliation
time, for the correspondence, acknowledgment, estimate, clarification or
denial transition, and Pacific-date tolling effect. Django also serializes
acknowledgment, clarification-request, estimate-revision, and denial work per
request: while any one is unresolved, later lifecycle queueing, request-state
changes, and competing requester-facing lifecycle effects fail closed. An
estimate message must still match the request's current estimate when approved,
queued, or retried.
The timestamp may be no earlier than five minutes before the send claim (or
delivery queue time for reconciliation), no later than the callback, and no
earlier than request receipt or the latest recorded lifecycle communication.
An identical already-recorded callback remains idempotent after later request
activity.

The worker reads a claim-bound attachment through the internal transport
service. No transport attachment is exposed through an HTTP route.

This transport is not a staff GUI capability or an agent authority. Django
already made the approval/queue decision; the worker can only carry out the
leased edge action. See [`docs/correspondence.md`](correspondence.md) for domain
semantics.

### Inbound correspondence transport

Inbound Gmail delivery is handled by an in-process poller. A
migration-provisioned, non-login system principal with exactly
`correspondence.submit_inbound_correspondence` attributes the work; it has no
API token. The poller supplies original RFC822 bytes and strict provider
metadata directly to the receiving service. Metadata includes provider
message/thread IDs, explicit-offset received timestamp, optional exact
envelope sender (omitted or `""` when absent), and bounded recipients. It
cannot supply request authority.
The authoritative provider fields are Gmail Message `message.id`, `threadId`,
and `internalDate` converted from epoch milliseconds to UTC RFC3339, never
sender-controlled MIME headers or workflow time. Gmail Message data has no
authoritative SMTP envelope fields: omit the sender unless separately exposed
by trusted provider data, and derive recipients from the authenticated district
mailbox/routing context rather than MIME `To`/`Cc`/`Bcc`. Preserve exact
recipient case and order; only exact duplicates violate version 1 uniqueness.
Django streams and retains raw evidence, safely parses MIME, quarantines and
scans attachments, then applies the deterministic reply-token,
`In-Reply-To`, provider-thread, `References`, and subject-candidate order.
One narrow post-match rule sends eligible plain mail addressed only to the bare
mailbox through canonical request intake. It rejects tokenized mail, DSNs,
automated/list/bulk/junk messages, invalid senders, and empty bodies. The atomic
receipt claim makes replay return the same request, and linked intake
attachments continue to use the original correspondence scan verdict.
The bounded raw preflight rejects unsupported nested `message/*` formats and
counts delivery-status and transfer-encoding-visible RFC822 pseudo-parts before
the full MIME tree is built; integrations must forward original bytes rather
than normalizing or reconstructing those structures.
For a top-level RFC 3464 delivery-status report, Django instead requires one
embedded application Message-ID and uses only that value to find the stable
outbound delivery. The receipt retains that strict target and, for an exact
match, the opaque delivery ID. Recipient association uses an ephemeral
domain-separated fingerprint against the frozen recipient snapshot and
persists no reported address or fingerprint. Django stores only
privacy-minimized, append-only, unverified/advisory outcome evidence; it never
falls back to ordinary reply
matching, rewrites the send result, queues a retry, changes request status, or
treats the DSN as a clarification response. The poller must not parse a bounce
or submit a caller-selected result.

Success responses expose only opaque receipt/correspondence identifiers and
state. Identical completed replay is idempotent. Conflicting idempotency or
provider identifiers are `409`; an in-progress first submission is retryable;
storage failure fails safely without exposing provider diagnostics. Accepted raw evidence that later fails
safe MIME parsing is retained and quarantined rather than becoming an
untracked failure. The poller never chooses an existing request, follows MIME
URLs, decides file safety, or resolves a clarification; its only request
creation path is the eligibility-gated canonical intake service above.

### Staff API (`/api/staff/`)

Mounted from `apps/api/urls.py`, which auto-discovers every module under
`apps/api/resources/` (`pkgutil.iter_modules`, sorted by module name) and
calls that module's `register(router)` on one shared `DefaultRouter`.
Adding a staff resource means adding a module there with a `register()`
function; nothing else needs to change.

Auth endpoints, wired directly rather than through a resource module
(`apps/api/urls.py:16-18`):

| Path | Method | View |
|---|---|---|
| `/api/staff/auth/login` | POST | Local-development mode only; omitted in production Google mode |
| `/api/staff/auth/oidc/authenticate/` | GET | Starts the Google OIDC authorization-code flow in production |
| `/api/staff/auth/oidc/callback/` | GET | Validates the Google response, group roles, and starts a session |
| `/api/staff/auth/logout` | POST | `LogoutView` |
| `/api/staff/auth/me` | GET | `MeView`, current-user payload, also sets the CSRF cookie |
| `/api/staff/auth/api-tokens/` | GET, POST | Session-only named personal-token list and creation |
| `/api/staff/auth/api-tokens/<id>/rotate` | POST | Session-only rotation; revokes the old token and returns its replacement once |
| `/api/staff/auth/api-tokens/<id>` | DELETE | Session-only revocation with retained metadata |

Router-registered resources, one row per module in `apps/api/resources/`:

| Module | Base path | Methods | Notes |
|---|---|---|---|
| `audit.py` | `audit-log/` | GET (list, retrieve) | `ReadOnlyModelViewSet`: no write route exists for `AuditLog` at any layer, matching its append-only model |
| `collection_tasks.py` | `collection-tasks/` | GET, POST, PATCH | no destroy: `CollectionTask` is a working record, not evidentiary |
| `correspondence.py` | `correspondence/` | GET list/retrieve; POST draft, approve, queue, cancel, retry; PATCH draft; GET preview and attempts | list payload is PII-minimal; detail is request-authorized; mutations use the correspondence domain services |
| `correspondence.py` | `correspondence-attachments/` | `POST <id>/download/`; `GET <id>/content/<token>/` | clean-only, five-minute, user-bound download; no storage key is exposed |
| `correspondence.py` | `clarification-cycles/` | GET list/retrieve; `POST <id>/resolve/` | receiving a response never resolves the cycle; resolution is human-only |
| `correspondence_operations.py` | `correspondence-operations/` | GET list; `GET readiness/` | Records Officer/Application Admin-only PII-free aggregate queue metrics, including failed/delayed delivery-notification evidence, and fixed-code dependency readiness; unavailable to the read-only MCP identity |
| `inbound_correspondence.py` | `inbound-correspondence-receipts/` | GET officer-only unmatched/quarantined queues and detail; `POST <id>/match/`, `<id>/rematch/`, `<id>/redirect/`, `<id>/associate-clarification/` | strict review queues; detail includes sanitized DSN outcomes; matching corrections and explicit clarification-response association are human-only, versioned, reasoned, and IP-attributed; `redirect/` additionally closes the automatically opened source case as not a records request in the same transaction, and applies only to an email-intake match; DSNs cannot use any of those generic mutation paths |
| `exemptions.py` | `exemptions/` | GET, POST, PUT, PATCH, DELETE | full CRUD on the RCW 42.56 exemption taxonomy |
| `intake_attachments.py` | `intake-attachments/` | `POST intake-attachments/<id>/download/` | Records Officer-only five-minute download URL; refuses every file without a clean malware scan |
| `identity_config.py` | `google-role-mappings/` | GET, POST, PUT, PATCH, DELETE | Application Admin-only mapping of existing Workspace groups to stable staff roles; changes are audited and expire Google sessions |
| `preservations.py` | `preservations/` | GET (list, retrieve); `POST preservations/<id>/exclude-camera/` | read-only plus one gated action, see (d) |
| `record_systems.py` | `record-systems/` | GET, POST, PUT, PATCH; DELETE returns `405` | registry rows are FK-referenced by `CollectionTask`; deactivate with `active=false` instead |
| `records_misc.py` | `requesters/` | GET, PATCH | no create: requesters only come from intake |
| `records_misc.py` | `record-items/` | GET (list, retrieve) | read-only |
| `records_misc.py` | `installments/` | GET (list, retrieve); `POST installments/`, `POST installments/<id>/set-items/`, `prepare/`, `generate-exemption-log/`, `assess-fee/`, `record-payment/`, `release/`, `release-and-send/`, `revoke/`, `set-access-code/`, `clear-access-code/` | Records Officer-only. Create takes a request number and record ids; membership may change only while no package is prepared. `release-and-send/` and `revoke/` are human-only and hand a token caller an explicit handoff. When the release commits but the notice cannot be sent (transport disabled, installment revoked meanwhile), `release-and-send/` answers `409` with `code: installment_notice_unsent` and the installment row: the release stands, the notice stays a draft, and pressing again once the cause is resolved sends it. No response ever carries the download token or the access code |
| `records_misc.py` | `time-entries/` | GET, POST, PUT, PATCH, DELETE | full CRUD |
| `requests.py` | `requests/` | GET, PATCH; `POST requests/intake/`, `POST requests/<number>/transition/`, `POST requests/<number>/close-non-request/`, `POST requests/<number>/close/` | Intake Clerk/Records Officer intake is actor-scoped and idempotent through the canonical pipeline; lookup uses `number`, not id; the not-a-records-request closure is Records Officer-only, reasoned, and limited to retained auto-created email evidence; `close/` records only withdrawal or abandonment, is open to every workflow staff role, and still demands a substantive reason; estimate revisions use correspondence and there is no direct request estimate-revision action |

Every resource explicitly declares its staff-role policy, enforced by
`IsStaffOrReadOnlyAgent`. Records Officer owns legal case actions; Liaison is
scoped to assigned requests/tasks; Reviewer receives review surfaces without
the requester directory; Intake Clerk can submit staff intake but cannot read
the resulting queue; Application Admin owns configuration but cannot make
legal transitions, estimate revisions, or camera exclusions. The nonstaff MCP
identity remains safe-method-only and is additionally denied sensitive
identity and requester resources. Anonymous and ordinary nonstaff users are
denied.

Correspondence draft routes are:

| Outcome | Method and path |
|---|---|
| General | `POST /api/staff/correspondence/drafts/general/` |
| Acknowledgment | `POST /api/staff/correspondence/drafts/acknowledgment/` |
| Clarification request | `POST /api/staff/correspondence/drafts/clarification/` |
| Estimate revision | `POST /api/staff/correspondence/drafts/estimate-revision/` |
| Installment notice | `POST /api/staff/correspondence/drafts/installment/` |
| Responsive-records notice | `POST /api/staff/correspondence/drafts/responsive-records/` |
| Closure | `POST /api/staff/correspondence/drafts/closure/` |
| No-records / out-of-retention closure | `POST /api/staff/correspondence/drafts/no-records-closure/` |
| Denial | `POST /api/staff/correspondence/drafts/denial/` |

`PATCH /api/staff/correspondence/<id>/` requires `expected_version`. Authored
kinds accept subject/body and optional recipients. Structured acknowledgment,
clarification, estimate-revision, and installment drafts accept only their
kind-specific fields and optional recipients; Django re-renders the versioned
template and rejects unknown or kind-inappropriate fields.

The installment routes take no download URL, on create or on edit. The server
derives the link from the installment's own scoped token, so no caller --
human or agent -- can point a requester notice at any other address, and a
notice for a revoked installment is refused.

The denial route is only for a fully exempt response and additionally requires
`"exemption_citation_ids": [<integer>, ...]`. Every ID must name an active
`ExemptionCitation`; the Records Officer selection and immutable citation
wording snapshot are persisted as protected foreign-key evidence and frozen at
approval. Approval also requires every selected citation to appear as a
complete string in the requester-visible body; the approval audit binds that
basis to the exact approved-body hash. Approval, initial or retry queue, and
the send claim require the citations to remain active and unchanged, apply the
current second-review rule, and verify the immutable through rows and body
against the approval evidence. Once Django issues a send claim, any
reconciliation claim, provider result, and canonical denied transition verify
frozen recipient/basis rows and approval evidence without consulting mutable
requester-contact or taxonomy fields; later edits cannot erase transport
evidence for a message Gmail may already have accepted. Corrupted frozen rows
or approval evidence still fail closed.
The exact current requester must be a `To` recipient at approval, queue/retry,
and send claim. Counsel-only delivery, requester-only `Cc`, and stale requester
identity or address snapshots are rejected before transport authorization.
Citations selected for a full-exemption denial and marked
`requires_second_review` fail closed at correspondence approval because that
denial-specific citation set does not yet have its own persisted second-review
artifact. This is distinct from record-item second-review evidence, which is
persisted and enforced before redaction burn-in, packaging, and release.
No-records and out-of-retention determinations require adequate-search evidence
and must not be represented by inventing an exemption citation or using this
full-exemption route. Their dedicated route is interactive-session-only and
requires the structured evidence documented in `docs/no-records-closure.md`.

An attributed Liaison agent may draft authorized correspondence for an
assigned request, including assisting with an estimate-revision proposal.
Capability `correspondence.draft_estimate_revision` requires an accountable
current staff identity and a substantive reason; an unattributed integration
identity is rejected. The draft has no legal or requester-facing effect. A
Records Officer reviews and approves the immutable snapshot, then queues it
through the normal correspondence route in an interactive staff session.
Drafting, approval, and queueing do not change
`PublicRecordsRequest.estimated_completion` and do not create communicated
`EstimateRevision` evidence. Only a successful provider result applies the new
date at the provider timestamp, creates the revision and status evidence,
audits the change, and emits `request.estimate_revised`. A failed, unknown,
expired, cancelled, or merely queued delivery has no estimate effect. There is
no direct estimate-revision action on the request resource.

A Records Officer token may prepare restricted closure or full-exemption
denial drafts with the required citation IDs.
The no-records/out-of-retention route is stricter: token callers receive a
human-only handoff before evidence or a draft is created. Installment version
3 requires a released, verified, unexpired package and scoped application
download. Tokens cannot approve or queue any
correspondence, associate a general inbound message with a clarification
cycle, resolve a clarification, or exclude a camera. Those human-gated calls
return the structured handoff above. A direct transition to denied is not
available at all; only confirmed delivery of the approved denial correspondence
applies that status. Application Admin alone has no authority for these legal
mutations.

Capability
`inbound_correspondence.associate_clarification_response` is `human_only` and
`high` risk. The endpoint
`POST /api/staff/inbound-correspondence-receipts/<id>/associate-clarification/`
accepts `clarification_cycle_id`, a substantive `reason`, and the
receipt's `expected_version`. The receipt must already be matched as general
correspondence to the same request, and the selected cycle must be open and
eligible for the message timestamp. The action records the explicit
association and append-only audit evidence; it does not resolve the cycle,
leave clarification status, or fabricate a new inbound message.
Token-authenticated calls receive the structured handoff before the receipt is
looked up.

Correspondence operations are exposed separately:

- `GET /api/staff/correspondence-operations/` returns `generated_at`, fixed
  counts for queued, claimed, awaiting-reconciliation, retryable, failed, sent,
  cancelled, stuck, inbound, unmatched, quarantined message/attachment work,
  and cumulative unverified/advisory failed or delayed status-report rows;
  oldest delivery, claim, and stuck ages in seconds; and current transport
  failure counts. A later status report does not erase an earlier advisory row,
  so `delivery_exceptions` is evidence volume rather than an actionable queue
  size. Failure keys are restricted to the provider/reconciliation allowlist;
  any historical unknown value is grouped under `other`.
- `GET /api/staff/correspondence-operations/readiness/` returns an overall
  boolean and fixed `component`/`status`/`code` rows for the outbound switch,
  object storage, ClamAV, Redis task broker, and the enabled Gmail transport
  with its migration-provisioned exact-scope system principal.

Both endpoints are read-only and limited to Records Officer or Application
Admin roles. They return no request/message/recipient identifiers, content,
requester PII, endpoint URLs or names, bucket/host/account configuration,
credentials, or raw exception text. A dependency failure is still an HTTP
`200` snapshot with `ready: false`; operators may use
`python manage.py check_correspondence_readiness --json` when a non-ready
result must also produce a nonzero process exit.

Generic request transitions cannot set `acknowledged`, `clarification`, or
`denied`. Those communication-dependent states are applied only by the
correspondence domain after a confirmed delivery result. The domain returns
`409 correspondence_delivery_required` for a direct administrative attempt;
an attributed token attempting the legacy denied transition instead receives
the stricter `403 human_approval_required` handoff. An interactive Records
Officer must approve and queue a denial, and its first confirmed successful
provider result performs the canonical denied transition. Queued, failed,
unknown, and cancelled denial deliveries have no request-status effect.

Agents may continue to propose or execute authorized administrative
transitions that are not tied to requester communication. Fulfilment still
requires a current Records Officer and a substantive case-specific reason, and
Application Admin authority alone is insufficient for it.

Withdrawal and abandonment are the exception, and they have their own route:
`POST requests/<number>/close/` (`requests.close_case`) accepts only
`closed_withdrawn` and `closed_abandoned`, and is open to every workflow staff
role—Records Officer, Department Liaison, Reviewer, Application Admin. They are
documentary facts about what the requester did rather than legal
determinations about the records, and what makes them accountable is the
substantive written reason and the append-only audit entry naming the actor,
both of which this route still requires. Every other closure—fulfilled,
no responsive records, not a records request, denied—and every reopen stays
with the Records Officer.

An open clarification may return to active work only through the human-only
clarification-cycle resolution primitive after a linked response and
substantive notes. The generic transition route otherwise returns `409
clarification_resolution_required`; the closure route above is the way out for
a requester who withdrew or a clarification period that expired. That terminal
path atomically cancels the open cycle and does not fabricate a response; it
cannot be used for fulfilled or denied.
If an already-matched general inbound message is the response, the Records
Officer first uses the separate human-only association primitive above. That
evidentiary action still does not resolve the clarification or change request
status.

### Auth model

The authentication modes are deliberately separate:

- **Session auth (the SPA)**: `SessionAuthentication`
  (`config/settings.py`), CSRF-protected. Production logs in through direct
  Google OIDC, identifies the person by immutable Google `sub`, and maps direct
  membership in administrator-configured Workspace groups to application
  roles. Local developers use `POST /api/staff/auth/login` without any Google
  or AWS access; that route does not exist in Google mode. The session cookie
  carries auth afterward. Built for the React staff app, not scripts.
- **Read-only MCP token**: provision with
  `python manage.py provision_mcp_readonly mcp-production`. The account is
  nonstaff, cannot log in, and can only issue staff API GET/HEAD/OPTIONS calls.
  Application Admins may provision or rotate this same identity at `/identity`
  using the shared exact-scope service; the replacement token is displayed
  once.
- **Integration Identity console**: `/identity` lists the read-only MCP scope
  and every named account carrying its target permission. Only an
  Application Admin using a CSRF-protected staff session may provision or
  rotate; any token caller receives a structured `human_approval_required`
  handoff. Lists, logs, and append-only audit rows omit token material.
- **Full attributed staff personal access token**: an active, role-mapped staff
  user manages named, expiring credentials through the Agent Access page or
  `/api/staff/auth/api-tokens/`. Creation takes a name and requested lifetime,
  bounded by `PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS` (90 by default), and each
  user may have at most `PRR_PERSONAL_TOKEN_MAX_ACTIVE` live credentials (5 by
  default). Only a SHA-256 digest of the high-entropy
  `prr_pat_<prefix>_<secret>` value is stored. The complete value appears only
  in the create or rotate response; later lists expose only name, public
  prefix, creation, expiry, last-use, and revocation timestamps. Rotation
  revokes the prior row and retains both rows as audit history. Authentication
  rejects expired and revoked credentials distinctly, and throttles
  `last_used_at` writes with
  `PRR_PERSONAL_TOKEN_LAST_USED_WINDOW_SECONDS` (60 by default).
  Lifecycle operations accept only the interactive, CSRF-protected staff
  session; any personal or service token caller receives the structured
  `human_approval_required` handoff. Every create, rotate, and revoke is
  attributed in the append-only audit log without key material. Role policy,
  Google-group revalidation, and all human-only handoffs apply on every use, so
  a token cannot outlive the holder's role membership or turn Application
  Admin authority into Records Officer authority. The migration to this model
  revokes legacy plaintext staff `authtoken.Token` rows; exact-scope nonstaff
  service tokens are deliberately preserved.

All token calls send `Authorization: Token <key>` and require HTTPS; they do
not use cookies or CSRF. Personal-token lifecycle calls are the inverse: they
use the interactive session cookie and CSRF, and reject token authentication.
A holder may give their personal token to their own agent, interactive or
scheduled (for example a personal AI Studio agent that runs a daily check),
because every call is still attributed to that holder and revalidated against
their current role. The token stays in that holder's own credential store and
is never shared between people; a scheduled run stops when the token expires
or is revoked, and the holder replaces it from `/agent-access`. A shared
server-side MCP deployment that acts for no one person continues to use its
separately provisioned read-only nonstaff identity.

### OpenAPI schema and docs

`drf-spectacular` generates the schema from the live DRF urlconf
(`config/settings.py:43,46-50`), so it covers both public and staff surfaces:

- `/api/schema/`: raw OpenAPI schema (`SpectacularAPIView`,
  `config/urls.py:12`)
- `/api/docs/`: Swagger UI over that schema
- `/api/redoc/`: ReDoc UI over that schema

`SERVE_PERMISSIONS` is `IsAdminUser` (`config/settings.py:49`), so the
schema and both docs UIs are staff-gated like everything else here.

### Pagination

Most router-registered list endpoints use `PageNumberPagination` with
`PAGE_SIZE = 50`. The append-only `audit-log` instead uses counted keyset
pagination ordered by timestamp and ID, so rows inserted at the
front cannot shift later pages or make tied timestamps unstable. All list
responses retain the `{"count", "next", "previous", "results"}` envelope.

For ordinary lists, callers may page with `?page=N`. For the audit log, treat
`next` as opaque and follow it unchanged; do not construct or modify the signed
`?cursor=...` value. Consumers that always follow the server-provided `next`
link work with either pagination strategy.

## (b) Removed webhook event bus

The webhook event bus described below was removed by #66; operators and agents
must not use it. Only this retirement summary remains. Its four escalation
signals now create internal staff notification email through the same
mailbox-pinned Gmail client used by requester correspondence. There
is no event subscription, endpoint, secret, signature, delivery, or retry
surface. The staff REST API, hosted and stdio MCP transports, and command-line
client remain available and retain their existing authorization boundaries.

## (c) MCP server

Hosted `/mcp` exposes exactly four attributed personal-token draft/proposal
tools; standalone stdio and the shared nonstaff MCP identity remain read-only.

`integrations/mcp/` is an MCP facade (`PRRClient`) over the staff API from
(a). The application hosts streamable HTTP at `/mcp`; the standalone
`uv run integrations/mcp/server.py` stdio entrypoint remains available. Both
transports import the registry in `integrations/mcp/tools.py` and expose eight
read tools:
`psd_prr_requests_list`, `psd_prr_request_get`,
`psd_prr_overdue_requests_list`, `psd_prr_collection_tasks_list`,
`psd_prr_record_systems_list`, `psd_prr_exemptions_list`,
`psd_prr_audit_log_search`, and `psd_prr_capabilities_list`.

Tool names follow the `psd_<system>_<resource>_<verb>` form that
psd-dev-standards `standards/07-mcp.md` build standard 3 requires, so they
stay unambiguous beside the district's other MCP servers. Every tool also
carries `readOnlyHint` and `destructiveHint` annotations (build standard 4);
`apps/api/tests/test_mcp_tool_contract.py` fails on a tool that is renamed
off the prefix, loses an annotation, or drifts from `server.json`.

`psd_prr_overdue_requests_list` takes `limit` (default 25, maximum 200) and
`response_format` (`concise` or `detailed`). `limit` caps the detail rows,
never the count: the envelope's `total` is how many requests are overdue and
is the number to report, while `count` is how many rows came back. Only
`scan_complete: false` makes `total` a floor rather than the real number, and
the envelope then carries a `warning` sentence saying so. Treat that case as
a sample, not a statutory total.

Hosted HTTP additionally exposes four tools after counsel and Records Officer
sign-off: `psd_prr_acknowledgment_draft`,
`psd_prr_clarification_letter_draft`,
`psd_prr_collection_task_notes_propose`, and
`psd_prr_exemption_citations_suggest`. They require an attributed personal
token.
The two drafting tools call only the existing acknowledgment- and
clarification-draft REST primitives; the other two read authoritative rows and
return `applied: false` proposals for a human to accept through normal staff
workflow. The standalone stdio deployment
registers none of these tools and remains safe for the shared read-only service
identity. See (d) for the exact gates.

Hosted clients connect to `https://<deployment-origin>/mcp` and send the
personal token created on `/agent-access` as `Authorization: Token <value>`.
Before any MCP handshake or tool execution, the mount passes that credential
through a safe staff REST read; missing, invalid, expired, revoked, or
unauthorized tokens receive `401` and no staff data. Tool calls then reuse the
same credential against their normal Django REST primitives. This adds no
authorization model: staff roles, Google-group revalidation, personal-token
`last_used_at` attribution, resource policy, and human-only handoffs remain in
Django. `/mcp` responses are noncacheable, and credential values are excluded
from application and MCP logs.

The top-level ASGI application owns `mcp.session_manager.run()` because a
mounted application's lifespan is not entered. Hosted mode is stateless, so
the four Gunicorn/Uvicorn workers need no session affinity. All other paths
delegate to Django's ASGI handler; the existing Caddy catch-all needs no
special `/mcp` route.

Stdio configuration remains `PRR_BASE_URL` (for example,
`https://prr.psd401.net`) plus `PRR_API_TOKEN`. Shared server-side automation
may use the token from
`python manage.py provision_mcp_readonly mcp-production`. See
[`integrations/mcp/README.md`](../integrations/mcp/README.md) for the
full tool and connection reference. Stdio exposes only the eight read tools,
including when configured with a personal token.

### Claude Code quick-start

Hosted streamable HTTP:

```bash
claude mcp add --transport http prr-records \
  https://prr.psd401.net/mcp \
  --header "Authorization: Token YOUR_PERSONAL_TOKEN"
```

Local or shared stdio:

```bash
claude mcp add --env PRR_BASE_URL=https://prr.psd401.net \
  --env PRR_API_TOKEN=YOUR_PERSONAL_TOKEN \
  prr-records -- uv run integrations/mcp/server.py
```

Then `claude mcp list` to confirm it connects. Hosted personal-token clients
see the eight read tools and four draft/proposal tools; stdio sees only the
eight reads. The guardrails in (d) remain binding on every result and handoff.

Static-token v1 is not MCP OAuth. Claude.ai and Claude Desktop's remote
Connector UI cannot accept an arbitrary personal-token header, so do not place
one in the URL; those web/mobile connectors remain out of scope until a
separate OAuth design. Local Claude Desktop stdio and hosted clients capable of
setting the header continue to work.

This is also the one place the MCP facade does not yet meet
psd-dev-standards `standards/07-mcp.md`. Build standard 2 requires the OAuth
2.1 resource-server pattern and forbids token passthrough, and hosted `/mcp`
still accepts a static attributed staff token and forwards it to the staff
REST API. `server.json` records the gap under
`_meta."net.psd401.registry/catalog".standard.notMet`, and a test asserts
build standard 2 is never claimed as met while that is true.

Closing it is not a repository-local change. The standard's selected design
is a Cognito-fronted gateway (`research/mcp-gateway/DECISION.md`), which is
Stage 1 of the Phase 5 MCP governance plan, and that plan is on hold; its
"What proceeds despite the hold" section lists the repo hygiene items
(`server.json`, tool annotations, mcp-scan in CI) and applies build standard
2 immediately only to *new* servers. Google Workspace cannot stand in as the
authorization server either: an MCP client needs RFC 8707 resource
indicators and dynamic client registration, which Google's OAuth service
does not offer, so there is no way to issue an audience-bound token for this
resource without either that gateway or making this application its own
authorization server. Issue #294 tracks the decision; AGENTS.md rule 11 is
deliberately unchanged until it ships, because the rule states the
credential model and must not describe one that does not exist.

## (c.1) Command-line client

`uv run integrations/cli/prr.py` is the scriptable read/write agent surface
over the same staff REST primitives. It accepts a named personal token through
`PRR_API_TOKEN`, requires HTTPS outside loopback, refuses redirects, renders
human-readable tables by default, and supports `--json` on every command.
Reads cover requests, collection tasks, record systems, exemptions, audit,
identity, and the live capability catalog. Attributed writes cover collection
task updates, time logging, authorized administrative transitions, and all
correspondence draft kinds.

The CLI has no human-only approval, queueing, denial, clarification-resolution,
or camera-exclusion command. If a reachable endpoint returns
`human_approval_required`, the shared HTTP layer prints its capability id,
action, required role, and staff-UI route and exits nonzero. Expired, revoked,
and invalid personal credentials remain distinguishable and point the user
back to `/agent-access`; no token is stored by the script.

See [`integrations/cli/README.md`](../integrations/cli/README.md) for
configuration, the full command reference, examples, exit behavior, and the
command-to-capability map.

## (d) AGENTIC GUARDRAILS

This is the load-bearing section. What the API and MCP surface let an
agent *do* is broader than what an agent is *allowed* to do. Some of the
gap is enforced in code (AST guards, missing write routes); the rest is a
line an agent, or the human directing one, has to hold on its own.

| Action | Verdict | AGENTS.md rule | Why |
|---|---|---|---|
| All reads: staff API GET routes, MCP `list_*`/`get_*`/`search_*` tools | MAY | n/a | Reading compliance state can't alter it; these shared read tools remain read-only on both MCP transports. |
| Submit a requester-directed public form | MAY | Rules 1, 5, 11 | Use the same-origin CSRF and signed-form flow, submit only contact/description data the requester supplied, and never evade honeypot or rate controls. The primitive creates a legal request; automation must not invent, duplicate, or probe requests. |
| Call a removed integration transport route or reuse a retired service token | MUST NEVER | Rules 1, 7, 11 | Request intake is public-only and correspondence transport is in process. The retired routes return 404, and migration 0016 revokes exact-scope legacy transport tokens. |
| Treat a DSN/bounce as authority to retry, replace a message, or reverse legal state | MUST NEVER | Rules 1, 2, 7 | A recognized RFC 3464 report is later append-only delivery evidence. It does not invalidate Gmail-acceptance evidence, change a delivery attempt, resolve a clarification, create a status event, or authorize an automatic resend. A Records Officer must separately review any replacement-address workflow. |
| Create or edit an authorized correspondence draft for human review | MAY | Rules 6, 7, 10 | The registered staff REST primitives create an audited draft without changing request state or reaching the requester. An estimate-revision proposal additionally requires an attributed current staff identity and substantive reason. Approval and queueing return an explicit handoff to token callers. |
| Autonomously associate an already-matched general inbound message with a clarification cycle | MUST NEVER | Rules 2, 10 | This is an evidentiary classification, not a transport match. Capability `inbound_correspondence.associate_clarification_response` is human-only/high-risk: a Records Officer selects the eligible open cycle and supplies a substantive reason in the staff session. Association records a response but never resolves the cycle or changes request status. |
| Decide, approve, queue, or trigger a requester-facing letter as an agent | MUST NEVER | Rule 6 | The outbound transport permission is not legal authority. Automated estimate-kicking is a Yousoufian penalty aggravator: courts weigh repeated non-substantive delay against the agency. The statutory day-5 safety net and non-statutory submission receipt are the two deliberate system exceptions; neither gives an agent authority, and the receipt can carry no statutory effect. |
| Apply an estimated completion-date revision | MUST NEVER | Rule 6 | An attributed agent may assist with the draft-only proposal above, but cannot approve, queue, send, or apply it. A Records Officer performs the interactive approval and queue handoffs, and the date changes only after successful provider evidence. No direct request action exists. The compliance AST guard forbids every background task, including the auto-ack safety net, from calling the estimate-revision draft path. |
| Deny a request | MUST NEVER | Rule 7 | Neighborhood Alliance's adequate-search standard requires a reasonable search before denial. An agent may prepare a draft only. A Records Officer completes the checklist, reviews and approves the immutable denial, and queues it in the staff session; Django applies denied status only after confirmed delivery. The generic transition route cannot create it. |
| Determine that no responsive records exist or that records are outside retention | MUST NEVER | Rules 2, 7, 10 | This is a human legal/search determination. Capability `correspondence.draft_no_records_closure` is human-only and requires complete collection tasks, detailed search evidence, counsel approval, and additional retention/obvious-lead checks when applicable. Only confirmed delivery applies `closed_no_records`; an agent cannot create the attestation, approve, queue, or transition it. |
| Notify a third party that records concerning them will be disclosed | MUST NEVER | Rules 6, 10 | RCW 42.56.540 notice starts a real clock for a named person to seek a court order, and it names the requester to somebody outside the case -- by name only; a requester who gave no name appears as "a member of the public", never by email address. Capability `correspondence.draft_third_party_notice` is human-only; the notice is addressed to the notified person and their optional attorney, never to the requester, and the service refuses a requester address in any recipient slot. |
| Close a case as fulfilled or as abandoned for non-payment | MUST NEVER | Rules 6, 10 | Both capabilities (`correspondence.draft_completion_closure`, `correspondence.draft_nonpayment_closure`) are human-only and create a draft only. FULFILLED and CLOSED_ABANDONED are applied by `services.transition()` after confirmed provider delivery, with the officer who approved the letter as the actor. A non-payment closure additionally records the amount, the date payment was requested, and the `fee_unpaid` reason code. |
| Approve and send correspondence to several cases at once | MUST NEVER | Rules 6, 10 | `correspondence.batch_approve_and_send` is human-only, capped at 50 cases, requires `confirmed: true` with the exact selection, and accepts only completion closures, non-payment closures, and general correspondence. Denials and no-records closures are refused outright. Each case gets one independent delivery and one audit row; the batch authorizes them together but does not merge them. The batch draft response returns each case's rendered subject and body, and the staff UI keeps the send locked until the officer has those on screen and confirms reading each one. A case that fails (refusal, validation, or a database error) is reported for that case and does not stop the others. |
| Apply an administrative terminal closure without Records Officer authority and a substantive reason | MUST NEVER | Rules 2, 10 | Fulfilled, withdrawn, and abandoned are legal terminal actions. The canonical lifecycle service requires a current Records Officer plus a case-specific reason; Application Admin authority alone is insufficient. |
| Exclude a camera from a footage preservation hold | MUST NEVER | Rule 7 | RCW 42.56.100's duty to preserve records responsive to a pending request. Narrowing a hold before human review risks destroying responsive footage. `triage.exclude_camera()` requires a human `actor` and a reason of 10+ characters, raising `ValueError` otherwise. |
| Assemble an installment and change what it contains | MAY | Rules 2, 4, 10 | `installments.create` and `installments.set_items` take explicit record ids, refuse anything that is not release-ready, refuse a record a live installment already holds, and are frozen the moment a package is prepared. Nothing reaches the requester: an installment that is never released is a filing decision, not a disclosure. |
| Release a package to the requester, or send its notice | MUST NEVER | Rules 6, 7, 10 | Releasing is the disclosure. `installments.release_and_send` is human-only and hands a token caller an explicit handoff; a Records Officer presses it in the staff session and is recorded as having done so. The notice is built by the server from the installment, so there is no URL for an agent to supply and none for a human to mistype. |
| Revoke a requester's download | MUST NEVER | Rules 2, 4, 6 | Taking disclosed records back from a requester is a legal decision with a cost. `installments.revoke` is human-only, demands a substantive reason, and is audited. The released artifact and its manifest are never destroyed; only the link stops working, and the requester is told whom to contact. |
| Edit `PublicRecordsRequest.description` | MUST NEVER | Rule 9 | `description` is the legal record of what was originally asked; every serializer marks it read-only after creation. Clarifications are new `StatusEvent` rows, never edits to the original text. |
| Write to `AuditLog` or `StatusEvent` directly, bypassing `services.transition()` / `audit()` | MUST NEVER | Rules 2, 4 | Both are append-only legal records. `AuditLogViewSet` is read-only at the API layer and no update/delete code path exists anywhere in the codebase. A direct write also skips the tolling/deadline recomputation that `transition()` exists to guarantee, corrupting the compliance record. |

### Hosted MCP draft/proposal tools

Following counsel confirmation that the human gates remain real and Records
Officer confirmation that the workflow matches office practice, hosted `/mcp`
exposes these four personal-token tools. The shared stdio deployment remains
read-only, and the dedicated read-only identity is rejected by every tool in
this table.

| Tool | What it does | Human gate |
|---|---|---|
| `psd_prr_acknowledgment_draft` | Calls the existing attributed `POST /api/staff/correspondence/drafts/acknowledgment/` primitive and stores an editable acknowledgment draft rendered from the versioned statutory template. It does not change status, set `acknowledgment_sent_at`, revise a published estimate, or reach the requester, so drafting it neither satisfies nor defers the RCW 42.56.520 five-business-day response. There is no batch form: one call drafts one case. | A Records Officer reviews the stored draft and completes approval and queueing through the interactive staff session; `acknowledged` is applied only by the correspondence service after confirmed delivery. |
| `psd_prr_clarification_letter_draft` | Calls the existing attributed `POST /api/staff/correspondence/drafts/clarification/` primitive and stores an editable clarification-request draft. It does not change status or reach the requester. | A Records Officer reviews the stored draft and completes approval and queueing through the interactive staff session; token execution is rejected by those endpoints. |
| `psd_prr_collection_task_notes_propose` | Reads the current `CollectionTask` and returns current/proposed `instructions` plus `applied: false`. It does not PATCH the task. | The assigned custodian or officer reviews and accepts the proposed note via the normal `PATCH /api/staff/collection-tasks/<id>/`. |
| `psd_prr_exemption_citations_suggest` | Reads the `RecordItem`, validates the proposed citation IDs against active `ExemptionCitation` rows, and returns `applied: false`. It does not change review state or attach evidence. | A reviewer confirms each citation before attaching it to a redaction or withholding. Rule 3 requires every redaction to carry a real exemption FK, and a wrong citation is itself a defect in the RCW 42.56.210(3) exemption log. |

There is no MCP approval, queue, send, estimate application, request
transition, citation attachment, clarification resolution, denial, or camera
exclusion tool. Adding any such outcome requires a separately reviewed REST
authority and cannot weaken rules 6 or 7.
