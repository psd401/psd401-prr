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
"""

from django.db.models import Exists, OuterRef

from apps.correspondence.models import ClarificationCycle

from .jlarc import TERMINAL_STATUSES
from .models import (
    CollectionTask,
    FeeDepositGate,
    Installment,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    RequestStatus,
)
from .redaction_batches import pending_proposal_count

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


def _open_clarifications(prr: PublicRecordsRequest) -> int:
    return ClarificationCycle.objects.filter(
        request=prr, resolved_at__isnull=True, cancelled_at__isnull=True,
    ).count()


def _open_deposit_gates(prr: PublicRecordsRequest) -> int:
    return FeeDepositGate.objects.filter(
        request=prr, satisfied_at__isnull=True, lifted_at__isnull=True,
    ).count()


def _open_work(prr: PublicRecordsRequest) -> dict[str, tuple[int, str, str]]:
    """Each stage's (open count, summary, blocked reason)."""
    tasks = CollectionTask.objects.filter(request=prr)
    records = RecordItem.objects.filter(request=prr)
    has_collection = tasks.exists() or records.exists()
    waiting_on_clarification = (
        prr.status == RequestStatus.CLARIFICATION or _open_clarifications(prr) > 0
    )
    open_tasks = tasks.exclude(state=CollectionTask.State.COMPLETE).count()
    to_review = records.filter(
        review_state=RecordItem.ReviewState.UNREVIEWED,
    ).count()
    proposals = pending_proposal_count(prr)
    released_coverage = Installment.objects.filter(
        request=prr, released_at__isnull=False, items=OuterRef("pk"),
    )
    unreleased = records.filter(
        review_state=RecordItem.ReviewState.RESPONSIVE,
    ).exclude(Exists(released_coverage)).count()
    open_installments = Installment.objects.filter(
        request=prr, released_at__isnull=True,
    ).count()
    deposits = _open_deposit_gates(prr)
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


def case_stages(prr: PublicRecordsRequest) -> list[dict]:
    """The rail for one case: id, label, state, open_count, summary, reason.

    ``state`` is ``done``, ``current``, ``blocked`` or ``upcoming``. ``reason``
    is set only on a blocked stage.
    """
    if prr.status in TERMINAL_STATUSES:
        return [
            {
                "id": stage_id, "label": label, "state": "done",
                "open_count": 0, "summary": "", "reason": "",
            }
            for stage_id, label in STAGES
        ]
    work = _open_work(prr)
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
