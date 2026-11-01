# Roadmap: making the system fully usable

Written 2026-09-21 from three meetings (2026-05-28, 2026-06-26, 2026-09-09)
reconciled against the code at commit `705faeb`. This is the working plan for
the next phases and the source for the GitHub issues that will track them.
Decisions recorded here were made by the CIO on 2026-09-21; the rest is
proposal until an issue is filed.

> Proposal status: sections marked **Decision** are settled. Everything else
> is a plan that an issue, a design doc, or counsel review may still change.

## Decisions (2026-09-21)

| # | Question | Decision |
|---|---|---|
| 1.1 | Download security | Possession token plus a requester-facing page, expiry, download counter, and officer revoke. An access code is optional per installment, chosen by the officer. |
| 1.2 | Hand-typed download URL in the notice composer | Remove it. The server builds the notice from the installment. |
| 2.1 | Bulk review | Every review role may bulk-review. Cap 200 items per action. |
| 2.2 | Counsel-domain list | Holds only the district's own law firms, because privilege attaches to communications with district counsel. Plaintiffs' firms need no list; the external-sender tag and a sender-domain filter cover them. Seeded empty; an application administrator enters the domains. |
| 3.1 | Automatic analysis at intake | Auto-run both the request-intent advisory and the Vault count at intake. |
| 3.2 | Scope letter and the clock | The officer chooses per case whether the letter is informational or a clarification request under RCW 42.56.520(3), which the existing clarification handling in `services.transition()` tolls; if the requester never answers and the request stays unclear, RCW 42.56.520(3)(b) applies. |
| 4.1 | Batch approve-and-send | Allowed for administrative closure notices (completion, non-payment, general). Never for denials or no-records closures. |
| 4.2 | Third-party notice window | 10 business days. Counsel confirms the template wording. |
| 5.1 | Fee schedule | The district will adopt one with counsel; Phase 5 is in scope. |
| 5.2 | Waiver floor | $1.00, entered with a counsel reference. |
| 6.1 | Other matter kinds | In scope. Litigation/discovery pulls and FERPA parent requests become distinct kinds alongside public records. Design doc first. |
| 7.1 | Plan location | This file, with one GitHub issue per phase: #238 (Phase 0) and #232 through #237 (Phases 1 to 6). |

## What the meetings asked for versus what the code has

Already built and working: the public form, email intake, the safety-net
acknowledgment, case merge, the NextRequest import with legacy timeline,
one-action approve-and-send, district-editable template wording, the Gmail
`PRR/Processed` label, the AI intent → Vault query → count → collect → ingest →
review pipeline, AI redaction candidates bound to exemption citations,
withhold-in-full with second review, the exemption log, package preparation,
the expiring token download, and MCP/agent parity for all of it.

| # | Ask | Code reality (2026-09-21) | Evidence |
|---|---|---|---|
| G1 | Package records as releases, send a link, then "here is your first installment" | Unreachable. Nothing outside tests creates an `Installment` or adds records to one. The Releases tab has fee, log, prepare, and release buttons only. The notice must be composed on Messages with the URL retyped by hand. No requester page, no access code, no revoke. | `apps/api/resources/records_misc.py:1116`, `frontend/src/pages/Installments.tsx`, `frontend/src/pages/CorrespondencePanel.tsx:1902` |
| G2 | "Your request matched 29,380 items, would you like to narrow?" with options | Absent. The count is stored but no letter kind carries a count, cost, or options. The intent advisory proposes no date range and never sees the receipt date. | `apps/correspondence/automatic_templates.py:342`, `apps/records/advisory_ai.py:651` |
| G3 | Search end date defaults to the request date | Not implemented. The end date starts blank. | `frontend/src/pages/RequestDetail.tsx:347` |
| G4 | One email in many mailboxes reviewed once | Byte-hash dedup only. The rendered hash includes To/Cc so per-recipient copies survive. Message-ID is stored but never used. | `apps/records/vault_ingest.py:692`, `:729` |
| G5 | Flag out-of-office, calendar, list mail, non-district senders, counsel, privilege markers | Nothing. `RecordItem` stores no From, To, or domain. | `apps/records/models.py:885` |
| G6 | Review thousands of records quickly | No bulk review. One record per action. | `apps/api/resources/records_misc.py:219` |
| G7 | Notify an employee whose personnel file is requested | Absent. No RCW 42.56.540 kind or workflow. | |
| G8 | "You have received everything", non-payment closure, "not required to create a record" | Free-form `closure` starting point only. | `apps/correspondence/automatic_templates.py:453` |
| G9 | Deposit, fee estimate, waive small amounts, close unpaid after 30 days | `deposit_amount` is stored and read by nothing. No fee letter; installment notice v3 dropped the fee sentence. No waiver floor. No non-payment path. | `apps/records/fees.py:331`, `apps/records/production.py:405` |
| G10 | Archive processed mail out of the inbox | Label only; `removeLabelIds` is always empty. | `apps/integrations/gmail/client.py:661` |
| G11 | Litigation and FERPA work in the same tool | DONE (#237). `PublicRecordsRequest.matter_kind` with the statutory machinery narrowed to `public_records`. | `docs/decisions/0004-matter-kinds.md` |
| G12 | Run analysis automatically | Intent advisory and count are manual buttons. Record-level advisories already auto-run at ingest. | `apps/api/resources/requests.py:2135` |

## Facts that bound the features

- **Copying fees, not search fees.** RCW 42.56.120 forbids charging to
  locate records. Chargeable costs are copying only: 10 cents a page, 5 cents
  per four electronic files, 10 cents a gigabyte, or a $2 flat fee. 29,380
  emails at the electronic rate is roughly $367; a deposit is capped at 10%,
  about $37. The volume number and the installment schedule are the lever,
  not dollars. A customized-service charge under RCW 42.56.120(3) applies only
  to IT customization.
- **No fee communication without an adopted schedule.** `activate_fee_schedule`
  requires a counsel approval reference (`apps/records/fees.py:78`).
- **Advisory AI stays advisory.** Auto-running the intent advisory and the
  count adds no requester-facing effect. `docs/project-status.md` records that
  production's AI switches were found enabled without retained approval
  evidence; that evidence is an operational precondition, not code.
- **Rule 6 holds.** No automated path may draft, send, or defer a statutory
  response. Every new letter kind is human-drafted and AST-guarded.
- **FERPA has its own clock.** A parent or eligible student request is
  governed by 34 CFR 99.10(b), with 45 days to comply, and is not a PRA request.
  It must never enter the RCW 42.56 clock, the safety-net acknowledgment, or
  JLARC reporting.

## Phases

Each phase is one or more PRs to `main`, deployed with the single CDK command
in `docs/aws-deploy.md`. Order reflects what blocks delivering records today.
Every phase registers its endpoints in `apps/api/capabilities.py` in the same
change (rule 10) and appends audit rows for every step (rule 4).

### Phase 0. Quick wins (#238)

- Default the Vault end date to `received_at`. Pass the receipt date to the
  intent prompt with the instruction that responsive records are those in
  existence at receipt.
- Add `PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED`; when true, remove `INBOX` after
  the processed label is applied. Default false.
- Correct the `/installments` capability surface that no route serves.

Done when: a new case shows the receipt date as the default end date; a
processed message leaves the inbox when the flag is on; the capability drift
test passes.

### Phase 1. Release path end to end (#232)

- Backend: `installments.create` from selected record ids;
  `installments.set_items` while no package is prepared; derived
  `release_ready` and `unreleased` filters on record items; the notice is
  drafted server-side from the installment with no URL input; one action
  "release and send notice" that releases, drafts, and approve-and-sends;
  `installments.revoke`; a download counter.
- Requester-facing download page: what the installment contains, the expiry,
  the officer's contact, then the package. An officer may set an access code
  on an installment; when set, the page requires it and the code is sent in a
  separate message.
- Releases tab: the ready-to-release list with select-all-filtered → Create
  installment → fee, log, prepare → Release and send.
- Remove the hand-typed URL field from the composer.

Done when: an officer creates, releases, and sends from the Releases tab
without retyping anything; the requester downloads; every step audits; a
revoked link returns 410.

### Phase 2. Review throughput (#233)

- Store email metadata on `RecordItem` at ingest: from, to, cc, date,
  Message-ID, In-Reply-To, sender domain, `Auto-Submitted`, calendar part,
  list headers. A backfill command populates existing items from retained raw
  messages.
- Dedup by Message-ID within a request, with the byte hash as fallback. The
  surviving item carries every custodian it was found in. Replace the
  comma-joined custodian attribution.
- Deterministic tags, facts not decisions: `auto_reply`, `calendar`,
  `bulk_list`, `external_sender`, `counsel_domain`, `privilege_marker`.
  `counsel_domain` reads an administrator-managed list of the district's own
  law firms, seeded empty.
- Records tab: filter, sort, and group by tag, sender domain, custodian, and
  AI confidence; full-text search over OCR text.
- Bulk human review: select a visible filtered set → responsive or
  non-responsive in one confirmation. Any review role. Cap 200. One audit row
  per item. `docs/advisory-ai.md` is revised to say that bulk is a human
  decision on a visible set, not an accept-all of AI output.
- Privilege queue: items tagged `counsel_domain` or `privilege_marker` in
  their own filter. The AI may propose withhold-in-full candidates bound to
  RCW 5.60.060 or RCW 42.56.290; the existing withheld path and second review
  apply unchanged.

Done when: one message in N mailboxes appears once in review with N
custodians; every out-of-office reply on a case can be marked and released in
one pass; counsel-domain items are separable before anything ships.

### Phase 3. Scope conversation with the requester (#234)

- Intent contract adds `date_range` (end defaults to receipt) and up to four
  `narrowing_options`, each with terms and a rationale. Display only.
- At intake, enqueue the intent advisory, then a Vault count using the
  proposed query, end date at receipt, entire-org scope, only when the
  advisory maps the request to a Vault system. The officer may re-run with
  edits. Nothing here touches the requester.
- New structured kind `scope_clarification`: count, query summary,
  officer-chosen options, response-by date, and a fee line that renders only
  when a schedule is active. The officer chooses informational or
  clarification; only the clarification form enters the existing tolled `CLARIFICATION` status under RCW 42.56.520(3).
  Human draft → approve-and-send. An AST guard proves no task can create it.

Done when: a new case shows a count without a click; an officer sends "your
request matched N items, here are three narrower options" from the Collection
tab; the AST guard test passes.

### Phase 4. Templates and closures (#235)

- `third_party_notice`: recipient is the named employee plus optional
  attorney, a 10-business-day response-by date, the disclosed requesters.
  Audited on the case, never sent to the requester.
- Structured `completion_closure` that applies `FULFILLED` on confirmed
  delivery through `transition()`.
- `nonpayment_closure` → `CLOSED_ABANDONED` with reason code `fee_unpaid`.
- `no_records_closure` gains a `not_maintained` determination with "the
  District is not required to create a record" language (WAC 44-14-04003(6)).
- Multiple named `general` templates instead of one slot.
- Inbox multi-select → draft closure notices for N cases from one template;
  `correspondence.batch_approve_and_send`, human confirmation, limited to
  completion, non-payment, and general kinds, cap 50, one audit row and one
  independent delivery per case.
- Every new template carries the COUNSEL REVIEW REQUIRED marker.

Done when: each kind can be drafted, sent, and found on the case; a batch
of closures produces N audited deliveries; a denial cannot enter the batch.

### Phase 5. Fees (#236)

- Enter the counsel-adopted schedule through the existing activation path.
- `fee_estimate` letter with the deposit request and payment instructions.
- A deposit gate the officer may apply to collection or production.
- Waiver floor setting, $1.00, with a counsel reference; assessments under the
  floor prompt the officer to waive. No automatic waiver.
- A 30-day-unpaid chip in the Inbox that feeds the Phase 4 non-payment
  closure. No automatic close.

Done when: an officer sends an estimate with a deposit line; a release is
blocked until the deposit is recorded when the gate is applied; an unpaid
case surfaces at day 30 and closes only by human action.

### Phase 6. Matter kinds: litigation and FERPA (#237)

Design doc first, as `docs/decisions/0004-matter-kinds.md`. Candidate shape:

- A `kind` on the case: `public_records`, `litigation_discovery`,
  `ferpa_parent`. Collection, review, redaction, and production are shared.
- `public_records` keeps every current rule.
- `litigation_discovery`: no statutory clock; deadlines set by counsel; no
  requester correspondence; production goes to counsel through the Phase 1
  release path; excluded from JLARC and from the safety net.
- `ferpa_parent`: a 45-day clock under 34 CFR 99.10(b); identity verification of
  the parent or eligible student recorded on the case; copying fees only;
  excluded from JLARC and the safety net; never reachable from the anonymous
  public form.
- Invariants the design must prove with tests: the safety net, the RCW
  42.56.520 clock, and JLARC counts see only `public_records`.

Done when: the ADR is accepted; then each kind ships behind its own tests.

DONE (#237). ADR 0004 is accepted and both kinds ship: `apps/records/tests/test_matter_kinds.py` proves the three invariants against the production querysets, and `matter-kind-ast-guard` in `test_compliance` keeps every task away from the one human-only entry point. Two counsel-reviewed templates are deliberately not written yet: a FERPA copying-fee letter and a counsel-facing production cover letter.

## Out of scope for this project

Email forwarding consolidation, retention purge and disposition automation,
the CBA and board-policy repository, leave forms and timesheets, and paper
records digitization. Each is district work in another system.

## Preconditions outside the code

- Retain the advisory-AI approval evidence noted in `docs/project-status.md`.
- Counsel review of every new template before activation.
- The adopted fee schedule with its counsel approval and adoption references.
- The district's own counsel domains for the `counsel_domain` list.

## Success criteria for the roadmap

| Criterion | Status |
|---|---|
| An officer releases an installment and its notice from one tab; the requester downloads | PENDING |
| One email across N mailboxes appears once in review | DONE (#233) |
| Auto-reply, calendar, external, and counsel-domain items are filterable and bulk-reviewable | DONE (#233) |
| A new case shows an AI-proposed date range ending at receipt and a count without a click | DONE (#234) |
| An officer sends a count-plus-options letter with no automation touching the requester | DONE (#234) |
| Third-party notice, completion, non-payment, and not-maintained letters exist and audit | DONE (#235) |
| A fee estimate with a deposit line can be sent under the adopted schedule | DONE (#236) |
| The matter-kinds ADR is accepted with the three invariants tested | DONE (#237) |
| Full suite, capability drift test, and AST guards pass | PENDING |
