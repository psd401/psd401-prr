from celery import shared_task

from .reconciliation import reconcile_inbound_receipt, reconcile_inbound_work_items
from .source_reconciliation import (
    reconcile_source_work_item,
    reconcile_source_work_items,
)
from .triage_alerts import alert_waiting_inbound_triage


@shared_task
def reconcile_inbound_receipt_work_item(receipt_id):
    """Reconcile one committed receipt outside the inbound request path."""
    result = reconcile_inbound_receipt(receipt_id, skip_locked=True)
    return {
        "created": result.created,
        "changed": result.changed,
        "excluded": result.excluded,
        "skipped": result.skipped,
        "work_item_id": (
            str(result.work_item.pk) if result.work_item is not None else None
        ),
    }


@shared_task
def reconcile_inbound_work_item_backfill(*, limit=250, after_id=None):
    """Repair a bounded page and continue without touching source evidence."""
    result = reconcile_inbound_work_items(limit=limit, after_id=after_id)
    if result.next_cursor is not None:
        reconcile_inbound_work_item_backfill.delay(
            limit=limit,
            after_id=result.next_cursor,
        )
    return {
        "scanned": result.scanned,
        "created": result.created,
        "changed": result.changed,
        "unchanged": result.unchanged,
        "excluded": result.excluded,
        "skipped": result.skipped,
        "failed": result.failed,
        "next_cursor": result.next_cursor,
    }


@shared_task
def reconcile_case_work_item_source(source, source_id):
    """Reconcile one committed source outside its authoritative transaction."""
    work_item = reconcile_source_work_item(
        source=source,
        source_id=source_id,
        seed_assignee=True,
    )
    return str(work_item.pk) if work_item is not None else None


@shared_task
def reconcile_case_work_item_backfill(
    *,
    source="requests",
    limit=250,
    after_id=None,
):
    """Repair every bounded source queue without modifying source evidence."""
    source_order = ["requests", "collections", "drafts", "records"]
    result = reconcile_source_work_items(
        source=source,
        limit=limit,
        after_id=after_id,
    )
    if result.next_cursor is not None:
        reconcile_case_work_item_backfill.delay(
            source=source,
            limit=limit,
            after_id=result.next_cursor,
        )
    else:
        index = source_order.index(source)
        if index + 1 < len(source_order):
            reconcile_case_work_item_backfill.delay(
                source=source_order[index + 1],
                limit=limit,
            )
    return {
        "source": result.source,
        "scanned": result.scanned,
        "failed": result.failed,
        "next_cursor": result.next_cursor,
    }


@shared_task
def alert_waiting_inbound_triage_daily():
    """Tell the records team about inbound mail still waiting for triage."""
    return alert_waiting_inbound_triage()
