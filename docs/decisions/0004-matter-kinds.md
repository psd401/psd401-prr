# ADR 0004: matter kinds — litigation/discovery and FERPA parent requests

- **Status:** Accepted
- **Date:** 2026-09-23
- **Issue:** #237 (roadmap Phase 6, decision 6.1 of 2026-09-21)

## Context

The application is PRA-only. Every case is a request under RCW 42.56: it gets a
five-business-day clock, it is eligible for the day-5 safety-net
acknowledgment, and it is counted in the RCW 40.14.026 (JLARC) annual report.

The District does two other kinds of records work with the same people, the
same systems, the same collection, review, redaction, and production, and today
does them outside this tool:

- **Litigation and discovery pulls.** Counsel asks for records for a claim or a
  lawsuit. There is no statutory requester and no statutory clock; the due date
  is whatever counsel sets. Nothing goes to a requester — the production goes to
  counsel.
- **FERPA parent and eligible-student requests.** A parent inspecting their
  child's education record is exercising 34 CFR 99.10, not RCW 42.56. The
  agency has **45 calendar days** to comply, the requester's identity must be
  verified before anything is disclosed (34 CFR 99.31(a)(9)/99.10), and only
  copying fees may be charged (34 CFR 99.11).

Running either through the PRA pipeline manufactures compliance defects: a
litigation pull would collect a five-day clock it does not owe and would appear
in the District's JLARC numbers; a FERPA request would be answered with a
letter citing the wrong statute, on the wrong deadline, and counted as a public
records request the District never received.

## Decision

Add a **matter kind** to the case. Collection, the system registry, triage,
review, redaction, exemption citations, production, releases, the work queue,
and the audit trail are shared by every kind. The *statutory* machinery is not.

### The field

`PublicRecordsRequest.matter_kind`, one of:

| Value | Meaning |
|---|---|
| `public_records` | A request under RCW 42.56. The default, and every existing row. |
| `litigation_discovery` | A records pull for counsel in a claim or lawsuit. |
| `ferpa_parent` | A parent or eligible student under 34 CFR 99.10. |

It is named `matter_kind`, not `kind`, because `Correspondence.kind` already
means the letter type and the two appear together throughout the correspondence
services.

`matter_kind` is **immutable after intake**, enforced the same way
`description` is: in `save()`, in `PublicRecordsRequestQuerySet.update()`, and
in `bulk_update()`. What law governs a matter is decided when the matter is
opened; flipping it later would retroactively rewrite which clock a case owed,
which letters it was allowed to send, and whether it belonged in a report
already filed. A miscategorised case is closed with a reason and re-opened as
the right kind, leaving both records on the timeline.

Case numbers carry the kind: `PRR-`, `LIT-`, and `FER-` prefixes over
independent per-kind sequences, so a number is never mistaken for a public
records request in an email, an export, or a court filing.

### `public_records`

Unchanged. Every rule in AGENTS.md continues to apply exactly as written.

### `litigation_discovery`

- **No statutory clock.** `statutory_response_due` is never computed and stays
  null. The deadline counsel sets is recorded in `estimated_completion`, which
  the existing estimate watchdog already surfaces without any requester effect.
- **No requester correspondence at all.** `_create_draft` — the single choke
  point for every outbound letter — refuses a litigation matter. Production
  reaches counsel through the Phase 1 release path (an installment, its
  integrity-checked package, and its scoped download), which is a records
  transfer, not correspondence.
- **Excluded from the safety net** and from every deadline escalation.
- **Excluded from JLARC**, both from the report's request set and from the
  per-request classification endpoint.
- **Not reachable from the public form or from email intake.** Only a staff
  actor on a staff channel may open one.

### `ferpa_parent`

- **A 45-calendar-day clock** under 34 CFR 99.10(b), stored in its own field
  `ferpa_access_due` and computed by its own function
  `deadlines.ferpa_access_due()`. It is calendar days, not business days: the
  RCW 42.56.520 business-day engine, its after-hours rollover, and its
  clarification tolling do not apply and are not reused.
  `statutory_response_due` stays null.
- **Identity verification is recorded on the case** — who verified, when, by
  what method (in-person photo ID, notarized statement, a match against
  district records, or a court order), and a substantive note. Recording it is
  an attributed Records Officer action with its own audit row. No background
  task may record it. It is stored as its own append-only
  `FerpaIdentityVerification` row, one per case, like
  `InitialResponseAttestation`: the ORM refuses to change or delete it, and so
  do database triggers on PostgreSQL and SQLite.
- **Copying fees only.** The existing fee module already charges for copying
  and nothing else (RCW 42.56.120 and 34 CFR 99.11 agree on this), so it is
  shared unchanged. The *fee estimate letter* is not: its adopted text cites
  RCW 42.56.120, so it stays a public-records instrument (below).
- **Excluded from the safety net, the RCW clock, and JLARC.**
- **Never reachable from the anonymous public form**, and never from email
  intake. A FERPA request arrives through a staff channel and is opened by a
  staff actor after the parent is identified.

### Which letters each kind may send

Outbound correspondence whose adopted text is a PRA instrument is
`public_records`-only:

`acknowledgment`, `clarification_request`, `scope_clarification`,
`estimate_revision`, `fee_estimate`, `no_records_closure`,
`nonpayment_closure`, `third_party_notice`, `denial`.

A `ferpa_parent` matter may still send the kinds that carry no RCW 42.56
assertion — the submission receipt, the responsive-records notice, the
installment notice, the completion closure, and general correspondence — which
is the set needed to hand a parent their child's records. A
`litigation_discovery` matter may send none of them.

Enforcement is one check in `_create_draft`, so a new letter kind is
`public_records`-only the moment it is added to the PRA list and otherwise
inherits the safe default automatically.

### Merging

`services.merge_requests()` refuses to merge across kinds. Two numbers for one
matter is a duplicate; a PRA request and a FERPA request for the same records
are two different legal obligations that happen to share a search.

## The invariants, and how they are proved

Tests in `apps/records/tests/test_matter_kinds.py` assert each of these
directly against the production querysets, not against a re-implementation:

1. **The safety net sees only `public_records`.** A litigation matter and a
   FERPA matter, both past any date, are not in `auto_acknowledge_safety_net`'s
   at-risk set and receive no queued acknowledgment.
2. **The RCW 42.56.520 clock sees only `public_records`.** Intake computes
   `statutory_response_due` for a public records request, `ferpa_access_due`
   (received + 45 calendar days) for a FERPA request, and neither for a
   litigation pull. `services.transition()` never writes
   `statutory_response_due` on a non-PRA matter, including on the
   clarification-tolling path. The deadline watchdog skips both.
3. **JLARC counts only `public_records`.** `build_jlarc_report()` excludes the
   other kinds from every count, and `classify_request_for_jlarc()` refuses
   them outright rather than silently producing a classification that would
   later be summed.

A fourth guard, `matter-kind-ast-guard` in `test_compliance`, asserts that no
`tasks.py` in the repository reaches
`services.record_ferpa_identity_verification` — the one human-only entry point
this ADR adds — matching the existing guards for estimate revision, scope,
fees, and camera exclusion. There is no setter to guard for `matter_kind`
itself: it is immutable, so no code path of any kind can change it.

## Consequences

- The schema change is additive and every existing row is `public_records`, so
  no historical case changes meaning and no filed report moves.
- Three fewer things happen outside the system of record: a litigation pull and
  a FERPA request now carry the same audit trail, the same exemption citations,
  and the same append-only history as a PRA request.
- The PRA rules get narrower rather than looser. Nothing in this ADR relaxes
  rule 6 or rule 7; each new kind removes automation rather than adding it.
- A FERPA copying-fee letter and a counsel-facing production cover letter are
  the two templates this ADR deliberately does not write. Each needs counsel
  review before it exists, and neither blocks the kinds shipping.
- Every endpoint added here is registered in `apps/api/capabilities.py` in the
  same change, per rule 10.
