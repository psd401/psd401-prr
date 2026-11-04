---
type: Domain
title: Collection, the System Registry, and Vault Ingest
description: How a PRR case finds its records - the RecordSystem registry and triage-created CollectionTasks, the human-reviewed Google Vault count and collection, and the resumable ingest of Vault exports into immutable, scanned RecordItems. Covers the gates, states, and tests for each step.
tags: [domain, collection, vault, record-systems, ingest, google-vault]
timestamp: 2026-09-24T00:00:00Z
openwiki:
  roles: [domain, integration, workflow]
  change_kinds: [vault, collection, record-system, ingest]
  source_paths: [apps/records/vault_counts.py, apps/records/vault_collections.py, apps/records/vault_ingest.py, apps/records/vault_metadata.py, apps/records/tasks.py, apps/records/triage.py, apps/records/models.py, apps/integrations/vault/client.py, apps/api/resources/record_systems.py, apps/api/resources/collection_tasks.py]
  symbols: [VaultCountRun, CollectionTask, VaultIngestRun, VaultMessageArtifact, request_vault_count, queue_vault_collection, start_vault_collection, ingest_completed_export, route_to_systems, poll_vault_exports, ingest_vault_export]
  test_paths: [apps/records/tests/test_vault_counts.py, apps/records/tests/test_vault_collections.py, apps/records/tests/test_vault_ingest.py, apps/records/tests/test_vault_tasks.py, apps/records/tests/test_vault_scope_recovery.py, apps/records/tests/test_vault_custodian_metadata.py, apps/api/tests/test_vault_counts.py, apps/api/tests/test_collection_tasks.py]
  invariants:
    - "A Vault count estimates a search universe; it never creates a hold, export, collection task, status change, or requester message."
    - "A Vault collection starts only from a completed count, with no blocking collection deposit gate, the same reviewed scope hash, a non-zero count, and a current Records Officer."
    - "Every Vault message becomes a RecordItem only after its raw bytes are retained and scanned; a RecordItem is never a responsiveness decision."
    - "The only unattended Vault action is the automatic entire-organization count from an intake intent proposal, and it is limited to a count."
  validation_commands: ["PYTHON_DOTENV_DISABLED=1 python manage.py test apps.records.tests.test_vault_counts apps.records.tests.test_vault_collections --settings=config.test_settings"]
---

# Collection, the System Registry, and Vault Ingest

Collection is how a PRR case gathers candidate records from the district's
systems. It has three parts: a registry of systems, a per-system **collection
task**, and a Google Vault path that counts, collects, and ingests Gmail and
Drive exports. Every step is human-initiated except the count the intake
advisory may request. Records found here go to review, and review decides
responsiveness.

## The system registry and triage

`RecordSystem` is the district's registry. Each system has a custodian title,
retention profile, triage keywords, and an adequate-search checklist. It is
seeded by `manage.py seed_systems`, and a re-seed keeps district-entered
custodian and retention edits. Staff CRUD is in
[`apps/api/resources/record_systems.py`](../../apps/api/resources/record_systems.py),
which blocks deletion of a system that a collection task references.

Each system's `search_checklist` is the adequate-search procedure for that source. Triage
copies it into the collection task's instructions (`apps/records/triage.py`), and staff
alerts carry it too ([background jobs](../architecture/background-jobs.md#staff-alerts)).
Some seeded entries are off-system custodian sources with `RecordSystem.Integration.MANUAL`
and no connector. The personal-devices entry is one. Its checklist points to the human
personal-device search directive in `templates/sops/personal-device-search.md`. That SOP is
not rendered by code. Before changing registry or triage scope, read
[`docs/custodian-gap-analysis.md`](../../docs/custodian-gap-analysis.md), which
`AGENTS.md` names as required reading for that change.

Intake calls `triage.route_to_systems()` in
[`apps/records/triage.py`](../../apps/records/triage.py). It creates one manual
`CollectionTask` per matching system. An `urgent_hold` system, such as bus video,
also sends an immediate preservation alert. The footage path is separate. See
[Footage triage](footage.md).

A `CollectionTask` has a `kind` (`vault_mail`, `vault_drive`, or `manual`), a
`state` (`pending`, `running`, `ingesting`, `complete`, or `failed`), and an
optional `assigned_to`. Changing `assigned_to` is the one explicit handoff into
the [work queue](work-queue.md). Tasks are not deleted, because they are the
working record.

## The Vault path

Google Vault is reached through the keyless, delegated identity in
[`apps/integrations/vault/client.py`](../../apps/integrations/vault/client.py).
The module docstring lists the flow: matter, hold, export, poll, and download.
Django owns every decision around it.

```mermaid
flowchart TD
    A[Count request by Records Officer or intake intent] --> B[VaultCountRun pending]
    B --> C[execute_vault_count runs start_vault_count]
    C --> D{Count complete}
    D -->|no| E[Count failed or retried]
    D -->|yes| F[Officer reviews scope and count]
    F --> G[queue_vault_collection with the reviewed input hash]
    G --> H[CollectionTask pending and execute_vault_collection]
    H --> I[Hold and export created, state running]
    I --> J[poll_vault_exports every 10 minutes]
    J --> K[ingest_vault_export, state ingesting]
    K --> L[RecordItems, state complete]
```

Caption: the Vault path. Only the count from an intake intent can start
without a named officer, and it stops at the count.

### Count

`VaultCountRun` in [`apps/records/models.py`](../../apps/records/models.py)
keeps the provenance of a count: scope, custodian emails, date range, input
SHA-256, trigger (`manual` or `automatic`), and the Vault matter. `request_vault_count()`
creates it. `start_vault_count()` and `advance_vault_count()` in
[`apps/records/vault_counts.py`](../../apps/records/vault_counts.py) run it.
`vault_count_enabled()` reports whether the delegated identity is configured. The
count does no more than estimate. It creates no hold, export, collection task, or
requester message.

The staff API serializers in [`apps/api/vault_counts.py`](../../apps/api/vault_counts.py)
enforce the scope rule before a count is requested. `VaultCountRequestSerializer`
requires at least one custodian account for an `accounts` scope and forbids custodian
accounts for an `entire_org` scope, and it rejects a date range that ends before it starts.
`VaultCountRunSerializer` is read-only and exposes the run's provenance and the linked
collection task's state and retry eligibility. Its focused test is
`apps/api/tests/test_vault_counts.py`.

### Query construction

`vault_query_from_intent()` in `apps/records/vault_counts.py` assembles the Vault query
text from a proposal's terms. Each required term is quoted and joined with AND. The
alternative terms become one parenthesized OR group, and any term that repeats a required
term is dropped. The staff UI builds the same string with `proposedVaultQuery()` in
[`frontend/src/vaultQuery.ts`](../../frontend/src/vaultQuery.ts), so the officer's Count
button and the automatic intake count scope the same search. Both suites read one shared
case file, `frontend/src/vaultQueryCases.json`. A change to either side that is not
mirrored fails `VaultQueryFromIntentTests` in `apps/records/tests/test_scope_conversation.py`
or `frontend/src/vaultQuery.test.ts`. The UI side of the contract is described in
[staff frontend](../architecture/staff-frontend.md#helpers-that-mirror-server-contracts).

When the officer types a count into a letter, `scopeCountNotice()` in
`frontend/src/scopeCountNotice.ts` compares it with the latest Vault count. It warns
when the two differ, or when a count is still running. It never blocks sending, because
the district chose to leave that judgment with the officer. Its test is
`frontend/src/scopeCountNotice.test.ts`.

### Collection

`queue_vault_collection()` in
[`apps/records/vault_collections.py`](../../apps/records/vault_collections.py) is
the human gate. It requires, in order:

1. Vault configured.
2. No open deposit gate for the collection scope (`assert_deposit_gate_clear`; see
   [fees and deposits](fees-and-deposits.md#deposit-gates)).
3. A `COMPLETE` count with its matter evidence.
4. An `expected_input_sha256` equal to the count's hash. If the scope changed
   after review, it refuses.
5. A non-zero total count.
6. A current Records Officer.

It creates one `CollectionTask` per count. A failed task that has no export can
be requeued. The task's instructions say that an entire-organization scope does
not claim an organization-wide hold. Named custodians are refined later for a
targeted hold.

### Export polling and ingest

The `poll_vault_exports` beat task (every 10 minutes) advances running exports
and reconciles stale ingest claims. `ingest_vault_export` in
[`apps/records/vault_ingest.py`](../../apps/records/vault_ingest.py) is resumable:

- `VaultIngestRun` holds a lease token and heartbeat. A stale claim is
  reconciled, not duplicated.
- `VaultExportObject` records each file of the export with its sizes and digests.
  Archives are bounded (member count and expanded bytes), and messages are capped.
- Each message is retained as raw bytes before any conversion. `VaultMessageArtifact`
  holds the digest and the quarantine and scan state.
  `convert_message_to_searchable_pdf()` renders the message to a text PDF and
  OCRs it, and the result becomes a `RecordItem`.
- Duplicate messages are detected with `_message_duplicate()`.
- Custodian facts come from the export's metadata CSV. `vault_metadata.py`
  reads it so that one message names every account it was delivered to.
  `RecordItemCustodian` stores those rows. Each message's parsed header facts and
  body digest are stored by `parse_email_metadata`, and the review tags derived from
  them are in [record facts and tags](record-facts-and-tags.md).

A completed ingest starts the record-review advisories described in
[Advisory AI](advisory-ai.md). Those are proposals only.

## Superseding an over-broad collection

A Vault export can bring in far more than the request asked for. The Records Officer
can take that collection's undecided records out of review without deleting them.
`supersede_collection()` in
[`apps/records/collection_supersession.py`](../../apps/records/collection_supersession.py)
does the work. The staff endpoint is `POST /api/staff/collection-tasks/{id}/supersede/`
(`CollectionTaskViewSet.supersede` in
[`apps/api/resources/collection_tasks.py`](../../apps/api/resources/collection_tasks.py)).
Its capability entry, `collection_tasks.supersede`, is `high` risk and requires human
confirmation, so an agent can only propose it. See [agent parity](../architecture/agent-parity.md).

```mermaid
stateDiagram-v2
    [*] --> unreviewed: ingest creates RecordItem
    unreviewed --> superseded: supersede_collection (Records Officer)
    superseded --> responsive: review_record (one record at a time)
    superseded --> unreviewed: collection finds same message (restore_superseded_record)
    unreviewed --> responsive: review_record
```

Caption: `superseded` is a `RecordItem.ReviewState`. Reviewed records do not enter it.

The rules, all enforced in `supersede_collection()`:

- Only a current Records Officer may supersede, and the reason must be at least
  `MIN_SUPERSESSION_REASON` (20) characters after trimming.
- Only records of that task still `unreviewed` change. A record a reviewer already decided is
  left alone. Nothing is deleted, and the records keep their custody chain.
- Each change writes one `record.reviewed` audit row per record. A
  `CollectionSupersession` row records the reason, the actor, and the count. The
  `CollectionSupersession` model is append-only (`AppendOnlyModelMixin`). A
  `collection.superseded` audit row and a requester-hidden `StatusEvent` follow, and work
  reconciliation is scheduled for the affected records.
- A second call on the same task changes nothing and writes no new supersession row.

Superseded records leave the working set in these places:

- The record-item list hides them unless `review_state=superseded` is requested. Detail
  endpoints still return them (`apps/api/resources/records_misc.py`).
- `bulk_review_records` refuses them, because each must be reconsidered on purpose.
  `review_record` can still decide one at a time (`apps/records/review.py`).
- Installment packaging refuses them (`apps/records/production.py`). See
  [installments and release](installments-and-release.md).

When a later collection finds the same message, Vault ingest calls
`restore_superseded_record()` from `_mark_duplicate()` in
[`apps/records/vault_ingest.py`](../../apps/records/vault_ingest.py). The record returns to
`unreviewed` under the new task, its reviewer fields are cleared, and the restore is audited
as `record.restored_by_collection`. A narrower re-collection therefore cannot quietly hide a
message that it still considers relevant.

Focused test: `apps/records/tests/test_collection_supersession.py`.
`SupersedeCollectionServiceTests` covers scope, append-only evidence, the no-op retry, the reason
and role gates, release refusal, and bulk-review refusal. `SupersedeCollectionApiTests`
covers the endpoint status codes and the list and retrieval behavior. The test
`test_a_recollected_message_returns_to_review` in `RecollectionRestoresSupersededRecordsTests`
covers the restore path.

Minimal validation: `PYTHON_DOTENV_DISABLED=1 python manage.py test apps.records.tests.test_collection_supersession --settings=config.test_settings`.

## Automatic count from intake intent

With `PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED` on, a completed intake intent
proposal that maps a request to an active Vault-backed system runs one
entire-organization count with `trigger=automatic`. This is the only unattended
Vault action. It may create the count's matter, but it never holds, exports,
collects, changes status, or reaches the requester. AGENTS.md rule 11 records the
district decision and its accepted volume risk. See [Advisory AI](advisory-ai.md).

## Change guidance

- Any new Vault operation must sit behind the same count, review hash, and officer
  gate. Do not widen the automatic path to another operation.
- Keep the expected-hash check. It is what stops a collection from running on a
  scope a person did not review.
- For ingest changes, keep retention before conversion, and keep
  `ingest_vault_export` idempotent under re-runs.
- Use `apps.records.tests.test_vault_*` for ingest behavior. Use
  `apps/api/tests/test_vault_counts.py` for the staff API contract.
- Supersession changes must keep the evidence append-only and keep the restore path in
  `_mark_duplicate()`. Run `apps.records.tests.test_collection_supersession` with them.

Related: [Requests](requests.md) (the case that owns these tasks),
[Work queue](work-queue.md) (collection and record-review work items),
[Compliance](compliance.md) (human-only and count-only rules).
