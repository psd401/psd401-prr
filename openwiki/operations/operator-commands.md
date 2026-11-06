---
type: Operations
title: Operator Management Commands
description: The Django management commands that operators run against live data - system registry seeding, Gmail and correspondence readiness gates, NextRequest import, file migration, cutover checks, and activation - and which of them are dry-run by default or refuse to run without an explicit confirmation.
tags: [operations, management-commands, migration, readiness, runbook]
openwiki:
  roles: [operations, delivery]
  change_kinds: [management-command, migration, readiness-gate]
  source_paths: [apps/records/management/commands/seed_systems.py, apps/records/management/commands/check_nextrequest_cutover.py, apps/records/management/commands/activate_nextrequest_import.py, apps/records/management/commands/import_legacy_timeline.py, apps/records/management/commands/nextrequest_import_files.py, apps/correspondence/management/commands/gmail_inbound_status.py, apps/correspondence/management/commands/check_correspondence_readiness.py, apps/correspondence/management/commands/seed_casework_test_fixture.py, apps/correspondence/operations.py]
  symbols: [gmail_inbound_status_snapshot, readiness_snapshot, plan_nextrequest_import, assess_nextrequest_cutover, activate_legacy_request, commit_entries, build_plan, commit_plan]
  test_paths: [apps/correspondence/tests/test_operations.py, apps/records/tests/test_systems.py, apps/records/tests/test_nextrequest_migration.py, apps/records/tests/test_legacy_timeline.py, apps/records/tests/test_nextrequest_files.py, apps/correspondence/tests/test_casework_fixture_command.py]
  invariants:
    - Import and migration commands are dry runs unless `--commit` is passed; a file migration commit also requires the plan digest from the reviewed dry run.
    - Activation of imported open requests refuses to run without `--confirm-live-triage`.
    - Readiness and status commands print fixed codes and exit nonzero when the dependency is not ready, so a shell gate can branch on the exit code.
    - Legacy timeline import records history only and never changes a request's status.
    - The casework fixture seeder refuses to run unless `PRR_TEST_FIXTURE_SEED_ENABLED` is set.
  validation_commands: ["PYTHON_DOTENV_DISABLED=1 python manage.py test apps.correspondence.tests.test_operations apps.records.tests.test_systems --settings=config.test_settings"]
---

# Operator management commands

Operators run these Django management commands by hand, from a host or container with
the production settings, during migration, cutover, and readiness work. Most of them
touch live records, so each command's `--help` and module docstring are the first
reference. This page groups them by job and states the guard each one enforces. The
migration procedure they belong to is in [NextRequest migration](../domain/nextrequest-migration.md).

## Readiness and status gates

These commands read state and never write to it. They print fixed codes, not hostnames,
bucket names, or requester data, and they exit nonzero when the dependency is unsafe.

| Command | Behavior | Source | Tests |
|---|---|---|---|
| `check_correspondence_readiness [--json]` | Runs the storage, scanning, broker, and event checks from `readiness_snapshot()` and exits nonzero unless `ready`. The officer-only API endpoint returns the same result ([correspondence](../domain/correspondence.md#operations-and-readiness)). | `apps/correspondence/management/commands/check_correspondence_readiness.py` | `apps/correspondence/tests/test_operations.py` |
| `gmail_inbound_status [--json]` | Reports the inbound poller's status and freshness from `gmail_inbound_status_snapshot()`. Exits nonzero when unhealthy. The poll metrics it reflects are described in [background jobs](../architecture/background-jobs.md#gmail-poll-telemetry). | `apps/correspondence/management/commands/gmail_inbound_status.py` | `apps/correspondence/tests/test_operations.py` (the `gmail_inbound_status` JSON tests) |
| `check_nextrequest_cutover SOURCE_CSV MAPPING_JSON` | Plans the import in memory, then assesses it. Prints JSON with `ready`, `plan_sha256`, row counts, `blockers`, and `warnings`. Exits nonzero when blocked. | `apps/records/management/commands/check_nextrequest_cutover.py` | `apps/records/tests/test_nextrequest_migration.py` (cutover assessment) |

`check_nextrequest_cutover` is the gate the NextRequest runbook ([`docs/nextrequest-migration.md`](../../docs/nextrequest-migration.md)) uses before activation. No file under `infra/` invokes it, so the release pipeline does not enforce it.

## Data writes with a dry run first

| Command | Default | Commit path | Source | Tests |
|---|---|---|---|---|
| `nextrequest_import SOURCE_CSV MAPPING_JSON --operator USER` | Dry run | `--commit` with `--expected-plan-sha256` from the reviewed run | `apps/records/management/commands/nextrequest_import.py` | `apps/records/tests/test_nextrequest_migration.py` |
| `nextrequest_import_files MANIFEST FILES_ROOT --operator USER` | Dry run (`build_plan`) | `--commit` requires the exact plan digest (`commit_plan`), so the reviewed bytes are the stored bytes | `apps/records/management/commands/nextrequest_import_files.py` | `apps/records/tests/test_nextrequest_files.py` |
| `import_legacy_timeline TIMELINE_JSON --operator USER` | Dry run | `--commit` (`commit_entries`, `commit_corrections`) | `apps/records/management/commands/import_legacy_timeline.py` | `apps/records/tests/test_legacy_timeline.py` |
| `record_vault_scope`, `record_document_reconciliation`, `annotate_legacy_response_gap`, `retire_closed_case_inbox_rows` | Dry run | `--commit` | `apps/records/management/commands/` (same names) | `apps/records/tests/test_vault_scope_recovery.py`, `test_document_reconciliation.py`, `test_legacy_response_gap.py`, `test_retire_closed_case_inbox_rows.py` |

The timeline importer is explicit about its limit: it "records history only." Whether a
migrated acknowledgment satisfies RCW 42.56.520 is a records officer's decision made
through the lifecycle service, not by the import. The post-import evidence commands are
listed in [NextRequest migration](../domain/nextrequest-migration.md#companion-imports).

## Activation

`activate_nextrequest_import BATCH_ID --operator USER [--source-id ID] --confirm-live-triage`
runs preservation and system routing for imported requests that are still open. It
excludes closed statuses, and it refuses with "No activation performed" unless
`--confirm-live-triage` is passed. The flag is a statement that the live hold and
notification integrations were verified first. Each activation goes through
`activate_legacy_request()`. Routing is the same registry-driven path described in
[collection and Vault](../domain/collection-and-vault.md#the-system-registry-and-triage).

## Registry and test fixtures

- `seed_systems` upserts the record-system registry by slug. Re-running it keeps district
  edits to custodian and retention fields. Its tests are in `apps/records/tests/test_systems.py`.
  The custodian and retention values it seeds are described in
  [collection and Vault](../domain/collection-and-vault.md#the-system-registry-and-triage).
- `seed_casework_test_fixture` writes deterministic synthetic casework for local and CI
  browser tests. It refuses to run unless `PRR_TEST_FIXTURE_SEED_ENABLED` is true. The
  browser lanes that use it are in [testing and quality gates](testing.md#lanes).

## Change guidance

- A new command that writes live data should follow the dry-run-then-commit shape above,
  and its commit path should be pinned to a digest or an explicit confirmation flag when
  the input can change between review and commit. Add it to the relevant test module.
- Keep output fixed-shape. Readiness and status commands must not print hostnames, bucket
  names, exception text, or requester data.
- Before running any command in this page against live data, read the command's module
  docstring. Do not infer its behavior from the names of the flags alone.

Focused check for changes to the readiness, status, or registry commands:

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py test apps.correspondence.tests.test_operations apps.records.tests.test_systems --settings=config.test_settings
```
