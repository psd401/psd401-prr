import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence.models import (
    Correspondence,
    InboundCorrespondenceReceipt,
)
from apps.records.models import PublicRecordsRequest, Requester
from apps.work_queue import services as work_queue_services
from apps.work_queue.models import WorkItem, WorkItemEvent
from apps.work_queue.reconciliation import reconcile_inbound_work_items
from apps.work_queue.services import (
    IdempotencyConflict,
    VersionConflict,
    assign_work_item,
    mark_work_item_read,
    materialize_work_item,
    set_work_item_disposition,
)


@skipUnless(
    connection.vendor == "postgresql",
    "Work-item concurrency contracts require PostgreSQL row locks.",
)
@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class WorkQueueConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "concurrent-work-officer",
            "concurrent-work-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="concurrent@example.org"),
            description="Concurrent work-item contract.",
        )

    @staticmethod
    def run_concurrently(callbacks):
        barrier = threading.Barrier(len(callbacks))

        def run(callback):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return callback()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(callbacks)) as executor:
            futures = [executor.submit(run, callback) for callback in callbacks]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_materialization_creates_one_item_event_and_audit(self):
        def materialize():
            request = PublicRecordsRequest.objects.get(pk=self.request.pk)
            result = materialize_work_item(
                source_type=WorkItem.SourceType.REQUEST,
                source_id=request.pk,
                work_kind=WorkItem.WorkKind.REQUEST_ACTION,
                request=request,
            )
            return result.created, result.work_item.pk, result.event.pk

        results = self.run_concurrently([materialize, materialize])

        self.assertEqual(sorted(result[0] for result in results), [False, True])
        self.assertEqual(len({result[1] for result in results}), 1)
        self.assertEqual(len({result[2] for result in results}), 1)
        self.assertEqual(WorkItem.objects.count(), 1)
        self.assertEqual(
            WorkItemEvent.objects.filter(
                event_type=WorkItemEvent.EventType.CREATED
            ).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(action="work_queue.item_created").count(),
            1,
        )

    def test_concurrent_disposition_has_one_success_and_one_version_conflict(self):
        item = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=self.request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=self.request,
        ).work_item

        def disposition(value, key):
            current_item = WorkItem.objects.get(pk=item.pk)
            actor = get_user_model().objects.get(pk=self.officer.pk)
            try:
                result = set_work_item_disposition(
                    current_item,
                    disposition=value,
                    actor=actor,
                    expected_version=1,
                    idempotency_key=key,
                )
            except VersionConflict as exc:
                return "conflict", exc.current
            return "success", result.work_item.version

        results = self.run_concurrently(
            [
                lambda: disposition(WorkItem.Disposition.WAITING, "race:waiting"),
                lambda: disposition(WorkItem.Disposition.HANDLED, "race:handled"),
            ]
        )

        self.assertEqual(sorted(result[0] for result in results), ["conflict", "success"])
        self.assertTrue(all(result[1] == 2 for result in results))
        item.refresh_from_db()
        self.assertIn(
            item.disposition,
            {WorkItem.Disposition.WAITING, WorkItem.Disposition.HANDLED},
        )
        self.assertEqual(item.version, 2)
        self.assertEqual(
            WorkItemEvent.objects.filter(
                work_item=item,
                event_type=WorkItemEvent.EventType.DISPOSITIONED,
            ).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="work_queue.item_dispositioned",
                target=str(item.pk),
            ).count(),
            1,
        )

    def test_concurrent_exact_retry_returns_one_mutation_event_and_audit(self):
        item = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=self.request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=self.request,
        ).work_item

        def disposition():
            current_item = WorkItem.objects.get(pk=item.pk)
            actor = get_user_model().objects.get(pk=self.officer.pk)
            result = set_work_item_disposition(
                current_item,
                disposition=WorkItem.Disposition.WAITING,
                actor=actor,
                expected_version=1,
                idempotency_key="race:exact-retry",
            )
            return result.idempotent, result.event.pk

        results = self.run_concurrently([disposition, disposition])

        self.assertEqual(sorted(result[0] for result in results), [False, True])
        self.assertEqual(len({result[1] for result in results}), 1)
        self.assertEqual(
            WorkItemEvent.objects.filter(
                work_item=item,
                idempotency_key="race:exact-retry",
            ).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="work_queue.item_dispositioned",
                target=str(item.pk),
            ).count(),
            1,
        )

    def test_actor_lock_serializes_same_key_across_different_work_items(self):
        first_item = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=self.request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=self.request,
        ).work_item
        second_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="second@example.org"),
            description="Second concurrent work-item contract.",
        )
        second_item = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=second_request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=second_request,
        ).work_item

        def disposition(item_id):
            current_item = WorkItem.objects.get(pk=item_id)
            actor = get_user_model().objects.get(pk=self.officer.pk)
            try:
                result = set_work_item_disposition(
                    current_item,
                    disposition=WorkItem.Disposition.WAITING,
                    actor=actor,
                    expected_version=1,
                    idempotency_key="race:cross-item-key",
                )
            except IdempotencyConflict:
                return "conflict"
            return "success" if not result.idempotent else "idempotent"

        actor_lock_barrier = threading.Barrier(2)
        lock_authorized_staff = work_queue_services.lock_authorized_staff

        def rendezvous_before_actor_lock(*args, **kwargs):
            actor_lock_barrier.wait(timeout=10)
            return lock_authorized_staff(*args, **kwargs)

        with patch.object(
            work_queue_services,
            "lock_authorized_staff",
            side_effect=rendezvous_before_actor_lock,
        ):
            results = self.run_concurrently(
                [
                    lambda: disposition(first_item.pk),
                    lambda: disposition(second_item.pk),
                ]
            )

        self.assertEqual(sorted(results), ["conflict", "success"])
        self.assertEqual(
            WorkItemEvent.objects.filter(
                actor=self.officer,
                idempotency_key="race:cross-item-key",
            ).count(),
            1,
        )
        self.assertEqual(
            sorted(
                WorkItem.objects.filter(
                    pk__in=[first_item.pk, second_item.pk]
                ).values_list("version", flat=True)
            ),
            [1, 2],
        )
        self.assertEqual(
            AuditLog.objects.filter(action="work_queue.item_dispositioned").count(),
            1,
        )

    def test_reconciliation_batch_skips_locked_source_evidence(self):
        correspondence = Correspondence.objects.create(
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.UNMATCHED,
            received_subject="Locked reconciliation source",
            received_body="Retained inbound evidence.",
            received_at=timezone.now(),
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="locked-reconciliation-correspondence",
        )
        receipt = InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="locked-reconciliation-source",
            received_at=timezone.now(),
            envelope_sender="requester@example.org",
            envelope_recipients=["records@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
            matched_correspondence=correspondence,
            quarantined=False,
            raw_message_size=256,
            raw_message_sha256="a" * 64,
            submitted_by=self.officer,
            idempotency_key="locked-reconciliation-source",
            payload_sha256="b" * 64,
            completed_at=timezone.now(),
        )
        source_locked = threading.Event()
        release_source = threading.Event()

        def hold_source_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    InboundCorrespondenceReceipt.objects.select_for_update().get(
                        pk=receipt.pk
                    )
                    source_locked.set()
                    release_source.wait(timeout=10)
            finally:
                close_old_connections()

        def run_reconciliation():
            close_old_connections()
            try:
                return reconcile_inbound_work_items(limit=10)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            locker = executor.submit(hold_source_lock)
            self.assertTrue(source_locked.wait(timeout=10))
            reconciliation = executor.submit(run_reconciliation)
            try:
                result = reconciliation.result(timeout=2)
            finally:
                release_source.set()
                locker.result(timeout=10)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.skipped, 1)
        self.assertFalse(WorkItem.objects.filter(source_id=str(receipt.pk)).exists())
        retry = reconcile_inbound_work_items(limit=10)
        self.assertEqual(retry.created, 1)

    def test_reassignment_closes_stale_liaison_read_race_under_item_lock(self):
        first_liaison = get_user_model().objects.create_user(
            "race-former-liaison",
            "race-former-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        second_liaison = get_user_model().objects.create_user(
            "race-current-liaison",
            "race-current-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(first_liaison, {StaffRole.LIAISON})
        apply_staff_roles(second_liaison, {StaffRole.LIAISON})
        item = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id="race-liaison-collection",
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        assign_work_item(
            item,
            assignee=first_liaison,
            actor=self.officer,
            expected_version=1,
            idempotency_key="race:liaison:initial",
        )
        stale_fetched = threading.Event()
        reassigned = threading.Event()

        def stale_liaison_read():
            close_old_connections()
            try:
                stale_item = WorkItem.objects.get(pk=item.pk)
                stale_actor = get_user_model().objects.get(pk=first_liaison.pk)
                stale_fetched.set()
                if not reassigned.wait(timeout=10):
                    return "timeout"
                try:
                    mark_work_item_read(
                        stale_item,
                        actor=stale_actor,
                        idempotency_key="race:liaison:stale-read",
                    )
                except PermissionDenied:
                    return "denied"
                return "mutated"
            finally:
                close_old_connections()

        def officer_reassignment():
            close_old_connections()
            try:
                if not stale_fetched.wait(timeout=10):
                    return "timeout"
                current_item = WorkItem.objects.get(pk=item.pk)
                current_officer = get_user_model().objects.get(pk=self.officer.pk)
                current_liaison = get_user_model().objects.get(
                    pk=second_liaison.pk
                )
                assign_work_item(
                    current_item,
                    assignee=current_liaison,
                    actor=current_officer,
                    expected_version=2,
                    idempotency_key="race:liaison:reassigned",
                )
                return "reassigned"
            finally:
                reassigned.set()
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            liaison_future = executor.submit(stale_liaison_read)
            officer_future = executor.submit(officer_reassignment)
            results = {
                liaison_future.result(timeout=20),
                officer_future.result(timeout=20),
            }

        self.assertSetEqual(results, {"denied", "reassigned"})
        item.refresh_from_db()
        self.assertEqual(item.assignee, second_liaison)
        self.assertFalse(
            item.receipts.filter(user=first_liaison).exists()
        )
