# ABOUTME: Acceptance coverage for the Application-Admin integration console.
# ABOUTME: Proves shared provisioning, show-once secrets, scope, audit, and auth.
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils.dateparse import parse_datetime
from rest_framework.authtoken.models import Token

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.resources.integration_identities import (
    IntegrationIdentityViewSet,
)
from apps.audit.models import AuditLog
from apps.records import service_identities


def permission(app_label, codename):
    return Permission.objects.get(
        content_type__app_label=app_label,
        codename=codename,
    )


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class IntegrationIdentityConsoleTests(TestCase):
    endpoint = "/api/staff/integration-identities/"

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "application-admin",
            "application-admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.client.force_login(self.admin)

    @staticmethod
    def service_user(username, *permissions):
        user = get_user_model().objects.create_user(
            username,
            password=None,
        )
        user.user_permissions.add(*permissions)
        return user

    @staticmethod
    def auth(key):
        return {"HTTP_AUTHORIZATION": f"Token {key}"}

    def test_admin_lists_readonly_scope_without_key_material(self):
        reader = self.service_user(
            "mcp-reader",
            permission("records", "read_staff_api"),
        )
        reader_token = Token.objects.create(user=reader)
        broadened_reader = self.service_user(
            "mcp-reader-broadened",
            permission("records", "read_staff_api"),
            permission("correspondence", "deliver_outbound_correspondence"),
        )
        broadened_token = Token.objects.create(user=broadened_reader)

        response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        payload = response.json()
        self.assertEqual(
            [row["id"] for row in payload],
            ["mcp_readonly"],
        )
        reader_row = payload[0]
        self.assertTrue(reader_row["exists"])
        self.assertEqual(len(reader_row["accounts"]), 2)
        self.assertEqual(
            reader_row["accounts"][0]["username"],
            reader.username,
        )
        self.assertEqual(
            parse_datetime(
                reader_row["accounts"][0]["token_created_at"],
            ),
            reader_token.created,
        )
        self.assertTrue(reader_row["accounts"][0]["scope_valid"])
        self.assertEqual(
            reader_row["accounts"][1]["username"],
            broadened_reader.username,
        )
        self.assertFalse(reader_row["accounts"][1]["scope_valid"])
        serialized = response.content.decode()
        self.assertNotIn(reader_token.key, serialized)
        self.assertNotIn(broadened_token.key, serialized)
        self.assertNotIn('"token"', serialized)
        self.assertNotIn("key", serialized.lower())

    def test_provision_and_rotate_return_each_key_once_and_revoke_the_old_key(self):
        provision = self.client.post(
            f"{self.endpoint}mcp_readonly/provision/",
            {"username": "mcp-console-reader"},
            content_type="application/json",
            REMOTE_ADDR="203.0.113.40",
        )

        self.assertEqual(provision.status_code, 201)
        first_payload = provision.json()
        first_key = first_payload.pop("token")
        self.assertEqual(first_payload["identity_id"], "mcp_readonly")
        self.assertEqual(first_payload["username"], "mcp-console-reader")
        self.assertTrue(first_payload["scope_valid"])
        self.assertIsNotNone(first_payload["token_created_at"])

        token_client = Client()
        first_use = token_client.get(
            "/api/staff/requests/",
            **self.auth(first_key),
        )
        self.assertEqual(first_use.status_code, 200)

        rotate = self.client.post(
            f"{self.endpoint}mcp_readonly/rotate/",
            {"username": "mcp-console-reader"},
            content_type="application/json",
            REMOTE_ADDR="203.0.113.41",
        )

        self.assertEqual(rotate.status_code, 200)
        second_payload = rotate.json()
        second_key = second_payload.pop("token")
        self.assertNotEqual(second_key, first_key)
        self.assertEqual(second_payload["username"], "mcp-console-reader")

        old_use = token_client.get(
            "/api/staff/requests/",
            **self.auth(first_key),
        )
        new_use = token_client.get(
            "/api/staff/requests/",
            **self.auth(second_key),
        )
        self.assertEqual(old_use.status_code, 401)
        self.assertEqual(new_use.status_code, 200)

        listed = self.client.get(self.endpoint)
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(first_key, listed.content.decode())
        self.assertNotIn(second_key, listed.content.decode())
        self.assertNotIn('"token"', listed.content.decode())

        audits = list(
            AuditLog.objects.filter(
                actor=self.admin,
                action__startswith="identity.integration_",
            ).order_by("at")
        )
        self.assertEqual(
            [row.action for row in audits],
            [
                "identity.integration_provisioned",
                "identity.integration_rotated",
            ],
        )
        self.assertEqual(
            [row.ip_address for row in audits],
            ["203.0.113.40", "203.0.113.41"],
        )
        for row in audits:
            self.assertEqual(row.detail["identity_id"], "mcp_readonly")
            self.assertEqual(row.detail["username"], "mcp-console-reader")
            self.assertNotIn(first_key, str(row.__dict__))
            self.assertNotIn(second_key, str(row.__dict__))

    def test_commands_and_console_call_the_same_shared_provisioner(self):
        provisioner = service_identities.provision_integration_identity

        with mock.patch.object(
            service_identities,
            "provision_integration_identity",
            wraps=provisioner,
        ) as shared:
            call_command(
                "provision_mcp_readonly",
                "mcp-shared-service",
                stdout=StringIO(),
            )
            response = self.client.post(
                f"{self.endpoint}mcp_readonly/provision/",
                {"username": "mcp-console-service"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(shared.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in shared.call_args_list],
            ["mcp_readonly", "mcp_readonly"],
        )

    def test_only_admin_sessions_execute_and_every_token_caller_gets_a_handoff(self):
        for role in (
            StaffRole.RECORDS_OFFICER,
            StaffRole.LIAISON,
            StaffRole.REVIEWER,
        ):
            with self.subTest(role=role):
                user = get_user_model().objects.create_user(
                    f"{role}-console-denied",
                    is_staff=True,
                )
                apply_staff_roles(user, {role})
                self.client.force_login(user)
                response = self.client.get(self.endpoint)
                self.assertEqual(response.status_code, 403)

        token_callers = [
            self.admin,
            self.service_user(
                "integration-token-caller",
                permission("records", "read_staff_api"),
            ),
        ]
        self.client.logout()
        for user in token_callers:
            with self.subTest(username=user.username):
                token = Token.objects.create(user=user)
                response = self.client.get(
                    self.endpoint,
                    **self.auth(token.key),
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json(),
                    {
                        "code": "human_approval_required",
                        "detail": (
                            "This action requires an accountable human to "
                            "execute it in the staff application."
                        ),
                        "handoff": {
                            "capability_id": "integration_identities.list",
                            "action": (
                                "Review integration identity credential "
                                "status in a staff session."
                            ),
                            "required_role": "application_admin",
                        },
                    },
                )

        self.assertFalse(
            AuditLog.objects.filter(
                action__startswith="identity.integration_",
            ).exists()
        )

    def test_session_permission_uses_the_view_declared_role_policy(self):
        officer = get_user_model().objects.create_user(
            "configured-console-role",
            is_staff=True,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(officer)

        with mock.patch.object(
            IntegrationIdentityViewSet,
            "allowed_roles",
            {StaffRole.RECORDS_OFFICER},
        ):
            response = self.client.get(self.endpoint)

        self.assertEqual(response.status_code, 200)

    def test_unsafe_existing_account_is_not_repurposed(self):
        get_user_model().objects.create_user(
            "interactive-existing-user",
            password="usable-password",
        )

        response = self.client.post(
            f"{self.endpoint}mcp_readonly/provision/",
            {"username": "interactive-existing-user"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.json())
        self.assertFalse(
            Token.objects.filter(
                user__username="interactive-existing-user",
            ).exists()
        )
