"""Take an over-broad collection's undecided records out of review.

A Vault export can sweep in thousands of messages the request never asked for.
Nothing here deletes them: every record stays, with its custody chain, and the
supersession itself is append-only evidence. What changes is that the records
nobody has decided yet stop counting as work. Records a reviewer has already
decided are left exactly as they are.

A later collection that finds one of these messages again restores it to
review (``restore_superseded_record``), so re-scoping can never quietly hide a
message the narrower search still considers relevant.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.api.identity import StaffRole, has_staff_role
from apps.audit.models import AuditLog, audit

from .models import (
    CollectionSupersession,
    CollectionTask,
    RecordItem,
    StatusEvent,
)

MIN_SUPERSESSION_REASON = 20


@dataclass(frozen=True)
class SupersessionResult:
    collection_task_id: int
    record_count: int


def _schedule_work_reconciliation(record_ids):
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    for record_id in record_ids:
        schedule_source_work_item_reconciliation("records", record_id)


@transaction.atomic
def supersede_collection(collection_task_id, *, actor, reason):
    if not has_staff_role(actor, StaffRole.RECORDS_OFFICER):
        raise ValidationError(
            "A current Records Officer is required to supersede a collection."
        )
    reason = (reason or "").strip()
    if len(reason) < MIN_SUPERSESSION_REASON:
        raise ValidationError(
            "Say why this collection is being superseded "
            f"(at least {MIN_SUPERSESSION_REASON} characters)."
        )
    task = (
        CollectionTask.objects.select_for_update()
        .select_related("request")
        .get(pk=collection_task_id)
    )
    # Only a finished Vault Gmail export: an export still ingesting would keep
    # adding unreviewed records after this, and other tasks are not exports.
    if (
        task.kind != CollectionTask.Kind.VAULT_MAIL
        or task.state != CollectionTask.State.COMPLETE
    ):
        raise ValidationError(
            "Only a completed Vault Gmail collection can be superseded."
        )
    items = list(
        RecordItem.objects.select_for_update()
        .filter(
            collection_task=task,
            review_state=RecordItem.ReviewState.UNREVIEWED,
        )
        .order_by("created_at", "pk")
    )
    if not items:
        return SupersessionResult(collection_task_id=task.pk, record_count=0)

    now = timezone.now()
    ids = [item.pk for item in items]
    RecordItem.objects.filter(pk__in=ids).update(
        review_state=RecordItem.ReviewState.SUPERSEDED,
        reviewed_by=actor,
        reviewed_at=now,
    )
    request = task.request
    # One row per record, the same shape a single review writes, so a
    # record's own history shows how it left the queue.
    AuditLog.objects.bulk_create([
        AuditLog(
            action="record.reviewed",
            target=str(item.pk),
            actor=actor,
            detail={
                "request_number": request.number,
                "before": {
                    "state": item.review_state,
                    "reviewed_by_id": None,
                    "withholding_exemption_ids": [],
                },
                "review_state": RecordItem.ReviewState.SUPERSEDED,
                "withholding_exemption_ids": [],
                "collection_task_id": task.pk,
            },
        )
        for item in items
    ])
    CollectionSupersession.objects.create(
        collection_task=task,
        actor=actor,
        reason=reason,
        record_count=len(items),
    )
    audit(
        "collection.superseded",
        target=request.number,
        actor=actor,
        request_number=request.number,
        collection_task_id=task.pk,
        record_count=len(items),
        reason=reason,
    )
    StatusEvent.objects.create(
        request=request,
        actor=actor,
        note=(
            f"Collection superseded: {len(items)} unreviewed record(s) from "
            f"collection task {task.pk} left review. Reason: {reason}"
        ),
        visible_to_requester=False,
    )
    _schedule_work_reconciliation(ids)
    return SupersessionResult(collection_task_id=task.pk, record_count=len(items))


def restore_superseded_record(item, *, task):
    """Put a superseded record back in review for the collection that found it.

    Called by Vault ingest when a message it would fold into an existing
    record turns out to be one a Records Officer superseded. The new
    collection found it on its own terms, so it needs a fresh human decision.

    The record moves to the new collection: ``collection_task`` is replaced
    and the old link is kept only in the audit row. Superseding the old
    collection again therefore no longer reaches this record, which is the
    intent -- it now belongs to the search that found it.
    """
    if item.review_state != RecordItem.ReviewState.SUPERSEDED:
        return item
    before = {
        "state": item.review_state,
        "reviewed_by_id": item.reviewed_by.pk if item.reviewed_by else None,
        "collection_task_id": (
            item.collection_task.pk if item.collection_task else None
        ),
    }
    item.review_state = RecordItem.ReviewState.UNREVIEWED
    item.reviewed_by = None
    item.reviewed_at = None
    item.collection_task = task
    item.save(update_fields=[
        "review_state", "reviewed_by", "reviewed_at", "collection_task",
    ])
    audit(
        "record.restored_by_collection",
        target=str(item.pk),
        actor=None,
        request_number=item.request.number,
        before=before,
        collection_task_id=task.pk,
    )
    _schedule_work_reconciliation([item.pk])
    return item
