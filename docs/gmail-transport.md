# Gmail transport operations

The application uses the Gmail API as `publicrecords@psd401.net` for requester
correspondence. Django retains the legal state machine, delivery leases,
idempotency, inbound evidence, and audit trail; Gmail is only the transport.
The outbound sender and inbound mailbox poller are both implemented as
in-process Celery work. Inbound polling remains disabled by default and is
enabled only in production after deployment verification.

## Identity and data flow

Production proves its exact AWS runtime role to the Google Workload Identity
pool. A bare, non-secret external-account ADC file supplies that AWS proof. The
application then impersonates the dedicated
`prr-gmail@psd401-prr-identity.iam.gserviceaccount.com` identity and delegates
only as the pinned `publicrecords@psd401.net` mailbox.

The Workspace grant contains exactly:

- `https://www.googleapis.com/auth/gmail.send`
- `https://www.googleapis.com/auth/gmail.modify`

`gmail.modify` is needed to label inbound messages after durable receipt. No
Gmail private key exists. The Vault and group-reader identities are separate
and must never be reused.

Outbound flow:

1. A human approves correspondence in Django, except for the narrowly guarded
   safety-net acknowledgment and non-statutory submission receipt paths.
2. Django's durable delivery queue and lease prevent duplicate sends.
3. A Celery worker renders the frozen message and calls Gmail.
4. Gmail message and thread IDs are recorded for reconciliation and replies.

Inbound flow:

1. Production beat schedules a mailbox poll; a worker performs the Gmail API
   calls.
2. A durable checkpoint records the Gmail `historyId` for this mailbox and
   environment. Production incrementally reads only messages added to Inbox.
3. A missing or expired checkpoint uses a time-bounded Inbox query; a
   non-production query additionally requires its `PRR/<environment>` label.
4. Raw bytes and Gmail's authoritative message/thread/internal-date metadata
   enter the existing quarantine, MIME, dedupe, and request-matching pipeline
   before the Gmail message receives the processed label.
5. A parsed, non-automated message sent only to the bare mailbox that does not
   match an existing request enters the canonical intake pipeline as an email
   request. Tokenized replies, DSNs, list/bulk/junk mail, invalid senders, and
   empty messages remain in the inbound review evidence instead.
6. The retired development deployment must not poll the shared mailbox or be
   restarted for application work. The retained non-production code boundary
   still restricts a deliberately isolated test poller to its own internal
   environment label.

## Configuration and kill switches

The kill switches are **district settings, changed in the application**, at
Configuration > District settings. Only an Application Admin may change one,
every change needs a written reason, and the change reaches web, worker, and
beat within seconds without a restart or a redeploy - which is what a kill
switch has to do. Each one states its consequence in plain terms before it is
changed, and keeps a permanent history of who changed it, when, and why.

Almost everything in this table is application-managed. The switches moved
first: `PRR_GMAIL_ENABLED`, `PRR_GMAIL_INBOUND_POLL_ENABLED`,
`PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED`, `PRR_STAFF_NOTIFICATIONS_ENABLED`,
`PRR_CORRESPONDENCE_OUTBOUND_ENABLED`,
`PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED`, `AUTO_ACK_SAFETY_NET`, and
`PRR_CLOUDWATCH_METRICS_ENABLED`. The values they operate on followed:
`PRR_GMAIL_MAILBOX`, `PRR_GMAIL_TIMEOUT_SECONDS`,
`PRR_GMAIL_INBOUND_PROCESSED_LABEL`, `PRR_GMAIL_INBOUND_LOOKBACK_DAYS`,
`PRR_GMAIL_INBOUND_STALE_SECONDS`,
`PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST`, and
`PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS`. Setting any of them in `.env.app`
does nothing once this deployment has imported its values; the table's
Production column describes the intended stored value, not a file entry. Each
one's bounds are enforced when an administrator saves it, with the limit in
the refusal, rather than by a container refusing to start.

What is left in `.env.app` is `GOOGLE_APPLICATION_CREDENTIALS` -- the
container's own keyless identity, which everything else is impersonated
*from* -- and `PRR_ENVIRONMENT`. Both must be identical across web, worker,
and beat before containers are restarted. The AWS overlay intentionally does
not set them because a Compose service `environment` entry would override the
operator's `.env.app` value.

Phase C moved the rest, including the service identity itself: the Gmail
service account the application impersonates, the development-only key-file
path, and the CloudWatch region are all Settings-page values now. Rotating
the service account is therefore a change with an accountable name and a
reason on it, and the application checks the new identity against Google on
save -- a read-only check of credentials, the pinned subject, the granted
scopes, and the mailbox profile, which never sends. A failure does not refuse
the save; it marks the row "Unverified" with the reason, because the identity
already in place is the one that does not work.

No private key is involved in any of this. Deployed Gmail access is keyless
impersonation, and `apps/integrations/gmail/client._credentials` refuses to
build a credential from a key file whenever `DEBUG` is false, whatever the
setting says.

The mailbox is the one worth naming twice. It is validated on save as a single
email address of at most 100 characters -- the width of the correspondence
`provider_account` column -- and changing it points the whole transport at a
different mailbox: mail already waiting in the old one is never polled again.

The development column below is an isolated-process safety boundary retained
in code; it is not permission to redeploy the retired dev site.

| Setting | Production | Isolated test process | Purpose |
|---|---|---|---|
| `PRR_ENVIRONMENT` | `production` | `development` | Selects the mandatory non-production recipient allowlist boundary independently of security deployment mode |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/run/secrets/prr-google-runtime-credential.json` | same | Bare keyless AWS federation source |
| `PRR_GMAIL_ENABLED` | `true` after verification | `true` only for a named, controlled send test | Global Gmail credential/client gate |
| `PRR_GMAIL_MAILBOX` | `publicrecords@psd401.net` | same | Pinned delegated mailbox |
| `PRR_GMAIL_IMPERSONATION_TARGET` | `prr-gmail@psd401-prr-identity.iam.gserviceaccount.com` | same | Dedicated target identity |
| `PRR_GMAIL_CREDENTIALS_FILE` | empty | empty | Private-key path; forbidden when deployed |
| `PRR_GMAIL_TIMEOUT_SECONDS` | `30` | `30` | Bounded Gmail request timeout |
| `PRR_STAFF_NOTIFICATIONS_ENABLED` | `true` only after separate acceptance | `false` by default; `true` only for a named, allowlisted staff-alert acceptance | Independent staff-alert send gate |
| `PRR_CORRESPONDENCE_OUTBOUND_ENABLED` | direction-specific | direction-specific | Stop or allow new outbound work |
| `PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED` | `false` during manual rollout | `false` | Independently blocks automatic requester receipts/acknowledgments at queue and claim while preserving human-approved sends |
| `PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST` | empty | explicit test addresses | Comma-separated hard allowlist enforced for every To/Cc/Bcc development recipient |
| `PRR_GMAIL_INBOUND_POLL_ENABLED` | `true` only after inbound verification | **always `false`** | Stop or allow mailbox polling |
| `PRR_GMAIL_INBOUND_PROCESSED_LABEL` | `PRR/Processed` | same | Internal Gmail label applied only after durable receipt |
| `PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED` | `false` | same | When `true`, the same modify call that applies the processed label also removes `INBOX`. Every durably received message leaves the Gmail inbox view, including unmatched and quarantined mail that created no case, so enable it only once staff triage those through the application Inbox queues rather than the mailbox |
| `PRR_GMAIL_INBOUND_LOOKBACK_DAYS` | `7` | same | Bounded resync query window after a missing or expired history checkpoint |
| `PRR_GMAIL_INBOUND_STALE_SECONDS` | `300` | same | Maximum acceptable age of the durable Gmail checkpoint for read-only diagnostics |
| `PRR_CLOUDWATCH_METRICS_ENABLED` | `true` from the AWS Compose overlay | `false` | Publish requester-data-free poll health, outcome, and duration metrics |
| `PRR_CLOUDWATCH_REGION` | `us-west-2` | empty | Region for the `PSD401/PRR` custom metrics; required when telemetry is enabled |
| `PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS` | `nextrequest.com` | same | Exact sender domains, including subdomains, retained for human review but excluded from automatic matching and request creation |
| `AUTO_ACK_SAFETY_NET` | `false` during manual rollout | `false` | Stops the hourly safety-net producer; the independent automated-outbound claim gate still protects already-queued work |

Routine `.env.app` changes are still deployments and must run inside the
mechanism [`aws-deploy.md`](aws-deploy.md) requires. Turning a mail surface on
or off is not one of them any more, and the fixed
`psd401-prr-prod-enable-gmail-inbound` Systems Manager document that used to
do it is retired: it existed only to flip two switches in an operator-held
file safely, and that is now a page in the application with an accountable
name on every change.

To stop outbound while leaving replies flowing, turn off
`PRR_CORRESPONDENCE_OUTBOUND_ENABLED`; to stop polling while leaving sends
flowing, turn off `PRR_GMAIL_INBOUND_POLL_ENABLED`. For a credential,
delegation, or mailbox incident, turn off `PRR_GMAIL_ENABLED`, which stops
both directions at once. None of these needs a container recreate, and there
is no window in which web, worker, and beat disagree: they read one stored
value. Write the reason as if it will be read back to you later, because the
history is permanent and it will be.

Enable one surface at a time. Turn on `PRR_GMAIL_ENABLED` alone first, with
inbound polling, outbound, staff alerts, automated outbound, and the safety
net still off: that enables credential and readiness verification without
consuming a reply or sending a message. The settings store enforces the
ordering rather than trusting it - inbound polling, archiving, and staff
alerts each refuse to turn on until Gmail transport is on, and automated
outbound refuses until outbound is on. Accept each surface and retain its
acceptance evidence before enabling the next. Keep
`PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED` and `AUTO_ACK_SAFETY_NET` off
throughout the manual rollout. Cancel an existing
unsent automatic delivery through the staff UI before creating its
human-approved replacement; never mutate or delete the delivery row directly.

Messages from a configured blocked sender domain are still retained, scanned,
deduplicated, labeled processed, and visible in the inbound review queue. They
cannot automatically match a request or create a new request. A Records Officer
may associate one manually with a substantive audit reason if it is genuinely
case correspondence.

Only production may turn `PRR_GMAIL_INBOUND_POLL_ENABLED` on. Do not restart
the retired dev deployment for application work while its credentials await
separate decommissioning.
Non-production polling remains disabled and restricted in code to
`label:PRR/<environment>` so an isolated test cannot consume an unlabeled
production reply. Any non-production outbound test must retain the application
recipient allowlist.

## Verification before enabling

1. Confirm both keyless JSON files exist under `/srv/prr/secrets`, are mode
   600, and contain no `private_key` field.
2. Confirm `.env.app` names the bare runtime ADC, and the Settings page's
   Gmail service identity is the exact Gmail target above.
3. Run:

   ```bash
   docker compose exec web python manage.py check --deploy --fail-level ERROR
   docker compose exec web python manage.py verify_gmail
   ```

4. `verify_gmail` must report credentials OK, the pinned mailbox subject,
   exactly the two scopes, the matching mailbox profile, and that no message
   was sent.
5. Inside the approved deployment mechanism, after a named operator approves
   the credential gate, set `PRR_GMAIL_ENABLED=true` while keeping both
   direction switches and `PRR_STAFF_NOTIFICATIONS_ENABLED` false, then
   recreate web, worker, and beat together. Run
   `python manage.py check_correspondence_readiness --json`; every dependency
   must now be ready except the deliberate `outbound_transport: disabled`
   result.
6. Confirm migration 0014 provisioned the non-login inbound principal with
   exactly `correspondence.submit_inbound_correspondence` and no token.
7. Have that mechanism enable exactly one production mail surface, recreate all
   three processes, and complete its controlled acceptance before separately
   approving another.
   For any explicitly authorized isolated test process, populate the recipient
   allowlist first. Do not start or redeploy the retired dev site for this
   purpose.

### Staff-alert acceptance and disabled coverage

Before `PRR_STAFF_NOTIFICATIONS_ENABLED=true`, assign a named Records Officer
to manually monitor statutory deadlines, estimate dates, stalled requests,
camera-hold status, and new collection tasks in the staff workspace. That
manual coverage remains assigned until the alert acceptance below succeeds;
an unstaffed disabled switch is not a safe rollout state.

After the deployment gates are open, the acceptance requires a named Records
Officer and Workspace operator. The approved mechanism keeps inbound, outbound,
automated outbound, and `AUTO_ACK_SAFETY_NET` false; enables only global Gmail
and staff notifications; and recreates web, worker, and beat together. Through
the supported staff-intake workflow, create one clearly labeled
district-controlled synthetic request with a district-controlled requester
address and an original receipt time that puts its statutory response due
within two business days. Allow the scheduled deadline watchdog to run.
Verify exactly one alert reaches only the configured staff recipients, the
audit log contains `staff_notification.sent`, a repeated unchanged watchdog
run is suppressed, and no requester `Correspondence` or lifecycle transition
was created. Preserve the non-PII evidence and carry the synthetic case through
a supported human disposition; never delete or rewrite it. Only then may the
named owners retain the staff switch as enabled and release manual coverage.

## Inbound checkpoint and replay safety

`GmailSyncState` stores one checkpoint per mailbox and application environment.
The minute beat task uses `users.history.list` from that checkpoint and hands
each `format=raw` message directly to
`receive_inbound_message()`. The receiving layer's provider-message uniqueness
and payload digest remain the correctness boundary: a lost checkpoint, an
overlapping worker, or a history-expiry resync may replay work but cannot create
a second `InboundCorrespondenceReceipt` or a second request for an eligible
bare-mailbox email. The receipt-to-request claim and canonical intake transaction
are one atomic unit.

Email intake uses Gmail `internalDate` as `received_at`, the parsed `From`
address/display name as the minimized requester identity, and the exact parsed
plain-text body (including the existing HTML-to-text fallback) as the immutable
request description. A forward is the exception: when the subject is a forward
(`Fwd:`/`FW:`) or the body carries a Gmail or Apple Mail forward marker,
`apps/records/email_forwards.py` reads the forwarded header blocks and the
request opens under the deepest one, the person who originally wrote it, with
the date they sent it (never later than the mailbox arrival). The whole
forward stays the description, and `request.email_forward_attributed` records
who forwarded it. Anyone may forward; a forward with no readable header block
opens under the forwarder. It runs the same deadline, triage, footage-hold, routing,
submission-receipt, and audit pipeline as every other intake channel. Email
attachments remain one quarantined blob with one correspondence scan verdict;
their linked intake representation is downloadable only while that original
verdict is clean.

A Gmail provider or read failure leaves the checkpoint unchanged for the next
beat. A failure receiving one particular message does not: that message is
audited as `gmail.inbound_message_failed`, the records officers and the PRA
supervisor get an `[INBOUND MAIL FAILED]` alert, it is left without the
processed label, and the rest of the batch continues, so one bad message can
no longer hold every later message back. Label failure after durable receipt
is logged but may still advance the checkpoint; labels are efficiency and
operator hygiene, not legal acceptance evidence. The resync boundary is read before the bounded query, so
mail arriving during resync is either processed immediately and safely replayed
or discovered by the next history read—never skipped.

A message the MIME parser refuses is still durably received, and Gmail still
labels it processed, but it has no correspondence, matches no case, and opens
none. Each one sends an immediate `[INBOUND MAIL HELD]` alert to the records
officers and the PRA supervisor, and it waits in the staff Inbox under **Held
for review** as "PRR could not read it". The parser refuses only structural
hazards (size, depth, part-count, and encoding bombs, malformed boundaries);
it never refuses a message over which related part is the body or over an
undecodable encoded-word header, which RFC 2047 shows as literal text. Until
2026-10-21 it did both, and every Gmail message with an inline signature image
(Content-ID `<ii_...>`) and every Gmail bounce notice (`<icon.png>`) was held.

To bring held messages in once the parser reads them, run a dry run:

```bash
python manage.py reparse_quarantined_inbound
```

It re-reads each held message's retained raw bytes (refusing any whose digest
no longer matches) and prints `WOULD RELEASE` or `STILL QUARANTINED (reason)`.
`--commit --all`, or `--commit --receipt <id>` for named ones, releases them:
each gains its correspondence and attachments, is matched to its case, and an
eligible new request opens with its real arrival time (audited as
`correspondence.inbound_reparsed`). The same deadline warning as catch-up
applies.

## Mailbox reconciliation, triage alerts, and catch-up

The history feed only reports messages that arrive carrying the INBOX label.
It never reports mail a Gmail filter skipped past the inbox, mail archived by
hand before it was received, mail moved out of spam, or a message whose
receipt failed. The hourly `reconcile-inbound-gmail` beat job (`:45`) closes
that gap. It lists every message outside sent, drafts, chat, spam, and trash
that lacks the processed label and arrived within the inbound lookback window.
It leaves anything newer than the stale threshold to the minute poll, and
receives the rest through the same path as the poll. Anything it receives is
mail the poller missed. It is audited as
`gmail.inbound_reconciliation_recovered`, and an `[INBOUND MAIL RECOVERED]`
alert names the Gmail message ids.

Mail that opens no case on its own lands in the staff Inbox as Unmatched,
Candidate, Ambiguous, or Quarantined inbound. Each row names the sender and
subject. A records officer clears mail that is no request at all with **Not a
request…** on the row, or ticks several rows and uses **Dismiss N selected**;
both record the work item's no-action decision (category **Not a records
request** for marketing, alerts, spam, and bounces for mail PRR never sent),
one audited decision per message. A dismissed message leaves the triage queue
and stays listed under Inbox -> No action. That includes mail delivered to
the former records officer's address, which Gmail delivers into this mailbox. Every weekday at 07:45, `alert-waiting-inbound-triage`
sends one `[INBOX TRIAGE]` alert to the records officers when any such item has
waited at least the **Untriaged inbound mail alert** setting's number of
business days. The alert counts the items by kind and by the secondary address
each was sent to.

Mail older than the lookback window is never received automatically. To find
it, run a dry run:

```bash
python manage.py catch_up_inbound_mailbox --after 2026-01-01
```

It lists each unprocessed message with its most likely outcome (`NEW CASE`,
`EXISTING CASE?`, `TRIAGE (...)`) and writes nothing. To receive messages, a
records officer names each one:

```bash
python manage.py catch_up_inbound_mailbox --commit --message-id <gmail id>
```

Add `--triage-only` to receive a message into the Inbox without opening a
case. A case opened this way keeps the message's real Gmail arrival time.
Its RCW 42.56.520 deadline may therefore already have passed, and with
`AUTO_ACK_SAFETY_NET` on, the hourly safety net queues an automated
acknowledgment for it at its next run. If the request was already answered
through another channel, record that with
`services.record_external_initial_response()` first.

## Outbound task and duplicate-send safety

Queue and staff retry register `send_queued_delivery` only after the database
transaction commits. Redis suppresses duplicate task nudges by delivery UUID
and current attempt count; the database lease and append-only attempt ledger
remain the correctness boundary. The task builds MIME from the frozen subject,
body, recipients, Message-ID, Reply-To, and clean released attachments, then
uses `gmail.send_raw()` and records the result through
`report_delivery_result()`.

If a worker stops after Gmail accepts the message but before result recording,
the delivery lease expires into `awaiting_reconciliation`. The next task uses
the existing RFC Message-ID with `gmail.find_by_rfc822_message_id()` and never
sends again. A not-found reconciliation becomes human-retryable; it does not
silently authorize a resend. Overlapping beat ticks and stale duplicate tasks
may create queue noise, but row locking and attempt generation checks prevent a
second Gmail send.

## First response when mail stops

For CDK-managed releases under `/srv/prr/cdk-releases/`, the supported repair
is a tested change merged to `main` followed by the single production CDK
deployment command in `docs/aws-deploy.md`. That deployment validates and
consumes the exact retained activation overlay, immediately replays the durable
checkpoint, and proves a later scheduled poll. The older fixed activation
document described below resolves only the retired `runtime-releases/<commit>`
layout and must not be invoked against a CDK-managed release.

Start by preserving the durable Django state. Do not delete delivery attempts,
requeue database rows by hand, relabel Gmail messages manually, or rotate to a
service-account key.

1. Invoke the fixed, parameter-free diagnostic document through Systems
   Manager. Record the command id printed by the first command, substitute it
   into the second, and do not open a shell on the host:

   ```bash
   aws --profile psd401-prr-prod --region us-west-2 ssm send-command \
     --document-name psd401-prr-prod-email-diagnostics \
     --instance-ids i-016ee26e06547aa82 \
     --comment 'Inbound Gmail incident diagnostics' \
     --query 'Command.CommandId' --output text

   aws --profile psd401-prr-prod --region us-west-2 ssm get-command-invocation \
     --command-id <command-id> \
     --instance-id i-016ee26e06547aa82 \
     --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'
   ```

   The fixed command reports web/worker/beat container state, whether both
   Gmail switches are enabled, whether the durable checkpoint is missing or
   stale, and whether the pinned Gmail credentials, scopes, delegation, and
   profile verify without sending a message. It accepts no caller-supplied
   host command and returns no requester message data. The command fails when
   a required container is stopped, intake is disabled, the checkpoint is
   missing or stale, or Gmail verification fails.
2. The following activation material is retained only as historical design
   context and is superseded. The fixed
   `psd401-prr-prod-enable-gmail-inbound` document it describes no longer
   exists, and the activation overlay it wrote is no longer what decides
   whether polling runs. Turn inbound polling on or off at Configuration >
   District settings instead.
   Do not invoke it against a CDK-managed release. Before any separately
   reviewed use on its legacy layout,
   require separately retained operator evidence that the one-time initial
   baseline adoption completed and left the private `deployed-release.json`,
   retained source/image rollback artifacts, and (for an observed legacy image)
   `baseline-adoption.json` described in [`aws-deploy.md`](aws-deploy.md). The
   activation document intentionally cannot create or infer that trust boundary.
   The 2026-08-18 audit recorded it as incomplete; unless a later approved
   operator record proves completion, stop here until a separately reviewed
   AWS-native adoption procedure is available and a named operator completes it.

   On the legacy layout only, if and only if that adoption prerequisite is
   proven and diagnostics show
   valid Gmail credentials and a preserved checkpoint but both Gmail and inbound
   polling are disabled, invoke the reviewed, fixed activation document. It
   holds both the production deployment
   and backup locks. The deployment lock may be owned only by root or the
   established `ubuntu` production operator because the reviewed historical
   deployment receiver ran as that account; the backup lock remains restricted
   to the `ubuntu` backup service. Both must still be private, single-link
   regular files and must be acquired nonblocking. Retained deployment evidence
   may likewise be owned only by root or that established `ubuntu` operator:
   historical baseline adoption created it as `ubuntu`, while fixed SSM
   activation runs as root. Every evidence file must still be a private,
   single-link regular file and is opened with no-follow and inode-binding
   checks. The document then resolves
   the active immutable release from the common
   containers'
   fixed Compose labels or the matching private baseline-adoption provenance
   while the adopted containers still carry `/srv/prr/app` labels, verifies the
   two retained Compose files against their hashed source archive, rejects any
   release-path or image mismatch, starts the fixed application backup while
   retaining the deployment lock, then proves its manifest identity, timestamp,
   and audit boundary did not regress for this activation and matches the
   common image.
   It rejects unresolved deployment or baseline-adoption journals and pending
   migrations, validates every protected switch directly from the private base
   `.env.app` before any recovery or recreation can reread it, and accepts a
   two-link file only when the other link is the private canonical
   `/srv/prr/app/.env.app` retained by initial baseline adoption. It also
   fingerprints the complete canonical `.env` and `.env.app` files and compares
   the complete effective environment derived from those bytes with every
   existing web/worker/beat container before recovery or mutation. For the
   adopted baseline, it separately renders both the legacy live Compose context
   and the retained release-tree context and requires both complete environments
   to match the live container, so recreation cannot switch to an unvalidated
   canonical runtime file. The
   fingerprints are rechecked before every recreation, so unapplied or
   concurrent runtime drift cannot be introduced by activation or reused by
   rollback. It
   requires a known checkpoint age within the configured Gmail resync lookback
   minus a safety margin equal to the activation document's full timeout, so an
   expired Gmail history ID cannot age past the fallback query and silently skip
   older mail while activation is running. It also
   requires every outbound, automated-outbound, staff-notification, and safety-
   net gate to be off. It leaves `.env.app` and rollback evidence unchanged,
   adds only the two inbound values through a fixed Compose overlay while
   forcing `RUN_MIGRATIONS=false` and `AUTO_SEED=false`, recreates
   web first, proves object storage, ClamAV, Redis, and both Gmail principals
   are ready, and gives an immediate replay up to five minutes before recreating
   worker and beat. A replay that reaches that bound is stopped safely and
   reported as deferred; the newly enabled minute poller then resumes from the
   last durable checkpoint, with any partial receipts deduplicated normally.
   Before reporting success, the command stops beat first and gives the old
   Celery worker an unbounded warm-shutdown grace period so acknowledged legal
   work is completed rather than killed. It then records
   the queued Gmail-poll task IDs, starts only the replacement beat, and captures
   a deterministic task ID from the new IDs that it enqueues. More than one new
   ID is valid when queue observation spans another minute tick. It then starts
   the replacement worker and requires the selected exact task to complete with
   an `ok` poll result and advance the post-replay checkpoint. A task queued
   before activation cannot satisfy this proof. If beat cannot enqueue through
   Redis, the worker cannot execute
   the exact poll, intake dependencies are unavailable, or Gmail polling fails,
   the command restores the disabled base and fails instead of claiming intake
   is working.
   It accepts no parameters. A completed replay returns only aggregate counts;
   replay logs are suppressed so SSM output contains no message or requester
   identifiers:

   ```bash
   aws --profile psd401-prr-prod --region us-west-2 ssm send-command \
     --document-name psd401-prr-prod-enable-gmail-inbound \
     --instance-ids i-016ee26e06547aa82 \
     --comment 'Restore inbound Gmail polling after disabled-switch incident' \
     --query 'Command.CommandId' --output text

   aws --profile psd401-prr-prod --region us-west-2 ssm get-command-invocation \
     --command-id <command-id> \
     --instance-id i-016ee26e06547aa82 \
     --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}'
   ```

   Legacy retry behavior recognizes the valid private root-owned overlay retained by a
   successful activation, as well as that overlay or a prefix-only temporary
   write left by an interrupted invocation. For the adopted baseline only,
   valid residue also permits the expected temporary mix of `/srv/prr/app` and
   retained-release working-directory labels, or a missing service container,
   created when an invocation stops during or after recreating only some
   services. At least one service container must remain as live evidence; an
   all-missing state fails closed. Each container that remains must still match
   the validated image, Compose-file set, switch state, and effective
   environment. After validating the immutable release and both complete
   runtime files, it consumes valid residue before requiring
   every process to be running; this lets a retry recover worker or beat after
   an interruption during their deliberate stop. It stops beat first, drains
   the worker with warm-shutdown semantics, and restores all three processes
   from the disabled base while
   the file remains as a durable recovery marker, and removes it only after all
   three prove disabled. Unknown or malformed evidence, unresolved deployment
   journals, and unknown post-interruption Compose labels fail closed. This
   disabled-base recovery happens before the activation's backup, migration,
   and checkpoint-age gates; no inbound enablement or replay can occur until
   those gates pass. A failed verification removes the activation overlay only
   after recreating all three
   application processes from the unchanged base composition with the same
   immutable image. Durable receipts written by a replay remain authoritative
   and must never be deleted during rollback. After successful verification the
   exact overlay remains private and root-owned so an interrupted cleanup cannot
   erase the recovery path; a retry consumes it by first restoring the disabled
   base. A later reviewed release likewise starts from its own disabled runtime
   and consumes the marker before reactivation. Do not invoke this document for a credential,
   delegation, image, release-path, container-health, missing-checkpoint, or
   mixed-switch incident.
3. Determine whether send, poll, or both stopped. After a telemetry-bearing
   application release has been deployed, query the bounded failure categories
   in CloudWatch; do not paste raw messages or requester addresses into tickets:

   ```bash
   aws --profile psd401-prr-prod --region us-west-2 cloudwatch list-metrics \
     --namespace PSD401/PRR \
     --metric-name GmailInboundPollFailure \
     --dimensions Name=Environment,Value=production \
     --recently-active PT3H \
     --query 'Metrics[].Dimensions[?Name==`FailureCategory`].Value[]'
   ```

   The CDK deployment creates the missing-or-failed alarm for this metric and
   treats missing telemetry as breaching. It emails `techalerts@psd401.net` through
   the `psd401-prr-prod-alarms` SNS topic when it fires and when it clears. The
   fixed Systems Manager diagnostic remains the canonical detailed intake-health
   check.
4. The diagnostic document runs `verify_gmail` read-only. If it fails before
   reading the profile, check the AWS
   workload principal has both Workload Identity User and service-account-
   scoped Token Creator on `prr-gmail` (domain delegation requires `signJwt`).
   Then check the AWS runtime identity, ADC file path, IAM Credentials API, and
   domain-wide delegation. If the profile differs, stop both directions and
   correct the pinned mailbox configuration.
5. For outbound-only failure, set the outbound switch false. Inspect pending
   delivery leases and reconciliation evidence through supported application
   tools; retry with the same durable delivery identity after transport is
   healthy. Public and email intake remain available while the switch is off;
   the non-statutory submission receipt is skipped until transport is restored.
6. For inbound-only failure, set polling false. Confirm production alone owns
   the poller and the expected environment label still exists. Preserve the
   checkpoint and let the durable dedupe path replay only after the approved
   deployment mechanism restores the poller; do not mark messages by hand.
7. For rate limits or temporary provider failures, leave evidence intact and
   allow normal backoff. Authentication failures require configuration repair,
   not repeated retries.

After repair, rerun the fixed diagnostic read-only. Except for the exact
disabled-switch incident covered by the fixed inbound activation document,
re-enablement is a deployment, not part of the false-only incident exception:
have a tested, globally locked mechanism restore only the affected direction,
recreate web/worker/beat, and monitor one complete send or poll cycle. Keep the
unaffected direction disabled only as long as the incident requires.

## Credential and delegation changes

- The external-account files are non-secret but environment-specific. Replace
  them only from the matching Terraform provider and exact AWS role.
- The Workspace client ID comes from
  `terraform output -raw gmail_service_account_unique_id`.
- Any scope change requires code, tests, security review, and a runbook update.
  Never widen the Admin-console grant as an incident shortcut.
- Disabling Gmail safely means switches first, then revoking the Workspace
  grant or Terraform binding if the incident requires it.

See [google-identity-bootstrap.md](google-identity-bootstrap.md) for the
one-time Workspace steps and [aws-deploy.md](aws-deploy.md) for production.
