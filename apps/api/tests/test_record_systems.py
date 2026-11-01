# ABOUTME: Staff API tests for the RecordSystem registry resource — staff
# ABOUTME: gate, custodian edits, filters, and the delete-is-blocked guardrail.
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.api.identity import StaffRole, apply_staff_roles
from apps.audit.models import AuditLog
from apps.records.models import RecordSystem


class RecordSystemApiTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.client.login(username="officer", password="pw")
        self.workspace = RecordSystem.objects.create(
            slug="google-workspace", name="Google Workspace",
            category="Email & documents", vendor="Google",
            custodian_title="Director of Technology",
            needs_verification=True, active=True,
        )
        self.bus_video = RecordSystem.objects.create(
            slug="bus-video", name="School Bus Video",
            category="Video surveillance", vendor="Vendor TBD",
            custodian_title="Transportation Supervisor",
            needs_verification=False, active=True,
        )

    def test_staff_gate_refuses_anonymous(self):
        self.client.logout()
        r = self.client.get("/api/staff/record-systems/")
        self.assertEqual(r.status_code, 403)

    def test_staff_gate_refuses_non_staff(self):
        get_user_model().objects.create_user(
            "civilian", "civilian@example.com", "pw", is_staff=False)
        self.client.logout()
        self.client.login(username="civilian", password="pw")
        r = self.client.get("/api/staff/record-systems/")
        self.assertEqual(r.status_code, 403)

    def test_list(self):
        r = self.client.get("/api/staff/record-systems/")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertTrue(any(x["slug"] == "google-workspace" for x in rows))

    def test_patch_custodian_email(self):
        r = self.client.patch(
            f"/api/staff/record-systems/{self.workspace.id}/",
            {"custodian_email": "cto@psd401.net"},
            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.custodian_email, "cto@psd401.net")

    def test_patch_default_task_assignee(self):
        liaison = get_user_model().objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})

        response = self.client.patch(
            f"/api/staff/record-systems/{self.workspace.id}/",
            {"default_assignee_id": liaison.id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.default_assignee, liaison)
        self.assertEqual(response.json()["default_assignee_username"], "liaison")

    def test_default_task_assignee_must_be_workflow_staff(self):
        reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})

        response = self.client.patch(
            f"/api/staff/record-systems/{self.workspace.id}/",
            {"default_assignee_id": reviewer.id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("liaison", str(response.json()).lower())

    def test_slug_read_only_after_create(self):
        r = self.client.patch(
            f"/api/staff/record-systems/{self.workspace.id}/",
            {"slug": "changed-slug"},
            content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.slug, "google-workspace")

    def test_needs_verification_filter(self):
        r = self.client.get(
            "/api/staff/record-systems/?needs_verification=true")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        slugs = {x["slug"] for x in rows}
        self.assertIn("google-workspace", slugs)
        self.assertNotIn("bus-video", slugs)

    def test_active_filter(self):
        RecordSystem.objects.create(
            slug="retired-system", name="Retired System", active=False)
        r = self.client.get("/api/staff/record-systems/?active=false")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        slugs = {x["slug"] for x in rows}
        self.assertIn("retired-system", slugs)
        self.assertNotIn("google-workspace", slugs)

    def test_category_filter(self):
        r = self.client.get(
            "/api/staff/record-systems/?category=Video+surveillance")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        slugs = {x["slug"] for x in rows}
        self.assertIn("bus-video", slugs)
        self.assertNotIn("google-workspace", slugs)

    def test_search_filter(self):
        r = self.client.get(
            "/api/staff/record-systems/?search=Transportation")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        slugs = {x["slug"] for x in rows}
        self.assertIn("bus-video", slugs)
        self.assertNotIn("google-workspace", slugs)

    def test_destroy_blocked(self):
        r = self.client.delete(
            f"/api/staff/record-systems/{self.workspace.id}/")
        self.assertEqual(r.status_code, 405)
        self.assertTrue(
            RecordSystem.objects.filter(id=self.workspace.id).exists())
        self.assertIn("active", r.json()["detail"].lower())

    def test_create_appears_in_list(self):
        r = self.client.post("/api/staff/record-systems/", {
            "slug": "new-system", "name": "New System",
            "category": "Operations", "vendor": "TBD",
            "custodian_title": "TBD",
            "change_reason": (
                "Adding the newly approved operations records source."
            ),
        }, content_type="application/json")
        self.assertEqual(r.status_code, 201)
        r2 = self.client.get("/api/staff/record-systems/")
        payload = r2.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertTrue(any(x["slug"] == "new-system" for x in rows))
        self.assertTrue(
            AuditLog.objects.filter(
                action="record_system.created",
                target=str(r.json()["id"]),
            ).exists()
        )

    def test_preservation_routing_change_requires_reason_and_is_audited(self):
        path = f"/api/staff/record-systems/{self.bus_video.id}/"

        rejected = self.client.patch(
            path,
            {"active": False},
            content_type="application/json",
        )

        self.assertEqual(rejected.status_code, 400)
        self.bus_video.refresh_from_db()
        self.assertTrue(self.bus_video.active)
        self.assertFalse(
            AuditLog.objects.filter(
                action="record_system.updated",
                target=str(self.bus_video.pk),
            ).exists()
        )

        accepted = self.client.patch(
            path,
            {
                "active": False,
                "change_reason": (
                    "The records source was retired after retention review."
                ),
            },
            content_type="application/json",
        )

        self.assertEqual(accepted.status_code, 200)
        self.bus_video.refresh_from_db()
        self.assertFalse(self.bus_video.active)
        event = AuditLog.objects.get(
            action="record_system.updated",
            target=str(self.bus_video.pk),
        )
        self.assertEqual(event.detail["before"]["active"], True)
        self.assertEqual(event.detail["after"]["active"], False)
        self.assertEqual(
            event.detail["reason"],
            "The records source was retired after retention review.",
        )
