# ABOUTME: Drafts the informational scope letter once a Gmail count finishes and
# ABOUTME: before anything is pulled; a person approves and sends it. Sends nothing.
"""Automatic drafting of the scope letter (CIO decision, 2026-11-08).

A case is ready when it is an open ``public_records`` request with a
completed request-intent proposal, a finished Vault count not yet covered by
a drafted letter, no scoping session still counting, no Vault collection
yet, and no unsent scope letter. A draft a person deletes is not drafted
again for the same count; a later count (say, after the requester narrows)
gets its own draft. For each
ready case this drafts the "your request matched N items; you may narrow it"
letter in its informational form, as a *system-prepared* draft, from the
latest finished count and the AI's reading of the request. It never
approves, queues, or sends: only a person does (rule 6).
``PRR_AI_AUTOMATIC_SCOPE_LETTER_ENABLED`` switches it off.
"""

import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.configuration import runtime

from .deadlines import add_business_days
from .models import (
    AdvisoryRun,
    CollectionTask,
    MatterKind,
    PublicRecordsRequest,
    RequestStatus,
    ScopingSession,
    VaultCountRun,
)
from .services import CLOSED_STATUSES

logger = logging.getLogger(__name__)

#: How long the letter gives the requester to say whether to narrow.
REPLY_BUSINESS_DAYS = 10


def ready_requests(*, limit: int):
    """Open public records cases whose scope letter can be drafted now."""
    from apps.correspondence.models import Correspondence

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
            Exists(
                VaultCountRun.objects.filter(
                    request=OuterRef("pk"),
                    state=VaultCountRun.State.COMPLETE,
                    scope_letter_prepared_at__isnull=True,
                )
            ),
        )
        .exclude(
            # Iterative scoping is still narrowing; wait for its final count.
            Exists(
                ScopingSession.objects.filter(
                    request=OuterRef("pk"), state=ScopingSession.State.RUNNING,
                )
            )
        )
        .exclude(
            Exists(
                CollectionTask.objects.filter(
                    request=OuterRef("pk"),
                    kind__in=[CollectionTask.Kind.VAULT_MAIL, CollectionTask.Kind.VAULT_DRIVE],
                )
            )
        )
        .exclude(
            Exists(
                Correspondence.objects.filter(
                    request=OuterRef("pk"),
                    kind=Correspondence.Kind.SCOPE_CLARIFICATION,
                    status__in=[
                        Correspondence.Status.DRAFT,
                        Correspondence.Status.APPROVED,
                        Correspondence.Status.QUEUED,
                        Correspondence.Status.FAILED,
                    ],
                )
            )
        )
        .order_by("received_at", "pk")[:limit]
    )


def _usable_options(options) -> list[dict]:
    """The AI's narrowing options the letter can state; the rest are left out."""
    from apps.correspondence.automatic_templates import MAX_NARROWING_OPTIONS

    usable = []
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        terms = [str(term).strip() for term in option.get("terms") or () if str(term).strip()]
        rationale = str(option.get("rationale") or "").strip()
        if 1 <= len(terms) <= 10 and all(len(t) <= 200 for t in terms) and 10 <= len(rationale) <= 500:
            usable.append({"terms": terms, "rationale": rationale})
    return usable[:MAX_NARROWING_OPTIONS]


def prepare_scope_letters(*, limit: int = 10) -> int:
    """Draft letters for ready cases; returns how many were drafted."""
    if not (
        runtime.get("PRR_AI_ADVISORY_ENABLED")
        and runtime.get("PRR_AI_AUTOMATIC_SCOPE_LETTER_ENABLED")
    ):
        return 0
    from apps.correspondence.services import create_system_scope_letter_draft

    drafted = 0
    for prr in ready_requests(limit=max(1, min(int(limit), 50))):
        finished = VaultCountRun.objects.filter(request=prr, state=VaultCountRun.State.COMPLETE)
        count = finished.order_by("-completed_at", "-created_at").first()
        reading = (
            AdvisoryRun.objects.filter(
                request=prr,
                kind=AdvisoryRun.Kind.REQUEST_INTENT,
                state=AdvisoryRun.State.COMPLETE,
            )
            .order_by("-created_at")
            .first()
        )
        if count is None or reading is None:
            continue
        summary = str(reading.proposal.get("summary") or "").strip()[:500]
        try:
            with transaction.atomic():
                create_system_scope_letter_draft(
                    prr,
                    match_count=int(count.total_count),
                    query_summary=summary,
                    narrowing_options=_usable_options(reading.proposal.get("narrowing_options")),
                    response_by=add_business_days(timezone.localdate(), REPLY_BUSINESS_DAYS),
                )
                # This letter covers every count so far, not only the latest.
                finished.filter(scope_letter_prepared_at__isnull=True).update(
                    scope_letter_prepared_at=timezone.now(),
                )
                drafted += 1
        except ValidationError:
            # A case that changed underneath, or a reading too short to state,
            # is simply not drafted this time; staff can still write one.
            logger.warning("Scope letter not drafted for %s.", prr.number, exc_info=True)
    return drafted
