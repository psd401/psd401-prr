# ABOUTME: The officer records a first response sent outside this system through the staff API.
# ABOUTME: Officer-only, refused for agents, and reflected on the case detail.
import datetime as dt

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.api.tests.test_requests import StaffApiTestCase, make_request
from apps.records.models import InitialResponseAttestation

EVIDENCE = "Acknowledged in NextRequest before the cutover; copy in the officer mailbox."


class ExternalInitialResponseApiTests(StaffApiTestCase):
    def setUp(self):
        super().setUp()
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.prr = make_request(
            description="Board minutes",
            received_at=timezone.now() - dt.timedelta(days=10),
        )

    def _post(self, body):
        return self.client.post(
            f"/api/staff/requests/{self.prr.number}/external-initial-response/",
            body,
            content_type="application/json",
        )

    def test_officer_statement_is_recorded_and_shown_on_the_case(self):
        response = self._post(
            {"sent_via": "nextrequest", "responded_on": None, "evidence": EVIDENCE}
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertTrue(payload["initial_response_delivered"])
        self.assertEqual(payload["initial_response_plan"]["state"], "delivered")
        recorded = payload["external_initial_response"]
        self.assertEqual(recorded["sent_via"], "nextrequest")
        self.assertEqual(recorded["sent_via_label"], "NextRequest (retired system)")
        self.assertIsNone(recorded["responded_on"])
        self.assertEqual(recorded["evidence"], EVIDENCE)
        self.assertEqual(recorded["attested_by"], "officer")
        listed = self.client.get("/api/staff/requests/", {"queue": "inbox"}).json()
        row = next(r for r in listed["results"] if r["number"] == self.prr.number)
        self.assertTrue(row["initial_response_delivered"])

    def test_a_dated_statement_keeps_its_date(self):
        # Three days after receipt, so the statement stays valid on any calendar
        # day; a fixed date predates the ten-days-ago receipt once time passes.
        responded_on = (
            timezone.localtime(self.prr.received_at).date() + dt.timedelta(days=3)
        ).isoformat()
        response = self._post(
            {"sent_via": "mail", "responded_on": responded_on, "evidence": EVIDENCE}
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["external_initial_response"]["responded_on"],
            responded_on,
        )

    def test_short_statement_is_refused(self):
        response = self._post({"sent_via": "nextrequest", "evidence": "done"})

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(InitialResponseAttestation.objects.exists())

    def test_non_officers_are_refused(self):
        liaison = get_user_model().objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})
        self.client.logout()
        self.client.login(username="liaison", password="pw")

        response = self._post({"sent_via": "nextrequest", "evidence": EVIDENCE})

        self.assertIn(response.status_code, (403, 404), response.content)
        self.assertFalse(InitialResponseAttestation.objects.exists())
