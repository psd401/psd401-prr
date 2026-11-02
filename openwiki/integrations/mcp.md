---
type: Integration
title: MCP Server
description: MCP access to the PSD401 PRR staff API for LLM clients. Standalone stdio server exposes eight read-only tools; the hosted /mcp endpoint adds four attributed draft and proposal tools that never approve, send, or change legal state.
tags: [integration, mcp, agent, llm, read-only]
timestamp: 2026-10-07T22:37:58-07:00
---

# MCP Server

MCP access to the PSD401 PRR staff API for LLM clients such as Claude Code and
Claude Desktop. There are two transports, and they differ in what they can do:

- **Standalone stdio** (`integrations/mcp/server.py`): read-only, for local and
  server-side automation under a nonstaff identity.
- **Hosted streamable HTTP** at `/mcp` (`integrations/mcp/hosted.py`, mounted in
  `config/asgi.py`): the same reads, plus four personal-token tools that create
  drafts or return proposals. It rejects the shared read-only identity.

Both transports are facades over the staff REST API. No MCP tool holds a rule
of its own. The capability contract that maps these tools to REST primitives is
in [agent-parity.md](../architecture/agent-parity.md).

## Architecture

```
┌─────────────────┐    stdio     ┌──────────────────┐    HTTP GET    ┌─────────────────┐
│ Claude Code     │ ──────────▶ │ server.py        │ ──────────────▶│ Django API      │
│ (MCP client)    │              │ (FastMCP)        │                │ /api/staff/*    │
└─────────────────┘              └──────────────────┘                └─────────────────┘
                                       │
                                       ▼
                                 ┌──────────────────┐
                                 │ client.py        │
                                 │ (PRRClient)      │
                                 │ No MCP import    │
                                 └──────────────────┘
```

### Separation of Concerns

- **`client.py`** — Pure HTTP client, no MCP dependency, loads in Django venv
- **`tools.py`** — FastMCP tool registry shared by both transports; `create_mcp_server()`
  registers the write tools only when `include_write_tools=True` (hosted)
- **`server.py`** — standalone stdio entrypoint, runs via `uv run` standalone
- **`hosted.py`** — `DjangoPRRClient` and `create_hosted_mcp()`, with the
  `StaffTokenAuthenticationMiddleware` that authenticates the `/mcp` route

This separation allows `client.py` to be used outside the MCP context (e.g.,
in Django tests) while `server.py` can run independently with its own
dependency tree via PEP 723 inline metadata.

Source: [`integrations/mcp/`](../../integrations/mcp/)

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `PRR_BASE_URL` | Yes | Django app base URL (e.g., `https://prr.psd401.net`) |
| `PRR_API_TOKEN` | Yes | Read-only MCP service token |

Provision a dedicated nonstaff identity with
`manage.py provision_mcp_readonly <identity-name>`. Never reuse a staff credential.

## Running the Server

```bash
# Run from repository root
uv run integrations/mcp/server.py
```

The `server.py` file includes PEP 723 inline metadata declaring its
dependencies, so `uv run` handles installation automatically without
touching the Django venv.

## Tool Catalog

The **shared read tools** are registered on both transports. The standalone
stdio server registers no other tools. Wire names are namespaced: every tool name
starts with `TOOL_NAMESPACE` (`psd_prr_`) and is defined once as a constant in
`integrations/mcp/tools.py`. The `PRRClient` method behind each name is a separate
vocabulary, mapped in `TOOL_CLIENT_METHODS`. The server is capped at `MAX_TOOLS` (15) in
`tools.py`. The contract gate pins one SHA-256 digest over all twelve tool definitions, so a
change to a name, title, description, or annotation fails until the pin is regenerated on purpose.

| Wire tool name | Client method | Endpoint | Purpose |
|------|------|----------|---------|
| `psd_prr_requests_list` | `list_requests` | `GET /api/staff/requests/` | Browse and filter the request queue |
| `psd_prr_request_get` | `get_request` | `GET /api/staff/requests/<number>/` | Request detail and status timeline |
| `psd_prr_overdue_requests_list` | `list_overdue_requests` | Client-side filter over the request list | Requests past their statutory deadline |
| `psd_prr_collection_tasks_list` | `list_collection_tasks` | `GET /api/staff/collection-tasks/` | Search assignments by system |
| `psd_prr_record_systems_list` | `list_record_systems` | `GET /api/staff/record-systems/` | System registry |
| `psd_prr_exemptions_list` | `list_exemptions` | `GET /api/staff/exemptions/` | RCW 42.56 and FERPA exemption taxonomy |
| `psd_prr_audit_log_search` | `search_audit_log` | `GET /api/staff/audit-log/` | Append-only audit log |
| `psd_prr_capabilities_list` | `list_capabilities` | `GET /api/staff/capabilities/` | GUI/agent parity contract and policy |

The **hosted-only tools** require an attributed personal token (`prr_pat_…`).
The shared nonstaff identity is rejected before any REST call. Each one creates or
returns something for a human to act on, and none executes a legal action.

| Wire tool name | Client method | Behavior | Human handoff |
|------|------|----------|---------------|
| `psd_prr_acknowledgment_draft` | `draft_acknowledgment` | Creates one RCW 42.56.520 acknowledgment draft from the district's versioned statutory template; takes an optional ISO `estimated_completion` and rejects a date that disagrees with a published estimate. There is no batch form. The response confirms the draft but omits the letter text, because the letter quotes the full request description, which can name a student | A human reads and approves the letter in the case Messages tab. Drafting does not satisfy or defer the five-business-day response |
| `psd_prr_clarification_letter_draft` | `draft_clarification_letter` | Calls the staff clarification draft primitive; creates an editable draft | A Records Officer approves, queues, and sends it in the staff UI |
| `psd_prr_collection_task_notes_propose` | `update_collection_task_notes` | Reads the task and returns current and proposed instructions | The normal PATCH is performed by a human |
| `psd_prr_exemption_citations_suggest` | `suggest_exemption_citations` | Validates active candidate citations for one record item and returns a proposal | A reviewer confirms; nothing is attached or withheld |

### Tool Parameters

Each tool accepts parameters matching the staff API. Arguments are passed by name
to the wire tool:

```python
psd_prr_requests_list(status="review", search="transportation")
psd_prr_request_get(number="PRR-2026-0012")
psd_prr_audit_log_search(target="PRR-2026-0012")
psd_prr_clarification_letter_draft(request_number="PRR-2026-0012", clarification_questions="...", clarification_due="2026-09-30")  # hosted only
```

## Read-Only Enforcement

The standalone stdio server is **read-only by construction**. It registers only
the eight shared read tools, and its dedicated nonstaff credential carries only
`records.read_staff_api`. Hosted `/mcp` adds the four gated tools above. The
draft tools POST to the staff draft endpoints, and the proposal tools only read and
return a proposal. The attributed personal token is required for all four. No hosted tool
approves, queues, sends, applies an estimate, attaches a citation, resolves a
clarification, denies a request, or excludes a camera.

### Transport Security

The client enforces secure transport:

- **HTTPS required** for remote base URLs (rejects `http://` for non-localhost)
- **HTTP allowed only for loopback** development (`http://127.0.0.1:8000`)
- **No authentication redirect following** — `allow_redirects=False` on all requests

These rules prevent credential-leaking redirects and ensure tokens are never
sent over plaintext to remote hosts. Transport validation occurs before the
authenticated session is created.

Source: [`integrations/mcp/client.py`](../../integrations/mcp/client.py)

This aligns with architecture rules 6 and 7:

> 6. Estimate revisions require a human and a substantive reason
> 7. Footage: auto-hold always, auto-deny never

Agents must not be able to perform statutory actions programmatically.

## Authentication

- Uses DRF TokenAuthentication with `Authorization: Token <token>` header
- Standalone stdio: the token belongs to a nonstaff, unusable-password service
  identity (`manage.py provision_mcp_readonly`), which has only
  `records.read_staff_api` and cannot mutate staff resources
- Hosted `/mcp`: the token must be an active attributed personal token
  (`prr_pat_…`, issued from the staff application). Its actions are attributed
  to that person in the audit trail. The shared read-only identity is refused.
- The personal-token format and lifecycle are described in
  [agent-parity.md](../architecture/agent-parity.md#tokens-and-identities)

## Testing

Client and tool tests run against a Django test server:

- [`apps/api/tests/test_mcp_client.py`](../../apps/api/tests/test_mcp_client.py): shared read tools and transport validation
- [`apps/api/tests/test_hosted_mcp.py`](../../apps/api/tests/test_hosted_mcp.py): hosted `/mcp` route, token authentication, and the gated tools
- [`apps/api/tests/test_mcp_write_tools.py`](../../apps/api/tests/test_mcp_write_tools.py): the four hosted draft and proposal tools, the shared read-only identity rejection, and the absence of approval or legal-effect tools
- [`apps/api/tests/test_mcp_client_security.py`](../../apps/api/tests/test_mcp_client_security.py): plaintext-URL refusal before a session is created, loopback-only HTTP, and no redirect following
- [`apps/api/tests/test_mcp_tool_contract.py`](../../apps/api/tests/test_mcp_tool_contract.py): a static gate on the tool list itself. It checks the tool-name namespace, the tool ceiling, that read and write registries do not overlap, that every tool maps to a real `PRRClient` method, the read-only and destructive hints, and the pinned tool-definition digest and `server.json` inventory (`server.json` at the repository root). A change to a tool name, annotation, or description fails here until the manifest and pin are updated.
- [`apps/api/tests/test_mcp_evals.py`](../../apps/api/tests/test_mcp_evals.py): runs every case in `integrations/mcp/evals.py` over a real hosted MCP session with an attributed personal token, rather than calling `PRRClient` directly. A case fails when the wire contract breaks, for example a renamed tool, a dropped annotation, a changed argument name, or a serializer that stops returning a field an agent is told to read. The suite also requires that every read tool has an eval case.

Focused command:
`PYTHON_DOTENV_DISABLED=1 python manage.py test apps.api.tests.test_mcp_client apps.api.tests.test_hosted_mcp apps.api.tests.test_mcp_write_tools apps.api.tests.test_mcp_tool_contract --settings=config.test_settings`

The eval suite exercises the mounted transport, so run `apps.api.tests.test_mcp_evals`
when a change touches `integrations/mcp/evals.py`, a serializer behind a read tool,
or the hosted route in `config/asgi.py`. A new or renamed tool also needs the
`server.json` inventory and the tool-definition pin updated together. Hand-editing
only one of them fails the contract gate. A wire-name change also touches the
`mcp_tool=` field on the matching capability in `apps/api/capabilities.py`, the
name constant in `tools.py`, and the eval case in `integrations/mcp/evals.py`.
Keep these in step with the [agent parity](../architecture/agent-parity.md) catalog
that `list_capabilities` returns.

Two CI lanes also read the MCP surface. `psd-ci-jobs.yml` enumerates the tools through
Snyk Agent Scan without a token, and the separate `mcp-scan.yml` workflow runs the
authenticated analysis for prompt-injection and toxic-flow findings. That workflow runs
on pushes to `main` and weekly, not on pull requests, and it is skipped until the
analysis secret is provisioned. [Testing and quality gates](../operations/testing.md#ci-workflows)
has the workflow table.

Tests verify:

- Client connects and authenticates
- Each tool returns expected data structure
- Error handling for non-existent resources

## Related Concepts

- [../domain/requests.md](../domain/requests.md) — Request lifecycle and data model
- [../domain/correspondence.md](../domain/correspondence.md) — Correspondence domain, which hosts the draft tools' outputs
- [cli.md](cli.md) — Terminal client that exposes the same staff capabilities with the same human handoffs
- [../architecture/overview.md](../architecture/overview.md) — API architecture
re/overview.md) — API architecture
