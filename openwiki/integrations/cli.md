---
type: Integration
title: Staff Command-Line Client
description: The single-file token-authenticated CLI (integrations/cli/prr.py) over the staff REST API. It maps each command to a registered capability, adds no compliance logic, and renders human-only server handoffs with exit status 2. Read before changing a CLI command, its capability mapping, or its configuration checks.
tags: [integration, cli, agent, capabilities, human-in-the-loop]
timestamp: 2026-10-13T00:00:00Z
openwiki:
  roles: [integration, architecture]
  change_kinds: [cli-command, capability-mapping, human-handoff]
  source_paths: [integrations/cli/prr.py, integrations/cli/README.md]
  symbols: [COMMAND_CAPABILITIES, CLIError, HumanApprovalRequired]
  test_paths: [apps/api/tests/test_cli_client.py]
  invariants:
    - "Every CLI leaf command maps to a non-human-only capability id or to the auth/me exception; human-only rows are never CLI commands."
    - "The CLI never writes a token to disk and refuses plain HTTP except for loopback hosts and refuses redirects."
    - "A human-only server response prints the capability id, action, required role, and staff UI route, then exits with status 2."
  validation_commands: ["PYTHON_DOTENV_DISABLED=1 python manage.py test apps.api.tests.test_cli_client --settings=config.test_settings"]
---

# Staff Command-Line Client

`integrations/cli/prr.py` is a single-file command-line client for the staff REST
API. Like the MCP transports, it is a thin presentation layer: it does not reproduce
deadline, authorization, exemption, correspondence, or lifecycle rules. Django
validates every read and write, attributes it to the personal-token holder, and
returns the same role errors and human-only handoffs that any other agent client
receives. See [MCP server](mcp.md) for the sibling LLM transports and
[agent parity](../architecture/agent-parity.md) for the capability contract.

## Where it sits

```mermaid
sequenceDiagram
    participant User as Token holder shell
    participant CLI as prr.py (requests)
    participant API as Staff REST API
    participant Svc as Domain services
    User->>CLI: requests transition PRR-... denied
    CLI->>API: POST lifecycle transition (personal token)
    API->>API: human_only_handoff before any state change
    API-->>CLI: human_approval_required payload
    CLI-->>User: capability id, action, required role, staff UI route (exit 2)
    User->>CLI: time log / tasks update / correspondence draft
    CLI->>API: attributed direct or human_confirmation call
    API->>Svc: domain service validates and audits
    Svc-->>CLI: JSON result (or refusal, exit 1)
```

Caption: the CLI forwards every operation to the server; the server decides whether
an action is allowed, and the CLI only prints the result or the structured handoff.

## Commands and capability mapping

`COMMAND_CAPABILITIES` in `integrations/cli/prr.py` is the authoritative table from
command path to registered capability id. It covers:

- Reads: `capabilities`, `requests list|get`, `tasks list`, `systems list`,
  `exemptions list`, `audit search`, and `me` (which calls `/api/staff/auth/me`, the
  catalog's documented auth exception).
- Attributed writes: `tasks update`, `time log` (`time_entries.create`), and
  `requests transition`, which still passes through Django's lifecycle service.
- Correspondence drafts: `correspondence draft general|acknowledgment|clarification|estimate-revision|installment|responsive-records|closure|denial`.
  Drafts can be created but never approved, queued, or sent; those human-only
  operations have no command.

`requests transition … denied` cannot deny a request. The server returns the handoff
and the CLI exits with status 2 (`HumanApprovalRequired`, which extends `CLIError`).
Other expected failures raise `CLIError` and exit with status 1, including missing
configuration, which is detected before any network call.

## Configuration and transport rules

- Settings come from `PRR_BASE_URL` and `PRR_API_TOKEN`, or from the global
  `--base-url` and `--api-token` flags. Environment variables are preferred because
  command-line arguments may appear in process listings.
- The token must be a personal, attributed token created on the staff application's
  Agent Access page (`_AGENT_ACCESS_ROUTE`). A shared service or non-login identity
  must not be used.
- HTTPS is required except for `localhost` and numeric loopback addresses, and HTTP
  redirects are refused.
- Expired and revoked credentials produce different messages that point to the
  Agent Access fix.
- Output is a human table by default. Every leaf command accepts `--json`, which
  emits one parseable value on stdout. `audit search` pages with an opaque keyset
  cursor from the previous `next` URL; callers must not construct it.

## Change guidance

To add or change a CLI command:

1. Confirm the capability row exists in `apps/api/capabilities.py` and is not
   `human_only`. If it is human-only, do not add a command; the handoff path already
   prints the route.
2. Add the command to the argparse tree and to `COMMAND_CAPABILITIES` in
   `integrations/cli/prr.py`, keeping the mapping in sync with the capability id.
3. Add a case to `apps/api/tests/test_cli_client.py`. Two invariants are already
   asserted there: `test_each_command_maps_to_a_non_human_only_capability` and
   `test_human_only_response_prints_complete_handoff_and_exits_nonzero`.
4. Update the command table in `integrations/cli/README.md` when the command surface
   changes.

Non-goals: do not add client-side authorization, deadline arithmetic, or statutory
checks to the CLI. Those belong in the domain services the server calls.

## Testing

The CLI is tested against a live Django test server from the Django suite, so its
`requests` dependency and the real REST contract are exercised together.

- `CLIConfigurationTests` (`apps/api/tests/test_cli_client.py`): help output, command
  mapping, audit-search pagination rejection, missing configuration, and plain-HTTP
  refusal, all without a network call.
- `PRRCLILiveServerTests`: JSON output for reads, the attributed write commands,
  cursor following, the human-only handoff, and expired or revoked token messages.

Focused command (quiet on success, full diagnostics on failure):

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py test apps.api.tests.test_cli_client --settings=config.test_settings
```

Related: the server-side capability rules are checked by
`apps/api/tests/test_agent_capabilities.py`, and the handoff machinery by
`apps/api/tests/test_human_only_handoffs.py`. Run those when a CLI change depends on a
new or altered capability row.
