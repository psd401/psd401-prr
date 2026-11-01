# ABOUTME: Closing a case retires its requester-email Inbox rows, attributed to the closing officer.
# ABOUTME: The same service backs the Inbox's per-case Mark caught up.
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records import services
from apps.records.models import PublicRecordsRequest, Requester, RequestStatus
from apps.records.tests.inbox_fixtures import matched_email
from apps.work_queue.models import WorkItem, WorkItemEvent
from apps.work_queue.services import handle_case_inbox_items, materialize_work_item


class CaseInboxHandlingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.liaison = User.objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        requester = Requester.objects.create(
            email="jane@example.org", name="Jane Q. Public"
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Board minutes",
            status=RequestStatus.GATHERING,
        )
        _, _, self.intake_row = matched_email(
            self.prr, officer=self.officer, intake=True
        )
        _, _, self.reply_row = matched_email(self.prr, officer=self.officer)
        self.request_row = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=self.prr.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=self.prr,
        ).work_item

    def test_closing_a_case_retires_its_inbox_rows(self):
        services.transition(
            self.prr,
            RequestStatus.FULFILLED,
            actor=self.officer,
            note="Records produced in full on 2026-09-09.",
        )

        for row in (self.intake_row, self.reply_row):
            row.refresh_from_db()
            self.assertEqual(row.disposition, WorkItem.Disposition.HANDLED)
            self.assertEqual(row.disposed_by, self.officer)
            event = WorkItemEvent.objects.get(
                work_item=row,
                event_type=WorkItemEvent.EventType.DISPOSITIONED,
            )
            self.assertEqual(event.actor, self.officer)
            self.assertEqual(event.metadata["reason"], "request_closed:fulfilled")
        # The request-action row is a projection of the request itself and is
        # reconciled from it after commit, not handled here.
        self.request_row.refresh_from_db()
        self.assertEqual(
            self.request_row.disposition, WorkItem.Disposition.NEEDS_ATTENTION
        )
        self.assertEqual(
            AuditLog.objects.filter(action="work_queue.item_dispositioned").count(),
            2,
        )

    def test_caught_up_leaves_already_disposed_rows_alone(self):
        first = handle_case_inbox_items(
            self.prr, actor=self.officer, reason="officer_caught_up"
        )
        self.assertEqual(len(first), 2)
        self.reply_row.refresh_from_db()
        version = self.reply_row.version

        again = handle_case_inbox_items(
            self.prr, actor=self.officer, reason="officer_caught_up"
        )

        self.assertEqual(again, [])
        self.reply_row.refresh_from_db()
        self.assertEqual(self.reply_row.version, version)

    def test_caught_up_requires_a_records_officer(self):
        with self.assertRaises(PermissionDenied):
            handle_case_inbox_items(
                self.prr, actor=self.liaison, reason="officer_caught_up"
            )
        self.reply_row.refresh_from_db()
        self.assertEqual(
            self.reply_row.disposition, WorkItem.Disposition.NEEDS_ATTENTION
        )
