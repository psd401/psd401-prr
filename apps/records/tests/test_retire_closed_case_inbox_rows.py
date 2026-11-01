# ABOUTME: The one-time repair retires waiting Inbox rows on cases closed before closure did.
import io

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import PublicRecordsRequest, Requester, RequestStatus
from apps.work_queue.models import WorkItem, WorkItemEvent

from .inbox_fixtures import matched_email


class RetireClosedCaseInboxRowsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.clerk = User.objects.create_user(
            "clerk", "clerk@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.clerk, {StaffRole.INTAKE_CLERK})
        requester = Requester.objects.create(
            email="jane@example.org", name="Jane Q. Public"
        )
        self.closed = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Closed before the fix",
            status=RequestStatus.FULFILLED,
            closed_at=timezone.now(),
        )
        _, _, self.closed_row = matched_email(
            self.closed, officer=self.officer, intake=True
        )
        self.open = PublicRecordsRequest.objects.create(
            requester=requester, description="Still open"
        )
        _, _, self.open_row = matched_email(self.open, officer=self.officer)

    def _run(self, *args, actor="officer"):
        out = io.StringIO()
        call_command(
            "retire_closed_case_inbox_rows", "--actor", actor, *args, stdout=out
        )
        return out.getvalue()

    def test_dry_run_lists_and_writes_nothing(self):
        output = self._run()

        self.assertIn(self.closed.number, output)
        self.assertNotIn(self.open.number, output)
        self.assertIn("Dry run", output)
        self.closed_row.refresh_from_db()
        self.assertEqual(
            self.closed_row.disposition, WorkItem.Disposition.NEEDS_ATTENTION
        )

    def test_commit_retires_only_closed_cases_rows(self):
        output = self._run("--commit")

        self.assertIn("Retired 1 row(s) on 1 case(s).", output)
        self.closed_row.refresh_from_db()
        self.assertEqual(self.closed_row.disposition, WorkItem.Disposition.HANDLED)
        self.assertEqual(self.closed_row.disposed_by, self.officer)
        event = WorkItemEvent.objects.get(
            work_item=self.closed_row,
            event_type=WorkItemEvent.EventType.DISPOSITIONED,
        )
        self.assertEqual(event.metadata["reason"], "request_closed_backfill:fulfilled")
        self.open_row.refresh_from_db()
        self.assertEqual(
            self.open_row.disposition, WorkItem.Disposition.NEEDS_ATTENTION
        )

    def test_refuses_a_non_officer(self):
        with self.assertRaises(CommandError):
            self._run("--commit", actor="clerk")
        self.closed_row.refresh_from_db()
        self.assertEqual(
            self.closed_row.disposition, WorkItem.Disposition.NEEDS_ATTENTION
        )
