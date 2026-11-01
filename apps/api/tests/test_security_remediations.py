# ABOUTME: Regression coverage for cross-role authorization and identity
# ABOUTME: findings validated by the 2026-07-25 Codex Security scan.
import datetime as dt

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

from apps.api.identity import StaffRole, apply_staff_roles, provision_google_user
from apps.api.models import GoogleGroupRoleMapping, StaffIdentity
from apps.correspondence import services as correspondence_services
from apps.correspondence.models import Correspondence
from apps.records import services as record_services
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
    RequestStatus,
    StatusEvent,
    TimeEntry,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class RoleBoundarySecurityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "security-officer", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.admin = User.objects.create_user(
            "security-admin", is_staff=True
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.reviewer = User.objects.create_user(
            "security-reviewer", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.combined = User.objects.create_user(
            "security-admin-liaison", is_staff=True
        )
        apply_staff_roles(
            self.combined,
            {StaffRole.ADMIN, StaffRole.LIAISON},
        )
        self.assigned_liaison = User.objects.create_user(
            "security-assigned-liaison", is_staff=True
        )
        apply_staff_roles(self.assigned_liaison, {StaffRole.LIAISON})
        self.requester = Requester.objects.create(
            name="Private Requester",
            email="private-requester@example.org",
            phone="253-555-0199",
            mailing_address="123 Private Lane",
            city="Tacoma",
            state="WA",
            postal_code="98402",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Sensitive personnel correspondence.",
            estimated_completion=timezone.localdate() + dt.timedelta(days=30),
        )
        self.assigned_task = CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Assigned only to the designated liaison.",
            assigned_to=self.assigned_liaison,
        )
        self.draft = correspondence_services.create_authored_draft(
            self.prr,
            kind=Correspondence.Kind.GENERAL,
            subject="Private subject",
            body="Private requester message body.",
            actor=self.officer,
        )

    def test_reviewer_and_application_admin_cannot_read_correspondence_pii(self):
        for user in (self.reviewer, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                listing = self.client.get("/api/staff/correspondence/")
                detail = self.client.get(
                    f"/api/staff/correspondence/{self.draft.pk}/"
                )

                self.assertEqual(listing.status_code, 403)
                self.assertEqual(detail.status_code, 403)
                self.assertNotIn(
                    "private-requester@example.org",
                    listing.content.decode(errors="replace"),
                )
                self.assertNotIn(
                    "Private requester message body",
                    detail.content.decode(errors="replace"),
                )

    def test_application_admin_request_views_minimize_requester_pii(self):
        self.client.force_login(self.admin)

        listing = self.client.get("/api/staff/requests/")
        detail = self.client.get(f"/api/staff/requests/{self.prr.number}/")
        email_search = self.client.get(
            "/api/staff/requests/?search=private-requester%40example.org"
        )

        self.assertEqual(listing.status_code, 200)
        row = next(
            item
            for item in listing.json()["results"]
            if item["number"] == self.prr.number
        )
        self.assertIsNone(row["requester_email"])
        self.assertIsNone(row["requester_name"])
        self.assertEqual(detail.status_code, 200)
        self.assertIsNone(detail.json()["requester"])
        self.assertEqual(email_search.json()["results"], [])

    def test_admin_liaison_cannot_escape_assignment_scope(self):
        self.client.force_login(self.combined)

        correspondence_list = self.client.get(
            f"/api/staff/correspondence/?request={self.prr.number}"
        )
        correspondence_detail = self.client.get(
            f"/api/staff/correspondence/{self.draft.pk}/"
        )
        task_detail = self.client.get(
            f"/api/staff/collection-tasks/{self.assigned_task.pk}/"
        )
        task_edit = self.client.patch(
            f"/api/staff/collection-tasks/{self.assigned_task.pk}/",
            {"state": CollectionTask.State.RUNNING},
            content_type="application/json",
        )

        self.assertEqual(correspondence_list.status_code, 200)
        self.assertEqual(correspondence_list.json()["results"], [])
        self.assertEqual(correspondence_detail.status_code, 404)
        self.assertEqual(task_detail.status_code, 404)
        self.assertEqual(task_edit.status_code, 404)
        self.assigned_task.refresh_from_db()
        self.assertEqual(self.assigned_task.state, CollectionTask.State.PENDING)

    def test_admin_liaison_cannot_create_any_draft_for_unassigned_request(self):
        self.client.force_login(self.combined)
        future = timezone.localdate() + dt.timedelta(days=45)
        routes = (
            (
                "/api/staff/correspondence/drafts/general/",
                {"subject": "Subject", "body": "Body"},
            ),
            (
                "/api/staff/correspondence/drafts/acknowledgment/",
                {"estimated_completion": future.isoformat()},
            ),
            (
                "/api/staff/correspondence/drafts/clarification/",
                {
                    "clarification_questions": "Which date range?",
                    "clarification_due": (
                        timezone.localdate() + dt.timedelta(days=10)
                    ).isoformat(),
                },
            ),
            (
                "/api/staff/correspondence/drafts/estimate-revision/",
                {
                    "new_date": future.isoformat(),
                    "reason": "The search volume requires additional review.",
                },
            ),
            (
                "/api/staff/correspondence/drafts/responsive-records/",
                {"subject": "Subject", "body": "Body"},
            ),
        )

        for route, payload in routes:
            with self.subTest(route=route):
                unassigned = PublicRecordsRequest.objects.create(
                    requester=self.requester,
                    description=f"Unassigned request for {route}.",
                    estimated_completion=(
                        timezone.localdate() + dt.timedelta(days=30)
                    ),
                )
                response = self.client.post(
                    route,
                    {"request_number": unassigned.number, **payload},
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 404)
                self.assertFalse(
                    Correspondence.objects.filter(request=unassigned).exists()
                )

    def test_admin_liaison_cannot_edit_authored_or_structured_unassigned_drafts(self):
        acknowledgment = (
            correspondence_services.create_acknowledgment_draft(
                self.prr,
                actor=self.officer,
                estimated_completion=(
                    timezone.localdate() + dt.timedelta(days=30)
                ),
            )
        )
        self.client.force_login(self.combined)

        authored = self.client.patch(
            f"/api/staff/correspondence/{self.draft.pk}/",
            {
                "expected_version": self.draft.version,
                "subject": "Illicit edit",
                "body": "Illicit body",
            },
            content_type="application/json",
        )
        structured = self.client.patch(
            f"/api/staff/correspondence/{acknowledgment.pk}/",
            {
                "expected_version": acknowledgment.version,
                "estimated_completion": (
                    timezone.localdate() + dt.timedelta(days=60)
                ).isoformat(),
            },
            content_type="application/json",
        )

        self.assertEqual(authored.status_code, 404)
        self.assertEqual(structured.status_code, 404)
        self.draft.refresh_from_db()
        acknowledgment.refresh_from_db()
        self.assertEqual(self.draft.draft_subject, "Private subject")
        self.assertNotEqual(
            acknowledgment.draft_metadata["estimated_completion"],
            (timezone.localdate() + dt.timedelta(days=60)).isoformat(),
        )

    def test_application_admin_cannot_change_urgent_preservation_routing(self):
        system = RecordSystem.objects.create(
            slug="security-camera",
            name="Security Camera",
            urgent_hold=True,
        )
        self.client.force_login(self.admin)

        response = self.client.patch(
            f"/api/staff/record-systems/{system.pk}/",
            {"urgent_hold": False},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        system.refresh_from_db()
        self.assertTrue(system.urgent_hold)

    def test_liaison_cannot_log_time_to_an_unassigned_request(self):
        self.client.force_login(self.combined)

        response = self.client.post(
            "/api/staff/time-entries/",
            {
                "request": str(self.prr.pk),
                "date": timezone.localdate().isoformat(),
                "minutes": 15,
                "activity": "Unassigned work",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(TimeEntry.objects.filter(staff=self.combined).exists())

    def test_nonterminal_transition_rechecks_current_officer_authority(self):
        apply_staff_roles(self.officer, set())

        with self.assertRaises(PermissionDenied):
            record_services.transition_manually(
                self.prr,
                RequestStatus.GATHERING,
                actor=self.officer,
                note="Attempt after authority was revoked.",
            )

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertFalse(
            StatusEvent.objects.filter(
                request=self.prr,
                to_status=RequestStatus.GATHERING,
            ).exists()
        )


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
    PRR_GOOGLE_GROUP_ALLOWED_DOMAINS=("psd401.net",),
)
class IdentityBoundarySecurityTests(TestCase):
    def claims(self):
        return {
            "sub": "disabled-google-subject",
            "email": "disabled.user@psd401.net",
            "email_verified": True,
            "hd": "psd401.net",
            "given_name": "Disabled",
            "family_name": "User",
        }

    def test_google_login_does_not_reactivate_disabled_linked_staff(self):
        user = get_user_model().objects.create_user(
            "disabled-linked",
            email="disabled.user@psd401.net",
            is_staff=True,
            is_active=False,
        )
        StaffIdentity.objects.create(
            user=user,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="disabled-google-subject",
        )

        with self.assertRaises(DRFPermissionDenied):
            provision_google_user(self.claims(), {StaffRole.REVIEWER})

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(user.groups.exists())

    def test_application_admin_cannot_create_records_officer_mapping(self):
        admin = get_user_model().objects.create_user(
            "mapping-admin",
            is_staff=True,
        )
        apply_staff_roles(admin, {StaffRole.ADMIN})
        self.client.force_login(admin)

        response = self.client.post(
            "/api/staff/google-role-mappings/",
            {
                "role": StaffRole.RECORDS_OFFICER,
                "google_group": "records-officers@psd401.net",
                "active": True,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            GoogleGroupRoleMapping.objects.filter(
                role=StaffRole.RECORDS_OFFICER
            ).exists()
        )

    def test_dual_admin_officer_can_maintain_records_officer_mapping(self):
        dual = get_user_model().objects.create_user(
            "mapping-admin-officer",
            is_staff=True,
        )
        apply_staff_roles(
            dual,
            {StaffRole.ADMIN, StaffRole.RECORDS_OFFICER},
        )
        self.client.force_login(dual)

        response = self.client.post(
            "/api/staff/google-role-mappings/",
            {
                "role": StaffRole.RECORDS_OFFICER,
                "google_group": "records-officers@psd401.net",
                "active": True,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)

    def test_admin_group_can_also_become_records_officer_group(self):
        GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="application-admins@psd401.net",
            active=True,
        )
        dual = get_user_model().objects.create_user(
            "mapping-separation-officer",
            is_staff=True,
        )
        apply_staff_roles(
            dual,
            {StaffRole.ADMIN, StaffRole.RECORDS_OFFICER},
        )
        self.client.force_login(dual)

        response = self.client.post(
            "/api/staff/google-role-mappings/",
            {
                "role": StaffRole.RECORDS_OFFICER,
                "google_group": "application-admins@psd401.net",
                "active": True,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            GoogleGroupRoleMapping.objects.filter(
                role=StaffRole.RECORDS_OFFICER,
                google_group="application-admins@psd401.net",
            ).exists()
        )

    def test_database_allows_shared_privileged_group(self):
        GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="database-admins@psd401.net",
            active=True,
        )
        GoogleGroupRoleMapping.objects.create(
            role=StaffRole.RECORDS_OFFICER,
            google_group="database-admins@psd401.net",
            active=True,
        )

        self.assertEqual(
            GoogleGroupRoleMapping.objects.filter(
                google_group="database-admins@psd401.net",
                active=True,
            ).count(),
            2,
        )
