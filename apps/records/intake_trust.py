# ABOUTME: Trust ladder for intake labels: auto-apply spam and non-request
# ABOUTME: closures above a confidence floor, with officer notice and undo.
"""Auto-applied intake labels (#34).

An automatic intake classification is advisory until a label's trust mode
says otherwise. Only two labels can ever be applied without a person
(Hagel, 2026-11-05): ``spam`` and ``non_request``, each with its own in-app
mode (``suggest`` or ``auto``) and one shared confidence floor. Every other
label -- a merge, a follow-up, a related or new request -- stays with an
officer or an approved decision batch.

When a label is applied the case is closed as non-request by
``services.auto_close_non_request`` with no human actor. Nothing reaches the
requester (rule 6), Records Officers are emailed, and the case's intake card
offers one-click undo, which is the ordinary ``reopen_request``.
"""

import logging

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.models import audit
from apps.configuration import runtime

from . import services
from .models import AdvisoryFeedback, AdvisoryRun, IntakeAutoApplication, RequestStatus
from .notifications import notify_intake_auto_applied

logger = logging.getLogger(__name__)

#: The only labels that may be auto-applied, and the setting that governs each.
AUTO_LABEL_SETTINGS = {
    "spam": "PRR_TRUST_INTAKE_SPAM_MODE",
    "non_request": "PRR_TRUST_INTAKE_NON_REQUEST_MODE",
}
_LABEL_TEXT = {"spam": "spam", "non_request": "not a records request"}


def auto_apply_intake_label(run_id) -> IntakeAutoApplication | None:
    """Close the run's case if its label is trusted to apply; else do nothing.

    Returns the new ``IntakeAutoApplication``, or None when the label is not
    in ``auto`` mode, is below the floor, or the case is no longer one the
    system may touch: the run was asked for by an officer, is not the case's
    latest classification, already has an answer, or the case has moved on.
    """
    run = AdvisoryRun.objects.select_related("request").get(pk=run_id)
    proposal = run.proposal or {}
    label = str(proposal.get("label") or "")
    setting = AUTO_LABEL_SETTINGS.get(label)
    if (
        setting is None
        or runtime.get(setting) != "auto"
        or run.kind != AdvisoryRun.Kind.INTAKE_CLASSIFICATION
        or run.state != AdvisoryRun.State.COMPLETE
        or run.trigger != AdvisoryRun.Trigger.AUTOMATIC
    ):
        return None
    confidence = float(proposal.get("confidence") or 0)
    if confidence * 100 < runtime.get("PRR_TRUST_INTAKE_AUTO_MIN_CONFIDENCE"):
        return None
    prr = run.request
    latest = (
        AdvisoryRun.objects.filter(request=prr, kind=run.kind, record_item__isnull=True)
        .order_by("-created_at")
        .values_list("pk", flat=True)
        .first()
    )
    # Read without a lock: auto_close_non_request re-checks the status under
    # select_for_update, and the one-to-one advisory_run refuses a second row.
    if (
        latest != run.pk
        or prr.status != RequestStatus.RECEIVED
        or AdvisoryFeedback.objects.filter(advisory_run=run).exists()
        or IntakeAutoApplication.objects.filter(advisory_run=run).exists()
    ):
        return None
    note = (
        f"Closed automatically: the intake classification labelled this "
        f"{_LABEL_TEXT[label]} with {round(confidence * 100)}% confidence, at "
        f"or above the district's automatic closure floor. Nothing was sent "
        f"to the sender. Reopen the case to undo."
    )
    try:
        with transaction.atomic():
            event = services.auto_close_non_request(prr, note=note)
            applied = IntakeAutoApplication.objects.create(
                request=prr,
                advisory_run=run,
                label=label,
                confidence=confidence,
                status_event=event,
            )
            audit(
                "intake.auto_applied",
                target=prr.number,
                actor=None,
                advisory_run=str(run.pk),
                label=label,
                confidence=confidence,
                model_identifier=run.model_identifier,
            )
    except (ValidationError, ValueError):
        # Every closure refusal is one of these: a web-form case, an
        # unresolved delivery, a case closed meanwhile. Nothing was written;
        # the case stays with a person, and the advisory worker never fails.
        return None
    notify_intake_auto_applied(applied)
    return applied
