# ABOUTME: Officer/admin-only correspondence operations and readiness API.
# ABOUTME: Proves fixed PII-free responses and rejects staff/agent overreach.
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CorrespondenceOperationsApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.users = {}
        for role in StaffRole.values:
            user = User.objects.create_user(
                f"operations-{role}",
                f"operations-{role}@psd401.net",
                "pw",
                is_staff=True,
            )
            apply_staff_roles(user, {role})
            self.users[role] = user
        self.agent = User.objects.create_user(
            "operations-read-agent",
            password=None,
        )
        self.agent.user_permissions.add(
            Permission.objects.get(codename="read_staff_api")
        )
        self.operations_payload = {
            "generated_at": "2026-07-24T12:00:00Z",
            "counts": {
                "queued": 2,
                "claimed": 1,
                "awaiting_reconciliation": 1,
                "retryable": 1,
                "failed": 1,
                "sent": 4,
                "cancelled": 0,
                "stuck": 2,
                "inbound": 3,
                "unmatched": 1,
                "quarantined_messages": 1,
                "quarantined_attachments": 2,
                "delivery_exceptions": 1,
            },
            "ages_seconds": {
                "oldest_delivery": 7200,
                "oldest_claim": 300,
                "oldest_stuck": 5400,
            },
            "failure_categories": {
                "provider_unavailable": 1,
                "other": 1,
            },
        }
        self.readiness_payload = {
            "ready": False,
            "checks": [
                {
                    "component": "object_storage",
                    "status": "not_ready",
                    "code": "unavailable",
                }
            ],
        }

    def token_headers(self, user):
        token = Token.objects.create(user=user)
        return {"HTTP_AUTHORIZATION": f"Token {token.key}"}

    def test_operations_snapshot_is_officer_or_admin_only(self):
        with patch(
            "apps.api.resources.correspondence_operations."
            "operations_snapshot",
            return_value=self.operations_payload,
        ):
            self.assertEqual(
                self.client.get(
                    "/api/staff/correspondence-operations/"
                ).status_code,
                403,
            )
            for role in (StaffRole.LIAISON, StaffRole.REVIEWER):
                self.client.force_login(self.users[role])
                response = self.client.get(
                    "/api/staff/correspondence-operations/"
                )
                self.assertEqual(response.status_code, 403)
                self.client.logout()
            for role in (StaffRole.RECORDS_OFFICER, StaffRole.ADMIN):
                self.client.force_login(self.users[role])
                response = self.client.get(
                    "/api/staff/correspondence-operations/"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), self.operations_payload)
                self.client.logout()

            agent_response = self.client.get(
                "/api/staff/correspondence-operations/",
                **self.token_headers(self.agent),
            )
            self.assertEqual(agent_response.status_code, 403)

    def test_readiness_is_officer_or_admin_only_and_never_mutates(self):
        with patch(
            "apps.api.resources.correspondence_operations."
            "readiness_snapshot",
            return_value=self.readiness_payload,
        ):
            for role in (StaffRole.RECORDS_OFFICER, StaffRole.ADMIN):
                self.client.force_login(self.users[role])
                response = self.client.get(
                    "/api/staff/correspondence-operations/readiness/"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), self.readiness_payload)
                self.assertEqual(
                    self.client.post(
                        "/api/staff/correspondence-operations/readiness/",
                        {},
                        content_type="application/json",
                    ).status_code,
                    405,
                )
                self.client.logout()

            self.client.force_login(self.users[StaffRole.REVIEWER])
            self.assertEqual(
                self.client.get(
                    "/api/staff/correspondence-operations/readiness/"
                ).status_code,
                403,
            )

    def test_openapi_includes_both_read_only_routes_without_sensitive_fields(self):
        self.client.force_login(self.users[StaffRole.ADMIN])

        response = self.client.get("/api/schema/?format=json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        for path in (
            "/api/staff/correspondence-operations/",
            "/api/staff/correspondence-operations/readiness/",
        ):
            self.assertIn(path, paths)
            self.assertSetEqual(set(paths[path]), {"get"})
        operation_schema = str(
            paths["/api/staff/correspondence-operations/"]
        ).lower()
        for sensitive in (
            "requester",
            "subject",
            "body",
            "email",
            "hostname",
            "bucket",
            "exception",
        ):
            self.assertNotIn(sensitive, operation_schema)
