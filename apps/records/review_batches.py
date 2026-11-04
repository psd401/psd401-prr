# ABOUTME: Batch approval with sampled QA and the Ready-to-discard elusion test
# ABOUTME: (#39): a reviewer judges a random sample before any batch applies.
"""Trust-ladder batch decisions on record responsiveness.

The trust policy lives in the in-app settings store (admin-only, with a
reason): ``PRR_TRUST_RESPONSIVE_MODE`` is ``suggest`` or ``batch`` and has no
automatic mode; ``PRR_TRUST_NON_RESPONSIVE_MODE`` is ``suggest``, ``batch``,
or ``auto``.

* **Cluster approval** (``batch`` or ``auto``): a reviewer proposes one
  outcome for a set of unreviewed records. A random sample of
  ``PRR_TRUST_BATCH_SAMPLE_SIZE`` is drawn when the batch opens.
* **Ready to discard** (non-responsive ``auto`` only): unreviewed records
  whose standing AI suggestion is non-responsive at or above
  ``PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE`` are staged, and a random
  elusion sample of ``PRR_TRUST_ELUSION_SAMPLE_SIZE`` is drawn from them.

``decide_batch`` takes the reviewer's judgment of every sampled record. Each
judgment is recorded as an ordinary single review. Only if every judgment
matches the proposed outcome is that outcome applied to the rest, through the
same review service, in the reviewer's name; one disagreement (for a discard
batch, one responsive record found) blocks the batch and the rest stay
unreviewed. A record someone reviewed after the batch opened is skipped, not
overturned. The AI applies nothing; ``AdvisoryRun.applied`` stays false.
"""

import random
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime

from .agreement import annotate_latest_suggestion
from .models import PublicRecordsRequest, RecordItem, ReviewBatch
from .review import _require_reviewer, _review_record

#: Records one batch may cover; a larger cluster is narrowed first.
MAX_BATCH_RECORDS = 2000
_OUTCOMES = (RecordItem.ReviewState.RESPONSIVE, RecordItem.ReviewState.NON_RESPONSIVE)
_MODE_SETTING: dict[str, str] = {
    RecordItem.ReviewState.RESPONSIVE: "PRR_TRUST_RESPONSIVE_MODE",
    RecordItem.ReviewState.NON_RESPONSIVE: "PRR_TRUST_NON_RESPONSIVE_MODE",
}
_random = random.SystemRandom()


def trust_mode(state: str) -> str:
    mode = runtime.get(_MODE_SETTING[state])
    # Responsive has no automatic mode, whatever a stored value says.
    if state == RecordItem.ReviewState.RESPONSIVE and mode == "auto":
        return "batch"
    return mode


def policy() -> dict[str, Any]:
    return {
        "responsive": trust_mode(RecordItem.ReviewState.RESPONSIVE),
        "non_responsive": trust_mode(RecordItem.ReviewState.NON_RESPONSIVE),
        "min_confidence": runtime.get("PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE") / 100,
        "batch_sample_size": runtime.get("PRR_TRUST_BATCH_SAMPLE_SIZE"),
        "elusion_sample_size": runtime.get("PRR_TRUST_ELUSION_SAMPLE_SIZE"),
    }


def _sample(ids: list[str], size: int) -> list[str]:
    return sorted(_random.sample(ids, min(size, len(ids))))


def _open_batch(*, request, kind, label, state, ids, sample_size, actor, min_confidence=None):
    if not ids:
        raise ValidationError("No unreviewed record is left for this batch.")
    if len(ids) > MAX_BATCH_RECORDS:
        raise ValidationError(
            f"One batch covers at most {MAX_BATCH_RECORDS} records. Narrow the cluster."
        )
    batch = ReviewBatch.objects.create(
        request=request,
        kind=kind,
        label=str(label or "")[:500],
        proposed_state=state,
        record_item_ids=ids,
        sample_ids=_sample(ids, sample_size),
        min_confidence=min_confidence,
        created_by=actor,
    )
    audit(
        "review.batch_created",
        target=request.number,
        actor=actor,
        batch_id=batch.pk,
        kind=kind,
        proposed_state=state,
        record_count=len(ids),
        sample_size=len(batch.sample_ids),
        min_confidence=min_confidence,
    )
    return batch


@transaction.atomic
def create_cluster_batch(record_item_ids, *, state, actor, label="") -> ReviewBatch:
    """Open a cluster batch over the unreviewed records among ``record_item_ids``."""
    _require_reviewer(actor)
    if state not in _OUTCOMES:
        raise ValidationError("A batch proposes responsive or non-responsive.")
    if trust_mode(state) not in ("batch", "auto"):
        raise ValidationError(
            "The trust policy does not allow batch approval for this outcome."
        )
    items = list(
        RecordItem.objects.filter(pk__in=list(dict.fromkeys(record_item_ids or ())))
        .select_related("request")
        .order_by("pk")
    )
    if len({item.request.pk for item in items}) != 1:
        raise ValidationError("A batch covers the records of one request.")
    ids = [
        str(item.pk) for item in items if item.review_state == RecordItem.ReviewState.UNREVIEWED
    ]
    return _open_batch(
        request=items[0].request,
        kind=ReviewBatch.Kind.CLUSTER,
        label=label,
        state=state,
        ids=ids,
        sample_size=runtime.get("PRR_TRUST_BATCH_SAMPLE_SIZE"),
        actor=actor,
    )


def discard_candidates(request: PublicRecordsRequest):
    """Unreviewed records staged as Ready to discard; none unless ``auto``."""
    if trust_mode(RecordItem.ReviewState.NON_RESPONSIVE) != "auto":
        return RecordItem.objects.none()
    floor = runtime.get("PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE") / 100
    return (
        annotate_latest_suggestion(
            RecordItem.objects.filter(
                request=request, review_state=RecordItem.ReviewState.UNREVIEWED,
            )
        )
        .filter(
            advisory_suggested_state=RecordItem.ReviewState.NON_RESPONSIVE,
            advisory_confidence__gte=floor,
        )
        .order_by("pk")
    )


@transaction.atomic
def create_discard_batch(request: PublicRecordsRequest, *, actor) -> ReviewBatch:
    """Open the Ready to discard batch and draw its elusion sample."""
    _require_reviewer(actor)
    if trust_mode(RecordItem.ReviewState.NON_RESPONSIVE) != "auto":
        raise ValidationError("The trust policy does not stage records for discard.")
    return _open_batch(
        request=request,
        kind=ReviewBatch.Kind.DISCARD,
        label="Ready to discard",
        state=RecordItem.ReviewState.NON_RESPONSIVE,
        ids=[str(pk) for pk in discard_candidates(request).values_list("pk", flat=True)],
        sample_size=runtime.get("PRR_TRUST_ELUSION_SAMPLE_SIZE"),
        actor=actor,
        min_confidence=runtime.get("PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE") / 100,
    )


@transaction.atomic
def decide_batch(batch_id, *, actor, judgments: dict) -> ReviewBatch:
    """Record the sample judgments, then apply or block the batch."""
    _require_reviewer(actor)
    batch = ReviewBatch.objects.select_for_update().select_related("request").get(pk=batch_id)
    if batch.outcome != ReviewBatch.Outcome.OPEN:
        raise ValidationError("This batch has already been decided.")
    judgments = {str(pk): state for pk, state in (judgments or {}).items()}
    if set(judgments) != set(batch.sample_ids):
        raise ValidationError("Judge every sampled record, and only those.")
    if any(state not in _OUTCOMES for state in judgments.values()):
        raise ValidationError("Each sampled record is judged responsive or non-responsive.")

    unreviewed = {
        str(pk)
        for pk in RecordItem.objects.select_for_update()
        .filter(pk__in=batch.record_item_ids, review_state=RecordItem.ReviewState.UNREVIEWED)
        .values_list("pk", flat=True)
    }
    for pk in batch.sample_ids:
        if pk in unreviewed:
            _review_record(pk, state=judgments[pk], actor=actor)

    agreed = all(state == batch.proposed_state for state in judgments.values())
    applied = skipped = 0
    if agreed:
        for pk in batch.record_item_ids:
            if pk in judgments:
                continue
            if pk not in unreviewed:
                skipped += 1
                continue
            _review_record(pk, state=batch.proposed_state, actor=actor, bulk=True)
            applied += 1
        applied += sum(1 for pk in batch.sample_ids if pk in unreviewed)

    batch.judgments = judgments
    batch.outcome = ReviewBatch.Outcome.APPLIED if agreed else ReviewBatch.Outcome.BLOCKED
    batch.applied_count = applied
    batch.skipped_count = skipped
    batch.decided_by = actor
    batch.decided_at = timezone.now()
    batch.save(update_fields=[
        "judgments", "outcome", "applied_count", "skipped_count", "decided_by", "decided_at",
    ])
    disagreements = sum(state != batch.proposed_state for state in judgments.values())
    audit(
        "review.batch_decided",
        target=batch.request.number,
        actor=actor,
        batch_id=batch.pk,
        kind=batch.kind,
        outcome=batch.outcome,
        proposed_state=batch.proposed_state,
        sample_size=len(batch.sample_ids),
        disagreements=disagreements,
        applied_count=applied,
        skipped_count=skipped,
        **(
            {"elusion_responsive_found": disagreements}
            if batch.kind == ReviewBatch.Kind.DISCARD
            else {}
        ),
    )
    return batch
