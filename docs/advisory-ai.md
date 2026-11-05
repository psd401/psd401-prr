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
accepted. There is no accept-all path: proposals are accepted one at a time,
or a group at a time after a sampled check under the trust policy below.
Existing distinct second-review and raster burn-in gates still apply to the
final human-created redaction.

The provider has no path to change request status, scope, collection tasks,
fees, estimates, correspondence, review state, legal redactions, withholding,
second review, release, or preservation. Staff may consider a proposal and
then explicitly use the existing human-attributed workflow. The UI preselects
a completed `responsive` or `non_responsive` recommendation to reduce clerical
work, but it never submits it. A named reviewer must still press the confirmation
control; changing the selection always wins over a later model result.

## Iterative scoping

A request-intent proposal (schema v7) may also name `custodians` (district
mailboxes), `sender_domains`, `exclude_senders` (typically the knowledge
pack's noise senders), and a `scope_rationale`. `vault_query_from_intent` turns
senders into Vault Gmail syntax: `(from:a OR from:b)` and `-from:x`.

When the automatic intent count runs, it opens a `ScopingSession`
(`apps/records/scoping.py`). Count 1 is the proposal: the named district
mailboxes (the `accounts` scope) when there are any, otherwise the whole
district; a custodian outside the district is never counted. When a count
finishes, the agent sees every iteration -- query, total, and the busiest
custodian accounts (Vault counts give no sender breakdown) -- with the noise
senders, and either stops or proposes a tighter query with its reason, which is
counted next. Scoping stops at `PRR_INTENT_MAX_COUNT_ITERATIONS` (in-app,
default 4), when the agent says stop, when a refinement is unusable or repeats
an earlier scope, or when a count fails; the last completed count is the
proposed scope. The Collection tab's **Scope iterations** card shows the trail
(`requests.scoping.read`); **Re-run scoping** (`requests.scoping.rerun`) starts
a fresh session from the latest intent proposal, with the officer and a reason
recorded, even when nothing changed. Scoping itself only counts: no hold,
export, collection, or status change.

### Collecting the approved scope

Collection starts from the proposed scope (#37, rule 11 amendment):

- **Approve and collect** on the Scope iterations card starts the Vault export
  of the final count in the officer's name (`requests.vault_collection.start`),
  with a hold on the named mailboxes when the scope names any.
- With `PRR_TRUST_SCOPE_MODE` (in-app, default `suggest`) set to `auto`, a
  finished *automatic* scoping session on a request with no Vault mail
  collection starts that export itself through
  `queue_automatic_vault_collection`. The task is recorded as
  `trigger=automatic` with no actor, and the card and Tasks list say so. A zero
  count, an officer's re-run, or an existing collection never starts one.
- **Approve re-scope** (`requests.rescope`) appears when the final count
  differs from the current collection. It shows both totals, needs a reason of
  at least 20 characters and a finished current collection, then collects the
  new scope and supersedes the old collection's unreviewed records.

Holds are unchanged: the export path places the same holds it always has.

## Intake classification suggests; an officer decides

When `PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED` is on, every new case
(web form, email, or staff intake) gets an `intake_classification` run after
its intake commits (`apps/records/advisory_ai.defer_automatic_intake_classification`).
The model sees the request text, its first email subject, its channel and
received date, and a bounded list of **earlier** cases of the **same matter
kind**: the last 180 days (at most 120) plus the same requester's earlier cases
(at most 20), each with its number, received date, a 300-character description
excerpt, and whether the requester email matches. Those excerpts are other
requesters' request text, which the 2026-11-03 model acceptance in
`docs/phase-2-production-handoffs.md` covers, student content included. Only immutable fields are
sent, so the input does not change while the run waits. Kinds never mix
(AGENTS.md rule 13).

It returns one label -- `new_request`, `follow_up`, `duplicate_of`,
`related_to`, `non_request`, or `spam` -- with a confidence and the evidence it
relied on. The three labels that point at a case must name one of the cases it
was shown; an invented number fails the run. The proposal is not applied,
except for spam and non-request under the intake trust ladder below.

The case Overview shows the label to Records Officers. **Correct** records the
officer's agreement (`AdvisoryFeedback`, append-only, one per run) and opens
the ordinary action prefilled: merge for a duplicate or follow-up, add to an
existing case for a follow-up on an automatically opened email case, or close
as non-request for a non-request or spam email case. The officer still reads
and submits that action; agreement is recorded whether or not they do, and
**Open the action** reopens it after a cancelled dialog. A label with no
action (a new or related request, or a non-request label on a web-form case)
shows no such button. **Not correct** records the label the officer judged
it to be instead. Web-form cases have no non-request closure, so a
`non_request` or `spam` label there is shown but has no one-click action.
Every answer is audited (`advisory.feedback_recorded`) with the proposed and
final label, which is the agreement data the evaluation work (#32) reads.

### Trust ladder: automatic spam and non-request closure (#34)

Two labels, and only two, may be applied with no officer: `spam` and
`non_request` (district decision, 2026-11-05). Each has its own in-app mode,
`PRR_TRUST_INTAKE_SPAM_MODE` and `PRR_TRUST_INTAKE_NON_REQUEST_MODE`
(`suggest` or `auto`, default `suggest`), and they share one floor,
`PRR_TRUST_INTAKE_AUTO_MIN_CONFIDENCE` (default 95%). Like every setting, a
change is admin-only, needs a reason, and is kept in its history. The
Settings page shows, beside each mode, the live intake model's latest
evaluated precision and recall on "not a request"; spam and non-request are
one class in the evaluation, so both rows show the same figures.

In `auto` mode, `apps/records/intake_trust.auto_apply_intake_label` runs when
an automatic classification completes. It closes the case as non-request
(`services.auto_close_non_request`) only when all of these hold: the run was
automatic, not asked for by an officer; it is the case's latest
classification and has no officer answer; the label is in `auto` mode at or
above the floor; the case is still `received`; and the case was auto-created
from email. A web-form request is never closed this way, and no other label
-- merge, follow-up, related, new -- is ever applied (merges stay with an
officer or an approved decision batch).

The closure has no human actor. Its status event says it was automatic and
why, an `IntakeAutoApplication` row (append-only) ties it to the
classification, and `intake.auto_applied` is audited. Nothing is sent to the
requester (rule 6). Records Officers get an email
(`intake.auto_applied`), and the case keeps its open inbox item, since no
officer handled it. On the case, **Undo** is the ordinary reopen back to
`received`, which appends a new event and leaves the closure on the timeline;
**Keep** records the officer's agreement. `test_compliance` pins
`intake_trust` as the only caller of the system closure.

An automatic closure an officer has not answered is not a gold label: the
evaluation set leaves it out, so the model is never scored against its own
decisions.

## Comparing models on past decisions

Models are chosen per kind from evidence, not by trial and error in live
casework (issue #32). Two management commands do it; both are human
operations and neither changes a case.

```bash
uv run python manage.py build_eval_set --kind intake --actor officer@psd401.net
uv run python manage.py evaluate_models --set <id> \
  --models us.anthropic.claude-haiku-5-5,us.openai.gpt-6.1-terra \
  --price us.anthropic.claude-haiku-5-5=<in>/<out> --actor officer@psd401.net
```

`build_eval_set` labels examples from what staff already decided
(`apps/records/evaluation.py`) and stores **references and labels only** in an
`EvaluationSet` -- no request or record text:

- `intake`: `new` (the case went on as a request), `existing` (merged into, or
  its message added to, another case, with that case's number), or
  `not_request` (closed as not a records request). An officer's answer to an
  intake suggestion outranks the case outcome. Cases still `received` are left
  out.
- `responsiveness`: `responsive` (reviewed responsive or withheld) or
  `non_responsive`, for records with searchable text.

Every matter kind is included (rule 13). An intake example's candidate cases
come from the live builder, which never offers a case of another kind, and the
set only scores models; it sends no requester mail and starts no clock.

Request intent and redaction have no single right answer to score against
yet, so `build_eval_set` refuses them.

`evaluate_models` rebuilds each input from the live rows with the same builder
a live advisory uses, sends it to every named model through
`invoke_advisory_model`, and validates each answer the same way. **Real text
reaches the provider**, so it refuses to start unless advisory AI is enabled
and configured, exactly as a live run would. It does not check the named
models itself: the production grant (`infra/cdk/lib/advisory-bedrock.ts`) is the
allow-list, so AWS refuses a model it does not name and every example for that
model scores as an error. Name only the profiles listed below. It stores an `EvaluationRun` with,
per model: accuracy, a confusion matrix, the **safety recall** (real requests
not labelled non-request; responsive records not labelled non-responsive,
where `uncertain` counts as kept because a person decides it), the share of
`existing` cases pointed at the right case, mean tokens, p50/p95 latency, and
cost per item. A failed call counts as an error and a miss. Cost needs a
`--price` (USD per million input/output tokens) per model; the application
holds no price list, so a model without one reports no cost.

Records Officers read the runs side by side under **Oversight → AI
evaluation** (`advisory_evaluations.list`).

## Stored agreement and record clusters

Every human responsiveness decision -- single, selected bulk, or filter bulk
-- writes an append-only `ReviewDecision` (`apps/records/review.py`). It keeps
the outcome, the reviewer, whether it was bulk, and the suggestion standing at
decision time: the record's latest *complete* responsiveness run, the same rule
the review list uses to show one (`apps/records/agreement.latest_suggestion_runs`),
with its label and confidence copied. If a newer run completed after the
reviewer's page loaded, the stored suggestion is that newer one; the review
request does not name the run it rendered. Agreement therefore measures the
model's standing answer against the human one, not anchoring on what was
displayed. A bulk audit row also lists those run ids
(`advisory_run_ids`). Storing the suggestion never changes the decision.

`agreement_report` turns those rows into agreement by suggested label and
confidence band (90-100%, 70-89%, 50-69%, below 50%), district-wide or for one
case. Only each record's latest decision counts. Withheld counts as
responsive; `uncertain` hands the call to a person and has no agreement rate;
a `duplicate` outcome is not a responsiveness call and is left out. Records
Officers read it under **Oversight -> AI agreement**
(`advisory_agreement.list`). These numbers are what trust-ladder thresholds
(#34, #39) are to be set from. The report covers every matter kind (rule 13):
it measures the model, not a statutory obligation, and starts no clock.

The Records tab's **Group by cluster** view (`record_items.clusters`) groups
every record the current filters match -- not just one page -- by email thread
(`In-Reply-To` -> `Message-ID`), sender or mailing list (`List-Id` first, then
sending domain), tag, or AI label and confidence band. Each cluster shows its
size, up to three sample records, and the majority AI suggestion. A cluster is
a way to look at records; it decides nothing.

Automatic responsiveness runs (`PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED`) use
the review model, `PRR_AI_MODEL_RESPONSIVENESS`, falling back to
`PRR_AI_MODEL` when it is blank.

## Estimates from measured agreement

`apps/records/estimates.estimate_request(request)` (#41) computes an estimate
for a `public_records` case and stores it, with every input, as an append-only
`RecordsEstimate`; an `records.estimate_computed` audit row names it. Other
matter kinds are refused (rule 13). It changes nothing on the case and sends
nothing: a person revises the statutory estimate and approves any letter.

- **Responsive range.** Reviewed records are certain (withheld counts as
  responsive). Unreviewed records are grouped by their standing AI suggestion
  and scaled by how often people found a record responsive after that
  suggestion (the latest `ReviewDecision` per record, district-wide). The range
  is each group's 95% Wilson interval; a label with no measured decisions, and
  a record with no suggestion, span zero to all.
- **Fee.** The adopted `FeeSchedule` in effect, applied to records that would
  be released (responsive, not withheld) for the low, expected, and high
  cases: per-four-file bundles and per-GB, with the scanned-page line at zero
  because the records are already electronic. The deposit is up to ten percent
  of the expected fee (RCW 42.56.120(4)); the flat-fee option and the adopted
  waiver floor are flagged. No adopted schedule means no fee, stated.
- **Completion date.** `record.reviewed` audit rows per business day over the
  trailing 20 business days, applied to every unreviewed record in open cases
  received earlier (all matter kinds, since they share reviewers) plus this
  case's own, on the WA business-day calendar. It covers review only. No
  measured throughput means no date, stated.

## The interpretation-and-estimate letter

With `PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED` on (it requires advisory AI),
an hourly task (`apps/records/estimate_letters.py`) finds open public records
cases with a completed intent proposal, at least one record, and an AI
suggestion on every unreviewed record. For each it computes an estimate and,
if the estimate has a completion date, drafts the `interpretation_estimate`
letter (template `interpretation_estimate/v1`, counsel review required): the
intent summary as the District's interpretation, the expected responsive count
and range, the copying charge from the adopted schedule (or a neutral sentence
when none is adopted), and the completion date as the RCW 42.56.520 estimate.
A case with a letter in any state but cancelled, or with an undated estimate
computed today, is skipped.

The draft is *system-prepared* and is never sent by the system. A Records
Officer sees it on the case header (`requests.estimate_letter.read`) and
sends it with **Approve and send** (`correspondence.approve_and_send`), or
rewords the interpretation first under Messages; the count, charge, and date
are the estimate's and are not edited there. Delivery records the date as an
`EstimateRevision` revised by that officer and names the estimate. A deposit is
still the existing officer action (`fee_deposit_gates.apply`); the requester's
reply arrives as ordinary inbound mail.

## Bulk review is a human decision, not an accept-all

The review queue lets a reviewer mark a whole filtered selection responsive or
non-responsive in one confirmation
(`record_items.bulk_review`, `apps/records/review.bulk_review_records`). This
is **not** an accept-all of AI output, and the distinction is structural rather
than a matter of wording:

* The request carries the record identifiers the reviewer selected and the
  outcome the reviewer chose. It carries no advisory run, no proposal, and no
  confidence threshold. The service looks up each record's standing
  suggestion only to store it beside the decision (see below); it never
  decides what is written.
* The set is what the reviewer filtered and could see. Tag, sender-domain,
  custodian, and text filters are facts about the records; the AI-confidence
  filter and sort only change what a reviewer looks at first, never what the
  confirmation writes.
* Every record goes through the same per-record review service the
  single-record control uses, so each gets its own validation and its own
  append-only audit row naming the reviewer, plus one row for the bulk action.
* An explicit selection is capped at 200 records. A reviewer can instead
  choose **Select all N matching** to decide every record the current filter
  matches (`record_items.bulk_selection`, then `record_items.bulk_review` with
  a `filter`). The confirmation first shows the server's exact count of
  records the outcome would change, and which records it skips and why; the
  decision sends that count and a SHA-256 digest of the record ids back, and
  the server refuses it (`409 selection_changed`) if the set changed in
  between. A filter may name only one request and match at most 10,000
  records. A filter decision is made only in the staff application: a token
  caller may preview the count but receives the `human_approval_required`
  handoff for the decision itself. Each record still gets its own audit row;
  withheld, duplicate, superseded, already-decided, and (for non-responsive)
  redacted records are skipped and counted rather than overturned. The bulk
  audit row keeps the filter but stores a SHA-256 of any free-text search,
  never the typed words.
* Only `responsive` and `non_responsive` are available. Withholding a record
  in full needs its own ExemptionCitation and second review, and a duplicate
  finding needs its own judgement, so neither can be expressed by one
  confirmation over a set. A record already withheld or already marked a
  duplicate is left out of a bulk selection for the same reason, and the
  service refuses a batch that carries one rather than overturning it.

For records, there is still no path by which a model result becomes an
applied legal outcome on its own. The trust ladder below lets a named
reviewer decide a set after checking a random sample of it; that is the
Records Officer's chosen policy, and the reviewer's decision, not the
model's, is what applies. The one exception anywhere is the intake trust
ladder above: in `auto` mode, a spam or non-request label closes a new email
case, with officer notice and one-click undo.

## Trust ladder: sampled batch approval and the elusion test

The Records Officer's trust policy (#39) lives in the in-app settings store,
admin-only with a reason, and every change is audited:

| Setting | Values | Default |
|---|---|---|
| `PRR_TRUST_RESPONSIVE_MODE` | `suggest`, `batch` (no automatic mode) | `suggest` |
| `PRR_TRUST_NON_RESPONSIVE_MODE` | `suggest`, `batch`, `auto` | `suggest` |
| `PRR_TRUST_REDACTION_MODE` | `suggest`, `batch` (no automatic mode) | `suggest` |
| `PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE` | 50-100 percent | 95 |
| `PRR_TRUST_BATCH_SAMPLE_SIZE` | 1-200 records | 10 |
| `PRR_TRUST_ELUSION_SAMPLE_SIZE` | 1-500 records | 30 |

- **Suggest** is per-record review, as before.
- **Batch.** In the Records tab's cluster view, **Approve cluster as ...**
  opens a `ReviewBatch` over the cluster's unreviewed records and draws a
  random sample (`random.SystemRandom`). The reviewer judges each sampled
  record. If every judgment matches, the outcome is applied to the whole
  cluster through the ordinary review service in the reviewer's name (each
  record gets its own audit row and `ReviewDecision`). One disagreement blocks
  the batch: the sample judgments are recorded and every other record stays
  unreviewed for per-record review.
- **Auto (non-responsive only).** Unreviewed records whose standing AI
  suggestion is non-responsive at or above the floor are staged as **Ready to
  discard**. Nothing is applied until a reviewer runs the elusion check: a
  random sample of the pile, judged one by one. Zero responsive findings
  applies non-responsive to the pile in the reviewer's name; any responsive
  finding blocks it. The batch stores the sample, the judgments, the floor,
  and the outcome; `review.batch_decided` records `elusion_responsive_found`.
  With no finding in 30 sampled records the pile's miss rate is below about
  10% at 95% confidence; larger samples catch rarer misses.

Responsive is never automatic: its mode has no `auto` value, the service
treats a stored `auto` as `batch`, and the database refuses a discard batch
that proposes anything but non-responsive. Release stays a human action.
`AdvisoryRun.applied` stays false: the batch decision is the reviewer's.
Deciding a batch is `review_batches.decide`, human-only; opening one decides
nothing.

### Redaction proposals, a group at a time

Each redaction proposal now carries a `pattern` (phone, email, address,
student identifier, government ID, financial account, medical, person's
name, other); proposals made before that are `other`. The Records tab's
**Redaction proposals** card groups a case's pending proposals by exemption
and pattern (`redaction_batches.groups`). With `PRR_TRUST_REDACTION_MODE` set
to `batch` (choices `suggest`, `batch`; no automatic mode), **Accept all in
group** opens a `RedactionBatch` and draws a random sample of
`PRR_TRUST_BATCH_SAMPLE_SIZE`. The reviewer accepts or rejects each sampled
proposal (a rejection needs a reason). Only if every sampled proposal is
accepted are the rest accepted, each through `accept_redaction_proposal` ->
`create_redaction` with its own box and exemption citation (rule 3); one
rejection blocks the group and leaves the rest pending. Second review follows
from each redaction's exemption as it always has. **Build redacted PDFs**
(`redaction_batches.build`) renders every responsive record whose redactions
are approved and whose required second review is done, and counts the ones
still waiting. Deciding a group and building PDFs are human-only.

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

Automatic results remain proposals. Responsiveness still needs a named human
decision: one record at a time, or a sampled batch under the trust policy
above. Each redaction candidate still needs an individual
named human accept/edit/reject decision with an active exemption citation, one
at a time or as a sampled group (below); no accept-all path exists. Second review and immutable raster burn-in remain
unchanged.

Request-intent proposals distinguish exact subject identifiers from topical
alternatives. A person name or case identifier that every result must contain
is a mandatory `AND` anchor; record-category terms and synonyms are grouped as
`OR` alternatives. For example, the UI drafts `"Jordan Sample" AND
("educational records" OR "counseling records")`, never a query that makes the
person's name optional. The Records Officer reviews and may edit the complete
Vault query before requesting a count.

### District knowledge in the intent input

Request-intent input (schema version 6) also carries what the Records Officer
knows about where records live. Each active registry system is offered with
its custodian title and email, retention days and note, adequate-search
checklist, and the officer's own search notes. A `district_knowledge` section
adds the role-to-account mappings (each with its date range; two holders of one
role may overlap, as in a handover or a co-principalship, and both are sent) and the active
noise senders from **Configuration > Knowledge**, which only a Records Officer
can read or edit and where every change is audited.

The whole context is cut to fit under `PRR_AI_MAX_INPUT_CHARACTERS` together
with the request description. Each system's slug, name and keywords are always
kept, since `record_system_slugs` may use only those; that list alone is not
bounded, exactly as before this change, so a registry too large for the ceiling
would exceed it rather than lose systems. A checklist or search
note over 2,000 characters is shortened, and the remaining detail, role
accounts and noise senders are added only while they fit. Anything cut marks
the run's input as truncated. Because the pack is in the input fingerprint, an
edit made between queueing a run and executing it fails that run as
`source_changed` rather than proposing from stale context.

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
| `PRR_AI_MODEL_INTENT` | blank (uses `PRR_AI_MODEL`) | model for request intent |
| `PRR_AI_MODEL_CASE_TITLE` | blank (uses `PRR_AI_MODEL`) | model for case titles |
| `PRR_AI_MODEL_RESPONSIVENESS` | blank (uses `PRR_AI_MODEL`) | model for record responsiveness |
| `PRR_AI_MODEL_REDACTION` | blank (uses `PRR_AI_MODEL`) | model for redaction candidates |
| `PRR_AI_TIMEOUT_SECONDS` | `30` | 1-120 |
| `PRR_AI_MAX_INPUT_CHARACTERS` | `50000` | 1000-250000 |
| `PRR_AI_MAX_RESPONSE_BYTES` | `65536` | 1024-1048576 |

The district's chosen model is `us.amazon.nova-2-lite-v1:0` in `us-west-2`,
and the production Compose overlay still carries those three values as the
one-time bootstrap seed the settings-store import reads. Editing the overlay
afterwards does nothing: change the model on the Settings page, where the
change has a name and a reason on it.

Each advisory kind can run on its own model, so the district can trial a
strong model for request intent and a fast, cheap one for record review, and
move one kind at a time. Every run records the model it was created with, and
the Bedrock call uses that recorded model even if the setting changes before a
worker picks the run up. Output budgets are per kind (`MAX_OUTPUT_TOKENS` in
`apps/records/advisory_ai.py`: intent 2000, case title 1000, responsiveness
1000, redaction 4000); replies are still bounded by the response ceiling and
validated against their schema.

The production grant (`infra/cdk/lib/advisory-bedrock.ts`) is the real allow
list. A setting naming any other model fails closed as `provider_rejected`.
Each entry is a US cross-region inference profile whose destination models are
in `us-east-1`, `us-east-2`, and `us-west-2` only:

- `us.amazon.nova-2-lite-v1:0`
- `us.anthropic.claude-opus-5-5`
- `us.anthropic.claude-fable-5-1`
- `us.anthropic.claude-sonnet-5-5`
- `us.anthropic.claude-haiku-5-5`
- `us.openai.gpt-6.1-sol`
- `us.openai.gpt-6.1-terra`

Before pointing a setting at a model for the first time, run
`python manage.py verify_advisory_ai --model <profile id>` in production. It
sends synthetic data only and confirms the model answers through the Converse
API with valid bounded JSON.

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
audit details. The deployed IAM policy pins each allow-listed US inference
profile and its documented US destination models; there is no wildcard Bedrock
grant.

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
