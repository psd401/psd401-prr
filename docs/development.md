# Development Guide

This is the practical starting point for work on the PSD401 Public Records
Request system. Legal/compliance invariants are indexed in
[`AGENTS.md`](../AGENTS.md) and stated in full in
[`compliance-rules.md`](compliance-rules.md); they take precedence over
ordinary application conventions.

## Current baseline

Verified 2026-08-02:

- Python 3.12 / Django 6 / DRF backend, PostgreSQL 16 at runtime
- React 19 / Vite / TypeScript staff workspace
- Redis + Celery for asynchronous and scheduled work
- MinIO/S3-compatible object-store configuration and private ClamAV scanning
- Durable outbound/inbound requester correspondence with immutable delivery
  evidence, reconciliation, and PII-free operations/readiness
- Production Docker image and SPA build successfully; backend/frontend tests
  and lint remain required gates rather than a frozen count in this guide

The repository includes the Phase 2 application workflows: public, email, and
least-privilege staff intake; integrity-checked storage; resumable Vault
ingest; human review, citation-backed redaction, second review, and raster
burn-in; exemption logs and installment release; JLARC and fee workflows;
safe migration; advisory-only AI; and human-only no-records closure. It is not
production-ready merely because the code is complete. District credentials,
counsel and records-officer approvals, representative-data rehearsals,
backup/restore validation, and cutover remain governed handoffs documented in
[`phase-2-production-handoffs.md`](phase-2-production-handoffs.md).

## Read before changing code

1. [`../openwiki/quickstart.md`](../openwiki/quickstart.md) — generated source
   map and architecture navigation once the initial OpenWiki bundle is present
2. [`../AGENTS.md`](../AGENTS.md) — the rule index and repository map, and
   [`compliance-rules.md`](compliance-rules.md) — the non-negotiable compliance
   rules in full
3. [`project-status.md`](project-status.md) — dated kickoff ledger and sharpest
   remaining gaps
4. [`custodian-gap-analysis.md`](custodian-gap-analysis.md) — required before
   changing registry or triage scope
5. [`agentic-integration.md`](agentic-integration.md) — required when calling a
   deployed instance instead of editing source

## Local development

Prerequisites: Docker, `uv` 0.13.1 or newer (`pyproject.toml` declares that
floor, and an older uv writes an older lockfile revision), and Bun. `uv sync`
provides the Python 3.12 interpreter itself — `requires-python` in
`pyproject.toml` pins the minor version, so the environment cannot be built
from a newer system Python by accident. The Docker image is pinned to the same
3.12.

Python dependencies come from the committed `uv.lock`: `uv sync --locked`
installs exactly what it pins and fails when it no longer matches
`pyproject.toml`, which is what makes the production image run the versions CI
tested. CI then calls `uv run --frozen` so no step re-locks. Change a
dependency in `pyproject.toml` and run `uv lock`, then commit both files
together.

```bash
docker compose up -d db redis minio clamav

uv sync --locked
source .venv/bin/activate

cd frontend
bun install
bun run build
cd ..
```

`pyproject.toml`'s `[dependency-groups] dev` holds the test-quality and audit
tooling — `coverage`, `hypothesis`, `mutmut`, `bandit`, `pip-audit`, `ruff`,
`pyright` and the Django/DRF type stubs. `uv sync`
installs it by default and the production image's `uv sync --no-dev` ships none
of it. None of it is imported by application code, but the suite does import
`hypothesis`, so a `--no-dev` environment cannot run the backend gate.

Create local configuration from `.env.example` yourself; coding agents must
never read or write `.env`. For host-run Django, process environment values
must override the compose-internal hostnames:

```bash
export DATABASE_URL=postgres://prr:prr-dev-only@localhost:5432/prr
export REDIS_URL=redis://localhost:6379/0
export OBJECT_STORE_ENDPOINT=http://localhost:9000
export CLAMAV_HOST=127.0.0.1
export DJANGO_DEBUG=true
export PRR_CORRESPONDENCE_OUTBOUND_ENABLED=false
```

`DJANGO_DEBUG=true` is what selects local sign-in and opens the Django admin
route: both are district settings now, and their development values are
seeded from `DEBUG` rather than from an environment variable. Export
`PRR_AUTH_MODE=google` if you deliberately want to exercise the deployed
sign-in mode locally; otherwise leave it unset.

The MinIO bucket `prr-records` must exist before record uploads work (console
at `localhost:9001`, `prr` / `prr-dev-only`). Local Django mail is console-only:
production rejects every generic Django-email send, and requester
correspondence plus internal preservation and operations alerts use the
mailbox-pinned Gmail paths and their independent gates.

Then initialize and run:

```bash
python manage.py migrate
python manage.py seed_exemptions
python manage.py seed_systems
python manage.py createsuperuser
python manage.py provision_mcp_readonly mcp-local
python manage.py runserver  # Django routes only
```

The staff workspace is at `http://127.0.0.1:8000/`; Django admin is at
`/admin/`. For frontend hot reload, run `bun run dev` in `frontend/` and use
`http://127.0.0.1:5173/`.

To exercise hosted MCP locally, run
`uvicorn config.asgi:application --host 127.0.0.1 --port 8000` instead of
`manage.py runserver`; `/mcp` exists in the composed ASGI application, not the
legacy WSGI development server.

Requester correspondence queue/retry/claim is disabled by default. Keep it
disabled for ordinary local work. To exercise outbound transport, configure a
local-debug Gmail credential, set `PRR_GMAIL_ENABLED=true`, populate
`PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST` with only controlled addresses,
and run:

```bash
python manage.py verify_gmail
python manage.py check_correspondence_readiness --json
```

With the switch off, that command exits nonzero and should report only
`outbound_transport` as intentionally disabled once storage, ClamAV, Redis, and
the Gmail transport are available. Set
`PRR_CORRESPONDENCE_OUTBOUND_ENABLED=true`, restart Django, worker, and beat,
then require the command to return `ready: true` before a local
queue/task/result test. The officer/admin-only
`GET /api/staff/correspondence-operations/readiness/` endpoint returns the same
fixed-code result without exposing hostnames, bucket names, exceptions, or
requester data.

Internal operational alerts use the same mailbox-pinned Gmail client but are
not requester `Correspondence` and have their own switch. Keep
`PRR_STAFF_NOTIFICATIONS_ENABLED=false` during ordinary development. To test
them deliberately, enable it only after `verify_gmail` succeeds, set
`PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS` to the configured Records Officer
addresses, and put every possible recipient in
`PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST`. Identical unchanged alerts are
suppressed for `PRR_STAFF_NOTIFICATION_SUPPRESSION_SECONDS` (one day by
default); production should use shared Redis so suppression is process-wide.
The production app example also leaves the staff switch off. Enable it only
after its separate named acceptance; setting it false remains the immediate
notification rollback.

Local identity mode deliberately requires no Google or AWS access. Any local
staff user without explicit `prr-role:*` groups receives the four development
roles from `PRR_LOCAL_IMPLICIT_STAFF_ROLES`; production rejects this mode at
startup. Google OIDC, group membership, and AWS secret injection are exercised
with mocks in the normal test suite and by an authorized operator during the
deployment acceptance check.

Run a Celery worker when testing asynchronous delivery. Run exactly one beat
process per database; two beat processes will double-fire the deadline
watchdogs, requester-facing safety net, and correspondence recovery task (the
event-outbox and delivery recovery paths are row-lock/lease-safe, but still
need beat to run). ClamAV publishes port 3310 to
loopback only in the development stack so a host-run worker can scan intake
attachments. The official scanner requires substantial memory for its
signature database; budget approximately 4 GiB for the container in a shared
development or production deployment.

### Local guards

- **gitleaks pre-commit hook.** Install it once per clone with
  `uvx pre-commit install`. It blocks a commit that stages a secret. CI still
  runs gitleaks, with the org rules, through `security-scan`.
- **CDK check after agent edits.** `.claude/settings.json` runs
  `.claude/hooks/cdk-synth.sh` after Claude Code edits `infra/cdk/bin/`,
  `infra/cdk/lib/`, `cdk.json`, or `cdk.context.json`. The hook typechecks and
  synthesizes without `AWS_PROFILE`, as CI does, and reports a failure back to
  the agent. It is skipped until `bun install` has run in `infra/cdk`.
- **Terraform check after agent edits.** The same settings run
  `.claude/hooks/terraform-check.sh` after Claude Code edits a `.tf` or
  `.tfvars` file under `infra/terraform/`. In that root it runs
  `terraform fmt -check -diff`, then `terraform validate`. If the root has no
  `.terraform/` yet, it first runs CI's `terraform init -backend=false` with
  `-lockfile=readonly`, so it never reaches the S3 state or rewrites the
  provider lockfile. It is skipped when `terraform` is not installed.
- **AWS MCP servers for agents.** `.mcp.json` gives Claude Code the hosted AWS
  Knowledge server (AWS docs, no credentials) and the AWS IaC server
  (CloudFormation validation and cfn-guard checks, CDK docs and samples). No AWS
  profile is configured, so the IaC server's deployment troubleshooting only
  works if you export one yourself.

## Required quality gates

Every feature and bugfix starts with a failing test. Before calling a change
complete:

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py test apps --settings=config.test_settings
uv run ruff check .
uv run python scripts/pyright_baseline.py

cd frontend
bun run test
bun run lint
bun run build
bun audit --production

cd ../infra/cdk
bun run typecheck
bun test
bunx cdk synth --quiet          # also runs cdk-nag and Guard; an unacknowledged finding fails here
uv tool run --from "checkov==3.3.29" checkov -d cdk.out --framework cloudformation --compact --quiet

cd ../terraform/google-identity
terraform fmt -check -recursive
terraform init -backend=false -input=false
terraform validate
tflint --init && tflint
uv tool run --from "checkov==3.3.29" checkov -d . --framework terraform --compact --quiet

cd ../../..
uv run pip-audit
```

Pin Checkov to the version `PRR_CHECKOV_VERSION` holds in
`.github/workflows/psd-ci-jobs.yml`, or a locally newer ruleset will report
findings CI does not.

The same gates run from one command, which is what `.psd/verify.json` calls:

```bash
bash scripts/verify-fast.sh                          # backend + frontend
bash scripts/run-casework-acceptance.sh --smoke      # four browser flows
bash scripts/verify-fast.sh --full                   # adds postgres + every browser flow
bash scripts/verify-fast.sh --patch-coverage         # both suites under coverage, then the 80% gate
```

### Test-quality gates

These implement psd-dev-standards `standards/05-testing.md`; the contracts that
keep them wired live in `apps/api/tests/test_quality_gates.py`.

**Patch coverage (rule 4).** 80% of the lines a change ADDS must be covered,
backend and frontend together. There is deliberately no project-coverage floor:
that number is gameable with assertion-free tests and punishes a change that
touches a long-untested file. `scripts/patch_coverage.py` reads the added lines
from `git diff` and intersects them with what the coverage report says is
executable, so comments and blank lines neither help nor hurt. It reads
coverage.py JSON and Istanbul/V8 JSON, and a file no report instruments is
listed and skipped rather than counted as a miss. In CI the four backend shards
each upload their `.coverage.*` data, the frontend job uploads
`coverage-final.json`, and the `patch-coverage` job combines them and enforces
the floor.

That last rule is the gate's boundary, and it is worth knowing: a change
confined to code no report instruments passes with "patch coverage does not
apply". Today that means `infra/cdk` — gated instead by its own `bun test`,
typecheck, and credential-free `cdk synth` in the `infrastructure` job — plus
docs, workflows, and shell. To widen the gate, widen a coverage report's scope
(`[tool.coverage.run] source`, or `coverage.include` in
`frontend/vite.config.ts`), rather than special-casing paths in the script.
`scripts/` is inside both the coverage source list and, since the same change,
bandit's scan: a gate runner is not exempt from the gates it enforces.

Coverage runs under `COVERAGE_CORE=sysmon`, CPython 3.12's own coverage core,
and that is not incidental. Measured on the `correspondence` shard: 101s with
no coverage, 164s under coverage.py's default trace core, 94s under sysmon. The
default core's 60% tax was the entire reason a shard ran past five minutes.
sysmon measures lines and not branches, so turning on `branch` in
`[tool.coverage.run]` would quietly need the slow core back; a contract test
fails if the two settings disagree.

**Strict typecheck and lint (rules 1-2).** `ruff` and `pyright` are pinned to
an exact version in `[dependency-groups] dev`, because a tool that moves under
the gate turns somebody else's green branch red. Both run in the
`backend-static` CI job and in `scripts/verify-fast.sh`.

`ruff` must report zero findings. Its `select` list in `pyproject.toml` is
explicit rather than inherited from ruff's defaults, so a ruff upgrade cannot
widen or narrow the gate without a commit that says so, and every entry in
`ignore` carries the reason it is there.

`pyright` is a **ratchet**, not a zero-error floor. The tree reports 1,848
errors today, nearly all of them django-stubs not modelling a reverse accessor
or a test-client keyword rather than a defect here; a gate that failed every
branch would be a gate people route around.
`scripts/pyright_baseline.py` compares per-file counts against
`pyright-baseline.json` and fails two ways:

* a file with MORE errors than the baseline — including a file the baseline
  has never seen, which starts at zero;
* a file with FEWER errors than the baseline — not a failure of the code, a
  stale baseline. Rerun with `--update` and commit the result. Without this
  half the number never ratchets down, because the slack an unrecorded fix
  leaves behind could be spent again tomorrow.

The comparison is per file and never a whole-tree total, so a fix in one
module can never pay for a break in another.

Two operational notes. `pyright-baseline.json` is rewritten whole, so two
branches that both fix something conflict textually; resolve by taking either
side and then rerunning `--update` on the merged tree, because taking one
side wholesale discards the other branch's recorded fixes. And pyright reads
whatever is in `.venv`, so a checkout whose environment predates the current
`uv.lock` type-checks against older stubs and can disagree with CI --
`uv sync --locked` first if a count looks unfamiliar.

**Test-quality lint (rule 2).** No assertion-free test, and no committed
`.only` or `.skip` without a reason, in any of the three suites:

* **Vitest** — `vitest/expect-expect`, `no-focused-tests` and
  `no-disabled-tests` in `frontend/.oxlintrc.json`, run by
  `bun run lint --deny-warnings`.
* **Playwright** — oxlint's vitest rules only recognise the vitest and jest
  globals, so a `test.only` imported from `@playwright/test` passes the
  frontend lint untouched. `PlaywrightTestQualityTests` reads the specs
  instead.
* **Django** — no linter understands `unittest`, so
  `DjangoTestQualityTests` walks the suite's own AST. A test that passes by
  not raising is a real pattern and is allowed out loud, with
  `# no-assert: <reason>` on the `def` line; a skip must carry a reason, and
  a flaky test is quarantined through `FLAKY.md` instead, where it keeps
  running.

**Flaky tests (rule 6).** [`FLAKY.md`](../FLAKY.md) is the register and the
quarantine process. Quarantine means the test still runs but stops gating:
Playwright flows carry `@quarantine` in the title, Django tests carry
`@tag("quarantine")`, every gating lane excludes them, and non-gating steps in
`backend-audit` and `acceptance` run exactly those. A row needs an owner, an
issue, an expiry no more than four weeks out, and links to the two CI runs on
the same commit — one red, one green — that prove it is a flake rather than a
test that fails every time being silenced.
`apps/api/tests/test_flaky_register.py` fails once a row expires, if a row is
missing any of that, or if a test is tagged without a row at all.

**Mutation testing (rule 7).** `.github/workflows/mutation-testing.yml` runs
mutmut monthly over the Tier A modules — the RCW 42.56.520 deadline engine,
identity, and the intake path that fixes `matter_kind` — and
`scripts/mutation_score.py` gates each module's kill rate at 80%. Coverage says
a line ran; a mutation score says an assertion would have noticed it change.
Survivors become an issue. It never gates a merge: mutmut re-runs the suite once
per mutant.

Two of those modules decide who may see what and which law a case answers to,
so say the obvious part out loud: a weak score there is answered with a better
test, never with a simpler rule. Nothing in `docs/compliance-rules.md` becomes
negotiable because a mutant was hard to kill. Note the deliberate `mutmut>=2.5,<3` pin — 3.x drives pytest only,
and this suite runs under the Django test runner.

If you run mutmut locally, let it finish. It rewrites the module under test in
place and restores it at the end, so interrupting it leaves a mutant in your
working tree: a killed run during this work left `closures = None` in
`apps/records/deadlines.py` and broke nine unrelated tests. `git diff` the
mutated paths, and `git checkout` them, after any interrupted run. CI is
immune — it mutates a fresh checkout and throws it away.

**Property tests (rule 8).** `apps/records/tests/test_deadlines_properties.py`
states the deadline engine's invariants for Hypothesis to attack: holidays,
district closures, tolling, after-hours rollover, the Pacific wall clock behind
a UTC-stored instant, and the fact that the FERPA 45-calendar-day window shares
none of that machinery. `test_deadlines.py` keeps the hand-checkable Washington
calendar dates; the two are complementary, not alternatives. The settings carry
`derandomize=True` on purpose: CI keeps no `.hypothesis` example database
between runs, so a random search that found a bad date once and missed it on
the re-run would look exactly like a flake — and the quarantine process would
then eat a real deadline defect. Fixed inputs make a failure reproducible on
the same commit, and the monthly mutation run covers the breadth instead.

Every gate lane finds Python through `scripts/prr-python.sh`: `PRR_PYTHON_BIN`
if set, else this checkout's `.venv`, else — in a git worktree, which has no
`.venv` of its own — the main checkout's `.venv`, else `python3`. A worktree
therefore needs no Python setup once the main checkout has its `.venv` (see
Local development above); only `bun install` in `frontend/`. Without either
`.venv`, `python3` must be a 3.12 interpreter with the locked dependencies
installed. `uv sync --locked` in the worktree is the simpler route: it creates
that worktree's own `.venv` from `uv.lock`.

`.verification/` is regenerated by every acceptance run and is not committed
(only `.gitkeep` is tracked; a contract test enforces it). Do not force-add
screenshots as PR evidence — CI uploads them as a workflow artifact, and that is
the copy reviewers read.

Backend tests use in-memory SQLite and require no services. Runtime behavior
must still be checked against PostgreSQL when a change depends on database
semantics. Deadline/tolling tests use real Washington calendar dates.

`PYTHON_DOTENV_DISABLED=1` is not optional locally. `config/settings.py` calls
`load_dotenv()` at import, so a populated `.env` overrides values the tests
assert on: a local `PORTAL_BASE_URL` fails four installment-notice tests with
"Installment downloads must use the configured application origin". CI has no
`.env` and stays green, which makes the local failure look like a real
regression.

Deadline and tolling changes REQUIRE tests with real Washington calendar dates
(see `apps/records/tests/test_deadlines.py` — note that Juneteenth 2026-06-19
and the observed July 4th trip naive expectations). Event delivery tests mock
`requests.post` and assert HMAC signatures. Tests run on SQLite via
`config/test_settings.py` while runtime is Postgres 16; the timezone is
America/Los_Angeles and deadline math depends on it.

The `infra/cdk` suite gates every pull request through the `infrastructure` job
in `.github/workflows/psd-ci-jobs.yml`. `cdk synth` runs there with no AWS credentials
configured, so a change that reintroduces a credentialed lookup fails the
build.

### Infrastructure-as-code gates

These implement psd-dev-standards `standards/08-iac.md`; the contracts that
keep them wired live in `apps/api/tests/test_iac_gates.py`.

**cdk-nag v3, AwsSolutions (CDK).** Registered with `Validations.of(app)` in
`infra/cdk/lib/app.ts`, so it runs as a CDK policy validation plugin rather
than an aspect: an unacknowledged ERROR-level finding *fails synthesis*, in
`bun test`, in the `cdk-synth` PostToolUse hook, and in CI. Suppressions are
`Validations.of(resource).acknowledge({id, reason})` next to the resource,
and the reason is the evidence. Adding one is a human-review item - agents may
propose, never merge.

**CfnGuardValidator, Control Tower rules (CDK).** Registered next to cdk-nag,
pinned exactly in `infra/cdk/package.json`, and acknowledged the same way:
`acknowledgeGuard()` in `infra/cdk/lib/suppressions.ts` calls `acknowledge()`
with `cdk-validator-cfnguard::<rule name>`. `lib/app.ts` wraps the plugin to
mark each finding an error. Without that, the first acknowledgment stops
every other Guard finding from failing synthesis: CDK then counts only
`error`-severity violations, and the plugin sets no severity.
`infra/cdk/test/app.test.ts` proves the gate holds with acknowledgments in
place.

**Checkov (both trees).** A second engine over the synthesized templates and
over the Terraform root, pinned to `PRR_CHECKOV_VERSION`. Suppressions are
per resource - `Metadata.checkov.skip` from `infra/cdk/lib/suppressions.ts`
for CDK, inline `checkov:skip` comments for Terraform - never a
repository-wide `--skip-check`, which would disable a rule for every future
resource too.

**Logical-ID pin tests.** `infra/cdk/test/stateful-logical-ids.test.ts` pins
the CloudFormation logical ID of every stateful resource, because a changed
logical ID is a replacement and for these resources a replacement is data
loss. It also fails when a new S3 bucket or Secrets Manager secret appears
without an entry, so somebody has to state why it is safe. If one of these
assertions fails, do not update the expected ID to match.

**The district tag set.** `psd:application`, `psd:environment`, `psd:owner`,
`psd:managed-by`, `psd:repo` and `psd:data-classification` go on with
`Tags.of(app)` in `infra/cdk/lib/app.ts`; each stack overrides only the
classification. `infra/cdk/test/app.test.ts` fails if a taggable resource
misses any of them. The one exception is the EC2 key pair: its tags cannot change in place, and replacing it replaces the instance and its root volume, so it keeps its deployed `Application`/`Environment`/`ManagedBy` tags, pinned by a test. Run `cdk diff` before every deploy and stop on any `replace`.

**Terraform (`terraform` job).** `fmt` -> `validate` -> `tflint` -> Checkov.
There is no `plan`: it needs Google credentials and write access to the S3
state backend, and no OIDC role exists for either yet. Agents never run
`apply`, `import` or `destroy` against a real account, and no workflow may.

GUI/agent parity is also a required gate. Any new production frontend call to
`/api/staff/` must reuse or add an atomic capability in
`apps/api/capabilities.py`. The `test_agent_capabilities` contract test scans
the frontend and fails if a GUI action has no registered agent path. Do not
solve parity by bypassing the statutory human-only actions in `docs/compliance-rules.md` (rules 6 and 7).

`.github/workflows/ci.yml` runs these backend and frontend gates for every
pull request and every push to `main`. It calls `psd-ci-jobs.yml` from a job
named `psd-ci`, so each check reports as `psd-ci / <job>` and the combined gate
as `psd-ci / psd-ci`, the context the org rulesets require.

Toolchain setup is two local composite actions, used by `psd-ci-jobs.yml`,
`nightly.yml`, and `mutation-testing.yml`:
`.github/actions/setup-python-deps` (uv, Python 3.12, `uv sync --locked`,
with `include-dev: false` for `--no-dev`) and `.github/actions/setup-bun-deps`
(Bun, `bun install --frozen-lockfile` in one directory, and Playwright
Chromium when `install-playwright: true`). Change a version or an install flag
there, not in a workflow; `test_quality_gates.py` fails if a workflow sets up
uv, Bun, or Playwright by hand. One `bun install --frozen-lockfile` stays
inline on purpose: the `infrastructure` job's frontend build step installs
`frontend/` after the action has set up `infra/cdk`. Each job that uses
`setup-python-deps` must also set `UV_PYTHON_PREFERENCE: "only-managed"` in its
workflow or job `env`, which `test_security_boundaries.py` checks per job. They are composite actions rather than a
reusable workflow because reusable workflows belong in `PSD401/.github`
(psd-dev-standards standards/06). Each job still checks out the repository
itself first, since GitHub reads a local action from the checkout. The SHA
pinning test scans these `action.yml` files as well as the workflows.

Two things keep those checks near the five-minute budget in standards/05's CI
shape. The Django suite runs as four shards by app label rather than one serial
job — `test_quality_gates.py` fails if the shard list stops covering an
installed app, or drifts from the split `verify-fast.sh` uses locally — and
pull requests run the Playwright smoke subset while
`.github/workflows/nightly.yml` runs every flow against `main` each night. A
nightly failure is a regression on `main`, not a PR to fix, and it opens an
issue rather than only emailing whoever last touched the workflow: the smoke
subset is a deliberately thinner merge gate, so the net under it has to close
its own loop. The pull-request template repeats the
agent-parity, risk-policy, human-handoff, test, and documentation checks so a
reviewer can verify the intent as well as the automated API-operation scan.
The PR is not complete until Claude has posted an actual review of the latest
commit. If the automatic workflow produces only a green check, request the
review with `@claude please review`, monitor the issue-comment run and posted
output, address valid findings, and re-request review after pushing fixes.
CI also queries Python and npm advisory databases, and Dependabot monitors both
ecosystems weekly.

## Security boundaries

- The sole anonymous business operation is write-only public intake. It
  requires same-origin CSRF, a signed HttpOnly form token, fill-time and
  honeypot checks, Redis-backed per-IP/global hard ceilings, and returns only a
  request number. MCP uses a dedicated nonstaff account with only
  `records.read_staff_api`; no integration-specific request-creation token
  remains.
- Outside `DJANGO_DEBUG=true`, Django requires a high-entropy secret, redirects
  HTTP to HTTPS, marks session/CSRF cookies Secure, enables HSTS, and disables
  admin unless an operator explicitly enables it. The container entrypoint runs
  deploy checks and aborts on an unsafe secret/HTTPS/cookie posture.
- `docker-compose.deploy.yml` binds Gunicorn with Uvicorn ASGI workers to host
  loopback. `config/asgi.py` serves hosted MCP at `/mcp` and delegates every
  other path to Django; its top-level lifespan owns FastMCP's session manager.
  A trusted reverse proxy must terminate TLS, replace client-supplied
  forwarding headers, set `X-Forwarded-Proto`, and supply the client address as
  the rightmost `X-Forwarded-For` value for public-intake throttling; only then enable
  `DJANGO_TRUST_X_FORWARDED_PROTO`. The AWS overlay
  (`docker-compose.aws.yml`) ships that proxy as Caddy and sets the header
  itself.
- The deploy stack separates `.env` bootstrap credentials from `.env.app`
  runtime configuration. Only one-shot bootstrap/migration containers receive
  Postgres-owner, MinIO-root, or superuser credentials; web, worker, and beat
  use a non-owner database role and a bucket-scoped object-store identity.
- The store overlay, not the base file, carries every `OBJECT_STORE_*` value.
  Compose interpolates the base before merging an overlay, so a required
  MinIO variable in the base would stop the AWS host from starting at all.
- Local staff login is CSRF-protected and rate-limited through the shared Redis
  cache. Production has no password-login route: Google OIDC validates the
  immutable subject and hosted domain, then administrator-configured groups map
  to server-enforced roles. API/admin responses use `Cache-Control: no-store`.
- Requester email transport uses a migration-provisioned non-login system
  principal with only `correspondence.deliver_outbound_correspondence`. The
  Celery worker calls the claim, claim-bound attachment, and result services
  directly. No transport claim, attachment, result, or inbound-submission route
  is exposed over HTTP.
- `PRR_CORRESPONDENCE_OUTBOUND_ENABLED` defaults false and is a coordinated
  drain switch: queue, retry, and new claim stop while disabled, but a valid
  active lease can still read its clean attachments and report its result.
  The watchdog keeps expired-attempt evidence but suppresses recovery nudges
  until re-enabled. In production, a named incident may move the setting only
  to false and recreate web, worker, and beat from the recorded common image as
  one serialized emergency action. Re-enablement and routine changes must use
  the tested, globally locked mechanism required by `docs/aws-deploy.md`; mixed
  rolling process settings are not an atomic stop or start.
- `PRR_CORRESPONDENCE_STUCK_SECONDS` defaults to 3600 (allowed range
  300-86400). Recovery tasks key suppression by delivery UUID and attempt
  count, while retaining the same deterministic Message-ID. They are claim
  prompts, never automatic resend authority or replacement-delivery creation.
- Every `PRR_*` name on this page that a district would legitimately tune —
  the stuck window, the notification recipients and suppression window, the
  non-production recipient allowlist — is a **district setting**, not an
  environment variable. Change it at Configuration > District settings with a
  written reason; it takes effect in web, workers, and beat within seconds and
  keeps a permanent history. `apps/configuration/registry.py` is the list, and
  `runtime.get(key)` is the only way to read one. Credentials, identity, and
  infrastructure addresses stay in `.env`.
- Never reuse a personal staff token or the shared read-only MCP token for
  another purpose. Gmail workers are tokenless non-login principals; rotate
  the MCP credential only through its provisioning command's `--rotate` option.

## OpenWiki maintenance

`openwiki/` is generated documentation and is committed to the repository.
Do not hand-edit it, except `openwiki/INSTRUCTIONS.md` (below). Hand-written
truth belongs in source code, `README.md`, `AGENTS.md`, and `docs/`; OpenWiki
turns that material and the codebase into an agent-navigable map.

`.github/workflows/openwiki-update.yml` is the org starter template
(`PSD401/.github` `workflow-templates/openwiki.yml`) with `main` as the branch.
It runs on every `main` push (ignoring `openwiki/**`), on a Monday schedule,
and by hand, and calls `PSD401/.github/.github/workflows/reusable-openwiki.yml@main`
with `BEDROCK_API_KEY` and `PSD_AUTOMATION_APP_PRIVATE_KEY` passed by name. The
org reusable owns the OpenWiki version pin, the Bedrock provider, the models,
concurrency, and the docs PR, which it opens and merges with the psd-automation
App token. Look those up in `PSD401/.github` and psd-dev-standards
`standards/06-cicd-enforcement.md` §3; do not restate or override them here.
Per that standard, a hand-written OpenWiki workflow, a SHA-pinned caller, or
`secrets: inherit` is drift. `test_security_boundaries` holds the caller to the
template.

Both secrets are `psd401` organization secrets already shared with this
repository; nothing repository-level is needed.

Do not generate the wiki locally or run `openwiki code --init`: it rewrites the
caller with OpenWiki's stock scaffold. To refresh, merge to `main` or run the
workflow by hand.

### What the wiki may read: `.openwikiignore`

`.openwikiignore` at the repository root is the read boundary. OpenWiki never
reads a path it lists, and the generated docs PR merges itself, so nothing
reviews wiki prose before it lands. Add a path when a document carries
district-internal hostnames, named service or staff accounts, AWS account IDs
or ARNs, or anything else that must not be restated in generated prose
(psd-dev-standards `standards/06-cicd-enforcement.md` §3). The file's header
comment records which document carries what, and why.

It is a read boundary, not a redaction: the wiki can still mention a topic it
infers from code, tests, or commit messages. Secrets themselves never belong in
the repository at all.

`openwiki/INSTRUCTIONS.md` covers what the boundary cannot, for the cases where
the source itself carries the detail — the delegated mailbox, the Vault subject
account and the retired records officer's address all appear in application
tests, not only in `docs/`. OpenWiki reads it as the brief for every run and
never rewrites it, so it is hand-maintained even though it lives under
`openwiki/`. It tells the model to describe people and identities by role
rather than by name, and not to describe routes around a control. That is a
model instruction, so it reduces the risk rather than guaranteeing it; put
anything that must not be read at all in `.openwikiignore` instead.

## Recommended next workstream

1. Execute the production-handoff checklist with named district owners; keep
   all provider and policy readiness gates fail-closed.
2. Rehearse representative Vault exports, OCR/redaction production, exemption
   logs, installment downloads, and NextRequest migration with synthetic local
   fixtures. Production-derived restores belong only on a separately approved,
   protected recovery environment under `docs/backups.md`, never a developer
   machine. Do not use the retired deployed dev site.
3. Validate backup/restore, transport reconciliation, observability, and the
   rollback plan before setting a production cutover date.
4. Treat later application changes as ordinary TDD work that preserves the
   compliance invariants and GUI/agent capability parity.

## Documentation ownership

- Generated source map: `openwiki/` (OpenWiki only)
- Rule index and repository map: `AGENTS.md` (kept to ~150 lines)
- Compliance and coding invariants, in full: `docs/compliance-rules.md`
- Annotated repository layout: `docs/architecture.md` §10
- Developer setup and quality gates: this file
- Product overview and operator entry points: `README.md`
- Kickoff progress ledger: `docs/project-status.md`
- Live API/agent guardrails: `docs/agentic-integration.md`

Update the owning hand-written document in the same change as behavior. Let
the OpenWiki workflow refresh the generated map afterward.
