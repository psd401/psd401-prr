from django.conf import settings
from django.db import transaction


def schedule_inbound_receipt_reconciliation(receipt_id):
    """Queue projection repair only after authoritative source evidence commits."""
    from .tasks import reconcile_inbound_receipt_work_item

    def enqueue_after_commit():
        reconcile_inbound_receipt_work_item.delay(str(receipt_id))

    transaction.on_commit(
        enqueue_after_commit,
        robust=True,
    )


def schedule_source_work_item_reconciliation(source, source_id):
    """Queue one source projection only after authoritative evidence commits."""
    # The shared SQLite test database cannot flush database-enforced append-only
    # work events created by Celery's synchronous eager mode. Dedicated source
    # projection tests opt back in and exercise the complete post-commit path.
    if not getattr(settings, "PRR_TEST_EAGER_SOURCE_WORK_RECONCILIATION", True):
        return

    from .tasks import reconcile_case_work_item_source

    def enqueue_after_commit():
        reconcile_case_work_item_source.delay(source, str(source_id))

    transaction.on_commit(
        enqueue_after_commit,
        robust=True,
    )
