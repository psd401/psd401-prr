"""Human-only validation and entry points for statutory correspondence.

Requester-visible rendering, approval, queueing, and delivery evidence live in
``apps.correspondence``. Legal request state changes only after a confirmed
delivery result.
"""
from __future__ import annotations

import datetime as dt

from django.core.exceptions import PermissionDenied

from .models import PublicRecordsRequest


def validate_estimate_revision(
    *,
    actor,
    reason: str,
    automated: bool = False,
    actor_previously_authorized: bool = False,
) -> str:
    """Validate estimate-revision authority and substance without side effects."""
    if automated:
        raise PermissionDenied("Estimate revisions cannot be automated.")
    if actor_previously_authorized:
        if actor is None or getattr(actor, "pk", None) is None:
            raise PermissionDenied(
                "A previously authorized human approver is required."
            )
    elif (
        actor is None
        or not getattr(actor, "is_authenticated", False)
        or not getattr(actor, "is_active", False)
        or not getattr(actor, "is_staff", False)
    ):
        raise PermissionDenied(
            "A current human staff identity is required for estimate revisions."
        )

    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValueError(
            "Estimate revisions require a substantive reason (what changed: "
            "volume found, third-party notice, clarification, staffing). "
            "A revision without a real reason is a Yousoufian aggravating factor."
        )
    return reason


def revise_estimate(
    prr: PublicRecordsRequest,
    new_date: dt.date,
    reason: str,
    *,
    actor=None,
):
    """Create a human-authored estimate-revision draft.

    This remains the anti-"kick it every 90 days" entry point: a current human
    staff identity and substantive reason are mandatory. Drafting deliberately
    does not mutate the request, create an ``EstimateRevision``, or claim the
    message was communicated; those legal effects occur only after confirmed
    correspondence delivery.
    """
    reason = validate_estimate_revision(actor=actor, reason=reason)

    from apps.correspondence import services as correspondence_services

    return correspondence_services.create_estimate_revision_draft(
        prr,
        new_date=new_date,
        reason=reason,
        actor=actor,
    )
