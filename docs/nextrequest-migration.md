# NextRequest migration and cutover

This is an evidence-preserving migration, not a generic CSV import. A dry-run
normalizes an exact UTF-8 source export and mapping into a SHA-256 plan. A
Records Officer can commit only that reviewed digest. Import creates no email,
receipt, custodian notice, camera hold, or other external side effect. Open
requests are put through live preservation and RecordSystem routing only by a
separate, explicit activation command.

The importer is intentionally mapping-driven because a representative PSD 401
NextRequest export and vendor data dictionary are not in this repository. Do
not rename a guessed template to match production. Start from
[`examples/nextrequest-mapping.v1.json`](examples/nextrequest-mapping.v1.json)
and map it to the district's verified export headers.

## Evidence required before the first rehearsal

The accountable Records Officer and system owner must obtain and retain:

1. A complete NextRequest request export, export timestamp, tenant identity,
   selected filters, row count, and vendor data dictionary.
2. A separate inventory of source attachments and correspondence, including
   source request ID, immutable source object ID, filename, byte size, source
   digest if available, and content category. The CSV importer blocks every
   row whose attachment count is nonzero, unless the mapping carries the
   explicit acknowledgement described below. Files must go through a separately
   approved manifest, generated storage keys, size bounds, quarantine, and
   ClamAV before they can be linked. Requester uploads must remain
   `IntakeAttachment` evidence and must never be converted into responsive
   `RecordItem` rows merely because they came from NextRequest.
3. Evidence for historic initial responses and terminal actions: delivered
   timestamps, correspondence content, provider/source IDs, closure timestamp,
   and a substantive closure reason. Delivery-backed statuses
   (`acknowledged`, `clarification`, and `denied`) cannot be reconstructed from
   one CSV row and are rejected. Map these rows to neither a convenient nor a
   more advanced state; reconcile their correspondence evidence first.
4. Confirmation of source timestamp semantics and timezone. Ambiguous daylight
   saving timestamps require an explicit UTC offset. The mapping's IANA zone is
   used only for unambiguous naive timestamps.
5. A tested database and object-storage backup/restore, a written rollback
   owner, the cutover freeze window, and the process for a final delta export.

Requester purpose or category must not be added to the mapping unless it was
already volunteered and is needed for a separately reviewed JLARC
classification. Migration is not permission to collect new requester PII.

## Attachments that stay in the source system

The CSV path blocks every row reporting attachments, because files need the
separately approved manifest, generated keys, quarantine, and ClamAV. That
default does not change.

There is one narrow, explicit exception for a source system that is being
retired before its files can be moved. Adding this block to the mapping does
**not** migrate a file and does not weaken any attachment rule; it records that
the district knowingly left the files where they are, named the system holding
them, and said why:

```json
"attachments_retained_in_source": {
  "acknowledged": true,
  "source_system": "NextRequest (agency.nextrequest.com)",
  "reason": "At least 40 characters stating why the files are staying and when they move."
}
```

Omit the block and attachments block the row, exactly as before. Include it and
each row's true source file count is retained on `LegacyRequestImport.
source_attachment_count`, inside the row digest that the approved plan
SHA-256 covers, in the batch audit entry, and as a status event on the case
timeline telling the officer the files are elsewhere and the case is not
complete on the migrated record alone. `check_nextrequest_cutover` then names
every such request as a warning, so the signed cutover record states exactly
what did not move.

Never write a zero attachment count to get a row past the importer. The count
is legal evidence about a public record, and a migration record asserting "no
attachments" about a case that has them is worse than a blocked row.

## Migrating the files themselves

`nextrequest_import_files` is the separately approved path that moves the files
the row importer left behind. It is dry-run-only unless both `--commit` and the
exact reviewed plan digest are supplied, exactly like the row importer.

It takes a manifest CSV of already-staged bytes with the columns
`document_id, request, classification, visibility, title, path, bytes, sha256,
status`, plus the directory those paths are relative to. Every staged file is
re-hashed and must match its manifest digest, so a file that changed after the
manifest was built blocks the run rather than being stored under a plan that no
longer describes it.

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py nextrequest_import_files \
  /controlled/export/manifest.csv /controlled/export/files \
  --operator records-officer
```

Three behaviours are deliberate and should not be "optimized" away:

**Archives are expanded, not stored.** `preview.py` renders PDF and nothing
else, so a stored ZIP cannot be previewed, OCR'd, or redacted - and a reviewer
who cannot open a file cannot withhold from it either. Members are stored
individually with the archive path preserved in `original_filename`. Expansion
is bounded on member count, expanded bytes, and nesting depth
(`MAX_ARCHIVE_DEPTH`), and refuses absolute, `..`, and encrypted members.
`--no-expand-archives` exists for a source system whose archives are themselves
the record, and it stores containers no reviewer can open.

**Byte-identical files collapse onto one stored item.** The district's export
carried heavy duplication - the same document attached to one request up to six
times. Every collapsed source document still gets its own `LegacyFileImport`
row recording what it duplicated, so dedupe is visible in the record instead of
looking like files went missing.

**An unresolved classification blocks the run.** `hand_gathered` becomes a
`RecordItem` and `requester_upload` becomes an `IntakeAttachment`; a row marked
`ambiguous` stops the run unless `--ambiguous-classification` names the
resolution explicitly. The two directions are not symmetric: filing a staff
collection as an intake attachment merely hides it, while filing a requester
upload as a responsive record invents a public record out of the requester's
own evidence.

A file that NextRequest had already released to the requester still arrives
`unreviewed`. Its source visibility is retained as provenance, but release from
this application is governed by this application's review.

The import writes a status event on each case superseding the earlier
"files remained in the source system" note, and audits the plan and manifest
digests. It does **not** touch `LegacyRequestImport.source_attachment_count`:
that counts what NextRequest held at export time, which stays true regardless
of what is migrated afterwards. Re-running is idempotent - an item already
recorded for a `(source document, member path)` pair is skipped.

## Rehearsal

Use a restored database only on a separately authorized protected rehearsal
host approved for production requester/FERPA data, with every inbound,
outbound, provider, and public endpoint disabled. Never put the export or a
production-derived restore on an ordinary developer machine; local development
uses synthetic fixtures only. The command is dry-run-only unless both
`--commit` and the exact reviewed digest are supplied.

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py nextrequest_import \
  /controlled/export/requests.csv \
  /controlled/export/mapping.json \
  --operator records-officer
```

The output contains no requester fields. Review every blocked issue, row count,
source digest, mapping digest, and plan digest. Correct the source mapping or
source evidence; never edit the application database to make a plan pass.

After two staff members compare the plan to source-system totals and spot-check
open, closed, after-hours, DST, mail-only, and installment cases, commit the
same bytes and mapping:

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py nextrequest_import \
  /controlled/export/requests.csv \
  /controlled/export/mapping.json \
  --operator records-officer \
  --commit \
  --expected-plan-sha256 PLAN_SHA256_FROM_REVIEWED_DRY_RUN
```

The commit is all-or-nothing and idempotent. It preserves the immutable request
wording, source ID/number/status, source-row digest, historic timestamps,
historic estimate as migration evidence, requester response details, and the
mapping snapshot. It calculates the statutory deadline synchronously and uses
the canonical lifecycle service for every imported workflow transition. It
does not make the historic estimate an active-system estimate; the readiness
report warns staff to verify the original requester correspondence before any
human estimate action.

## Activate open requests

First verify that live camera/Vault, broker, and staff-notification settings
point to the intended rehearsal or production environment. Then activate one
open source request as a canary:

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py activate_nextrequest_import BATCH_UUID \
  --operator records-officer \
  --source-id SOURCE_REQUEST_ID \
  --confirm-live-triage
```

Inspect the preservation, collection-task, notification, audit, and deadline
results. Then omit `--source-id` to activate the remaining open rows. Activation
is idempotent: the durable one-to-one activation, camera preservation, and
request/system task boundaries prevent retries from duplicating work.

## Cutover gate

Run the gate against the exact final export and mapping:

```bash
PYTHON_DOTENV_DISABLED=1 python manage.py check_nextrequest_cutover \
  /controlled/export/requests.csv \
  /controlled/export/mapping.json
```

It exits nonzero if any row is blocked, the exact plan was not committed, the
committed source IDs/count differ, or an imported open request was not
activated. Warnings remain human review items and must be attached to the
cutover record.

Before switching intake links or disabling NextRequest, the Records Officer
must also sign off on source-vs-target totals and status counts, sampled request
timelines and PII, attachment/correspondence reconciliation, open five-day
clocks, active estimates, installment/download access, fee balances, audit
events, backups/restores, and the final delta. Keep NextRequest read-only until
records retention and counsel approve decommissioning; this tool does not
delete or alter the source system and has no automated cutover switch.
