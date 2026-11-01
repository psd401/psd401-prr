# Advisory AI boundary

The application uses the district AWS account's Amazon Bedrock Converse API for
four bounded proposals:

- request-intent assistance: a summary, mandatory search anchors, alternative
  search terms, existing RecordSystem slugs, clarification questions,
  enumerated complexity flags, a proposed date range, and up to four proposed
  narrowing options;
- case-title assistance: one string of at most 80 characters, and nothing
  else;
- record responsiveness assistance: `responsive`, `non_responsive`, or
  `uncertain`, with confidence, matched terms, and a rationale;
- redaction assistance for a human-confirmed responsive PDF: page-specific
  candidate text spans, an active exemption-taxonomy identifier, confidence,
  and rationale for human editing and disposition.

The proposed date range ends at the date the district received the request:
only records already in existence then can be responsive, so an end the model
omits or pushes past receipt is pulled back to the receipt date. Each
narrowing option carries its terms and a rationale and is display-only
drafting material for the officer's scope conversation. Nothing in a proposal
narrows a request, and the advisory never drafts, queues, or sends the letter
that offers those options.

Request-intent assistance includes eligible intake attachments as Bedrock
document or image content blocks, because the email or form body may only say
"see attached." An attachment is eligible only after the existing ClamAV
boundary reports it clean. The worker re-reads the private object through the
same clean-download boundary and verifies its exact retained byte count and
SHA-256 before inference. Bedrock receives neutral labels such as
`attachment 1`, never requester-controlled filenames. PDF, Word, Excel,
CSV/text, PNG, JPEG, and GIF inputs are supported within the Converse API's
per-file and count limits; pending, infected, unsupported, and oversized files
are omitted and make the run's `input_truncated` provenance flag true.

It is off by default. Activation is an Application Admin's decision at
Configuration > District settings, with a written reason, on top of the
retained production approvals below; deployment overlays supply neither
switch. Model output is untrusted
data, not a legal action.
Each run retains whether it was manually requested or automatically prepared,
its named staff requester when one initiated the work, model identifier,
target, input SHA-256, truncation flag, bounded proposal or non-sensitive
failure category, and audit events. System initiation is limited to record
responsiveness, redaction preparation, and the intake-time request-intent
proposal; every manually requested run still requires a named staff requester,
and the database constraint enforces that.

The intake-time intent proposal has its own switch,
`PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED`, which also requires
`PRR_AI_ADVISORY_ENABLED` and defaults false. When it is on, a new request
prepares its intent proposal after intake commits and every file the requester
sent has a final malware verdict (the proposal reads those files, so it waits
for the last scan; a file whose scan never settles leaves the officer's Run
button as the way in), and a completed proposal
that maps the request to an active Vault-backed `RecordSystem` queues one
entire-organization Gmail count using the proposed query, ending at the
receipt date. That count creates no hold, export, collection task, status
change, or requester message, and the officer re-runs it from the Collection
tab with their own edited scope whenever the proposal was wrong. A count
produced this way is labelled as automatic in the staff workspace so its scope
is reviewed before any collection starts. The run does not persist the prompt or a
second copy of the source text or attachments. The input fingerprint includes
the full attachment manifest, so a scan verdict or attachment change cannot
reuse a stale result. A database constraint permanently requires
`applied = false`.

Accepted risk (district decision, 2026-09-23): the automatic count's query
comes from a model reading untrusted requester text, and the count runs with
no cap of its own. A flood of manipulated submissions could spend Vault quota.
The two channels are bounded differently:

- Web-form requests pass the public-intake per-IP and global throttles.
- Emailed requests have no rate limit. Each Gmail message creates at most one
  request, and `PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS` refuses listed
  domains; nothing else limits how many arrive.

Every automatic count is also limited to one per request, quotes each term
literally (a term cannot become a Vault operator), and cannot hold, export, or
reveal content. A separate volume cap, for either channel, was considered and
declined.

A scope-conversation letter states a count the officer types. It is not tied
to a count run, and a mismatch never blocks sending. When the number differs
from the request's latest completed count, or a count is still running, the
compose, edit, and draft views say so to the Records Officer before they
approve.

For redaction assistance, the worker integrity-verifies and opens the retained
PDF locally, extracts coordinate-bearing text lines, and sends Bedrock opaque
span identifiers plus text. Bedrock never supplies coordinates. It may return
only span identifiers issued for that page and IDs from the active exemption
taxonomy. The worker validates both vocabularies and derives normalized boxes
from the retained PDF locally. Unknown spans, inactive or invented exemptions,
unexpected fields, invalid confidence, and unsearchable PDFs fail closed. Page
inputs are bounded and chunked instead of silently dropping later pages.

Each grounded candidate is immutable and remains distinct from a `Redaction`.
A named reviewer can load its box into the normal editor, change the bounds,
exemption, and explanation, and explicitly accept it through the existing
redaction service, or reject it with a substantive reason. That append-only
decision links the proposal to the resulting exemption-backed `Redaction` when
accepted. There is no accept-all path. Existing distinct second-review and
raster burn-in gates still apply to the final human-created redaction.

The provider has no path to change request status, scope, collection tasks,
fees, estimates, correspondence, review state, legal redactions, withholding,
second review, release, or preservation. Staff may consider a proposal and
then explicitly use the existing human-attributed workflow. The UI preselects
a completed `responsive` or `non_responsive` recommendation to reduce clerical
work, but it never submits it. A named reviewer must still press the confirmation
control; changing the selection always wins over a later model result.

## Bulk review is a human decision, not an accept-all

The review queue lets a reviewer mark a whole filtered selection responsive or
non-responsive in one confirmation
(`record_items.bulk_review`, `apps/records/review.bulk_review_records`). This
is **not** an accept-all of AI output, and the distinction is structural rather
than a matter of wording:

* The request carries the record identifiers the reviewer selected and the
  outcome the reviewer chose. It carries no advisory run, no proposal, and no
  confidence threshold, and the service reads none.
* The set is what the reviewer filtered and could see. Tag, sender-domain,
  custodian, and text filters are facts about the records; the AI-confidence
  filter and sort only change what a reviewer looks at first, never what the
  confirmation writes.
* Every record goes through the same per-record review service the
  single-record control uses, so each gets its own validation and its own
  append-only audit row naming the reviewer, plus one row for the bulk action.
* The cap is 200 records, so one confirmation stays something a person could
  have looked at rather than an unbounded write.
* Only `responsive` and `non_responsive` are available. Withholding a record
  in full needs its own ExemptionCitation and second review, and a duplicate
  finding needs its own judgement, so neither can be expressed by one
  confirmation over a set. A record already withheld or already marked a
  duplicate is left out of a bulk selection for the same reason, and the
  service refuses a batch that carries one rather than overturning it.

There is still no path by which a model result becomes an applied legal
outcome, in bulk or otherwise.

The privilege queue follows the same rule. `counsel_domain` and
`privilege_marker` are deterministic tags derived from stored message
metadata and the administrator-managed counsel-domain list; they separate
records for a human to read before anything ships. They withhold nothing.

## Automatic record-review preparation

When the automatic record-review switch is enabled, the application prepares
the model work ahead of the reviewer:

1. A newly ingested Vault record queues a responsiveness recommendation after
   its record and integrity evidence commit.
2. A `responsive` or `uncertain` recommendation queues coordinate-grounded
   redaction candidates and rationales in the background. A `non_responsive`
   recommendation does not suppress or remove the record.
3. If a reviewer overrides the recommendation and confirms a record as
   responsive, that confirmation queues any missing redaction preparation.
4. A bounded five-minute supervisor recovers work missed during a transient
   queue/provider failure. It stops retrying a kind after three substantive
   automatic failures, leaving the visible named-user retry control. A queued
   run stopped by the global or automatic runtime gate records a planned pause
   and does not consume that retry budget.

Automatic results remain proposals. Responsiveness still needs one explicit
named human confirmation. Each redaction candidate still needs an individual
named human accept/edit/reject decision with an active exemption citation; no
accept-all path exists. Second review and immutable raster burn-in remain
unchanged.

Request-intent proposals distinguish exact subject identifiers from topical
alternatives. A person name or case identifier that every result must contain
is a mandatory `AND` anchor; record-category terms and synonyms are grouped as
`OR` alternatives. For example, the UI drafts `"Jordan Sample" AND
("educational records" OR "counseling records")`, never a query that makes the
person's name optional. The Records Officer reviews and may edit the complete
Vault query before requesting a count.

## Case titles

The staff case list needs one line per case saying what it is about. The
request description cannot be that line: it is the legal record of what was
asked and is never edited (AGENTS.md rule 9), and an emailed request opens
with whatever the sender's mail client put at the top — a name, a doctorate, a
job title, a pronoun line — so its first 140 characters are usually about the
sender rather than the records.

The `case_title` advisory proposes that line. Its entire output is
`{"title": "…"}`, at most 80 characters, validated and bounded like every
other advisory field, and `applied` stays false by database constraint. Its
input is the request description and, where the case arrived by email, the
subject line of the earliest inbound message. It reads no attachments, is
offered no record systems and no exemption taxonomy, and proposes no search
terms — so unlike a request-intent proposal there is nothing in it a Vault
query could be assembled from, and a completed run starts nothing.

That is why it runs automatically on its own switch,
`PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED`, independently of
`PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED`. Both require
`PRR_AI_ADVISORY_ENABLED` and both default off. With it on, every new case
queues a title after intake commits, on every channel; it does not wait on
attachment scanning because it reads no files.

The title is shown in the Subject column to Records Officers only — the same
boundary as every other advisory output — marked as AI-suggested, with the
description snippet as the fallback for everyone else and for any case without
a completed run. A completed request-intent run's `short_title` is used when a
case has no title run of its own.

### Backfilling existing cases

`manage.py backfill_case_titles` gives cases created before the feature the
same label. It is dry-run by default and reports what it would queue;
`--commit` queues. Open cases only unless `--all` is passed, `--limit` bounds
one pass, and `--batch-size` with `--pause` bounds how fast runs reach the
provider. It is idempotent: a case that already has a pending, running, or
complete title run is skipped, and the advisory service reuses a run over the
same request text and model anyway, so a second pass costs nothing.

It queues advisory runs and does nothing else — no message, no status change,
no deadline, no search — and it refuses to commit unless both switches are on,
rather than queueing runs the worker would fail one at a time.

## Approval prerequisites

Before sending district data, the CIO or delegated system owner documents the
following operational acceptance record:

1. The provider, model, AWS inference profile, region routing, subprocessors,
   retention and training terms, deletion terms, incident notification, audit
   access, and contract/DPA are approved for public-records request text and
   record OCR. These inputs can contain student, personnel, legal, health, and
   other confidential information.
2. The data classification permits the intended content. A provider approved
   only for ordinary district data must not receive FERPA, attorney-client,
   security, or similarly restricted records. Keep the feature disabled if one
   endpoint cannot safely receive every record available in the review queue;
   a future change should add enforceable per-classification routing first.
3. The application uses the existing environment-specific AWS runtime role,
   never an AI API key. IAM permits only `bedrock:InvokeModel` for the named
   inference profile and its exact foundation-model destinations. It must never
   use a personal token, Google Vault/Gmail credential, or shared MCP token.
4. Staff training explains automation bias, false negatives, prompt injection,
   and the requirement to inspect the retained source. Exact search hits are
   never suppressed, and a `non_responsive` proposal never removes an item from
   the queue.
5. A representative offline evaluation measures missed responsive records,
   false positives, subgroup/content-category variance, cost, latency, and
   failure behavior. The acceptance threshold and rollback owner are written
   before a controlled production pilot; production approval follows a
   separate named review.

## Configuration

The four advisory switches are district settings, not environment variables.
`PRR_AI_ADVISORY_ENABLED`, `PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED`,
`PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED`, and
`PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED` are changed by an Application Admin
at **Configuration > District settings**, with a written reason, taking effect
without a redeploy. Each automatic switch requires the advisory switch to be on
first, and the store says so when it refuses; turning the advisory switch off
is never blocked, and the automatic ones go inert because every consumer
requires both.

The model itself is a district setting too, on the same page. The provider, the
Region, the model identifier, the call timeout, the input ceiling, and the
response ceiling are all edited there with a reason and a history:

| Setting | Default | Bounds |
|---|---|---|
| `PRR_AI_PROVIDER` | `bedrock` | `bedrock` is the only approved provider |
| `PRR_AI_AWS_REGION` | blank | an AWS Region such as `us-west-2` |
| `PRR_AI_MODEL` | blank | a Bedrock model identifier, at most 255 characters |
| `PRR_AI_TIMEOUT_SECONDS` | `30` | 1-120 |
| `PRR_AI_MAX_INPUT_CHARACTERS` | `50000` | 1000-250000 |
| `PRR_AI_MAX_RESPONSE_BYTES` | `65536` | 1024-1048576 |

The district's chosen model is `us.amazon.nova-2-lite-v1:0` in `us-west-2`,
and the production Compose overlay still carries those three values as the
one-time bootstrap seed the settings-store import reads. Editing the overlay
afterwards does nothing: change the model on the Settings page, where the
change has a name and a reason on it.

Advisory AI cannot be enabled at all unless an approved model is set. Only the
`bedrock` provider, a real AWS Region, and a bounded model identifier count as
configured, and the store refuses to turn advisory AI on otherwise -- the same
guarantee startup used to give by refusing to boot, now given to a named
administrator with an explanation instead of to a crash loop. The reverse is
deliberately not blocked: blanking the model while advisory AI is on is
allowed, and advisory AI goes inert, exactly as a dependent switch does when
its parent is turned off.

In the sole supported deployment target, boto3 obtains short-lived credentials
from the production EC2 instance role. Local synthetic tests use provider
doubles unless a separate operator explicitly authorizes a bounded AWS
integration test.

The client uses the native Converse
API with bounded SDK timeouts/retries and output size, treats source content as
prompt-injection-bearing data, and validates exact keys, vocabularies, counts,
lengths, active RecordSystem slugs, and confidence ranges before persistence.
Provider error bodies and source/model text are not copied into failure rows or
audit details. The deployed IAM policy pins the US Nova 2 Lite profile and its
documented US destination models; there is no wildcard Bedrock grant.

Redaction analysis requires a PDF with a searchable coordinate-bearing text
layer. Vault-ingested Gmail records already pass through the searchable-PDF OCR
normalizer. An image-only PDF that reaches review without such a layer fails
legibly and must be normalized through the approved OCR ingestion path before
rerunning the advisory; the worker does not trust free-form OCR text that lacks
page coordinates.

Verify first with local or isolated synthetic-data tests. Exercise the queue,
worker, logs, cost limits, disabled-state behavior, and manual review before a
named operator authorizes a controlled production pilot. The provider/model
selection is Amazon Bedrock Nova 2 Lite through the US inference profile;
representative accuracy and harm evaluation remains an operational acceptance
step.

The 2026-08-10 activation decision does not replace the provider/data approval
and representative evaluation record above. Leave all three switches off until
those records and a named production approval are retained; the store defaults
them off and keeps them off through any failure it cannot read past. Enabling
one is an Application Admin's decision with a reason on the record, not a
deployment, and it no longer waits for a release.

Stopping is the same act in reverse and just as fast. Turning
`PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED` or
`PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED` off pauses the corresponding
automatic preparation while leaving manual advisory available; turning
`PRR_AI_ADVISORY_ENABLED` off stops every provider call, including both
automatic paths, within seconds and without recreating a container. The Compose
overlays deliberately do not override these switches, and could not: a
service `environment` entry is ignored for a setting the district manages in
the application.
