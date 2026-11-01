# No-records and out-of-retention closure

No-responsive-records and outside-retention findings are closure
determinations, not exemption denials. The application never makes either
determination automatically and never substitutes an exemption citation for a
documented search.

## Legal basis and design

The implementation follows the Washington model-rule search standard: the
agency searches the places where responsive records are reasonably likely to
be found, follows obvious leads, documents what it searched, and can explain
the search in nonconclusory terms. The retained search evidence also supports
the duty to provide fullest assistance and preserve responsive records.

Primary authorities:

- [RCW 42.56.100](https://app.leg.wa.gov/RCW/default.aspx?cite=42.56.100)
  (fullest assistance and preservation);
- [WAC 44-14-040](https://app.leg.wa.gov/WAC/default.aspx?cite=44-14-040)
  (completion and later-located records); and
- [WAC 44-14-04003](https://app.leg.wa.gov/WAC/default.aspx?cite=44-14-04003)
  (objectively reasonable search and documentation when nothing is located).

The Washington Supreme Court's
[Neighborhood Alliance opinion](https://www.courts.wa.gov/opinions/index.cfm?fa=opinions.showOpinion&filename=841080Co1)
is the controlling search-adequacy authority used by this workflow.

## Required human evidence

Only an interactive user with the Records Officer role may create the
versioned closure draft. Creation fails closed unless:

- every current `CollectionTask` is complete;
- every located candidate `RecordItem` has a completed human review and none
  is responsive or withheld;
- the completion timestamp follows receipt and is not in the future;
- at least one structured search step identifies the location, custodian,
  method, date range, result, and lead-following outcome;
- the officer records a substantive adequacy explanation;
- a retained district-counsel approval reference is supplied; and
- the request is active and not awaiting unresolved clarification.

For an outside-retention finding, the application additionally requires an
expired footage-preservation review, a verified retention-schedule reference,
and affirmative checks for incident/litigation holds, earlier exports,
email/Drive copies, and other likely copies.

`AdequateSearchAttestation` is append-only. It snapshots all collection tasks
plus the identities, digests, outcomes, and reviewers of located candidate
records, and binds the whole search-work snapshot with SHA-256. Approval
rechecks that snapshot and the outside-retention facts. If the search or
candidate set changes, staff create a new attestation and draft; the earlier
evidence remains retained and cannot be rewritten.

## Workflow and lifecycle effect

The staff UI and
`POST /api/staff/correspondence/drafts/no-records-closure/` collect the same
structured inputs. Capability `correspondence.draft_no_records_closure` is
`human_only`: token callers receive `403 human_approval_required`, and no
attestation or draft is created.

Drafting and approval do not close the request. A Records Officer previews the
exact versioned message, approves it, and queues it through the ordinary
correspondence transport. Only the first confirmed provider delivery applies
`closed_no_records` through `apps.records.services.transition()`, records the
provider timestamp as `closed_at`, links the resulting status event, and adds
append-only audit evidence. A queued, failed, cancelled, or unknown delivery
has no closure effect, and the generic transition API cannot create this
status.

If responsive records are found later, WAC 44-14-040 requires prompt
notification. Reopening and supplemental-production policy remains an
accountable Records Officer and counsel procedure; this workflow does not
silently discard later-located records.

## External approval before production use

District counsel must approve the version-1 requester language, the evidence
checklist, the retention-schedule references used by staff, and the operational
procedure for later-located records. Those approvals are deployment handoffs,
not facts the application can infer.
