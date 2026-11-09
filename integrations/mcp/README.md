# PRR Staff API MCP Server

The application serves [MCP](https://modelcontextprotocol.io) over streamable
HTTP at `/mcp`, and retains the standalone stdio entrypoint for local and
server-side automation. Both transports expose the eight shared read tools.
Hosted HTTP additionally exposes five attributed, personal-token tools that
create an acknowledgment or clarification draft, return reviewable
proposals, or propose a decision batch (#43) that only a person approves in
the staff Decisions queue. Hosted also adds four role-gated reads -- one
request's records, their clusters, a record's AI advisory labels, and decision
batches -- that need a reviewer or Records Officer personal token. The standalone
stdio deployment remains strictly read-only.

## Read-only stdio and gated hosted writes

The shared stdio deployment exposes no write tool even when configured with a
personal token. Its separately provisioned nonstaff identity has only
`records.read_staff_api`, and the registry omits every write method.

After counsel and Records Officer review, hosted HTTP exposes exactly:

- `psd_prr_acknowledgment_draft`, which calls the existing attributed staff REST
  primitive and creates only an editable acknowledgment draft from the
  versioned statutory template;
- `psd_prr_clarification_letter_draft`, which calls the existing attributed staff REST
  primitive and creates only an editable clarification-request draft;
- `psd_prr_collection_task_notes_propose`, which reads the task and returns current
  instructions, proposed instructions, and the normal human PATCH handoff
  without changing the task; and
- `psd_prr_exemption_citations_suggest`, which reads one record item and validates
  active candidate citations, then returns a proposal without attaching any
  citation or changing review state.

All four reject the shared read-only MCP identity before their REST work and
require an active `prr_pat_…` personal token. There is no MCP approval,
queueing, sending, estimate application, citation attachment, request
transition, clarification resolution, denial, or camera-exclusion tool. The
existing Django permissions and human-only handoffs remain authoritative.

## Files

| File | Purpose |
|---|---|
| `client.py` | `PRRClient`, the transport-independent REST contract: shared reads, the acknowledgment- and clarification-draft POSTs, and two read-backed proposal methods. |
| `tools.py` | Shared read registry plus the explicit hosted-only write-tool allowlist. Owns the namespaced tool names, their annotations, and `TOOL_CLIENT_METHODS` — the map that *is* the dispatch, so a tool can only reach its own REST method. |
| `evals.py` | The eval cases, as data: one per key tool, stating what a response must and must not carry. |
| `mcp-scan.json` | Scanner config naming the stdio entrypoint with placeholder credentials. |
| `hosted.py` | Per-request token pass-through, fail-closed auth preflight, and the in-process adapter through Django's real URL/middleware/auth stack. |
| `server.py` | Thin read-only stdio entrypoint over `tools.py`, runnable with `uv run`. |
| `config/asgi.py` | Top-level Starlette composition and lifespan: `/mcp` reaches FastMCP and every other route reaches Django. |
| `server.json` (repo root) | Registry manifest and district catalog metadata. |

## Hosted endpoint

The built application exposes:

```text
https://<deployment-origin>/mcp
```

For a local hosted-transport test, run the ASGI entrypoint rather than Django's
WSGI development server:

```bash
uvicorn config.asgi:application --host 127.0.0.1 --port 8000
```

Send a personal credential exactly as the staff REST API expects it:

```http
Authorization: Token prr_pat_…
```

The MCP middleware forwards that header to a safe Django staff-API read before
the handshake. Missing, malformed, expired, revoked, unauthorized, or invalid
credentials receive `401`; no tool is listed or executed. Each tool request
then uses the same header against its Django REST primitive, so role policy,
Google-group revalidation, human-only handoffs, and personal-token
`last_used_at` attribution stay authoritative in Django. Credentials and
response bodies are never written to MCP logs, and all `/mcp` responses use
`Cache-Control: no-store`.

The read-only service token may still use hosted HTTP for the eight read tools,
but every hosted write-tool invocation returns an MCP error before it can
create a draft or read proposal targets. Use a personal token for the five
hosted write tools.

The hosted server is deliberately stateless so the four Gunicorn/Uvicorn
workers do not require session affinity. `config/asgi.py` owns
`mcp.session_manager.run()` in the top-level lifespan; mounted application
lifespans do not run. Caddy's existing catch-all proxy already carries `/mcp`,
so no separate public port or proxy credential exists.

### Claude Code over hosted HTTP

```bash
claude mcp add --transport http prr-staff \
  https://prr.psd401.net/mcp \
  --header "Authorization: Token YOUR_PERSONAL_TOKEN"
```

The header value is stored in the client configuration. Use your own named,
expiring personal token, never a token shared between people. Your own
scheduled agent may use it too; every call is attributed to you.
Run `claude mcp list` and `claude mcp get prr-staff` to verify the connection.

### Claude Desktop and Claude.ai remote connectors

Static-token v1 is not OAuth. Claude.ai and Claude Desktop's hosted
**Settings → Connectors** flow supports authless or OAuth remote servers, not a
user-entered arbitrary `Authorization: Token …` header. Do not put a personal
token in the URL. Hosted `/mcp` works with clients such as Claude Code that can
set the header; web/mobile remote Connectors remain unavailable until a
separately designed MCP OAuth flow exists.

## Standalone stdio

`server.py` retains PEP 723 metadata for `mcp` and `requests`, so `uv` resolves
an isolated environment without changing the application venv:

```bash
PRR_BASE_URL=http://localhost:8000 \
PRR_API_TOKEN=<your-token> \
uv run integrations/mcp/server.py
```

Run with either variable missing and the server exits with a clear stderr
message. Use an HTTPS origin outside loopback. This entrypoint registers only
the eight read tools; it cannot expose the hosted write-tool allowlist.

For Claude Code:

```bash
claude mcp add --transport stdio \
  --env PRR_BASE_URL=http://localhost:8000 \
  --env PRR_API_TOKEN=YOUR_PERSONAL_TOKEN \
  prr-staff \
  -- uv run /absolute/path/to/integrations/mcp/server.py
```

For local Claude Desktop, add an absolute path in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "prr-staff": {
      "command": "uv",
      "args": ["run", "/absolute/path/to/integrations/mcp/server.py"],
      "env": {
        "PRR_BASE_URL": "http://localhost:8000",
        "PRR_API_TOKEN": "YOUR_PERSONAL_TOKEN"
      }
    }
  }
}
```

Save the file and fully quit and restart Claude Desktop.

## Choosing a token

For one staff member's client, open `/agent-access`, create a named personal
token, and save its one-time value. Django attributes every call to that user,
rechecks current roles, enforces expiry/revocation, and preserves all
human-only handoffs. A personal token is required for every hosted write tool.

Shared server-side stdio automation may instead use the dedicated nonstaff
read-only identity:

```bash
python manage.py provision_mcp_readonly mcp-production
python manage.py provision_mcp_readonly mcp-production --rotate
```

It has an unusable password and only `records.read_staff_api`. Never reuse a
personal, correspondence, or other service credential for shared automation.

## Shared read-tool catalog

Both transports expose these `GET`-only tools:

Every tool is annotated `readOnlyHint: true`, `destructiveHint: false`,
`idempotentHint: true`, `openWorldHint: false`.

| Tool | Arguments | Endpoint | Use it for |
|---|---|---|---|
| `psd_prr_requests_list` | `status`, `search`, `page`, `ordering` (optional) | `GET /api/staff/requests/` | Browse or filter the request queue. |
| `psd_prr_request_get` | `number` | `GET /api/staff/requests/<number>/` | Request detail, timeline, estimates, tasks, and preservation. |
| `psd_prr_overdue_requests_list` | `limit`, `response_format` (optional) | Paginated `GET /api/staff/requests/` | Find open requests past the statutory response deadline. |
| `psd_prr_collection_tasks_list` | `request_number` (optional) | `GET /api/staff/collection-tasks/` | Inspect search assignments and completion. |
| `psd_prr_record_systems_list` | none | `GET /api/staff/record-systems/` | Look up custodians, retention, and triage. |
| `psd_prr_exemptions_list` | none | `GET /api/staff/exemptions/` | Look up RCW 42.56 and FERPA citations. |
| `psd_prr_audit_log_search` | `target` (optional) | `GET /api/staff/audit-log/` | See who did what and when. |
| `psd_prr_capabilities_list` | none | `GET /api/staff/capabilities/` | Discover REST outcomes, risk, and execution policy. |

### Bounded responses

`psd_prr_overdue_requests_list` is the one read with no server-side filter
behind it: the staff requests resource cannot filter on
`statutory_response_due`, so the tool walks pages and filters client-side.
**`limit` caps the rows, never the count.** That separation is the whole
design: the number of overdue requests is a statutory fact, and a tool that
let a page size stand in for it would be the single most consequential bug
this surface could have. The scan counts every overdue request it finds and
shapes only the first `limit` of them.

| Field | Meaning |
|---|---|
| `count` | Detail rows returned. Capped by `limit`. |
| `total` | **How many requests are overdue.** Not capped by `limit`. |
| `limit` | Rows requested. Default 25, maximum 200. |
| `truncated` | `total > count` — more overdue requests exist than rows returned. |
| `scan_complete` | False when the 40-page (2,000-request) ceiling stopped the scan before the end of the queue. |
| `pages_scanned` | How far the scan read. The evidence behind an incomplete scan. |
| `as_of` | The district Pacific date the comparison used. |
| `note` | Present when `truncated`: says in words that `total` is the number overdue. |
| `warning` | Present when the scan is incomplete: says in words that `total` is a floor. |

Report `total`, not `count`. The one case where `total` is not the real
number is `scan_complete: false` — then it is a floor and the staff UI's own
overdue view is authoritative. At 50 rows per page the ceiling is 2,000
requests, well past any plausible district queue, so in practice `total` is
the real number.

`note` and `warning` exist because a model asked "what's overdue?" will
paraphrase this envelope into prose for an officer, and a boolean is the
first thing a paraphrase drops. A sentence carrying the caveat survives
being retold.

`response_format` is `concise` (the default) or `detailed`. The concise
projection always carries `number`, `subject`, `status`, `matter_kind`,
`statutory_response_due`, `next_due`, `next_due_basis`,
`assigned_officer_username`, `requester_name`, `requester_email`, and a
`days_overdue` computed against the district's Pacific date — always, as
`null` if the staff API stops sending one, so a serializer rename shows up as
an empty column rather than a vanished one.

**Concise carries the subject and requester contact by the data owner's
decision.** A tool result goes to the caller's **model provider**, and
`subject` can be the opening of the requester's own wording (when
`subject_source` is `description`), which can name a student. Hagel (CIO,
data owner) signed off on 2026-10-19 that MCP tool results may carry
`subject`, `requester_name` and `requester_email`; the sign-off is recorded
in [`docs/phase-2-production-handoffs.md`](../../docs/phase-2-production-handoffs.md).
The staff serializer still returns `null` requester contact to callers who
are not Records Officers, and that applies to both formats.

Use `detailed` when you need the full staff request-summary row, such as
`subject_source` or the latest case note.

## Hosted personal-token tool catalog

These tools exist only on hosted `/mcp` and reject the shared read-only
identity:

All four are annotated `readOnlyHint: false`, `destructiveHint: false`,
`openWorldHint: false`. Two of them only read and return a proposal, so
`readOnlyHint: false` is the conservative answer rather than the literal one:
these are the five tools a gateway must route through human approval, and
the honest-but-weaker hint would let a future gateway auto-approve them.
Only the two drafting tools are non-idempotent, because each call stores
another draft.

| Tool | Arguments | Existing REST primitive | Result and human gate |
|---|---|---|---|
| `psd_prr_acknowledgment_draft` | `request_number`, optional `estimated_completion` (ISO date) | `POST /api/staff/correspondence/drafts/acknowledgment/` | Stores a draft only. Omit `estimated_completion` to use the request's own estimate; a date that disagrees with a published estimate is rejected rather than applied. `acknowledged` follows human approval and confirmed delivery, so drafting neither satisfies nor defers the five-business-day response. One call per case — there is no batch form. The response confirms the draft (status, nothing approved/queued/sent, the stated estimate) and withholds the letter text, because the acknowledgment quotes `request.description` in full and a tool result leaves for the caller's model provider; the officer reads the letter in the case Messages tab. |
| `psd_prr_clarification_letter_draft` | `request_number`, `clarification_questions`, `clarification_due` | `POST /api/staff/correspondence/drafts/clarification/` | Stores a draft only. A Records Officer must approve and queue it in an interactive staff session. |
| `psd_prr_collection_task_notes_propose` | `task_id`, `proposed_instructions` | `GET /api/staff/collection-tasks/<id>/` | Returns `applied: false`, current/proposed text, and the normal human PATCH handoff. It never PATCHes. |
| `psd_prr_exemption_citations_suggest` | `record_item_id`, `candidate_citation_ids`, `rationale` | `GET /api/staff/record-items/<id>/` and `GET /api/staff/exemptions/<id>/` | Returns `applied: false` with active candidate rows. It never attaches a citation, redaction, or withholding. |

## Registry manifest, data touched, and owner

[`server.json`](../../server.json) at the repository root is the registry
manifest psd-dev-standards `standards/07-mcp.md` build standard 6 requires.
It validates against the official
`static.modelcontextprotocol.io/schemas/2025-12-11` schema, and the district
catalog fields the internal MCP directory reads live under
`_meta."net.psd401.registry/catalog"`:

| Field | Value |
|---|---|
| Owner | `krishagel` |
| Risk tier | **MCP-3**, because the staff API can return FERPA-governed student education records. The tool surface reads like Tier 1; the data does not. Tier 3 carries gateway-only access, per-user auth, and a full audit log. |
| Data touched | Request metadata and staff-visible timelines; requester contact details; student education records reachable through record items; collection tasks, custodians and the system registry; exemption citations; the append-only audit log. |
| Public registry | Not published. The repository is private and the server reads requester identity and student records. |

The manifest also records, under `standard.notMet`, the one build standard
this server does not meet: **build standard 2, OAuth 2.1 with no token
passthrough.** Hosted `/mcp` still accepts a static attributed staff token
and forwards it to the staff REST API. See
[`docs/agentic-integration.md`](../../docs/agentic-integration.md) §(c) for
why closing that is not a repository-local change, and issue #294 for the
decision. `apps/api/tests/test_mcp_tool_contract.py` asserts build standard
2 is never listed as met while that remains true.

## Protocol version and the 2026-07-28 stateless spec

The pinned floor is `mcp>=2.3.0,<3` (in `pyproject.toml` and in this
package's `server.py` PEP 723 block, both asserted by
`apps/api/tests/test_hosted_mcp.py`). The 2.x SDK renamed `FastMCP` to
`MCPServer` (`mcp.server.mcpserver`), moved the transport options
(`json_response`, `stateless_http`, `transport_security`) from the
constructor to `streamable_http_app()` (see `hosted.create_hosted_http_app`),
and reads model fields in snake_case (`read_only_hint`, `is_error`) while the
wire stays camelCase. Its latest protocol revision is **2026-07-28**; it
still negotiates **2025-11-25**, the minimum `standards/07-mcp.md` build
standard 1 names, with older clients.

Build standard 1 also asks for a migration plan to the **2026-07-28**
revision, which removes sessions and the `initialize` handshake entirely and
adds `Mcp-Method`/`Mcp-Name` request headers. Most of that migration is
already paid for here:

- The hosted server is created with `stateless_http=True` and
  `json_response=True`, so there is no session to remove and no session
  affinity across the four Uvicorn workers.
- No tool returns a server-side handle; all state lives in Django and is
  addressed by request number or row id, which is what "state = explicit
  handles returned by tools" asks for.
- Neither transport uses HTTP+SSE, Roots, Sampling, or the deprecated
  Logging capability.

What remains is upstream, not local: the Python SDK has to implement the
2026-07-28 revision, and `StaffTokenAuthenticationMiddleware` will need to
tolerate a request that arrives with no `initialize` in front of it (today
it pre-validates the credential before the handshake, which stays correct
either way — it does not depend on the handshake existing). When the SDK
ships support, raise the floor, re-run the eval suite below, and confirm the
negotiated version in the suite's log output. Do not pre-emptively relax the
pre-handshake credential check: fail-closed auth is compliance rule 11, and
the stateless revision does not change it.

## Testing

The plain client runs against a live Django test server, and the hosted
transport uses the SDK's streamable-HTTP client against the composed ASGI app:

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py test \
  apps.api.tests.test_mcp_client \
  apps.api.tests.test_hosted_mcp \
  apps.api.tests.test_mcp_write_tools \
  apps.api.tests.test_mcp_tool_contract \
  apps.api.tests.test_mcp_evals \
  --settings=config.test_settings
```

### Eval suite

`integrations/mcp/evals.py` holds the eval cases build standard 6 asks for,
and `apps/api/tests/test_mcp_evals.py` runs every one of them through a real
streamable-HTTP MCP session with an attributed personal token. The cases are
data rather than test methods so the same list can be reviewed in one place
and, once a gateway exists, replayed against a deployed endpoint.

Each case states the keys an agent is told to read, the keys a tool must
withhold (any field the data owner has not signed off on sending to a model
provider), exact values at dotted paths (`status: draft`, `applied: false`), and — for
the deliberate-failure cases — the vocabulary a bad argument's error has to
teach. Key checks walk the parsed JSON recursively rather than matching
substrings in rendered text, because a field name that merely occurs inside
a letter body is not a field.

Add a case whenever you add a tool: the suite asserts that every read tool
has at least one.

### Tool contract and the definition pin

`apps/api/tests/test_mcp_tool_contract.py` enforces what the standard makes
checkable without a network: namespaced lower_snake_case names, all four annotations plus a title on every tool,
`server.json`'s declared inventory matching the served inventory, and every
`mcp_tool` the capability catalog cites being a tool that exists.

It also pins a SHA-256 over the canonical tool definitions — name, title,
description, and all four annotations. A tool description is a prompt, so an
edit that rewrites one changes what every connected agent is instructed to
do, and nothing else in CI would notice (the standard calls this the
rug-pull defense). When you change wording deliberately, the failure message
prints the new digest; update `TOOL_DEFINITION_DIGEST` in the same commit so
the review sees the wording diff beside it.

### Snyk Agent Scan (mcp-scan)

`integrations/mcp/mcp-scan.json` points the scanner at the stdio entrypoint
with placeholder credentials — the scanner only needs the server to start and
answer `tools/list`, and it must never hold a real staff token.

The token-free half runs on every pull request, inside `psd-ci / Backend
audits and quarantined tests`, and gates the merge:

```bash
uv tool run "snyk-agent-scan==0.6.8" inspect integrations/mcp/mcp-scan.json \
  --ci --no-skills --dangerously-run-mcp-servers \
  --suppress-mcpserver-io true --server-timeout 120
```

The analysis half (prompt-injection and toxic-flow checks on tool
descriptions) needs a `SNYK_TOKEN`: the tool was renamed from `mcp-scan` to
`snyk-agent-scan` and has required an account since v0.6. It lives in
`.github/workflows/mcp-scan.yml`, which reports a visible skip rather than a
pass until the secret is provisioned — tracked in
[`docs/phase-2-production-handoffs.md`](../../docs/phase-2-production-handoffs.md).
Swap `inspect` for `scan` in the command above to run it locally with your
own token.

Coverage includes all registered tools, missing/invalid/expired/revoked auth,
per-user attribution, token-free logs, stdio read-only enforcement, shared
identity rejection, draft-only state, proposal non-mutation, approval
handoffs, top-level lifespan, API/SPA/static routing, security and identity
middleware, and synchronous bus-video preservation email under ASGI.
