"""A closed migrated case must be findable, and must never read as settled.

Only open NextRequest cases were imported, so a terminal status on a migrated
case is an anomaly. Worse, the safety-net worker and the awaiting-statutory
queue both skip terminal requests, so nothing automated will ever surface a
migrated case that was closed with no first response on record.
"""

import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.api.resources.requests import compute_initial_response_plan
from apps.records.models import (
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)


class MigratedCaseStatusAuditTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="ro", email="ro@psd401.net"
        )
        self.batch = LegacyImportBatch.objects.create(
            imported_by=self.user,
            source_sha256="0" * 64,
            mapping_sha256="1" * 64,
            plan_sha256="2" * 64,
            row_count=4,
            mapping_snapshot={},
        )

    def _case(self, number, *, status, responded_at=None):
        requester = Requester.objects.create(
            name=f"R{number}", email=f"r{number}@example.org"
        )
        request = PublicRecordsRequest.objects.create(
            requester=requester,
            description=f"records for {number}",
            received_at=timezone.now(),
            status=status,
        )
        LegacyRequestImport.objects.create(
            batch=self.batch,
            request=request,
            source_id=number,
            source_number=number,
            source_status="Open",
            source_row_sha256="3" * 64,
            legacy_initial_response_at=responded_at,
        )
        return request

    def _audit(self):
        out = io.StringIO()
        call_command("audit_migrated_case_status", "--json", stdout=out)
        return json.loads(out.getvalue())

    def test_an_open_migrated_case_is_not_reported(self):
        self._case("26-01", status=RequestStatus.GATHERING)
        payload = self._audit()
        self.assertEqual(payload["migrated_cases"], 1)
        self.assertEqual(payload["terminal_status_cases"], [])
        self.assertEqual(payload["terminal_without_first_response"], [])

    def test_a_terminal_migrated_case_is_reported_as_an_anomaly(self):
        self._case("26-02", status=RequestStatus.CLOSED_ABANDONED)
        payload = self._audit()
        numbers = [row["source_number"] for row in payload["terminal_status_cases"]]
        self.assertEqual(numbers, ["26-02"])

    def test_a_terminal_case_with_no_first_response_is_reported_separately(self):
        self._case("26-03", status=RequestStatus.FULFILLED)
        payload = self._audit()
        numbers = [
            row["source_number"]
            for row in payload["terminal_without_first_response"]
        ]
        self.assertEqual(numbers, ["26-03"])

    def test_a_terminal_case_with_a_historic_response_is_not_an_exposure(self):
        self._case(
            "26-04",
            status=RequestStatus.FULFILLED,
            responded_at=timezone.now(),
        )
        payload = self._audit()
        self.assertEqual(payload["terminal_without_first_response"], [])
        # Still an anomaly on status alone, because only open cases migrated.
        self.assertEqual(len(payload["terminal_status_cases"]), 1)

    def test_closed_non_request_never_owed_a_response(self):
        self._case("26-05", status=RequestStatus.CLOSED_NON_REQUEST)
        payload = self._audit()
        self.assertEqual(payload["terminal_without_first_response"], [])


class TerminalStatutoryStateTests(TestCase):
    """The badge on a closed case must not be calmer than the badge on an
    answered one. Every terminal state that reaches the plan is by definition
    one where no first response was confirmed."""

    def _request(self, status):
        requester = Requester.objects.create(email=f"{status}@example.org")
        return PublicRecordsRequest.objects.create(
            requester=requester,
            description="anything",
            received_at=timezone.now(),
            status=status,
        )

    def test_closed_without_a_response_reports_terminal_unverified(self):
        for status in (
            RequestStatus.FULFILLED,
            RequestStatus.CLOSED_WITHDRAWN,
            RequestStatus.CLOSED_ABANDONED,
            RequestStatus.CLOSED_NO_RECORDS,
            RequestStatus.DENIED,
        ):
            with self.subTest(status=status):
                plan = compute_initial_response_plan(self._request(status))
                self.assertEqual(plan["state"], "terminal_unverified")

    def test_closed_as_not_a_records_request_stays_excluded(self):
        plan = compute_initial_response_plan(
            self._request(RequestStatus.CLOSED_NON_REQUEST)
        )
        self.assertEqual(plan["state"], "terminal_excluded")

    def test_a_delivered_response_still_wins_over_the_terminal_branch(self):
        prr = self._request(RequestStatus.FULFILLED)
        plan = compute_initial_response_plan(
            prr, initial_response_delivered=True
        )
        self.assertEqual(plan["state"], "delivered")

    def test_terminal_unverified_shows_no_step_as_done(self):
        from apps.api.resources.requests import initial_response_guidance

        guidance = initial_response_guidance("terminal_unverified")
        self.assertTrue(
            all(step["state"] == "pending" for step in guidance["steps"]),
            "A case closed with no response never completed a step; showing "
            "the ladder as finished is the misreading this state prevents.",
        )
        self.assertIn("no five-day response", guidance["next_action"])
