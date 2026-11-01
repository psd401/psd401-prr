---
type: Architecture
title: GUI and Agent Parity Contract
description: The machine-readable capability catalog that maps every staff GUI operation to an agent-accessible REST primitive, classifies each by execution policy (anonymous, direct, human confirmation, human only), and drives the human-only handoff responses for token callers. Read before adding a staff action, an agent tool, or any path an API token can reach.
tags: [architecture, agent, parity, capabilities, human-in-the-loop, authorization]
timestamp: 2026-09-24T00:00:00Z
openwiki:
  roles: [architecture, integration, workflow]
  change_kinds: [capability, agent-parity, human-handoff, public-api]
  source_paths: [apps/api/capabilities.py, apps/api/handoffs.py, apps/api/access_tokens.py, apps/api/authentication.py, apps/api/resources/preservations.py, integrations/mcp/tools.py]
  symbols: [CAPABILITIES, _capability, AGENT_CONSTRAINT_DEFINITIONS, capability_catalog, AgentCapabilityCatalogView, human_only_handoff]
  test_paths: [apps/api/tests/test_agent_capabilities.py, apps/api/tests/test_agent_auth.py, apps/api/tests/test_mcp_client.py, apps/api/tests/test_hosted_mcp.py]
  invariants:
    - "Every production GUI API operation has exactly one capability row, and every row has a policy and transport metadata."
    - "A token caller receives a human_approval_required handoff, not an effect, for a human_only capability."
    - "Estimate revisions, denials, camera exclusions, and other statutory actions are never direct agent actions."
    - "The catalog is served at a staff-only endpoint and is a fresh copy on every call."
  validation_commands: ["PYTHON_DOTENV_DISABLED=1 python manage.py test apps.api.tests.test_agent_capabilities --settings=config.test_settings"]
---

# GUI and Agent Parity Contract

AGENTS.md rule 10 makes GUI and agent parity mandatory. Every outcome in the
staff UI must have an agent-accessible atomic REST primitive, or an explicit
human handoff. The contract is in
[`apps/api/capabilities.py`](../../apps/api/capabilities.py). It is the single
catalog that tells an agent what it may do, what it must hand to a person, and
which MCP tool, if any, exposes the action.

## The catalog

`CAPABILITIES` is a tuple of rows built by `_capability(...)`. A row carries:

- `id`, `outcome`, `method`, and `path`, which is the REST primitive.
- `ui_routes`, the canonical staff UI routes where the same outcome appears.
- `execution`, the policy below.
- `risk` (`read`, `low`, `moderate`, or `high`).
- `constraints`, names from `AGENT_CONSTRAINT_DEFINITIONS` such as
  `accountable_human_actor`, `substantive_reason`, `append_only_audit`, and
  `malware_scanned`.
- `mcp_tool`, when a Model Context Protocol tool exposes the action. The value is the
  tool's wire name (`psd_prr_*`), the same string [MCP](../integrations/mcp.md) registers.

The catalog holds about 176 rows, one per distinct operation the production
React application calls. Query variations are inputs to one list operation, not
separate powers. The live catalog is served by `AgentCapabilityCatalogView` at
`/api/staff/capabilities/`. `capability_catalog()` returns a fresh copy on each
call, so a caller cannot mutate policy.

## Execution policies

| Policy | Count in source | Meaning |
|---|---|---|
| `anonymous` | 1 | The public intake submission only (`requests.submit_public`). It is write-only, CSRF-protected, rate-limited, and idempotent. |
| `direct` | 1 | An agent may execute with its attributed token through the REST API. |
| `human_confirmation` | 61 | An agent may prepare the action, but a human must approve it explicitly before it runs. |
| `human_only` | 29 | The agent must hand off. The accountable human executes the action in the staff UI. |

The `human_only` and `human_confirmation` rows cover legal and statutory actions
(estimate revisions, denials, camera exclusions, correspondence approval and
queueing, no-action decisions, and manual association of retained inbound mail, see
[inbound review queue](../domain/correspondence.md#inbound-review-queue-and-manual-association)). Those are the rows that rules 6 and 7 of
AGENTS.md protect.

## Human-only handoffs

Token callers are not blocked silently. `human_only_handoff()` in
[`apps/api/handoffs.py`](../../apps/api/handoffs.py) returns a structured
`human_approval_required` response, with the capability id and action, before
any state changes. Session callers (the staff UI) pass through. The function is
called at the top of a human-only action, for example `exclude_camera` in
[`apps/api/resources/preservations.py`](../../apps/api/resources/preservations.py).
Attributed personal tokens may draft or preview. They cannot run a human-only
catalog entry.

## Tokens and identities

- Personal tokens are created, rotated, and revoked through session-only routes in
  `apps/api/access_tokens.py`. The lifecycle, limits, and authentication failure codes are in
  [personal access tokens](personal-access-tokens.md).
- `apps/api/authentication.py` authenticates session users first, then hashed
  personal tokens, and keeps the service-token identity boundary separate.
- The shared stdio MCP identity is a nonstaff account with `records.read_staff_api`
  only. Hosted MCP acts as the attributed personal-token holder.
  [MCP server](../integrations/mcp.md) explains the transports.
- Named integration principals are provisioned by `apps/records/service_identities.py`; see [integration identities](integration-identities.md) for the exact-scope rules.
  Each has one exact permission: the read-only MCP identity holds `records.read_staff_api`,
  and the outbound and inbound correspondence transports hold their own
  `correspondence` submit and deliver permissions. A transport principal is non-interactive,
  it cannot be given a presented token, and each request is checked against its exact scope
  (`require_exact_outbound_identity`, `require_exact_inbound_identity`). The read-only MCP
  identity is provisioned with the `provision_mcp_readonly` management command. Transport
  behavior for those principals is in [requester correspondence](../domain/correspondence.md#gmail-transport).
  The shape of these tests is in `apps/correspondence/tests/test_internal_gmail_sender.py` and
  `test_internal_gmail_poller.py`, which assert the system principal is non-interactive and
  has exact scope.

## Change guidance

To add a staff action:

1. Add the REST primitive and its permission check in the owning resource
   (`apps/api/resources/*.py`), delegating to the service layer.
2. Add a `_capability(...)` row with a `ui_route`, the correct `execution`, `risk`,
   and `constraints`. If the operation is human-only, call `human_only_handoff`
   in the view before any effect.
3. Add an `mcp_tool` only when the action is safe for agents under the policy.
4. Run `test_agent_capabilities`. It scans the production frontend for API calls
   and fails if the GUI and the catalog drift.

Do not register an agent-only operation as `direct` when a statutory effect
is reachable. The tests `test_statutory_human_handoffs_remain_explicit` and
`test_work_item_operations_are_explicit_and_no_action_is_human_only` exist to
catch that.

Related: [Compliance](../domain/compliance.md) (the statutory boundary this
catalog enforces), [Work queue](../domain/work-queue.md) (work actions and
their human-only decisions), [Advisory AI](../domain/advisory-ai.md) (proposal
actions are human confirmation).
