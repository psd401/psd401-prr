# ABOUTME: Drafts the interpretation-and-estimate letter (#42) once a case is
# ABOUTME: ready; a Records Officer approves and sends it. Sends nothing itself.
"""Automatic drafting of the interpretation-and-estimate letter.

A case is ready when it is an open ``public_records`` request with a
completed request-intent proposal, at least one collected record, and an AI
suggestion on every record still unreviewed (classification is done). For
each ready case this computes a ``RecordsEstimate`` and drafts the letter as
a *system-prepared* draft. It never approves, queues, or sends: the only way
the letter reaches a requester is a Records Officer's ``approve_and_send``
(rule 6). ``PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED`` switches it off.

A case is skipped once it has an interpretation-and-estimate letter in any
state but cancelled, and while an estimate computed today produced no letter
(usually no measured throughput yet), so a waiting case does not pile up
estimates.
"""

import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.configuration import runtime

from .agreement import latest_suggestion_runs
from .estimates import estimate_request
from .models import (
    AdvisoryRun,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    RecordsEstimate,
    RequestStatus,
)
from .services import CLOSED_STATUSES

logger = logging.getLogger(__name__)
_EXCLUDED = (RecordItem.ReviewState.SUPERSEDED, RecordItem.ReviewState.DUPLICATE)


def ready_requests(*, limit: int):
    """Open public records cases whose letter can be drafted now."""
    from apps.correspondence.models import Correspondence

    records = RecordItem.objects.filter(request=OuterRef("pk")).exclude(
        review_state__in=_EXCLUDED
    )
    unclassified = records.filter(
        review_state=RecordItem.ReviewState.UNREVIEWED,
    ).exclude(Exists(latest_suggestion_runs(OuterRef("pk"))))
    return (
        PublicRecordsRequest.objects.filter(matter_kind=MatterKind.PUBLIC_RECORDS)
        .exclude(status__in=[*CLOSED_STATUSES, RequestStatus.CLARIFICATION])
        .filter(
            Exists(
                AdvisoryRun.objects.filter(
                    request=OuterRef("pk"),
                    kind=AdvisoryRun.Kind.REQUEST_INTENT,
                    state=AdvisoryRun.State.COMPLETE,
                )
            ),
            Exists(records),
        )
        .exclude(Exists(unclassified))
        .exclude(
            Exists(
                Correspondence.objects.filter(
                    request=OuterRef("pk"),
                    kind=Correspondence.Kind.INTERPRETATION_ESTIMATE,
                ).exclude(status=Correspondence.Status.CANCELLED)
            )
        )
        .exclude(
            Exists(
                RecordsEstimate.objects.filter(
                    request=OuterRef("pk"), as_of=timezone.localdate(),
                )
            )
        )
        .order_by("received_at", "pk")[:limit]
    )


def prepare_estimate_letters(*, limit: int = 10) -> int:
    """Draft letters for ready cases; returns how many were drafted."""
    if not (
        runtime.get("PRR_AI_ADVISORY_ENABLED")
        and runtime.get("PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED")
    ):
        return 0
    from apps.correspondence.services import create_interpretation_estimate_draft

    drafted = 0
    for prr in ready_requests(limit=max(1, min(int(limit), 50))):
        try:
            with transaction.atomic():
                estimate = estimate_request(prr)
                if estimate.result.get("completion_date") is None:
                    continue
                create_interpretation_estimate_draft(prr, estimate=estimate)
                drafted += 1
        except ValidationError:
            # A case that changed underneath (closed, clarified, a letter
            # drafted by hand) is simply not drafted this time.
            logger.warning("Estimate letter not drafted for %s.", prr.number, exc_info=True)
    return drafted
