"""Reopening a closed request is normal practice, and it goes on the record.

A requester follows up, a closure turns out to have been premature, or records
surface after the fact. What reopening must never be is quiet: it reverses a
decision that was made in writing, so it carries a named Records Officer and a
substantive reason onto the append-only timeline, and it never invents a fresh
statutory deadline.
"""

import ast
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records import services
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


class ReopenRequestTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.liaison = get_user_model().objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.liaison, [StaffRole.LIAISON])

    def _closed_request(self, status=RequestStatus.CLOSED_WITHDRAWN):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="jane@example.org"),
            description="All emails about the stadium project",
            status=status,
            closed_at=timezone.now(),
        )
        StatusEvent.objects.create(
            request=prr,
            actor=self.officer,
            from_status=RequestStatus.GATHERING,
            to_status=status,
            note="Requester withdrew by phone.",
            occurred_at=timezone.now(),
        )
        return prr

    def test_an_officer_can_reopen_with_a_reason(self):
        prr = self._closed_request()
        reopened = services.reopen_request(
            prr,
            RequestStatus.GATHERING,
            actor=self.officer,
            reason="Requester called back and asked us to continue the search.",
        )
        self.assertEqual(reopened.status, RequestStatus.GATHERING)
        self.assertIsNone(reopened.closed_at)

    def test_the_closure_stays_on_the_timeline(self):
        prr = self._closed_request()
        services.reopen_request(
            prr,
            RequestStatus.GATHERING,
            actor=self.officer,
            reason="Requester called back and asked us to continue the search.",
        )
        events = list(
            StatusEvent.objects.filter(request=prr).order_by("occurred_at", "pk")
        )
        # Two events, not one rewritten: the history has to show this case was
        # closed and then reopened, not that it was never closed.
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].to_status, RequestStatus.CLOSED_WITHDRAWN)
        self.assertEqual(events[1].from_status, RequestStatus.CLOSED_WITHDRAWN)
        self.assertEqual(events[1].to_status, RequestStatus.GATHERING)

    def test_the_reason_and_actor_are_recorded(self):
        prr = self._closed_request()
        reason = "Requester called back and asked us to continue the search."
        services.reopen_request(
            prr, RequestStatus.GATHERING, actor=self.officer, reason=reason
        )
        event = StatusEvent.objects.filter(
            request=prr, from_status=RequestStatus.CLOSED_WITHDRAWN
        ).get()
        self.assertEqual(event.note, reason)
        self.assertEqual(event.actor, self.officer)
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.reopened", target=prr.number
            ).exists()
        )

    def test_reopening_does_not_restart_the_statutory_clock(self):
        prr = self._closed_request()
        original_due = prr.statutory_response_due
        original_received = prr.received_at
        reopened = services.reopen_request(
            prr,
            RequestStatus.GATHERING,
            actor=self.officer,
            reason="Requester called back and asked us to continue the search.",
        )
        # A request that closed overdue reopens overdue. Granting a fresh
        # five-day window here would erase a real statutory exposure.
        self.assertEqual(reopened.statutory_response_due, original_due)
        self.assertEqual(reopened.received_at, original_received)

    def test_a_reason_must_be_substantive(self):
        prr = self._closed_request()
        with self.assertRaises(services.TerminalActionRequired):
            services.reopen_request(
                prr, RequestStatus.GATHERING, actor=self.officer, reason="oops"
            )
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLOSED_WITHDRAWN)

    def test_only_a_records_officer_may_reopen(self):
        prr = self._closed_request()
        with self.assertRaises(PermissionDenied):
            services.reopen_request(
                prr,
                RequestStatus.GATHERING,
                actor=self.liaison,
                reason="Requester called back and asked us to continue.",
            )
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLOSED_WITHDRAWN)

    def test_reopening_cannot_fabricate_a_delivered_state(self):
        prr = self._closed_request()
        for target in (
            RequestStatus.ACKNOWLEDGED,
            RequestStatus.CLARIFICATION,
            RequestStatus.DENIED,
        ):
            with self.subTest(target=target):
                with self.assertRaises(ValidationError):
                    services.reopen_request(
                        prr,
                        target,
                        actor=self.officer,
                        reason="Requester called back and asked us to continue.",
                    )

    def test_an_open_request_is_not_reopenable(self):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="open@example.org"),
            description="anything",
            status=RequestStatus.GATHERING,
        )
        with self.assertRaises(services.ClosedRequestTransition):
            services.reopen_request(
                prr,
                RequestStatus.REVIEW,
                actor=self.officer,
                reason="This should be an ordinary transition instead.",
            )

    def test_ordinary_transition_still_refuses_to_leave_a_closed_status(self):
        """The wall stays up; reopen_request is the only door through it."""
        prr = self._closed_request()
        with self.assertRaises(services.ClosedRequestTransition):
            services.transition(
                prr,
                RequestStatus.GATHERING,
                actor=self.officer,
                note="Trying to sneak past the reopen path.",
            )

    def test_no_background_task_may_reopen_a_request(self):
        """Reopening is a human decision; no worker may make it."""
        task_paths = sorted((Path(settings.BASE_DIR) / "apps").glob("**/tasks.py"))
        self.assertTrue(task_paths)
        for task_path in task_paths:
            tree = ast.parse(task_path.read_text())
            calls = [
                (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            ]
            with self.subTest(task_path=task_path):
                self.assertNotIn("reopen_request", calls)
