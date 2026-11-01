import logging
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDeliveryNotification,
    InboundCorrespondenceReceipt,
)

from .models import WorkItem, WorkItemEvent
from .services import materialize_work_item

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundRoute:
    work_kind: str
    request_id: object | None
    routing_code: str


@dataclass(frozen=True)
class ReconciliationResult:
    work_item: WorkItem | None
    created: bool = False
    changed: bool = False
    excluded: bool = False
    skipped: bool = False


@dataclass(frozen=True)
class ReconciliationBatchResult:
    scanned: int
    created: int
    changed: int
    unchanged: int
    excluded: int
    skipped: int
    failed: int
    next_cursor: str | None


def _route_for_receipt(receipt) -> InboundRoute | None:
    if (
        receipt.parsing_status
        == InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE
        or receipt.matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE
    ):
        return None
    if (
        receipt.quarantined
        or receipt.parsing_status
        in {
            InboundCorrespondenceReceipt.ParsingStatus.FAILED,
            InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
        }
        or receipt.matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
    ):
        return InboundRoute(
            WorkItem.WorkKind.INBOUND_QUARANTINED,
            receipt.matched_request_id or receipt.candidate_request_id,
            "quarantined",
        )

    correspondence = receipt.matched_correspondence
    is_dsn = (
        receipt.match_method
        == InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS
        or (
            correspondence is not None
            and correspondence.kind
            == Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
        )
    )
    if is_dsn:
        exception_actions = set(
            receipt.delivery_notifications.filter(
                action__in=[
                    CorrespondenceDeliveryNotification.Action.FAILED,
                    CorrespondenceDeliveryNotification.Action.DELAYED,
                ]
            ).values_list("action", flat=True)
        )
        if not exception_actions:
            return None
        routing_code = (
            "failed_dsn"
            if CorrespondenceDeliveryNotification.Action.FAILED
            in exception_actions
            else "delayed_dsn"
        )
        return InboundRoute(
            WorkItem.WorkKind.DELIVERY_EXCEPTION,
            receipt.matched_request_id,
            routing_code,
        )

    if (
        receipt.matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.MATCHED
    ):
        if (
            receipt.match_method
            == InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
        ):
            return InboundRoute(
                WorkItem.WorkKind.EMAIL_INTAKE,
                receipt.matched_request_id,
                "email_intake",
            )
        return InboundRoute(
            WorkItem.WorkKind.INBOUND_REPLY,
            receipt.matched_request_id,
            "matched_reply",
        )
    if (
        receipt.matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
    ):
        return InboundRoute(
            WorkItem.WorkKind.INBOUND_CANDIDATE,
            receipt.candidate_request_id,
            "candidate",
        )
    if (
        receipt.matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS
    ):
        return InboundRoute(
            WorkItem.WorkKind.INBOUND_AMBIGUOUS,
            None,
            "ambiguous",
        )
    if (
        receipt.parsing_status
        == InboundCorrespondenceReceipt.ParsingStatus.PARSED
        and receipt.matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
    ):
        return InboundRoute(
            WorkItem.WorkKind.INBOUND_UNMATCHED,
            None,
            "unmatched",
        )
    return None


def _append_reconciliation_event(work_item, *, before, routing_code):
    event = WorkItemEvent.objects.create(
        work_item=work_item,
        event_type=WorkItemEvent.EventType.RECONCILED,
        resulting_version=work_item.version,
        metadata={
            "before": before,
            "after": {
                "work_kind": work_item.work_kind,
                "request_id": (
                    str(work_item.request_id) if work_item.request_id else None
                ),
                "disposition": work_item.disposition,
            },
            "routing_code": routing_code,
        },
    )
    audit(
        "work_queue.item_reconciled",
        target=str(work_item.pk),
        event_id=str(event.pk),
        routing_code=routing_code,
        work_kind=work_item.work_kind,
        request_id=(str(work_item.request_id) if work_item.request_id else None),
        version=work_item.version,
    )
    return event


@transaction.atomic
def reconcile_inbound_receipt(
    receipt_id,
    *,
    skip_locked=False,
) -> ReconciliationResult:
    supports_skip_locked = (
        skip_locked and connection.features.has_select_for_update_skip_locked
    )
    receipt_queryset = (
        InboundCorrespondenceReceipt.objects.select_for_update(
            skip_locked=supports_skip_locked,
            of=("self",),
        )
        .select_related("matched_correspondence")
        .filter(pk=receipt_id)
    )
    receipt = receipt_queryset.first()
    if receipt is None and supports_skip_locked:
        return ReconciliationResult(None, skipped=True)
    if receipt is None:
        raise InboundCorrespondenceReceipt.DoesNotExist
    route = _route_for_receipt(receipt)
    existing = list(
        WorkItem.objects.select_for_update()
        .filter(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=str(receipt.pk),
        )
        .order_by("created_at", "id")
    )
    if len(existing) > 1:
        raise ValidationError(
            "Inbound source has conflicting work-item projections."
        )
    if route is None:
        if not existing:
            return ReconciliationResult(None, excluded=True)
        work_item = existing[0]
        if work_item.is_terminal:
            return ReconciliationResult(work_item, excluded=True)
        before = {
            "work_kind": work_item.work_kind,
            "request_id": (
                str(work_item.request_id) if work_item.request_id else None
            ),
            "disposition": work_item.disposition,
        }
        work_item.disposition = WorkItem.Disposition.SOURCE_EXCLUDED
        work_item.disposed_at = timezone.now()
        work_item.disposed_by = None
        work_item.version += 1
        work_item.save(
            update_fields=[
                "disposition",
                "disposed_at",
                "disposed_by",
                "version",
                "updated_at",
            ]
        )
        _append_reconciliation_event(
            work_item,
            before=before,
            routing_code="source_excluded",
        )
        return ReconciliationResult(work_item, changed=True, excluded=True)

    if not existing:
        materialized = materialize_work_item(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=receipt.pk,
            work_kind=route.work_kind,
            request=(
                receipt.matched_request
                if receipt.matched_request_id == route.request_id
                else receipt.candidate_request
                if receipt.candidate_request_id == route.request_id
                else None
            ),
        )
        return ReconciliationResult(
            materialized.work_item,
            created=materialized.created,
        )

    work_item = existing[0]
    source_was_excluded = (
        work_item.disposition == WorkItem.Disposition.SOURCE_EXCLUDED
    )
    if (
        work_item.work_kind == route.work_kind
        and work_item.request_id == route.request_id
        and not source_was_excluded
    ):
        return ReconciliationResult(work_item)
    before = {
        "work_kind": work_item.work_kind,
        "request_id": (
            str(work_item.request_id) if work_item.request_id else None
        ),
        "disposition": work_item.disposition,
    }
    work_item.work_kind = route.work_kind
    work_item.request_id = route.request_id
    if source_was_excluded:
        work_item.disposition = WorkItem.Disposition.NEEDS_ATTENTION
        work_item.disposed_at = None
        work_item.disposed_by = None
    work_item.version += 1
    work_item.save(
        update_fields=[
            "work_kind",
            "request",
            "disposition",
            "disposed_at",
            "disposed_by",
            "version",
            "updated_at",
        ]
    )
    _append_reconciliation_event(
        work_item,
        before=before,
        routing_code=route.routing_code,
    )
    return ReconciliationResult(work_item, changed=True)


def reconcile_inbound_work_items(
    *,
    limit=250,
    after_id=None,
) -> ReconciliationBatchResult:
    try:
        bounded_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Reconciliation limit must be an integer.") from exc
    if bounded_limit < 1 or bounded_limit > 500:
        raise ValidationError("Reconciliation limit must be between 1 and 500.")
    queryset = InboundCorrespondenceReceipt.objects.order_by("id")
    if after_id:
        queryset = queryset.filter(id__gt=after_id)
    ids = list(queryset.values_list("id", flat=True)[: bounded_limit + 1])
    page = ids[:bounded_limit]
    counts = {
        "created": 0,
        "changed": 0,
        "unchanged": 0,
        "excluded": 0,
        "skipped": 0,
        "failed": 0,
    }
    for receipt_id in page:
        try:
            result = reconcile_inbound_receipt(receipt_id, skip_locked=True)
        except Exception:
            logger.exception(
                "Inbound work-item reconciliation failed for receipt %s.",
                receipt_id,
            )
            counts["failed"] += 1
            continue
        if result.created:
            counts["created"] += 1
        elif result.changed:
            counts["changed"] += 1
        elif result.skipped:
            counts["skipped"] += 1
        elif result.excluded:
            counts["excluded"] += 1
        else:
            counts["unchanged"] += 1
    return ReconciliationBatchResult(
        scanned=len(page),
        next_cursor=(str(page[-1]) if len(ids) > bounded_limit else None),
        **counts,
    )
