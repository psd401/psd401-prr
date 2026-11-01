# ABOUTME: Endpoint authorization matrix for Records Officer, Liaison,
# ABOUTME: Reviewer, and application Admin staff roles.
import importlib
import inspect
import pkgutil

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSetMixin

from apps.api import resources
from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.permissions import IsStaffOrReadOnlyAgent
from apps.records.models import CollectionTask, PublicRecordsRequest, Requester


class StaffRolePolicyInventoryTests(SimpleTestCase):
    def test_every_staff_resource_declares_its_role_policy(self):
        missing = []
        for module_info in pkgutil.iter_modules(resources.__path__):
            module = importlib.import_module(f"{resources.__name__}.{module_info.name}")
            for name, view_class in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(view_class, ViewSetMixin)
                    and view_class.__module__ == module.__name__
                    and not hasattr(view_class, "allowed_roles")
                    and not hasattr(view_class, "role_permissions")
                ):
                    missing.append(f"{module_info.name}.{name}")

        self.assertEqual(missing, [])


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class RolePermissionDefaultTests(TestCase):
    def test_an_unlisted_action_fails_closed_when_a_view_declares_an_action_matrix(self):
        user = get_user_model().objects.create_user("officer", is_staff=True)
        apply_staff_roles(user, {StaffRole.RECORDS_OFFICER})
        request = APIRequestFactory().post("/example")
        force_authenticate(request, user=user)
        request.user = user

        class ExampleView(APIView):
            action = "new_sensitive_action"
            role_permissions = {"list": {StaffRole.RECORDS_OFFICER}}

        self.assertFalse(IsStaffOrReadOnlyAgent().has_permission(request, ExampleView()))


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class StaffRoleBoundaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        requester = Requester.objects.create(email="requester@example.org")
        cls.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Records needed",
        )
        cls.other_request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Other records",
        )

    def user_for(self, role):
        user = get_user_model().objects.create_user(
            f"{role}-user", email=f"{role}@psd401.net", is_staff=True
        )
        apply_staff_roles(user, {role})
        self.client.force_login(user)
        return user

    def test_liaison_only_sees_tasks_and_requests_assigned_to_them(self):
        liaison = self.user_for(StaffRole.LIAISON)
        own = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            instructions="own",
            assigned_to=liaison,
        )
        CollectionTask.objects.create(
            request=self.other_request,
            kind=CollectionTask.Kind.MANUAL,
            instructions="other",
        )
        CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            instructions="another department",
        )

        tasks = self.client.get("/api/staff/collection-tasks/").json()["results"]
        requests = self.client.get("/api/staff/requests/").json()["results"]
        detail = self.client.get(
            f"/api/staff/requests/{self.request.number}/"
        ).json()

        self.assertEqual([row["id"] for row in tasks], [own.id])
        self.assertEqual([row["number"] for row in requests], [self.request.number])
        self.assertEqual([row["id"] for row in detail["collection_tasks"]], [own.id])
        self.assertIsNone(detail["requester"])

    def test_liaison_cannot_reassign_or_rewrite_a_task(self):
        liaison = self.user_for(StaffRole.LIAISON)
        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            instructions="do this",
            assigned_to=liaison,
        )

        response = self.client.patch(
            f"/api/staff/collection-tasks/{task.id}/",
            {"instructions": "changed"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        task.refresh_from_db()
        self.assertEqual(task.instructions, "do this")

    def test_reviewer_cannot_read_requester_directory(self):
        self.user_for(StaffRole.REVIEWER)

        response = self.client.get("/api/staff/requesters/")
        requester_search = self.client.get(
            "/api/staff/requests/?search=requester@example.org"
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(requester_search.json()["results"], [])

    def test_application_admin_cannot_make_a_legal_status_transition(self):
        self.user_for(StaffRole.ADMIN)

        response = self.client.post(
            f"/api/staff/requests/{self.request.number}/transition/",
            {"to_status": "acknowledged", "note": "not authorized"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_records_officer_can_transition_a_request(self):
        self.user_for(StaffRole.RECORDS_OFFICER)

        response = self.client.post(
            f"/api/staff/requests/{self.request.number}/transition/",
            {"to_status": "gathering", "note": "Collection started"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
