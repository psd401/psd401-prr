# ABOUTME: Tests for agent-facing API auth (TokenAuthentication, staff-only)
# ABOUTME: and the OpenAPI docs UI (/api/docs/, /api/redoc/, /api/schema/).
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase
from rest_framework.authtoken.models import Token


class TokenAuthenticationTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "agent", "agent@psd401.net", "pw", is_staff=True)
        self.staff_token = Token.objects.create(user=self.staff)
        self.civilian = get_user_model().objects.create_user(
            "agent-civilian", "agent-civilian@example.com", "pw", is_staff=False)
        self.civilian_token = Token.objects.create(user=self.civilian)

    def test_valid_staff_token_grants_access(self):
        r = self.client.get("/api/staff/requests/",
                            HTTP_AUTHORIZATION=f"Token {self.staff_token.key}")
        self.assertEqual(r.status_code, 200)

    def test_non_staff_token_forbidden(self):
        r = self.client.get("/api/staff/requests/",
                            HTTP_AUTHORIZATION=f"Token {self.civilian_token.key}")
        self.assertEqual(r.status_code, 403)

    def test_no_credentials_rejected(self):
        r = self.client.get("/api/staff/requests/")
        self.assertIn(r.status_code, (401, 403))


class ReadOnlyMcpTokenTests(TestCase):
    def setUp(self):
        self.agent = get_user_model().objects.create_user("mcp-readonly")
        self.agent.set_unusable_password()
        self.agent.save(update_fields=["password"])
        self.agent.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="records",
                codename="read_staff_api",
            )
        )
        self.token = Token.objects.create(user=self.agent)
        self.auth = {"HTTP_AUTHORIZATION": f"Token {self.token.key}"}

    def test_scoped_mcp_token_can_read_staff_resources(self):
        response = self.client.get("/api/staff/requests/", **self.auth)

        self.assertEqual(response.status_code, 200)

    def test_scoped_mcp_token_cannot_mutate_staff_resources(self):
        response = self.client.post(
            "/api/staff/exemptions/",
            {
                "citation": "RCW test",
                "short_name": "Must not be created",
            },
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 403)

    def test_scoped_mcp_token_cannot_read_staff_identity_configuration(self):
        response = self.client.get("/api/staff/google-role-mappings/", **self.auth)

        self.assertEqual(response.status_code, 403)

    def test_scoped_mcp_token_cannot_read_district_settings(self):
        # The district kill switches are Application-Admin-only, including
        # for the shared read-only agent identity.
        for path in (
            "/api/staff/settings/",
            "/api/staff/settings/PRR_GMAIL_ENABLED/history/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, **self.auth)
                self.assertEqual(response.status_code, 403)

    def test_provision_command_creates_a_noninteractive_readonly_account(self):
        output = StringIO()

        call_command("provision_mcp_readonly", "mcp-production", stdout=output)

        user = get_user_model().objects.get(username="mcp-production")
        token = Token.objects.get(user=user)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.has_perm("records.read_staff_api"))
        self.assertIn(token.key, output.getvalue())


class OpenApiDocsTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "docs-agent", "docs-agent@psd401.net", "pw", is_staff=True)
        self.token = Token.objects.create(user=self.staff)

    def test_swagger_ui_anonymous_rejected(self):
        r = self.client.get("/api/docs/")
        self.assertIn(r.status_code, (401, 403))

    def test_swagger_ui_staff_token_accepted(self):
        r = self.client.get("/api/docs/",
                            HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.assertEqual(r.status_code, 200)

    def test_redoc_anonymous_rejected(self):
        r = self.client.get("/api/redoc/")
        self.assertIn(r.status_code, (401, 403))

    def test_redoc_staff_token_accepted(self):
        r = self.client.get("/api/redoc/",
                            HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.assertEqual(r.status_code, 200)

    def test_schema_with_token_returns_openapi_document(self):
        r = self.client.get("/api/schema/",
                            HTTP_AUTHORIZATION=f"Token {self.token.key}",
                            HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertIn("openapi", payload)
