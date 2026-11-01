# ABOUTME: Records no renderer can open still need a decision, made by a human
# ABOUTME: on stated evidence rather than left permanently unreviewed.
"""Disposition for records that cannot be displayed.

A dead format, a proprietary container, a corrupt file. The PRA does not
excuse a district from deciding, and the requester is entitled to an answer.
Before this existed there were two options and both were bad: leave the record
unreviewed forever, or mark it reviewed and leave no trace that nobody could
open it. The second is the worse failure, because afterwards the file looks
reviewed while the reviewer certified something they never saw.

So the decision is allowed, and it is expensive to make: a named human, a
stated basis, and reasoning long enough to be an actual explanation. It is
recorded append-only next to the record, and the ordinary review verdict is
applied through the same path every other record uses.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.models import audit

from .handling import OPAQUE, handling_for_item
from .models import OpaqueRecordDisposition, RecordItem

#: Long enough that "cannot open" is not an acceptable answer on its own. The
#: reviewer has to say what they did and what they concluded, because this is
#: the entire evidentiary basis for the decision.
MIN_OPAQUE_REASONING = 80


def record_opaque_disposition(
    record_item_id,
    *,
    basis: str,
    reasoning: str,
    review_state: str,
    actor,
    withholding_exemption=None,
):
    """Decide a record that cannot be rendered, on stated evidence.

    Refuses on a record that *can* be displayed: if a renderer exists, the
    reviewer is required to look at it rather than describe it.
    """
    item = RecordItem.objects.select_related("request").get(pk=record_item_id)
    if handling_for_item(item) != OPAQUE:
        raise ValidationError(
            "This record can be displayed, so it must be reviewed on its "
            "contents rather than described."
        )
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise ValidationError("A named reviewer is required.")
    if basis not in OpaqueRecordDisposition.Basis.values:
        raise ValidationError("An explicit basis for the decision is required.")
    reasoning = (reasoning or "").strip()
    if len(reasoning) < MIN_OPAQUE_REASONING:
        raise ValidationError(
            "Record what was attempted and what was concluded, in at least "
            f"{MIN_OPAQUE_REASONING} characters. This reasoning is the whole "
            "evidentiary basis for deciding a record nobody could open."
        )
    if review_state not in {
        RecordItem.ReviewState.RESPONSIVE,
        RecordItem.ReviewState.NON_RESPONSIVE,
        RecordItem.ReviewState.WITHHELD,
    }:
        raise ValidationError("An explicit review outcome is required.")
    if (
        review_state == RecordItem.ReviewState.WITHHELD
        and withholding_exemption is None
    ):
        raise ValidationError(
            "Withholding requires an exemption citation, exactly as it does "
            "for a record that could be read."
        )
    if OpaqueRecordDisposition.objects.filter(record_item=item).exists():
        raise ValidationError("This record already has a recorded disposition.")

    with transaction.atomic():
        disposition = OpaqueRecordDisposition.objects.create(
            record_item=item,
            basis=basis,
            reasoning=reasoning,
            decided_by=actor,
        )
        item.review_state = review_state
        item.reviewed_by = actor
        from django.utils import timezone

        item.reviewed_at = timezone.now()
        if withholding_exemption is not None:
            item.withholding_exemption = withholding_exemption
        item.save(update_fields=[
            "review_state", "reviewed_by", "reviewed_at",
            "withholding_exemption",
        ])
        if withholding_exemption is not None:
            item.withholding_exemptions.add(withholding_exemption)
        audit(
            "record.opaque_disposition_recorded",
            target=str(item.pk),
            actor=actor,
            request_number=item.request.number,
            basis=basis,
            review_state=review_state,
            original_filename=item.original_filename,
            content_type=item.content_type,
        )
    return disposition
