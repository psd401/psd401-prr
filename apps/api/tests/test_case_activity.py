"""The running log has to be honest about three things.

It is now the only case history the staff workspace renders -- Overview shows
a bounded slice of it and the Activity tab shows the whole thing -- so a wrong
label or an invented actor here is wrong everywhere at once.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.legacy_timeline import GAP_MARKER
from apps.records.models import (
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


class CaseActivityTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        self.reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="jane@example.org"),
            description="All emails about the stadium project",
        )

    def _activity(self, user="officer"):
        self.client.login(username=user, password="pw")
        response = self.client.get(
            f"/api/staff/requests/{self.request.number}/activity/"
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_a_note_only_event_is_not_reported_as_a_transition(self):
        StatusEvent.objects.create(
            request=self.request,
            from_status="",
            to_status="",
            note="Footage preservation instructions sent.",
            occurred_at=timezone.now(),
        )
        entry = self._activity()[0]
        # "New request → " would assert a change of legal state that never
        # happened, on an append-only record.
        self.assertEqual(entry["label"], "Operational note")

    def test_a_real_transition_still_reads_as_one(self):
        StatusEvent.objects.create(
            request=self.request,
            from_status="received",
            to_status="gathering",
            note="",
            occurred_at=timezone.now(),
        )
        self.assertEqual(
            self._activity()[0]["label"], "Received → Gathering Records"
        )

    def test_an_officer_sees_that_no_person_acted(self):
        StatusEvent.objects.create(
            request=self.request,
            from_status="received",
            to_status="gathering",
            note="",
            actor=None,
            occurred_at=timezone.now(),
        )
        self.assertEqual(self._activity()[0]["actor_username"], "system")

    def test_a_reviewer_is_given_no_actor_rather_than_a_fabricated_one(self):
        StatusEvent.objects.create(
            request=self.request,
            from_status="received",
            to_status="gathering",
            note="internal reasoning",
            actor=self.officer,
            occurred_at=timezone.now(),
        )
        entry = self._activity(user="reviewer")[0]
        # Withheld is not the same fact as "the system did it". Sending null
        # for both let the UI print "By system" to a role that was simply not
        # entitled to see who acted.
        self.assertIsNone(entry["actor_username"])
        self.assertIsNone(entry["detail"])

    def test_a_migration_gap_note_is_flagged(self):
        StatusEvent.objects.create(
            request=self.request,
            from_status="",
            to_status="",
            note=(
                f"NextRequest {GAP_MARKER} 26-45. Confirm whether a response "
                "was sent."
            ),
            occurred_at=timezone.now(),
        )
        self.assertTrue(self._activity()[0]["flagged"])

    def test_a_routine_note_is_not_flagged(self):
        StatusEvent.objects.create(
            request=self.request,
            from_status="",
            to_status="",
            note="Called the requester to confirm the date range.",
            occurred_at=timezone.now(),
        )
        self.assertFalse(self._activity()[0]["flagged"])

    def test_a_flagged_note_survives_the_hundred_entry_cap(self):
        now = timezone.now()
        # The importer backdates compliance notes to the request's receipt, so
        # they sort last. Everything routine is newer than the gap note here.
        StatusEvent.objects.create(
            request=self.request,
            from_status="",
            to_status="",
            note=f"NextRequest {GAP_MARKER} 26-45.",
            occurred_at=now - timezone.timedelta(days=900),
        )
        for index in range(120):
            StatusEvent.objects.create(
                request=self.request,
                from_status="",
                to_status="",
                note=f"routine {index}",
                occurred_at=now - timezone.timedelta(minutes=index),
            )
        results = self._activity()
        self.assertEqual(len(results), 100)
        self.assertTrue(
            any(entry["flagged"] for entry in results),
            "A recency-only cut buries the one note that changes what staff "
            "must do about a migrated case.",
        )


class ReopenEndpointTests(TestCase):
    """The reopen endpoint is the GUI's door to the same guarded service."""

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer2", "officer2@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="reopen@example.org"),
            description="All emails about the stadium project",
            status=RequestStatus.CLOSED_ABANDONED,
            closed_at=timezone.now(),
        )
        self.client.login(username="officer2", password="pw")

    def _post(self, **body):
        return self.client.post(
            f"/api/staff/requests/{self.request.number}/reopen/",
            body,
            content_type="application/json",
        )

    def test_reopen_returns_the_updated_case(self):
        response = self._post(
            to_status="gathering",
            reason="Requester followed up and asked us to continue.",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "gathering")
        self.request.refresh_from_db()
        self.assertIsNone(self.request.closed_at)

    def test_a_short_reason_is_refused(self):
        response = self._post(to_status="gathering", reason="no")
        self.assertEqual(response.status_code, 400)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, RequestStatus.CLOSED_ABANDONED)

    def test_a_delivery_backed_target_is_refused(self):
        response = self._post(
            to_status="acknowledged",
            reason="Trying to claim a message that was never sent.",
        )
        self.assertEqual(response.status_code, 400)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, RequestStatus.CLOSED_ABANDONED)

    def test_an_open_case_cannot_be_reopened(self):
        self.request.status = RequestStatus.GATHERING
        self.request.closed_at = None
        self.request.save()
        response = self._post(
            to_status="review",
            reason="This should be an ordinary transition instead.",
        )
        self.assertEqual(response.status_code, 409)


class CaseSearchTests(TestCase):
    """Staff must be able to find a case by what they actually remember."""

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer3", "officer3@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.reviewer = get_user_model().objects.create_user(
            "reviewer3", "reviewer3@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])

        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email="jane@example.org", name="Jane Q. Public"
            ),
            description="All emails about the Harborview project",
        )
        batch = LegacyImportBatch.objects.create(
            imported_by=self.officer,
            source_sha256="0" * 64,
            mapping_sha256="1" * 64,
            plan_sha256="2" * 64,
            row_count=1,
            mapping_snapshot={},
        )
        LegacyRequestImport.objects.create(
            batch=batch,
            request=self.request,
            source_id="4242",
            source_number="25-42",
            source_status="Open",
            source_row_sha256="3" * 64,
        )

    def _search(self, term, user="officer3"):
        self.client.login(username=user, password="pw")
        response = self.client.get(f"/api/staff/requests/?search={term}")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        return [row["number"] for row in rows]

    def test_finds_a_migrated_case_by_its_nextrequest_number(self):
        # Staff know this case as 25-42; this system called it something else
        # entirely. Searching the number a person remembers has to work.
        self.assertIn(self.request.number, self._search("25-42"))

    def test_finds_a_case_by_requester_name(self):
        self.assertIn(self.request.number, self._search("Jane"))

    def test_still_finds_a_case_by_its_own_number_and_description(self):
        self.assertIn(self.request.number, self._search(self.request.number))
        self.assertIn(self.request.number, self._search("Harborview"))

    def test_a_reviewer_may_search_the_source_number_but_not_the_requester(self):
        # The source number is a district-issued identifier, not requester PII.
        self.assertIn(self.request.number, self._search("25-42", user="reviewer3"))
        # Requester identity stays officer-only, matching the list serializer.
        self.assertEqual(self._search("Jane", user="reviewer3"), [])

    def test_search_returns_each_case_once(self):
        self.assertEqual(len(self._search("Harborview")), 1)
