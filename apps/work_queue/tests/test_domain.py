from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence.models import ClarificationCycle, Correspondence
from apps.records.models import PublicRecordsRequest, Requester, StatusEvent
from apps.work_queue.models import WorkItem, WorkItemEvent, WorkItemReceipt
from apps.work_queue.services import (
    IdempotencyConflict,
    VersionConflict,
    assign_work_item,
    mark_work_item_read,
    mark_work_item_unread,
    materialize_work_item,
    reopen_work_item,
    set_no_action_required,
    set_work_item_disposition,
    unassign_work_item,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class WorkQueueDomainTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "work-queue-officer",
            "work-queue-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Immutable legal request description.",
            assigned_officer=self.officer,
        )

    def materialize(self):
        return materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=self.request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=self.request,
        )

    def test_materialization_is_idempotent_with_one_creation_event_and_audit(self):
        first = self.materialize()
        second = self.materialize()

        self.assertEqual(first.work_item, second.work_item)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(WorkItem.objects.count(), 1)
        self.assertEqual(
            WorkItemEvent.objects.filter(
                work_item=first.work_item,
                event_type=WorkItemEvent.EventType.CREATED,
            ).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="work_queue.item_created",
                target=str(first.work_item.pk),
            ).count(),
            1,
        )

    def test_source_kind_uniqueness_is_database_backed(self):
        item = self.materialize().work_item

        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkItem.objects.create(
                source_type=item.source_type,
                source_id=item.source_id,
                work_kind=item.work_kind,
                request=self.request,
            )

    def test_event_ledger_rejects_rewrite_and_delete(self):
        event = self.materialize().event
        event.metadata = {"rewritten": True}

        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            WorkItemEvent.objects.filter(pk=event.pk).update(
                metadata={"rewritten": True}
            )

    def test_per_user_receipts_are_distinct_from_team_state(self):
        colleague = get_user_model().objects.create_user(
            "work-queue-colleague",
            "work-queue-colleague@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(colleague, {StaffRole.RECORDS_OFFICER})
        item = self.materialize().work_item

        first = WorkItemReceipt.objects.create(work_item=item, user=self.officer)
        second = WorkItemReceipt.objects.create(work_item=item, user=colleague)

        self.assertNotEqual(first.pk, second.pk)
        self.assertIsNone(first.read_at)
        self.assertIsNone(second.read_at)
        self.assertEqual(item.disposition, WorkItem.Disposition.NEEDS_ATTENTION)

    def test_read_state_is_per_user_idempotent_and_cannot_change_legal_or_team_state(self):
        colleague = get_user_model().objects.create_user(
            "receipt-colleague",
            "receipt-colleague@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(colleague, {StaffRole.RECORDS_OFFICER})
        item = self.materialize().work_item
        clarification = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.DRAFT,
            draft_subject="Clarification needed",
            draft_body="Please clarify the retained request scope.",
            created_by=self.officer,
        )
        clarification_cycle = ClarificationCycle.objects.create(
            request=self.request,
            outbound_correspondence=clarification,
            opened_at=timezone.now(),
        )
        legal_before = (
            self.request.status,
            self.request.statutory_response_due,
            self.request.tolled_business_days,
            StatusEvent.objects.filter(request=self.request).count(),
            clarification.status,
            clarification.draft_subject,
            clarification.draft_body,
            clarification_cycle.inbound_response_correspondence_id,
            clarification_cycle.responded_at,
            clarification_cycle.resolved_at,
            clarification_cycle.resolution_notes,
        )

        first = mark_work_item_read(
            item,
            actor=self.officer,
            idempotency_key="read:item:1",
        )
        retry = mark_work_item_read(
            item,
            actor=self.officer,
            idempotency_key="read:item:1",
        )
        colleague_result = mark_work_item_unread(
            item,
            actor=colleague,
            idempotency_key="unread:item:colleague",
        )

        self.assertFalse(first.idempotent)
        self.assertTrue(retry.idempotent)
        self.assertEqual(first.event, retry.event)
        self.assertIsNotNone(first.receipt.read_at)
        self.assertIsNone(colleague_result.receipt.read_at)
        item.refresh_from_db()
        self.request.refresh_from_db()
        clarification.refresh_from_db()
        clarification_cycle.refresh_from_db()
        self.assertEqual(item.version, 1)
        self.assertEqual(item.disposition, WorkItem.Disposition.NEEDS_ATTENTION)
        self.assertIsNone(item.assignee)
        self.assertEqual(
            (
                self.request.status,
                self.request.statutory_response_due,
                self.request.tolled_business_days,
                StatusEvent.objects.filter(request=self.request).count(),
                clarification.status,
                clarification.draft_subject,
                clarification.draft_body,
                clarification_cycle.inbound_response_correspondence_id,
                clarification_cycle.responded_at,
                clarification_cycle.resolved_at,
                clarification_cycle.resolution_notes,
            ),
            legal_before,
        )
        self.assertEqual(
            WorkItemEvent.objects.filter(
                work_item=item,
                event_type=WorkItemEvent.EventType.READ,
                actor=self.officer,
            ).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="work_queue.item_read",
                actor=self.officer,
            ).count(),
            1,
        )

    def test_assignment_reassignment_and_unassignment_require_active_authorized_users(self):
        second_officer = get_user_model().objects.create_user(
            "second-work-officer",
            "second-work-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(second_officer, {StaffRole.RECORDS_OFFICER})
        liaison = get_user_model().objects.create_user(
            "unauthorized-work-liaison",
            "unauthorized-work-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})
        inactive = get_user_model().objects.create_user(
            "inactive-work-officer",
            "inactive-work-officer@psd401.net",
            "pw",
            is_staff=True,
            is_active=False,
        )
        apply_staff_roles(inactive, {StaffRole.RECORDS_OFFICER})
        item = self.materialize().work_item

        assigned = assign_work_item(
            item,
            assignee=self.officer,
            actor=self.officer,
            expected_version=1,
            idempotency_key="assign:item:1",
        )
        reassigned = assign_work_item(
            assigned.work_item,
            assignee=second_officer,
            actor=self.officer,
            expected_version=2,
            idempotency_key="assign:item:2",
        )
        unassigned = unassign_work_item(
            reassigned.work_item,
            actor=self.officer,
            expected_version=3,
            idempotency_key="unassign:item:3",
        )

        self.assertEqual(assigned.event.event_type, WorkItemEvent.EventType.ASSIGNED)
        self.assertEqual(
            reassigned.event.event_type,
            WorkItemEvent.EventType.REASSIGNED,
        )
        self.assertEqual(
            unassigned.event.event_type,
            WorkItemEvent.EventType.UNASSIGNED,
        )
        self.assertIsNone(unassigned.work_item.assignee)
        with self.assertRaises(PermissionDenied):
            assign_work_item(
                unassigned.work_item,
                assignee=liaison,
                actor=self.officer,
                expected_version=4,
                idempotency_key="assign:liaison:4",
            )
        with self.assertRaises(PermissionDenied):
            assign_work_item(
                unassigned.work_item,
                assignee=inactive,
                actor=self.officer,
                expected_version=4,
                idempotency_key="assign:inactive:4",
            )

    def test_assignee_cannot_be_deleted_without_an_attributed_unassignment(self):
        liaison = get_user_model().objects.create_user(
            "protected-work-liaison",
            "protected-work-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})
        item = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id="protected-assignee-collection",
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        assign_work_item(
            item,
            assignee=liaison,
            actor=self.officer,
            expected_version=1,
            idempotency_key="assign:protected-liaison:1",
        )

        with self.assertRaises(ProtectedError):
            liaison.delete()

        item.refresh_from_db()
        self.assertEqual(item.assignee, liaison)
        self.assertEqual(item.version, 2)

    def test_former_liaison_cannot_mutate_a_stale_assigned_projection(self):
        first_liaison = get_user_model().objects.create_user(
            "former-work-liaison",
            "former-work-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        second_liaison = get_user_model().objects.create_user(
            "current-work-liaison",
            "current-work-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(first_liaison, {StaffRole.LIAISON})
        apply_staff_roles(second_liaison, {StaffRole.LIAISON})
        item = materialize_work_item(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id="stale-liaison-assignment",
            work_kind=WorkItem.WorkKind.COLLECTION,
            request=self.request,
        ).work_item
        assigned = assign_work_item(
            item,
            assignee=first_liaison,
            actor=self.officer,
            expected_version=1,
            idempotency_key="assign:former-liaison:1",
        )
        stale_projection = WorkItem.objects.get(pk=assigned.work_item.pk)
        assign_work_item(
            assigned.work_item,
            assignee=second_liaison,
            actor=self.officer,
            expected_version=2,
            idempotency_key="assign:current-liaison:2",
        )

        with self.assertRaises(PermissionDenied):
            mark_work_item_read(
                stale_projection,
                actor=first_liaison,
                idempotency_key="read:former-liaison:stale",
            )

        self.assertFalse(
            WorkItemReceipt.objects.filter(
                work_item=item,
                user=first_liaison,
            ).exists()
        )

    def test_handled_projection_requires_an_attributed_disposer(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            WorkItem.objects.create(
                source_type=WorkItem.SourceType.REQUEST,
                source_id="unattributed-handled",
                work_kind=WorkItem.WorkKind.REQUEST_ACTION,
                request=self.request,
                disposition=WorkItem.Disposition.HANDLED,
                disposed_at=timezone.now(),
            )

    def test_disposition_version_idempotency_and_no_action_evidence_are_separate_from_legal_state(self):
        item = self.materialize().work_item
        request_status = self.request.status
        status_event_count = StatusEvent.objects.filter(request=self.request).count()

        waiting = set_work_item_disposition(
            item,
            disposition=WorkItem.Disposition.WAITING,
            actor=self.officer,
            expected_version=1,
            idempotency_key="disposition:waiting:1",
        )
        retry = set_work_item_disposition(
            waiting.work_item,
            disposition=WorkItem.Disposition.WAITING,
            actor=self.officer,
            expected_version=1,
            idempotency_key="disposition:waiting:1",
        )
        self.assertTrue(retry.idempotent)
        self.assertEqual(retry.event, waiting.event)
        with self.assertRaises(VersionConflict):
            set_work_item_disposition(
                waiting.work_item,
                disposition=WorkItem.Disposition.HANDLED,
                actor=self.officer,
                expected_version=1,
                idempotency_key="disposition:stale:1",
            )
        with self.assertRaises(IdempotencyConflict):
            set_work_item_disposition(
                waiting.work_item,
                disposition=WorkItem.Disposition.HANDLED,
                actor=self.officer,
                expected_version=2,
                idempotency_key="disposition:waiting:1",
            )

        no_action = set_no_action_required(
            waiting.work_item,
            category=WorkItem.NoActionCategory.INFORMATIONAL_ONLY,
            reason="The retained message supplies context and asks for no response.",
            actor=self.officer,
            expected_version=2,
            idempotency_key="no-action:item:2",
        )

        self.assertEqual(
            no_action.work_item.disposition,
            WorkItem.Disposition.NO_ACTION_REQUIRED,
        )
        self.assertEqual(no_action.work_item.no_action_by, self.officer)
        self.assertIsNotNone(no_action.work_item.no_action_at)
        self.assertEqual(no_action.work_item.disposed_by, self.officer)
        self.assertIsNotNone(no_action.work_item.disposed_at)
        self.assertEqual(no_action.work_item.version, 3)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, request_status)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.request).count(),
            status_event_count,
        )

        reopened = reopen_work_item(
            no_action.work_item,
            actor=self.officer,
            expected_version=3,
            idempotency_key="reopen:item:3",
        )
        self.assertEqual(
            reopened.work_item.disposition,
            WorkItem.Disposition.NEEDS_ATTENTION,
        )
        self.assertEqual(reopened.work_item.no_action_category, "")
        self.assertIsNone(reopened.work_item.disposed_at)
        self.assertEqual(reopened.event.event_type, WorkItemEvent.EventType.REOPENED)

    def test_not_a_records_request_is_an_allowlisted_no_action_category(self):
        item = self.materialize().work_item

        set_no_action_required(
            item,
            category="not_a_records_request",
            reason="Vendor marketing email sent to the former officer address.",
            actor=self.officer,
            expected_version=1,
            idempotency_key="no-action:not-a-request",
        )

        updated = WorkItem.objects.get(pk=item.pk)
        self.assertEqual(updated.disposition, WorkItem.Disposition.NO_ACTION_REQUIRED)
        self.assertEqual(updated.no_action_category, "not_a_records_request")
        self.assertEqual(
            WorkItem.NoActionCategory("not_a_records_request").label,
            "Not a records request",
        )

    def test_no_action_requires_allowlisted_category_and_substantive_reason(self):
        item = self.materialize().work_item

        with self.assertRaises(ValidationError):
            set_no_action_required(
                item,
                category="invented_legal_outcome",
                reason="This reason is long enough but the category is not allowed.",
                actor=self.officer,
                expected_version=1,
                idempotency_key="no-action:bad-category",
            )
        with self.assertRaises(ValidationError):
            set_no_action_required(
                item,
                category=WorkItem.NoActionCategory.SUPERSEDED,
                reason="too short",
                actor=self.officer,
                expected_version=1,
                idempotency_key="no-action:short",
            )
