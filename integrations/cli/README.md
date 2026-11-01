# PRR command-line client

`prr.py` is a single-file, token-authenticated client over the existing staff
REST API. It adds no compliance or authorization logic: Django validates every
read and write, attributes it to the personal-token holder, and returns the
same role errors and human-only handoffs as any other agent client.

The script uses [PEP 723](https://peps.python.org/pep-0723/) metadata, so `uv`
installs `requests` in an isolated script environment:

```bash
uv run integrations/cli/prr.py --help
```

## Configuration

Create a named, short-lived personal token on the staff application's
**Agent Access** page. Configure the deployment origin and the one-time token
value in the shell:

```bash
export PRR_BASE_URL=https://prr.psd401.net
export PRR_API_TOKEN='prr_pat_…'
```

The global `--base-url` and `--api-token` flags may be used instead, before the
command group. Environment variables are preferable because command-line
arguments may be visible in process listings. The CLI never writes a token to
disk.

Both settings are required. Missing settings fail locally with exit status 1
before any network call. HTTPS is required except for `localhost` and numeric
loopback addresses, and HTTP redirects are refused.

Expired, revoked, and invalid credentials produce different actionable errors.
Expired credentials must be replaced; revoked credentials can be replaced or
rotated through **Agent Access**. A non-login system or shared-service token
must never be reused here.

## Read commands

Every leaf command accepts `--json`. Human-readable tables are the default;
JSON output is a single parseable value on stdout.

```bash
# Current attributed identity and live execution policy
uv run integrations/cli/prr.py me
uv run integrations/cli/prr.py capabilities --json

# Requests
uv run integrations/cli/prr.py requests list
uv run integrations/cli/prr.py requests list --status gathering --page 2 --json
uv run integrations/cli/prr.py requests get PRR-2026-0042

# Collection and legal-reference reads
uv run integrations/cli/prr.py tasks list --request PRR-2026-0042
uv run integrations/cli/prr.py tasks list --state pending --urgent
uv run integrations/cli/prr.py systems list
uv run integrations/cli/prr.py exemptions list
uv run integrations/cli/prr.py audit search --target PRR-2026-0042
```

Request and task lists use numbered pages. The append-only audit feed uses a
stable keyset cursor instead: run the first `audit search` with `--json`, copy
the opaque `cursor` query value from its `next` URL, and pass that value back
unchanged. Do not construct or edit it.

```bash
uv run integrations/cli/prr.py audit search \
  --target PRR-2026-0042 \
  --cursor '<cursor value from the previous next URL>' \
  --json
```

## Attributed write commands

These commands call existing `direct` or `human_confirmation` capability rows.
They do not bypass server role policy. Invoking a write command is an
accountable request by the token holder; Django remains responsible for
validation, audit, and state changes.

```bash
# Update a collection task
uv run integrations/cli/prr.py tasks update 17 --state running
uv run integrations/cli/prr.py tasks update 17 \
  --due-date 2026-08-05 --assignee-id 9 --json
uv run integrations/cli/prr.py tasks update 17 --unassign

# The request UUID is the `id` returned by requests list/get.
uv run integrations/cli/prr.py time log \
  --request-id 7a2c6f40-b768-42bd-b9e8-786e7eec6c31 \
  --date 2026-07-30 \
  --minutes 25 \
  --activity 'Reviewed responsive records.'

# An administrative transition still passes through Django's lifecycle service.
uv run integrations/cli/prr.py requests transition \
  PRR-2026-0042 gathering \
  --note 'Collection work began after acknowledgment.'
```

`requests transition … denied` cannot deny a request. The server returns a
structured human handoff, and the CLI prints the capability id, action,
required role, and resolved staff-UI route before exiting with status 2.

## Correspondence drafts

The CLI can create the same editable drafts exposed by the capability catalog.
It cannot approve or queue correspondence; those human-only operations are not
commands.

```bash
uv run integrations/cli/prr.py correspondence draft general \
  PRR-2026-0042 \
  --subject 'Records request update' \
  --body 'Collection is still in progress.'

uv run integrations/cli/prr.py correspondence draft acknowledgment \
  PRR-2026-0042 --estimated-completion 2026-08-14

uv run integrations/cli/prr.py correspondence draft clarification \
  PRR-2026-0042 \
  --questions 'Which school years should the search cover?' \
  --due 2026-08-07

uv run integrations/cli/prr.py correspondence draft estimate-revision \
  PRR-2026-0042 \
  --new-date 2026-09-01 \
  --reason 'The search returned 12,000 additional messages requiring review.'
```

Additional draft kinds are `installment`, `responsive-records`, `closure`, and
`denial`. Run a leaf command with `--help` for its required fields:

```bash
uv run integrations/cli/prr.py correspondence draft denial --help
```

Authored drafts accept repeatable `--to`, `--cc`, and `--bcc` email options.
If none are supplied, Django applies the endpoint's normal requester-recipient
behavior. Creating a denial draft only prepares editable work; it does not
deny, approve, queue, or send anything.

## Command-to-capability contract

The public command set maps to these registered capability families:

| Command | Capability |
|---|---|
| `capabilities` | `capabilities.list` |
| `requests list/get/transition` | `requests.list`, `requests.retrieve`, `requests.transition` |
| `tasks list/update` | `collection_tasks.list`, `collection_tasks.update` |
| `systems list` | `record_systems.list` |
| `exemptions list` | `exemptions.list` |
| `audit search` | `audit.list` |
| `time log` | `time_entries.create` |
| `correspondence draft …` | the matching `correspondence.draft_*` row |
| `me` | `/api/staff/auth/me` (the catalog's documented auth exception) |

Human-only capability rows are deliberately absent. If any reachable endpoint
returns `human_approval_required`, the generic HTTP layer renders that handoff
instead of dumping a raw 403.

## Testing

The Django suite imports only the script's `requests` client dependency and
executes it against a real live test server:

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py test \
  apps.api.tests.test_cli_client \
  --settings=config.test_settings
```

Coverage includes request list/get JSON, task/time/draft writes, denial
handoffs, missing and unsafe configuration, and expired/revoked token advice.
