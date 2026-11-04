"""Human review and irreversible redaction services for retained records."""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass

import pymupdf as fitz
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.api.identity import StaffRole, has_staff_role
from apps.audit.models import AuditLog, audit
from apps.configuration import runtime

from .models import (
    ExemptionCitation,
    RecordItem,
    RecordItemWithholdingExemption,
    Redaction,
)
from .preview import _open_verified_pdf
from .storage import generated_record_storage_key, store_immutable_object

_HUMAN_REVIEW_STATES = {
    RecordItem.ReviewState.RESPONSIVE,
    RecordItem.ReviewState.NON_RESPONSIVE,
    RecordItem.ReviewState.DUPLICATE,
    RecordItem.ReviewState.WITHHELD,
}


def _require_reviewer(actor):
    if not has_staff_role(
        actor,
        StaffRole.REVIEWER,
        StaffRole.RECORDS_OFFICER,
    ):
        raise ValidationError("A currently authorized reviewer is required.")


def _clear_derived_artifact(item):
    item.redacted_storage_key = ""
    item.redacted_sha256 = ""
    item.redacted_content_length = None
    item.redacted_content_type = ""
    item.redacted_manifest_sha256 = ""


def _clear_second_review(item):
    item.second_reviewed_by = None
    item.second_reviewed_at = None


def _save_invalidated_item(item):
    _clear_second_review(item)
    _clear_derived_artifact(item)
    item.save(update_fields=[
        "second_reviewed_by",
        "second_reviewed_at",
        "redacted_storage_key",
        "redacted_sha256",
        "redacted_content_length",
        "redacted_content_type",
        "redacted_manifest_sha256",
    ])


#: How many records one bulk human decision may cover. A cap keeps the action
#: something a person can actually have looked at, and keeps one confirmation
#: from turning into an unbounded write.
BULK_REVIEW_MAX_ITEMS = 200

#: Bulk covers only the two plain responsiveness outcomes. Withholding needs a
#: per-record exemption citation and duplicate needs a per-record judgement, so
#: neither is something one confirmation can honestly express.
BULK_REVIEW_STATES = (
    RecordItem.ReviewState.RESPONSIVE,
    RecordItem.ReviewState.NON_RESPONSIVE,
)

#: How many records one filter-mode decision may cover. The reviewer sees the
#: exact count and confirms it; this ceiling only bounds one write.
BULK_REVIEW_FILTER_MAX_ITEMS = 10_000

#: States a bulk decision never overturns: each was a reviewer's own judgement
#: (an exemption citation, a duplicate call) or an officer's re-scoping.
_ONE_AT_A_TIME_STATES = {
    RecordItem.ReviewState.WITHHELD: "withheld",
    RecordItem.ReviewState.DUPLICATE: "duplicate",
    RecordItem.ReviewState.SUPERSEDED: "superseded",
}


class BulkSelectionChanged(ValidationError):
    """The filtered set is no longer the one the reviewer confirmed."""


@dataclass(frozen=True)
class BulkSelection:
    matching: int
    eligible_ids: tuple
    excluded: dict
    selection_sha256: str


def _selection_sha256(ids) -> str:
    return hashlib.sha256(
        "\n".join(sorted(str(pk) for pk in ids)).encode()
    ).hexdigest()


def bulk_review_selection(record_item_ids, *, state) -> BulkSelection:
    """Split a filtered set into the records one decision may change.

    Records already in ``state`` and records whose state needs its own
    judgement are counted, not changed. A non-responsive decision also skips a
    record with an active redaction, which has to be voided deliberately.
    """
    if state not in BULK_REVIEW_STATES:
        raise ValidationError(
            "Bulk review records only responsive or non-responsive outcomes."
        )
    ids = list(dict.fromkeys(record_item_ids))
    if len(ids) > BULK_REVIEW_FILTER_MAX_ITEMS:
        raise ValidationError(
            f"One filter decision covers at most {BULK_REVIEW_FILTER_MAX_ITEMS} "
            "matching records. Narrow the filter."
        )
    excluded = {"withheld": 0, "duplicate": 0, "superseded": 0, "already": 0, "redacted": 0}
    redacted = set()
    if state != RecordItem.ReviewState.RESPONSIVE:
        redacted = set(
            Redaction.objects.filter(
                record_item_id__in=ids, voided_at__isnull=True,
            ).values_list("record_item_id", flat=True)
        )
    eligible = []
    for pk, current in RecordItem.objects.filter(pk__in=ids).values_list(
        "pk", "review_state",
    ):
        if current in _ONE_AT_A_TIME_STATES:
            excluded[_ONE_AT_A_TIME_STATES[current]] += 1
        elif current == state:
            excluded["already"] += 1
        elif pk in redacted:
            excluded["redacted"] += 1
        else:
            eligible.append(pk)
    eligible.sort(key=str)
    return BulkSelection(
        matching=len(ids),
        eligible_ids=tuple(eligible),
        excluded=excluded,
        selection_sha256=_selection_sha256(eligible),
    )


@transaction.atomic
def bulk_review_confirmed_selection(
    record_item_ids,
    *,
    state,
    actor,
    expected_count,
    selection_sha256,
    filter_detail,
):
    """Apply one human decision to the exact set the reviewer confirmed.

    ``record_item_ids`` is the filter's current match, resolved by the
    caller just before this runs. Those rows are locked here and re-split, and
    the decision is refused unless the result is still exactly the set the
    reviewer confirmed (same count, same digest). Each record's write and
    audit row match a single-record review.
    """
    _require_reviewer(actor)
    ids = list(dict.fromkeys(record_item_ids))
    if len(ids) > BULK_REVIEW_FILTER_MAX_ITEMS:
        raise ValidationError(
            f"One filter decision covers at most {BULK_REVIEW_FILTER_MAX_ITEMS} "
            "matching records. Narrow the filter."
        )
    locked = list(
        RecordItem.objects.select_for_update()
        .filter(pk__in=ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    selection = bulk_review_selection(locked, state=state)
    if (
        len(selection.eligible_ids) != expected_count
        or selection.selection_sha256 != selection_sha256
    ):
        raise BulkSelectionChanged(
            "The records matching this filter changed after you reviewed the "
            "count. Look at the count again and confirm."
        )
    if not selection.eligible_ids:
        raise ValidationError("No record in this selection can take that outcome.")
    items = list(
        RecordItem.objects.select_related("request", "reviewed_by")
        .filter(pk__in=selection.eligible_ids)
        .order_by("created_at", "pk")
    )
    request_ids = {item.request.pk for item in items}
    if len(request_ids) != 1:
        raise ValidationError(
            "Bulk review applies to the records of one request at a time."
        )
    request = items[0].request
    now = timezone.now()
    # The same fields review_record writes for a plain responsiveness
    # outcome, including the prior citations it records before clearing them.
    prior_exemptions = {}
    for record_id, exemption_id in (
        RecordItemWithholdingExemption.objects.filter(record_item__in=items)
        .order_by("pk")
        .values_list("record_item_id", "exemption_id")
    ):
        prior_exemptions.setdefault(record_id, []).append(exemption_id)
    RecordItemWithholdingExemption.objects.filter(record_item__in=items).delete()
    RecordItem.objects.filter(pk__in=[item.pk for item in items]).update(
        review_state=state,
        reviewed_by=actor,
        reviewed_at=now,
        withholding_exemption=None,
        second_reviewed_by=None,
        second_reviewed_at=None,
        redacted_storage_key="",
        redacted_sha256="",
        redacted_content_length=None,
        redacted_content_type="",
        redacted_manifest_sha256="",
    )
    AuditLog.objects.bulk_create([
        AuditLog(
            action="record.reviewed",
            target=str(item.pk),
            actor=actor,
            detail={
                "request_number": request.number,
                "before": {
                    "state": item.review_state,
                    "reviewed_by_id": (
                        item.reviewed_by.pk if item.reviewed_by else None
                    ),
                    "withholding_exemption_ids": prior_exemptions.get(item.pk, []),
                },
                "review_state": state,
                "withholding_exemption_ids": [],
            },
        )
        for item in items
    ])
    decisions = _store_decisions(items, state=state, actor=actor)
    audit(
        "record.bulk_reviewed",
        target=request.number,
        actor=actor,
        request_number=request.number,
        review_state=state,
        selection="filter",
        filter=filter_detail,
        record_item_ids=[str(item.pk) for item in items],
        record_item_count=len(items),
        excluded=selection.excluded,
        # The suggestions standing at decision time, for agreement only.
        advisory_run_ids=[
            str(decision.advisory_run_id)
            for decision in decisions
            if decision.advisory_run_id
        ],
    )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    for item in items:
        if state == RecordItem.ReviewState.RESPONSIVE:
            from .advisory_ai import defer_automatic_record_advisory
            from .models import AdvisoryRun

            defer_automatic_record_advisory(
                item.pk,
                kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
                actor=actor,
            )
        schedule_source_work_item_reconciliation("records", item.pk)
    return len(items)


@transaction.atomic
def bulk_review_records(record_item_ids, *, state, actor):
    """Apply one human responsiveness decision to a visible set of records.

    This is a human decision about a set the reviewer chose and could see — it
    is *not* an accept-all of AI output. Nothing about the model's
    recommendation changes what this writes; the suggestion each record showed
    is only stored beside the decision so agreement can be measured. Each
    record goes through the same :func:`review_record` service the single-record
    control uses, so each gets its own append-only audit row, its own
    validation, and its own advisory follow-up; one additional row records the
    bulk action itself.

    All-or-nothing on purpose: an ineligible record aborts the whole action and
    is named, rather than being silently skipped so the reviewer believes a
    decision was recorded that was not.
    """
    _require_reviewer(actor)
    if state not in BULK_REVIEW_STATES:
        raise ValidationError(
            "Bulk review records only responsive or non-responsive outcomes."
        )
    ids = list(dict.fromkeys(record_item_ids or ()))
    if not ids:
        raise ValidationError("Select at least one record to review.")
    if len(ids) > BULK_REVIEW_MAX_ITEMS:
        raise ValidationError(
            f"Bulk review covers at most {BULK_REVIEW_MAX_ITEMS} records "
            "in one action."
        )
    items = list(
        RecordItem.objects.select_for_update()
        .select_related("request")
        .filter(pk__in=ids)
        .order_by("created_at", "pk")
    )
    if len(items) != len(ids):
        raise ValidationError("Every selected record must exist.")
    request_ids = {item.request_id for item in items}
    if len(request_ids) > 1:
        raise ValidationError(
            "Bulk review applies to the records of one request at a time."
        )
    # A record withheld in full carries an exemption citation a named reviewer
    # chose. One confirmation over a set must not quietly drop it, so a
    # withheld record has to be revisited one at a time.
    withheld = [
        str(item.pk)
        for item in items
        if item.review_state == RecordItem.ReviewState.WITHHELD
    ]
    if withheld:
        raise ValidationError(
            "These records are withheld in full and must be reviewed one at a "
            "time so their exemption citations are reconsidered "
            "deliberately: " + ", ".join(sorted(withheld))
        )
    # A record marked a duplicate carries a reviewer's judgement about which
    # copy is the record. The same rule applies: one confirmation over a set
    # must not quietly overturn it, so it too is revisited one at a time.
    duplicates = [
        str(item.pk)
        for item in items
        if item.review_state == RecordItem.ReviewState.DUPLICATE
    ]
    if duplicates:
        raise ValidationError(
            "These records are marked duplicates and must be reviewed one at "
            "a time so that judgement is reconsidered deliberately: "
            + ", ".join(sorted(duplicates))
        )
    # A superseded record left review when a Records Officer re-scoped its
    # collection. Bringing it back is a deliberate, single-record decision.
    superseded = [
        str(item.pk)
        for item in items
        if item.review_state == RecordItem.ReviewState.SUPERSEDED
    ]
    if superseded:
        raise ValidationError(
            "These records were superseded with their collection and must be "
            "reviewed one at a time: " + ", ".join(sorted(superseded))
        )
    if state != RecordItem.ReviewState.RESPONSIVE:
        # One query rather than one per selected record, asked of the
        # redactions themselves. Asking RecordItem for
        # `redactions__voided_at__isnull=True` instead would promote the join
        # to a LEFT OUTER and match every record that has no redaction at all,
        # blocking the ordinary case this feature exists for.
        blocked = set(
            Redaction.objects.filter(
                record_item__in=[item.pk for item in items],
                voided_at__isnull=True,
            ).values_list("record_item_id", flat=True)
        )
        if blocked:
            raise ValidationError(
                "Void the active redactions on these records before marking "
                "them non-responsive: "
                + ", ".join(sorted(str(pk) for pk in blocked))
            )
    results = [
        _review_record(item.pk, state=state, actor=actor, bulk=True)
        for item in items
    ]
    reviewed = [item for item, _decision in results]
    request = items[0].request
    audit(
        "record.bulk_reviewed",
        target=request.number,
        actor=actor,
        request_number=request.number,
        review_state=state,
        record_item_ids=[str(item.pk) for item in reviewed],
        record_item_count=len(reviewed),
        # The suggestions standing at decision time, for agreement only.
        advisory_run_ids=[
            str(decision.advisory_run_id)
            for _item, decision in results
            if decision.advisory_run_id
        ],
    )
    return reviewed


@transaction.atomic
def review_record(
    record_item_id,
    *,
    state,
    actor,
    withholding_exemption_ids=(),
):
    item, _decision = _review_record(
        record_item_id,
        state=state,
        actor=actor,
        withholding_exemption_ids=withholding_exemption_ids,
    )
    return item


def _review_record(
    record_item_id,
    *,
    state,
    actor,
    withholding_exemption_ids=(),
    bulk=False,
):
    """The review itself; also returns the stored decision (#38)."""
    _require_reviewer(actor)
    if state not in _HUMAN_REVIEW_STATES:
        raise ValidationError("A valid human review outcome is required.")
    item = (
        RecordItem.objects.select_for_update()
        .select_related("request")
        .get(pk=record_item_id)
    )
    active_redactions = item.redactions.filter(voided_at__isnull=True)
    if state != RecordItem.ReviewState.RESPONSIVE and active_redactions.exists():
        raise ValidationError(
            "Active redactions must be voided before choosing this review outcome."
        )

    exemption_ids = list(dict.fromkeys(withholding_exemption_ids or ()))
    exemptions = list(
        ExemptionCitation.objects.select_for_update()
        .filter(pk__in=exemption_ids, active=True)
        .order_by("pk")
    )
    if len(exemptions) != len(exemption_ids):
        raise ValidationError("Every withholding exemption must be active and valid.")
    if state == RecordItem.ReviewState.WITHHELD and not exemptions:
        raise ValidationError("A fully withheld record requires an exemption citation.")
    if state != RecordItem.ReviewState.WITHHELD and exemptions:
        raise ValidationError("Withholding exemptions apply only to fully withheld records.")

    previous = {
        "state": item.review_state,
        "reviewed_by_id": item.reviewed_by_id,
        "withholding_exemption_ids": list(
            item.withholding_exemptions.values_list("pk", flat=True)
        ),
    }
    RecordItemWithholdingExemption.objects.filter(record_item=item).delete()
    item.review_state = state
    item.reviewed_by = actor
    item.reviewed_at = timezone.now()
    item.withholding_exemption = exemptions[0] if exemptions else None
    _clear_second_review(item)
    _clear_derived_artifact(item)
    item.full_clean(exclude=["withholding_exemptions"])
    item.save(update_fields=[
        "review_state",
        "reviewed_by",
        "reviewed_at",
        "withholding_exemption",
        "second_reviewed_by",
        "second_reviewed_at",
        "redacted_storage_key",
        "redacted_sha256",
        "redacted_content_length",
        "redacted_content_type",
        "redacted_manifest_sha256",
    ])
    RecordItemWithholdingExemption.objects.bulk_create([
        RecordItemWithholdingExemption(record_item=item, exemption=exemption)
        for exemption in exemptions
    ])
    decision = _store_decision(item, state=state, actor=actor, bulk=bulk)
    audit(
        "record.reviewed",
        target=str(item.pk),
        actor=actor,
        request_number=item.request.number,
        before=previous,
        review_state=state,
        withholding_exemption_ids=[exemption.pk for exemption in exemptions],
    )
    if state == RecordItem.ReviewState.RESPONSIVE:
        from .advisory_ai import defer_automatic_record_advisory
        from .models import AdvisoryRun

        defer_automatic_record_advisory(
            item.pk,
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS,
            actor=actor,
        )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("records", item.pk)
    return item, decision


def _store_decision(item, *, state, actor, bulk):
    """Store the decision and the suggestion standing at decision time (#38)."""
    from .agreement import latest_suggestion_runs
    from .models import ReviewDecision

    standing = latest_suggestion_runs(item).first()
    proposal = standing.proposal if standing else {}
    confidence = proposal.get("confidence")
    return ReviewDecision.objects.create(
        record_item=item,
        request=item.request,
        review_state=state,
        advisory_run=standing,
        advisory_label=proposal.get("suggested_state") or "",
        advisory_confidence=float(confidence) if confidence is not None else None,
        bulk=bulk,
        actor=actor,
    )


def _store_decisions(items, *, state, actor):
    """``_store_decision`` for a filter decision over up to 10,000 records:
    each record's standing suggestion, read in one query."""
    from django.db.models import OuterRef, Subquery

    from .agreement import annotate_latest_suggestion, latest_suggestion_runs
    from .models import ReviewDecision

    standing = {
        row["pk"]: row
        for row in annotate_latest_suggestion(
            RecordItem.objects.filter(pk__in=[item.pk for item in items]).annotate(
                advisory_run_id=Subquery(
                    latest_suggestion_runs(OuterRef("pk")).values("pk")[:1],
                ),
            )
        ).values("pk", "advisory_run_id", "advisory_suggested_state", "advisory_confidence")
    }
    return ReviewDecision.objects.bulk_create([
        ReviewDecision(
            record_item=item,
            request=item.request,
            review_state=state,
            advisory_run_id=standing[item.pk]["advisory_run_id"],
            advisory_label=standing[item.pk]["advisory_suggested_state"] or "",
            advisory_confidence=standing[item.pk]["advisory_confidence"],
            bulk=True,
            actor=actor,
        )
        for item in items
    ])


@transaction.atomic
def create_redaction(
    record_item_id,
    *,
    actor,
    page_number,
    x0,
    y0,
    x1,
    y1,
    exemption_id,
    explanation="",
    _client=None,
):
    _require_reviewer(actor)
    coordinates = (x0, y0, x1, y1)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in coordinates
    ):
        raise ValidationError("Redaction coordinates must be finite numbers.")
    item = (
        RecordItem.objects.select_for_update()
        .select_related("request")
        .get(pk=record_item_id)
    )
    if item.review_state != RecordItem.ReviewState.RESPONSIVE:
        raise ValidationError("Only a human-confirmed responsive record can be redacted.")
    exemption = ExemptionCitation.objects.select_for_update().filter(
        pk=exemption_id,
        active=True,
    ).first()
    if exemption is None:
        raise ValidationError("An active exemption citation is required.")

    document = _open_verified_pdf(item, _client=_client)
    try:
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise ValidationError("A valid PDF page number is required.") from exc
        if page_number < 1 or page_number > document.page_count:
            raise ValidationError("The redaction page does not exist in the retained PDF.")
    finally:
        document.close()

    redaction = Redaction(
        record_item=item,
        page_number=page_number,
        x0=float(x0),
        y0=float(y0),
        x1=float(x1),
        y1=float(y1),
        exemption=exemption,
        explanation=str(explanation or "").strip()[:500],
        created_by=actor,
    )
    redaction.full_clean()
    redaction.save()
    _save_invalidated_item(item)
    audit(
        "record.redaction.created",
        target=str(item.pk),
        actor=actor,
        request_number=item.request.number,
        redaction_id=redaction.pk,
        page_number=page_number,
        coordinates=[redaction.x0, redaction.y0, redaction.x1, redaction.y1],
        exemption_id=exemption.pk,
        citation=exemption.citation,
    )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("records", item.pk)
    return redaction


@transaction.atomic
def void_redaction(redaction_id, *, actor, reason):
    _require_reviewer(actor)
    reason = str(reason or "").strip()
    if len(reason) < 10:
        raise ValidationError("A substantive reason is required to void a redaction.")
    redaction = (
        Redaction.objects.select_for_update()
        .select_related("record_item__request")
        .get(pk=redaction_id)
    )
    if redaction.voided_at is not None:
        if redaction.void_reason == reason:
            return redaction
        raise ValidationError("This redaction was already voided with retained evidence.")
    redaction.voided_by = actor
    redaction.voided_at = timezone.now()
    redaction.void_reason = reason[:500]
    redaction.full_clean()
    redaction.save(update_fields=["voided_by", "voided_at", "void_reason"])
    _save_invalidated_item(redaction.record_item)
    audit(
        "record.redaction.voided",
        target=str(redaction.record_item_id),
        actor=actor,
        request_number=redaction.record_item.request.number,
        redaction_id=redaction.pk,
        reason=redaction.void_reason,
    )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("records", redaction.record_item_id)
    return redaction


@transaction.atomic
def approve_second_review(record_item_id, *, actor):
    _require_reviewer(actor)
    item = (
        RecordItem.objects.select_for_update()
        .select_related("request")
        .get(pk=record_item_id)
    )
    if not item.second_review_required:
        raise ValidationError("This record does not require a second review.")
    if item.reviewed_by_id is None:
        raise ValidationError("The first human review must be completed first.")
    if item.reviewed_by_id == actor.pk:
        raise ValidationError("The second review must be performed by a different reviewer.")
    item.second_reviewed_by = actor
    item.second_reviewed_at = timezone.now()
    item.save(update_fields=["second_reviewed_by", "second_reviewed_at"])
    audit(
        "record.second_review.approved",
        target=str(item.pk),
        actor=actor,
        request_number=item.request.number,
        first_reviewer_id=item.reviewed_by_id,
    )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("records", item.pk)
    return item


def _redaction_manifest(item, redactions):
    payload = {
        "source_sha256": item.sha256,
        "redactions": [
            {
                "id": redaction.pk,
                "page": redaction.page_number,
                "coordinates": [
                    redaction.x0,
                    redaction.y0,
                    redaction.x1,
                    redaction.y1,
                ],
                "exemption_id": redaction.exemption_id,
            }
            for redaction in redactions
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _redaction_source_rectangle(page, redaction):
    """Convert preview-normalized display coordinates to PDF source space."""
    bounds = page.rect
    display_rectangle = fitz.Rect(
        bounds.x0 + redaction.x0 * bounds.width,
        bounds.y0 + redaction.y0 * bounds.height,
        bounds.x0 + redaction.x1 * bounds.width,
        bounds.y0 + redaction.y1 * bounds.height,
    )
    return display_rectangle * page.derotation_matrix


def _rasterize_redactions(document, redactions):
    by_page = {}
    for redaction in redactions:
        by_page.setdefault(redaction.page_number - 1, []).append(redaction)
    output = fitz.open()
    # Read once: every page of one release burns in at the same resolution,
    # and a store read per page would be a cache round trip per page.
    dpi = runtime.get("PRR_REDACTION_DPI")
    try:
        for page_index, page in enumerate(document):
            for redaction in by_page.get(page_index, ()):
                rectangle = _redaction_source_rectangle(page, redaction)
                page.add_redact_annot(rectangle, fill=(0, 0, 0))
            if by_page.get(page_index):
                page.apply_redactions()
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            output_page = output.new_page(
                width=page.rect.width,
                height=page.rect.height,
            )
            output_page.insert_image(output_page.rect, stream=pixmap.tobytes("png"))
        return output.tobytes(garbage=4, deflate=True)
    finally:
        output.close()


@transaction.atomic
def render_redacted_record(record_item_id, *, actor, _client=None):
    _require_reviewer(actor)
    item = (
        RecordItem.objects.select_for_update()
        .select_related("request")
        .get(pk=record_item_id)
    )
    if item.review_state != RecordItem.ReviewState.RESPONSIVE:
        raise ValidationError("Only a responsive record can produce a redacted artifact.")
    redactions = list(
        item.redactions.select_related("exemption")
        .filter(voided_at__isnull=True)
        .order_by("page_number", "pk")
    )
    if not redactions:
        raise ValidationError("At least one active redaction is required.")
    if item.second_review_required and item.second_reviewed_at is None:
        raise ValidationError("Required second review must be approved before burn-in.")
    manifest_sha256 = _redaction_manifest(item, redactions)
    if (
        item.redacted_storage_key
        and item.redacted_manifest_sha256 == manifest_sha256
    ):
        return item

    document = _open_verified_pdf(item, _client=_client)
    try:
        output = _rasterize_redactions(document, redactions)
    finally:
        document.close()
    output_sha256 = hashlib.sha256(output).hexdigest()
    storage_key = generated_record_storage_key(
        item.request_id,
        original_filename="redacted.pdf",
    )
    store_immutable_object(
        storage_key,
        io.BytesIO(output),
        content_length=len(output),
        sha256=output_sha256,
        content_type="application/pdf",
        _client=_client,
    )
    item.redacted_storage_key = storage_key
    item.redacted_sha256 = output_sha256
    item.redacted_content_length = len(output)
    item.redacted_content_type = "application/pdf"
    item.redacted_manifest_sha256 = manifest_sha256
    item.save(update_fields=[
        "redacted_storage_key",
        "redacted_sha256",
        "redacted_content_length",
        "redacted_content_type",
        "redacted_manifest_sha256",
    ])
    audit(
        "record.redaction.rendered",
        target=str(item.pk),
        actor=actor,
        request_number=item.request.number,
        manifest_sha256=manifest_sha256,
        output_sha256=output_sha256,
        output_size=len(output),
        redaction_count=len(redactions),
    )
    return item
