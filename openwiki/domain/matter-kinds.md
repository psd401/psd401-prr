---
type: Domain
title: Matter Kinds
description: The case-level matter kind (public_records, litigation_discovery, ferpa_parent) that decides which law governs a case - its clock, letters, safety net, JLARC inclusion, case-number prefix, and intake restrictions. Explains ADR 0004 and where each rule is enforced.
tags: [domain, matter-kind, litigation, ferpa, rcw-42-56, adr-0004]
timestamp: 2026-10-26T00:00:00Z
openwiki:
  roles: [domain, architecture]
  change_kinds: [matter-kind, statutory-rule, intake-boundary]
  source_paths: [apps/records/models.py, apps/records/intake.py, apps/records/deadlines.py, apps/records/response_due.py, apps/records/services.py, apps/records/jlarc.py, apps/correspondence/services.py, apps/api/resources/requests.py]
  symbols: [MatterKind, MATTER_KIND_NUMBER_PREFIXES, PublicRecordsRequest, ferpa_access_due, record_ferpa_identity_verification, merge_requests, PRA_STATUTORY_CORRESPONDENCE_KINDS, next_response_due]
  test_paths: [apps/records/tests/test_matter_kinds.py, apps/records/tests/test_next_response_due.py, apps/records/tests/test_request_numbering.py, apps/records/tests/test_compliance.py]
  invariants:
    - "matter_kind is fixed at intake and cannot change through save(), queryset update(), bulk_update(), or the database."
    - "litigation_discovery never computes statutory_response_due and never sends requester correspondence."
    - "ferpa_parent uses a 45-calendar-day ferpa_access_due and never the RCW business-day engine."
    - "Only public_records matters can arrive through the public form or the intake mailbox."
    - "Cases governed by different law cannot be merged."
  validation_commands: ["PYTHON_DOTENV_DISABLED=1 python manage.py test apps.records.tests.test_matter_kinds --settings=config.test_settings"]
---

# Matter Kinds

A case has a **matter kind** that decides which law answers it. The governing
decision is [ADR 0004](../../docs/decisions/0004-matter-kinds.md), and AGENTS.md
rule 13 repeats it as an architecture rule. Collection, the system registry,
triage, review, redaction, production, the [work queue](work-queue.md), and the
audit trail are shared by every kind. The *statutory* machinery is not.

## The three kinds

| `MatterKind` value | Governing law | Clock | Number prefix |
|---|---|---|---|
| `public_records` | RCW 42.56 (the default and every pre-existing row) | Five business days, `statutory_response_due` | `PRR-` |
| `litigation_discovery` | Counsel's records pull for a claim or lawsuit | None. The due date counsel sets goes in `estimated_completion` | `LIT-` |
| `ferpa_parent` | 34 CFR 99.10 for a parent or eligible student | 45 calendar days, `ferpa_access_due` | `FER-` |

The enum is `MatterKind` in [`apps/records/models.py`](../../apps/records/models.py).
Each prefix runs its own sequence in `PublicRecordsRequest._next_number`, so a
number is never confused with a public records request in mail, an export, or a
court filing.

## What each kind does

- **Public records.** Unchanged. The day-5 safety net, the deadline watchdogs,
  JLARC counting, and the RCW letter instruments belong to this kind alone.
- **Litigation.** No statutory clock. `_create_draft` in
  [`apps/correspondence/services.py`](../../apps/correspondence/services.py)
  refuses the matter, so it sends no requester correspondence. Production reaches
  counsel through the release path, which is a records transfer rather than
  correspondence. The matter is excluded from the safety net, from deadline
  escalation, and from [JLARC reporting](jlarc-reporting.md) (`apps/records/jlarc.py`
  refuses a non-public-records classification).
- **FERPA parent.** `deadlines.ferpa_access_due()` computes 45 calendar days. It
  shares none of the business-day engine's holiday, rollover, or tolling logic.
  `statutory_response_due` stays null. Intake sets `ferpa_access_due` after
  `prr.save` (`apps/records/intake.py`). Identity verification is recorded on the
  case by `record_ferpa_identity_verification()` in
  [`apps/records/services.py`](../../apps/records/services.py). It is one
  append-only `FerpaIdentityVerification` row per case, and it is an attributed
  Records Officer action with its own audit row. A thin note is refused, and the
  method must be one of `in_person_photo_id`, `notarized_statement`,
  `district_records_match`, or `court_order`. The staff API exposes it as
  `POST /api/staff/requests/{number}/ferpa-identity-verification/`. That action is a
  `human_only_handoff` under the `requests.verify_ferpa_identity` capability, so an
  agent cannot record a verification. Only the Records Officer role sees the note text.

## Immutability and intake boundary

`matter_kind` is immutable after intake, enforced the same way as `description`.
The ORM rejects changes in `save()`, in `PublicRecordsRequestQuerySet.update()`,
and in `bulk_update()`, and a database trigger blocks rewrites below the ORM
(`MATTER_KIND_IMMUTABLE_MESSAGE` in `models.py`; see
`test_matter_kinds.MatterKindFieldTests`). A miscategorized case is closed with a
reason and reopened as the right kind, so both records stay on the timeline.

Intake in `apps/records/intake.py` refuses any requester-originated channel
(`REQUESTER_ORIGINATED_CHANNELS`) that asks for a non-public-records kind. Only a
staff actor on a staff channel may open a litigation or FERPA matter. `merge_requests()`
refuses to merge two matters of different kinds.

## Letters by kind

Letters whose adopted text asserts RCW 42.56 are `public_records`-only. The set
is `PRA_STATUTORY_CORRESPONDENCE_KINDS` in `apps/correspondence/services.py`:
acknowledgment, clarification request, scope clarification, estimate revision,
interpretation and estimate, fee estimate, no-records closure, nonpayment closure,
third-party notice, and denial. The interpretation-and-estimate letter is drafted by
the system and approved by a Records Officer, as described in [estimates](estimates.md).
The informational scope letter is drafted by the system for `public_records` cases only
and is likewise approved by a person ([correspondence](correspondence.md#scope-conversation-letter)).
A FERPA matter may still send letters that make no RCW assertion.

## Enforcement status

Recording a FERPA verification is implemented and tested. I did not find a
production, installment, or release check that refuses to disclose a
`ferpa_parent` matter until that verification row exists. No test asserts that
refusal either. ADR 0004 and AGENTS.md state the rule, so treat the release gate
as an open verification item (also listed in [the quickstart backlog](../quickstart.md#backlog)).

## Where kinds show up

- The case list and the Next due column. `next_response_due()` in
  [`apps/records/response_due.py`](../../apps/records/response_due.py) returns a
  basis for each kind: `statutory`, `estimate`, `district_target`, `ferpa`, or
  none. The table in that module is the canonical rule. See
  [Case workspace](case-workspace.md).
- The request serializers in `apps/api/resources/requests.py` expose
  `matter_kind` and `matter_kind_display` read-only and reject a change.
- JLARC reporting in [`apps/records/jlarc.py`](../../apps/records/jlarc.py) counts
  only `public_records` matters, and its per-request classification refuses any
  other kind. See [JLARC annual reporting](jlarc-reporting.md) and [Compliance](compliance.md).

## Change guidance

- Any new statutory query, watchdog, report, or letter must say which kinds it
  applies to. Add the kind check at the same place the statutory behavior lives.
- Do not reuse the business-day engine for FERPA dates. Use
  `ferpa_access_due()`.
- Run `test_matter_kinds` for any change to intake, the clock, correspondence
  refusal, JLARC, or merges. Run `apps.records.tests.test_next_response_due` if
  you change the Next due basis table.

Related: [Request lifecycle](requests.md), [Compliance guarantees](compliance.md),
[Correspondence](correspondence.md).
