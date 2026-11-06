# ABOUTME: The case stage rail (#45): Intake through Close, each stage's open
# ABOUTME: work, and which stage the case is in now. Read-only.
"""Where a case stands, as the stage rail shows it.

The rail runs Intake, Scope, Collect, Review, Redact, Release, Close. Each
stage counts its own open work. The current stage is the earliest one with
open work; earlier stages are done and later ones upcoming, though a later
stage still reports what is already waiting there. A current stage that
waits on the requester is blocked, with the reason. A closed case is done
throughout.

This is a reading of the case for navigation, not a legal state: status
still changes only through ``services.transition()`` (rule 2), and closure
readiness stays the authority on what blocks closing.

Matter kinds (rule 13, ADR 0004): the rail serves every kind, because
collection, review, redaction, production, and closure are shared. Its one
RCW 42.56 reading -- that a received case still owes its acknowledgment --
is made for ``public_records`` alone; another kind reads only as received.
The blocked reasons read the case's own state (an open clarification, an
open deposit gate) and assert no statute.

The counts are read for many cases at once (``_work_counts``), so the Today
list costs the same handful of queries for one case or a full page.
"""

from collections import Counter
from collections.abc import Iterable

from django.db.models import Count, Exists, OuterRef, Q

from apps.correspondence.models import ClarificationCycle

from .jlarc import TERMINAL_STATUSES
from .models import (
    CollectionTask,
    FeeDepositGate,
    Installment,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    RedactionProposal,
    RedactionProposalDecision,
    RequestStatus,
)

STAGES = (
    ("intake", "Intake"),
    ("scope", "Scope"),
    ("collect", "Collect"),
    ("review", "Review"),
    ("redact", "Redact"),
    ("release", "Release"),
    ("close", "Close"),
)
STAGE_IDS = tuple(stage_id for stage_id, _label in STAGES)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _by_request(rows, key: str = "request_id") -> Counter:
    return Counter({row[key]: row["n"] for row in rows})


def _open_deposit_gates(ids: list[int]) -> Counter:
    return _by_request(
        FeeDepositGate.objects.filter(
            request_id__in=ids, satisfied_at__isnull=True, lifted_at__isnull=True,
        ).values("request_id").annotate(n=Count("pk"))
    )


def _pending_proposals(ids: list[int]) -> Counter:
    """Redaction proposals still waiting for a person's decision, per case.
    The same rule as ``redaction_batches.pending_proposal_count``."""
    return _by_request(
        RedactionProposal.objects.filter(
            record_item__request_id__in=ids,
            record_item__review_state=RecordItem.ReviewState.RESPONSIVE,
        ).exclude(
            Exists(RedactionProposalDecision.objects.filter(proposal=OuterRef("pk")))
        ).values("record_item__request_id").annotate(n=Count("pk")),
        key="record_item__request_id",
    )


def _work_counts(ids: list[int]) -> dict[str, Counter]:
    """Every count the rail reads, per case, in six queries."""
    tasks = CollectionTask.objects.filter(request_id__in=ids).values(
        "request_id",
    ).annotate(
        total=Count("pk"),
        open=Count("pk", filter=~Q(state=CollectionTask.State.COMPLETE)),
    )
    released_coverage = Installment.objects.filter(
        released_at__isnull=False, items=OuterRef("pk"),
    )
    records = RecordItem.objects.filter(request_id__in=ids).values(
        "request_id",
    ).annotate(
        total=Count("pk"),
        unreviewed=Count(
            "pk", filter=Q(review_state=RecordItem.ReviewState.UNREVIEWED),
        ),
        unreleased=Count(
            "pk",
            filter=Q(review_state=RecordItem.ReviewState.RESPONSIVE)
            & ~Q(Exists(released_coverage)),
        ),
    )
    counts: dict[str, Counter] = {
        name: Counter()
        for name in (
            "tasks", "open_tasks", "records", "to_review", "unreleased",
        )
    }
    for row in tasks:
        counts["tasks"][row["request_id"]] = row["total"]
        counts["open_tasks"][row["request_id"]] = row["open"]
    for row in records:
        counts["records"][row["request_id"]] = row["total"]
        counts["to_review"][row["request_id"]] = row["unreviewed"]
        counts["unreleased"][row["request_id"]] = row["unreleased"]
    counts["clarifications"] = _by_request(
        ClarificationCycle.objects.filter(
            request_id__in=ids, resolved_at__isnull=True, cancelled_at__isnull=True,
        ).values("request_id").annotate(n=Count("pk"))
    )
    counts["open_installments"] = _by_request(
        Installment.objects.filter(
            request_id__in=ids, released_at__isnull=True,
        ).values("request_id").annotate(n=Count("pk"))
    )
    counts["deposits"] = _open_deposit_gates(ids)
    counts["proposals"] = _pending_proposals(ids)
    return counts


def _open_work(
    prr: PublicRecordsRequest, counts: dict[str, Counter],
) -> dict[str, tuple[int, str, str]]:
    """Each stage's (open count, summary, blocked reason)."""
    pk = prr.pk
    has_collection = counts["tasks"][pk] > 0 or counts["records"][pk] > 0
    waiting_on_clarification = (
        prr.status == RequestStatus.CLARIFICATION or counts["clarifications"][pk] > 0
    )
    open_tasks = counts["open_tasks"][pk]
    to_review = counts["to_review"][pk]
    proposals = counts["proposals"][pk]
    unreleased = counts["unreleased"][pk]
    open_installments = counts["open_installments"][pk]
    deposits = counts["deposits"][pk]
    received = prr.status == RequestStatus.RECEIVED
    if not received:
        intake_summary = ""
    elif prr.matter_kind == MatterKind.PUBLIC_RECORDS:
        intake_summary = "Received and not yet acknowledged"
    else:
        intake_summary = "Received and not yet triaged"

    scope_open = int(not has_collection or waiting_on_clarification)
    release_open = unreleased + open_installments + deposits
    if unreleased:
        release_summary = _plural(
            unreleased,
            "responsive record not yet released",
            "responsive records not yet released",
        )
    elif open_installments:
        release_summary = _plural(
            open_installments, "installment not yet released", "installments not yet released",
        )
    else:
        release_summary = ""
    return {
        "intake": (int(received), intake_summary, ""),
        "scope": (
            scope_open,
            "No collection has started" if not has_collection else "",
            "Waiting on the requester's clarification" if waiting_on_clarification else "",
        ),
        "collect": (
            open_tasks,
            _plural(open_tasks, "open collection", "open collections") if open_tasks else "",
            "",
        ),
        "review": (
            to_review,
            _plural(to_review, "record to review", "records to review") if to_review else "",
            "",
        ),
        "redact": (
            proposals,
            _plural(
                proposals, "redaction proposal to decide", "redaction proposals to decide",
            ) if proposals else "",
            "",
        ),
        "release": (
            release_open,
            release_summary,
            "Waiting on the requester's deposit" if deposits else "",
        ),
        "close": (0, "", ""),
    }


def _rail(prr: PublicRecordsRequest, counts: dict[str, Counter]) -> list[dict]:
    if prr.status in TERMINAL_STATUSES:
        return [
            {
                "id": stage_id, "label": label, "state": "done",
                "open_count": 0, "summary": "", "reason": "",
            }
            for stage_id, label in STAGES
        ]
    work = _open_work(prr, counts)
    current = next(
        (stage_id for stage_id in STAGE_IDS if work[stage_id][0] > 0),
        "close",
    )
    current_index = STAGE_IDS.index(current)
    stages = []
    for index, (stage_id, label) in enumerate(STAGES):
        open_count, summary, blocked_reason = work[stage_id]
        if index < current_index:
            state = "done"
        elif index > current_index:
            state = "upcoming"
        else:
            state = "blocked" if blocked_reason else "current"
        stages.append({
            "id": stage_id,
            "label": label,
            "state": state,
            "open_count": open_count,
            "summary": summary,
            "reason": blocked_reason if state == "blocked" else "",
        })
    return stages


def case_stages(prr: PublicRecordsRequest) -> list[dict]:
    """The rail for one case: id, label, state, open_count, summary, reason.

    ``state`` is ``done``, ``current``, ``blocked`` or ``upcoming``. ``reason``
    is set only on a blocked stage.
    """
    if prr.status in TERMINAL_STATUSES:
        return _rail(prr, {})
    return _rail(prr, _work_counts([prr.pk]))


def current_stages(cases: Iterable[PublicRecordsRequest]) -> dict[int, dict]:
    """The stage each case is in now, keyed by primary key.

    The rail's current (or blocked) stage; a closed case gets its Close
    stage, done. One set of grouped queries for all of them.
    """
    cases = list(cases)
    counts = _work_counts([case.pk for case in cases])
    current = {}
    for case in cases:
        rail = _rail(case, counts)
        current[case.pk] = next(
            (stage for stage in rail if stage["state"] in {"current", "blocked"}),
            rail[-1],
        )
    return current
