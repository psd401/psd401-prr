# ABOUTME: Idempotent current-work projections and bounded repair for case-work sources.
# ABOUTME: Reconciliation changes only work metadata; it never changes legal source state.
import logging
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.api.identity import staff_roles
from apps.audit.models import audit

from .models import WorkItem, WorkItemEvent
from .services import materialize_work_item, roles_for_work_kind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceReconciliationBatch:
    source: str
    scanned: int
    failed: int
    next_cursor: str | None


def _authorized_assignee(candidate, work_kind):
    if candidate is None:
        return None
    if staff_roles(candidate).intersection(roles_for_work_kind(work_kind)):
        return candidate
    return None


def _append_reconciliation_event(work_item, *, before, reason):
    event = WorkItemEvent.objects.create(
        work_item=work_item,
        event_type=WorkItemEvent.EventType.RECONCILED,
        resulting_version=work_item.version,
        metadata={
            "before": before,
            "after": {
                "assignee_id": work_item.assignee_id,
                "disposition": work_item.disposition,
            },
            "reason": reason,
        },
    )
    audit(
        "work_queue.source_reconciled",
        target=str(work_item.pk),
        event_id=str(event.pk),
        work_kind=work_item.work_kind,
        source_type=work_item.source_type,
        source_id=work_item.source_id,
        reason=reason,
        version=work_item.version,
    )


@transaction.atomic
def _reconcile(
    *,
    source_type,
    source_id,
    work_kind,
    request,
    eligible,
    assignee=None,
):
    current = (
        WorkItem.objects.select_for_update()
        .filter(
            source_type=source_type,
            source_id=str(source_id),
            work_kind=work_kind,
        )
        .first()
    )
    if current is None:
        if not eligible:
            return None
        desired_assignee = _authorized_assignee(assignee, work_kind)
        return materialize_work_item(
            source_type=source_type,
            source_id=source_id,
            work_kind=work_kind,
            request=request,
            assignee=desired_assignee,
        ).work_item

    if current.request_id != request.pk:
        raise ValidationError(
            "A source projection cannot move between public-records requests."
        )

    before = {
        "assignee_id": current.assignee_id,
        "disposition": current.disposition,
    }
    changed = False
    reason = "source_unchanged"
    if eligible:
        if current.disposition == WorkItem.Disposition.SOURCE_EXCLUDED:
            current.disposition = WorkItem.Disposition.NEEDS_ATTENTION
            current.disposed_at = None
            current.disposed_by = None
            current.no_action_category = ""
            current.no_action_reason = ""
            current.no_action_by = None
            current.no_action_at = None
            changed = True
            reason = "source_reopened"
    elif not current.is_terminal:
        current.disposition = WorkItem.Disposition.SOURCE_EXCLUDED
        current.disposed_at = timezone.now()
        current.disposed_by = None
        changed = True
        reason = "source_completed"

    if not changed:
        return current
    current.version += 1
    current.save(
        update_fields=[
            "disposition",
            "disposed_at",
            "disposed_by",
            "no_action_category",
            "no_action_reason",
            "no_action_by",
            "no_action_at",
            "version",
            "updated_at",
        ]
    )
    _append_reconciliation_event(current, before=before, reason=reason)
    return current


def reconcile_request_work(request, *, seed_assignee=True):
    from apps.records.models import RequestStatus

    return _reconcile(
        source_type=WorkItem.SourceType.REQUEST,
        source_id=request.pk,
        work_kind=WorkItem.WorkKind.REQUEST_ACTION,
        request=request,
        eligible=(
            request.status == RequestStatus.RECEIVED
            and not request.initial_response_delivered
        ),
        assignee=request.assigned_officer if seed_assignee else None,
    )


def reconcile_fee_unpaid_work(request, *, eligible):
    """One case-level unpaid-fee chip that retires itself once nothing is owed.

    ``eligible`` comes from ``records.fees.unpaid_fee_attention_requests``; the
    chip is excluded, never Handled, when the fee is paid, the deposit lifted,
    or the case closed, and a later unpaid fee on the same case reopens it.
    An officer's own disposition is left exactly as the officer set it.
    """
    return _reconcile(
        source_type=WorkItem.SourceType.REQUEST,
        source_id=request.pk,
        work_kind=WorkItem.WorkKind.FEE_UNPAID,
        request=request,
        eligible=eligible,
    )


def reconcile_collection_task_work(task, *, seed_assignee=True):
    from apps.records.models import CollectionTask

    return _reconcile(
        source_type=WorkItem.SourceType.COLLECTION_TASK,
        source_id=task.pk,
        work_kind=WorkItem.WorkKind.COLLECTION,
        request=task.request,
        eligible=task.state != CollectionTask.State.COMPLETE,
        assignee=task.assigned_to if seed_assignee else None,
    )


def reconcile_correspondence_draft_work(correspondence, *, seed_assignee=True):
    from apps.correspondence.models import Correspondence

    return _reconcile(
        source_type=WorkItem.SourceType.CORRESPONDENCE,
        source_id=correspondence.pk,
        work_kind=WorkItem.WorkKind.CORRESPONDENCE_DRAFT,
        request=correspondence.request,
        eligible=(
            not correspondence.automated
            and correspondence.status == Correspondence.Status.DRAFT
        ),
        assignee=correspondence.created_by if seed_assignee else None,
    )


def reconcile_record_item_work(item, *, seed_assignee=True):
    from apps.records.models import RecordItem

    first_review = _reconcile(
        source_type=WorkItem.SourceType.RECORD_ITEM,
        source_id=item.pk,
        work_kind=WorkItem.WorkKind.RECORD_REVIEW,
        request=item.request,
        eligible=item.review_state == RecordItem.ReviewState.UNREVIEWED,
    )
    second_review = _reconcile(
        source_type=WorkItem.SourceType.RECORD_ITEM,
        source_id=item.pk,
        work_kind=WorkItem.WorkKind.SECOND_REVIEW,
        request=item.request,
        eligible=(
            item.review_state != RecordItem.ReviewState.UNREVIEWED
            and item.second_review_required
            and item.second_reviewed_at is None
        ),
    )
    if second_review is not None and (
        second_review.disposition != WorkItem.Disposition.SOURCE_EXCLUDED
        or first_review is None
        or first_review.disposition == WorkItem.Disposition.SOURCE_EXCLUDED
    ):
        return second_review
    return first_review


def _source_registry():
    from apps.correspondence.models import Correspondence
    from apps.records.models import CollectionTask, PublicRecordsRequest, RecordItem

    return {
        "requests": (
            PublicRecordsRequest.objects.select_related("assigned_officer"),
            reconcile_request_work,
        ),
        "collections": (
            CollectionTask.objects.select_related("request", "assigned_to"),
            reconcile_collection_task_work,
        ),
        "drafts": (
            Correspondence.objects.filter(
                direction=Correspondence.Direction.OUTBOUND,
            ).select_related("request", "created_by"),
            reconcile_correspondence_draft_work,
        ),
        "records": (
            RecordItem.objects.select_related(
                "request",
                "withholding_exemption",
            ),
            reconcile_record_item_work,
        ),
    }


def reconcile_source_work_item(*, source, source_id, seed_assignee=True):
    sources = _source_registry()
    if source not in sources:
        raise ValidationError("Unknown source reconciliation queue.")
    queryset, reconciler = sources[source]
    return reconciler(
        queryset.get(pk=source_id),
        seed_assignee=seed_assignee,
    )


def reconcile_source_work_items(*, source, limit=250, after_id=None):
    sources = _source_registry()
    if source not in sources:
        raise ValidationError("Unknown source reconciliation queue.")
    try:
        bounded_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Reconciliation limit must be an integer.") from exc
    if bounded_limit < 1 or bounded_limit > 500:
        raise ValidationError("Reconciliation limit must be between 1 and 500.")

    queryset, reconciler = sources[source]
    queryset = queryset.order_by("pk")
    if after_id is not None:
        queryset = queryset.filter(pk__gt=after_id)
    ids = list(queryset.values_list("pk", flat=True)[: bounded_limit + 1])
    page = ids[:bounded_limit]
    failed = 0
    for source_id in page:
        try:
            # Historical repair must not infer that a case/source owner owns the
            # next action. ADR 0003 requires missing projections to backfill as
            # unassigned; live post-commit hooks may seed an initial assignee.
            reconciler(queryset.get(pk=source_id), seed_assignee=False)
        except Exception:
            failed += 1
            logger.exception(
                "Source work-item reconciliation failed",
                extra={"source": source, "source_id": str(source_id)},
            )
    return SourceReconciliationBatch(
        source=source,
        scanned=len(page),
        failed=failed,
        next_cursor=(str(page[-1]) if len(ids) > bounded_limit else None),
    )
