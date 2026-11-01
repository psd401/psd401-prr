# ABOUTME: Read primitives that back recognizable staff workflow selectors.
# ABOUTME: Selector payloads stay narrowly scoped and request conversations avoid N+1 reads.
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api.capabilities import CAPABILITIES
from apps.api.identity import StaffRole, apply_staff_roles
from apps.correspondence import services as correspondence_services
from apps.records.models import PublicRecordsRequest, Requester


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class WorkflowChoiceApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
            first_name="Records",
            last_name="Officer",
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.liaison = User.objects.create_user(
            "case.worker",
            "case.worker@psd401.net",
            "pw",
            is_staff=True,
            first_name="Case",
            last_name="Worker",
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        User.objects.create_user("not-staff", "outside@example.org", "pw")
        self.client.force_login(self.officer)

    def test_staff_choices_return_only_minimum_recognizable_fields(self):
        response = self.client.get("/api/staff/staff-users/")

        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        liaison = next(row for row in rows if row["username"] == "case.worker")
        self.assertEqual(
            liaison,
            {
                "id": self.liaison.id,
                "username": "case.worker",
                "display_name": "Case Worker",
            },
        )
        self.assertNotIn("not-staff", {row["username"] for row in rows})
        self.assertNotIn("email", str(rows).lower())

    def test_staff_choices_require_records_officer_role(self):
        self.client.force_login(self.liaison)

        response = self.client.get("/api/staff/staff-users/")

        self.assertEqual(response.status_code, 403)

    def test_staff_choices_are_registered_as_an_agent_capability(self):
        capability = next(
            row for row in CAPABILITIES if row["id"] == "staff_users.list"
        )

        self.assertEqual(capability["method"], "GET")
        self.assertEqual(capability["path"], "/api/staff/staff-users/")
        self.assertEqual(
            capability["ui_routes"],
            [
                "/requests/:number/overview",
                "/requests/:number/messages",
                "/requests/:number/collection",
            ],
        )

    @override_settings(
        PRR_AUTH_MODE="local",
        PRR_LOCAL_IMPLICIT_STAFF_ROLES=(StaffRole.RECORDS_OFFICER,),
    )
    def test_role_filter_uses_effective_local_roles(self):
        local_officer = get_user_model().objects.create_user(
            "local.officer",
            "local.officer@psd401.net",
            "pw",
            is_staff=True,
        )

        response = self.client.get(
            "/api/staff/staff-users/?role=records_officer"
        )

        self.assertEqual(response.status_code, 200, response.content)
        usernames = {row["username"] for row in response.json()["results"]}
        self.assertIn(local_officer.username, usernames)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class RequestConversationListTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        requester = Requester.objects.create(
            name="Requesting Person",
            email="requester@example.org",
        )
        self.request_record = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Requested records",
        )
        self.message = correspondence_services.create_authored_draft(
            self.request_record,
            kind="general",
            subject="Request-scoped update",
            body="The collection is continuing.",
            actor=self.officer,
        )
        self.client.force_login(self.officer)

    def test_request_filter_returns_summary_and_retrieve_returns_complete_row(self):
        response = self.client.get(
            "/api/staff/correspondence/",
            {"request": self.request_record.number},
        )

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["id"], str(self.message.id))
        self.assertEqual(row["subject"], "Request-scoped update")
        self.assertEqual(row["snippet"], "The collection is continuing.\n")
        self.assertNotIn("draft_body", row)
        self.assertNotIn("recipients", row)

        detail = self.client.get(
            f"/api/staff/correspondence/{self.message.pk}/"
        ).json()
        self.assertEqual(detail["draft_body"], "The collection is continuing.\n")
        self.assertEqual(detail["recipients"][0]["email"], "requester@example.org")

    def test_global_list_remains_pii_minimized(self):
        response = self.client.get("/api/staff/correspondence/")

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertNotIn("draft_subject", row)
        self.assertNotIn("recipients", row)
        self.assertNotIn("requester@example.org", str(row))
