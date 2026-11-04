# Phase 2 production handoffs

The Phase 2 application workflow is implemented, but code completion is not
authority to enable legal communications, fees, model processing, or a data
cutover. The following gates require named district owners and evidence from
the production environment.

## Counsel and Records Officer

- Approve every active requester-facing template, including installment
  notice version 3 and no-records/out-of-retention closure version 1.
- Approve the no-records search checklist, later-located-record procedure, and
  the retention-schedule references staff may use. See
  `docs/no-records-closure.md`.
- Adopt and enter the applicable fee schedule before any assessment or fee
  message. The application will not infer a schedule or treat statutory maxima
  as district policy.
- Review the active exemption taxonomy and provide distinct second-review
  evidence where a citation requires it.
- Name the Records Officers authorized to approve/queue correspondence,
  activate legacy imports, classify JLARC data, and record annual supplements.

## Google Workspace and identity administrators

- Configure direct Google OIDC, approved existing group-to-role mappings, and
  the keyless read-only Groups Reader described in
  `openwiki/architecture/google-identity.md`.
- Provision least-privilege Gmail and Vault identities without reusing the
  Groups credential, the shared-MCP identity, or a staff token.
- A dedicated service account is the permanent Vault subject, provisioned
  2026-10-09 with the district-wide `PSD-Vault` admin role. It replaces the
  interim personal subject the CIO selected on 2026-08-10. Production
  switched on 2026-10-12, after the service account was added as a
  collaborator on the three open app-created matters, and a live count
  completed under it. Hold and export polling on those three matters has not
  yet run under the service account: confirm that the next hold or export
  poll on each one succeeds. The account in use is shown on the setting
  below. The subject is the in-app "Vault administrator account"
  setting (`PRR_VAULT_ADMIN_USER`): an Application Admin changes it on
  Configuration > District settings with a written reason, and it takes
  effect without a deploy or restart. `docker-compose.deploy.yml` carries no
  default for it; a blank setting stops Vault collection rather than acting
  as a named person. Before changing it, add the new subject
  as a collaborator on every open app-created matter; a subject without matter
  access cannot poll that matter's holds or exports. The setting's Verified
  status only confirms that credentials can be built and makes no Vault call,
  so confirm the change with a live count. Do not use the retired dev runtime;
  handle its credential removal in the separate decommission. Record the
  rotation evidence outside this repository.
- Supply production values for the documented `PRR_GOOGLE_*`, `PRR_GMAIL_*`,
  `PRR_VAULT_*`, and `GOOGLE_APPLICATION_CREDENTIALS` settings through the
  deployment configuration. Deployed Google access is keyless; private
  service-account key files are forbidden.
- Begin with global Gmail, inbound polling, outbound delivery, and staff
  notifications disabled. Activation or routine config rotation must wait for
  the tested, globally locked deployment mechanism in `docs/aws-deploy.md`;
  these are requirements for that mechanism, not a manual restart path. Run the
  read-only `python manage.py verify_gmail` check, obtain the named activation
  approval, then have the mechanism set only `PRR_GMAIL_ENABLED=true`, leave
  both direction switches and `PRR_STAFF_NOTIFICATIONS_ENABLED` false, and
  recreate web/worker/beat together. At that
  stage `python manage.py check_correspondence_readiness --json` must report
  every fixed-code check ready except the deliberate
  `outbound_transport: disabled` result. Enable and verify exactly one approved
  mail surface at a time; require the final acceptance result appropriate to
  that surface before enabling another. Staff alerts require the synthetic-case
  acceptance and named manual monitoring coverage in `docs/gmail-transport.md`.
- Run one representative Vault export through download, resumable ingest,
  archive bounds, per-message quarantine and ClamAV classification, message
  rendering/OCR, deduplication, and review-queue creation. Exercise both clean
  promotion and infected/unavailable fail-closed outcomes, then retain the
  resulting audit/status evidence.

## Cloud and operations owners

- Provision the private object store, encryption/KMS policy, database, Redis,
  task workers, ClamAV, OCR/PDF tools, TLS/origin, monitoring, and recovery
  procedures described in the deployment documentation. The automated
  PostgreSQL/audit backup timer and manifest-marked uploads were verified
  through 2026-08-18; retain evidence from the isolated restore drill in
  `docs/backups.md` before any migration-bearing or data-destructive
  deployment. A migration-free release still requires a fresh complete backup
  inside its serialized deployment transaction.
- Confirm web, worker, and beat use the same release and settings. Exercise
  object integrity verification, quarantine fail-closed behavior, scoped
  installment download expiry and notice-bound renewal, package/exemption-log
  manifests, and backup restoration in the district account.
- Keep `PRR_CORRESPONDENCE_OUTBOUND_ENABLED=false` and
  `PRR_GMAIL_INBOUND_POLL_ENABLED=false` until identity, recipient, DNS/reply,
  readiness, and staff-review evidence is complete. Have the approved
  deployment mechanism enable one surface and recreate web, worker, and beat
  together under the runbook; do not bypass its global lock with a manual
  restart.
- Establish alerts and ownership for missed statutory responses, stuck/failed
  correspondence, Vault failures, malware-scanner failure, storage-integrity
  failure, and stale collection work.

## NextRequest migration owner

- Provide a representative and then final export plus the separately governed
  attachment/correspondence manifests; do not place source attachments into
  the generic CSV path.
- Review the mapping file, run the default dry run, approve the exact plan
  SHA-256, rehearse commit and explicit activation only on a separately
  authorized protected recovery/rehearsal host approved for production
  requester/FERPA data, with every integration disabled, and reconcile
  counts/status/timestamps/audit evidence. Ordinary local development uses
  synthetic fixtures and must never receive the export or restored data.
- Run the final delta and `check_nextrequest_cutover` against that exact source,
  mapping, and plan. Do not cut over while any imported open request lacks
  activation or any reported count/digest differs. Follow
  `docs/nextrequest-migration.md`.

## Records Officer: pre-deduplication custodian copies

Records ingested before Message-ID deduplication existed can hold one message
several times, once per mailbox it was collected from.
`backfill_record_email_metadata` reports these rather than guessing: it fills
in metadata for every retained message it can read, and leaves a record
unchanged when another record of the same request already carries the same
Message-ID *and* body digest. The 2026-09-22 production run filled 2328
records and named 1324 such copies.

`fold_duplicate_record_copies` resolves them, in the name of a Records Officer
or reviewer given on the command line. Dry run first:

```bash
python manage.py fold_duplicate_record_copies --reviewed-by <username>
python manage.py fold_duplicate_record_copies --reviewed-by <username> --commit
python manage.py fold_duplicate_record_copies --reviewed-by <username> --request PRR-2026-0123
```

Reading the report:

- `examined` is every record with no Message-ID that still retains its raw
  message — the set the backfill left behind.
- `would fold` / `to fold` lists `fold <copy> -> <survivor> <case> <Message-ID>`.
  Committing gives the survivor the copy's custodians, so it names every
  mailbox the message was found in, marks the copy `Duplicate` through the
  ordinary per-record review service, and writes one `record.copy_folded`
  audit row per copy plus one `record.copies_folded` row per run.
- `skip <record> <case>: <reason>` names every record left alone. A copy is
  skipped when it already carries a human review decision, carries an active
  redaction, was already released in an installment, has no body digest to
  corroborate its Message-ID (an attachment-only message never folds), has no
  surviving record carrying the message, or would fold into a survivor a human
  already marked duplicate.
- `unusable` counts records whose retained bytes failed their recorded digest
  or could not be parsed. They are named on stderr and never guessed at.

Within one run, the dry run reaches the same decision for every record that
`--commit` would. Between a dry run and a later commit a reviewer may have
decided a copy; every fold re-reads the copy and its survivor under a lock at
the moment of writing and steps back if either changed, reporting it as
`FAILED ... changed since planning` rather than overruling anyone. A folded
copy's retained Vault message is re-pointed at the survivor, as ingest does for
a duplicate found at arrival, so a later collection of the same message
attaches its custodian to the record under review. Nothing here communicates, releases, or denies,
and no background task may invoke it — a folded copy shows as `Duplicate` in
the staff workspace and can be re-reviewed one record at a time.

## Records Officer: per-mailbox custodians from export metadata

Every Google Vault mail export retains a `<export>-metadata.csv` beside the
MBOX/ZIP objects, with one row per copy of every exported message and the
columns `Rfc822MessageId`, `GmailMessageId`, `FileName`, `Account`. `Account`
is Google's answer to which mailbox held the message. Ingest now retains and
parses that file *before* any message, so every record — created or
deduplicated — names every mailbox the export says it was found in, even when
the collection task's own `custodian_emails` list is empty. That empty list is
how PRR-2026-0052 ended up with no custody evidence at all.

Only accounts whose domain is in `PRR_CUSTODIAN_ACCOUNT_DOMAINS` are ever
written. The district's exports sweep student accounts as well as staff ones;
a student mailbox is never recorded as the custodian of a public record.
Accounts outside the list are counted in the report and never listed by
address. The setting is deliberately separate from
`PRR_DISTRICT_EMAIL_DOMAINS` so widening the `external_sender` tag can never
silently widen what gets written as evidence.

A missing, unreadable or unparseable CSV degrades to the previous behaviour
and is recorded on the ingest run's export snapshot under
`custodian_metadata`; it is never a failure of an otherwise complete export.

### Which mailbox a copy proves: `Delivered-To` versus the CSV

The metadata CSV is keyed on `Rfc822MessageId` alone, so its account set
describes **every copy** of that message at once. That set is the right thing
to attach only when the record it is attached to is the **sole live record**
of its request carrying that Message-ID — that is, when every other copy has
been folded into it. While several live records share a Message-ID they are
distinct personalized copies (a mail-merged bulk notification delivered to a
hundred mailboxes, each body slightly different, which the dedup fold
correctly leaves as separate records). Attaching the shared set to any one of
them would say that copy sat in all hundred mailboxes.

What each copy *does* prove is its own `Delivered-To` header, written by the
delivering server and naming the one mailbox it delivered into. So:

- Every message attaches its own `Delivered-To` to the record it lands on,
  whether that record was created or deduplicated onto, and nothing else from
  that copy. Nothing is lost by stopping there: a message the fold really does
  collapse is collapsed one copy at a time, and each copy contributes its own
  mailbox as it is processed, so the surviving record still names every
  mailbox a copy came from.
- A `Delivered-To` is a header a sender can write. When the CSV names the
  mailboxes that held a message, the header must be one of them; a header
  naming a mailbox the CSV never saw is treated as absent.
- The CSV's Message-ID set is attached **once, when the run has read every
  message of the export**, and only to a record that is then the sole live
  record of its request carrying that Message-ID. It cannot be decided per
  message: the first copy of a bulk mailing a run reads is momentarily the
  only live record carrying that Message-ID — the other ninety-nine have not
  been read yet — so deciding then would put every recipient's mailbox on
  whichever copy was read first. The end-of-run pass takes the request's row
  lock so two runs of one request cannot each decide it blind to the other's
  copies. The run records how many records received a set under
  `custodian_metadata.csv_sets_attached`.
- A message with no single usable `Delivered-To` — none at all, several hops,
  a value that is not one address, or one the CSV does not corroborate —
  names nothing of its own; its record gets the task's custodian list and,
  if it is sole when the run ends, the CSV set. The ingest run counts these
  under `custodian_metadata.delivered_to_missing` (counted over the messages
  that pass processed; a resumed run does not re-read the artifacts an
  earlier pass completed).
- Every write still goes through `attach_custodians`, so a `Delivered-To`
  outside `PRR_CUSTODIAN_ACCOUNT_DOMAINS` — a student mailbox — is refused at
  the same boundary as a CSV account.

### Backfilling records ingested before the CSV was parsed

`backfill_vault_custodians` reads the retained CSV for each Vault mail task
and attaches what it says, through the same `attach_custodians` ingest uses.
`--run-by` names the reviewer or Records Officer whose run it is, so the audit
row is attributable; a user who holds neither role is refused. Dry run first:

```bash
python manage.py backfill_vault_custodians --run-by <username>
python manage.py backfill_vault_custodians --run-by <username> --commit
python manage.py backfill_vault_custodians --run-by <username> --request PRR-2026-0123
```

Reading the report:

- `tasks with a retained CSV` / `tasks without a usable CSV` — a task whose CSV
  cannot be used is named and skipped, and the three reasons are reported
  separately because they need different answers: *no retained metadata CSV*
  (the export never wrote one, or it was never retained), *could not be read*
  (one was retained and failed its integrity check or could not be parsed —
  the detail is on stderr), and *names no message* (read, but carrying no
  usable rows). The ingest run's own `custodian_metadata` snapshot records the
  same distinction.
- `records examined` counts the live records of those tasks: review state not
  `Duplicate`, Message-ID present. A record a human already marked duplicate is
  never revived.
- `would attach` / `to attach` lists `attach <record> <case> <n> account(s)`.
  Committing writes one `RecordItemCustodian` row per account and repairs an
  empty primary `custodian` with the first of them. No review state, release,
  redaction or correspondence is touched.
- `skip <record> <case>: <reason>` names every record left alone:
  - *Message-ID absent from this export's metadata CSV* — the CSV cannot say
    where a message it never listed was found.
  - *the CSV names no account in the configured domains* — every account for
    that message was outside `PRR_CUSTODIAN_ACCOUNT_DOMAINS`.
  - *several live records share this Message-ID and this copy has no retained
    raw message* — nothing survives from which to read its own mailbox.
  - *… this copy's retained raw message could not be read* — the object failed
    its integrity check or exceeded `PRR_VAULT_MESSAGE_MAX_BYTES`; the detail
    is on stderr.
  - *… this copy carries no single Delivered-To header naming its mailbox* —
    none, or several hops, neither of which is a delivery fact.
  - *this copy's Delivered-To mailbox is outside the configured domains* — a
    student mailbox, counted and never printed.
- `resolved from this copy's Delivered-To` counts the shared-Message-ID copies
  this pass placed from their own retained message rather than from the CSV.
  Running `fold_duplicate_record_copies` first is still the right move when the
  copies really are one record; it is no longer a prerequisite for naming a
  personalized copy's mailbox.
- `accounts outside the configured domains` is the total declined from CSV
  rows, and `Delivered-To mailboxes outside the configured domains` the total
  declined from copies' own headers, so a run can say how much it chose not to
  attach without naming a mailbox.

Within one run, the dry run reports exactly the counts a commit would
produce. Between a dry run and a later commit a record may have been marked
duplicate; each write re-reads its record under a lock and reports such a
record as `FAILED ... changed since planning` rather than reviving it. A commit writes
one `record.custodians_backfilled` audit row for the run with those counts.
No background task may invoke this command; it is a human operation, guarded
in `test_compliance`.

## Advisory AI owner and privacy/security review

- The CIO selected Amazon Bedrock with the US Nova 2 Lite inference profile.
  The application uses native Converse calls and keyless runtime roles; no AI
  API key or third-party endpoint is deployed.
- A 2026-08-10 note records CIO authorization, but the required provider/data
  approval and representative evaluation evidence below is not retained in
  this ledger. Dev was retired as a deployment target on 2026-08-18, and a
  read-only production audit that day found both effective AI switches enabled.
  Treat the production activation as unresolved until the evidence is retained
  or both switches are disabled. The switches live in `.env.app`; the Compose
  overlay intentionally does not override them.
- **2026-11-03, model acceptance (items 1-3 of the approval prerequisites in
  `docs/advisory-ai.md`).** The district Public Records Officer accepted seven
  US cross-region Bedrock inference profiles for request text and record OCR,
  including student, personnel, legal, and other restricted content:
  `us.amazon.nova-2-lite-v1:0`, `us.anthropic.claude-opus-5-5`,
  `us.anthropic.claude-fable-5-1`, `us.anthropic.claude-sonnet-5-5`,
  `us.anthropic.claude-haiku-5-5`, `us.openai.gpt-6.1-sol`, and
  `us.openai.gpt-6.1-terra`. Basis: every call runs through the district's own
  AWS account under the keyless runtime role; each profile routes only to
  us-east-1, us-east-2, and us-west-2; and the IAM grant names exactly these
  profiles (`infra/cdk/lib/advisory-bedrock.ts`). Because one acceptance
  covers every data classification, no per-classification routing is
  required. Still open: item 5, the representative offline evaluation with a
  written threshold and rollback owner (issue #32) -- choose each kind's model
  from that evidence before pointing a setting at it.
- Evaluate representative request text and OCR, including prompt-injection,
  student, personnel, health, legal, multilingual, empty, and adversarial
  examples. Record accuracy and harm thresholds plus rollback ownership.
- Confirm the model remains advisory only: `applied=false`, no automatically
  submitted human outcome, no status/fee/estimate/correspondence mutation, and
  no raw provider error retention. A completed responsiveness proposal may
  preselect the form value, but a named human must still confirm or change it.
  See `docs/advisory-ai.md`.

## Training and cutover sign-off

- Train intake clerks, liaisons, reviewers, Records Officers, application
  administrators, and on-call operators against their distinct roles.
- Rehearse intake, day-5 response, collection, Vault failure recovery, review,
  second review, raster redaction, fee/payment, installment release/download,
  no-records closure, failed delivery/reconciliation, and later-located-record
  handling.
- Record named approvals, rehearsal dates, unresolved exceptions, rollback
  criteria, support ownership, and the final go/no-go decision outside this
  repository. Off-repo completion must not be inferred from green code checks.

## Environment and accounts still needed (human tasks)

These are the remaining non-code prerequisites. They are operational, not
missing application implementation. Do not bypass a readiness check or activate
a workflow from sample data merely to make an environment appear ready.

- Vault service account w/ domain-wide delegation (scopes in
  `apps/integrations/vault/client.py` docstring) — district has Education
  Standard but Vault + API access is confirmed available.
- Apply `infra/terraform/google-identity` and complete the identity bootstrap in
  [`google-identity-bootstrap.md`](google-identity-bootstrap.md); developers need
  neither Google nor AWS access.
- Deploy `infra/cdk` per [`aws-deploy.md`](aws-deploy.md) for production only. Do
  not deploy the retired district dev site or its identity stack; its later
  inventory and decommission are a separate approved operation. The
  PostgreSQL/audit backup timer is installed and complete manifest-marked S3
  uploads were observed through 2026-08-18. Production application deployment is
  the single CDK deploy command in `aws-deploy.md`; CDK owns the source asset,
  fresh backup, migrations, container replacement, and automatic container
  rollback. Nothing runs between merge and that command. The records retention
  schedule is unconfirmed, so no lifecycle deletion rule exists.
- Local-only `.env` may be seeded from `.env.example`; never commit it.
  Production uses its existing operator-managed `.env` and `.env.app` under
  `aws-deploy.md`; never overwrite either with an example. Neither file decides a
  district-tunable value any more: those are changed on the Settings page
  (Application Admin, written reason, full history). A line for one in an
  operator file looks like a control and silently does nothing once the
  deployment has run `manage.py import_environment_settings`, so the operator may
  delete the moved lines after the first deploy that imports them.
- `SNYK_TOKEN` repository secret, for the MCP security-scan lane that
  psd-dev-standards `standards/07-mcp.md` build standard 6 requires. The
  scanner the standard names as `mcp-scan` was renamed to `snyk-agent-scan`
  and has required an account since v0.6, so its analysis half — the
  prompt-injection and toxic-flow checks on tool descriptions — cannot run
  without one. Get a token from <https://app.snyk.io/account> and add it as a
  repository secret; `.github/workflows/mcp-scan.yml` picks it up with no code
  change. Until then that workflow reports a visible skip rather than a pass,
  so the gap shows in the Actions tab instead of looking green. The token-free
  half of the same tool already gates every pull request inside `psd-ci /
  Backend audits and quarantined tests`: it boots the stdio MCP server and
  enumerates its tools, so a server that stops starting fails CI today.
- **Resolved 2026-10-19 (data owner): what requester text may an MCP tool
  result send to the caller's model provider?** **Answer: yes, both.** Hagel
  (CIO, data owner) decided on 2026-10-19 that MCP tool results may carry
  `subject` and `requester_name`/`requester_email` to the caller's model
  provider. The concise overdue projection now returns all three
  (`OVERDUE_CONCISE_FIELDS` in `integrations/mcp/client.py`), and the eval
  case that forbade them now requires them. The staff serializer still nulls
  requester contact for callers who are not Records Officers. This decision
  does not edit the text of compliance rule 5 or rule 11. The question as it
  was posed, kept for the record:
  - **The case subject.** The staff case list shows `subject` — the AI short
    title or, failing that, the opening of the request description — to every
    list role, decided 2026-09-24. That decision is about who may read it
    *inside the district*. A tool result is different: it leaves for the
    caller's model provider, and when `subject_source` is `description` the
    text is the requester's own wording and can name a student.
  - **Requester contact.** `requester_email` and `requester_name` are PRA
    requester identity, which compliance rule 5 minimizes.

  Where each stood before the decision. `psd_prr_requests_list`,
  `psd_prr_request_get` and the `detailed` overdue format carry both, because they return the staff row
  as the REST API serialized it; that predates the MCP standards work and is
  unchanged by it. The `concise` overdue projection, which is new, omits both
  pending this decision, and `integrations/mcp/evals.py` forbids them so the
  default cannot change silently.

  What each answer costs. If a field may go to a model provider, add it back
  to `OVERDUE_CONCISE_FIELDS`, drop it from that eval case's
  `forbidden_keys`, and record the sign-off here. If it may not, the three
  surfaces that already carry it need the same treatment — a separate change
  with a real blast radius, since `subject` is the scanning label the whole
  agent-facing queue view is built around, so plan it rather than patching it
  in. Either way the decision should name both fields, or the next person
  will re-ask about the one it skipped.
- Ongoing legal review: exemption seed list, letter templates, close-of-business
  receipt policy, and fee schedule.
