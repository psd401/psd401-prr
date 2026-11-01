# ABOUTME: Contract tests for named, hashed, expiring personal access tokens.
# ABOUTME: Covers lifecycle, show-once secrets, auth failures, audit, and gates.
from datetime import timedelta
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.models import PersonalAccessToken, StaffIdentity
from apps.audit.models import AuditLog
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS=90,
    PRR_PERSONAL_TOKEN_MAX_ACTIVE=5,
    PRR_PERSONAL_TOKEN_LAST_USED_WINDOW_SECONDS=60,
    PRR_PERSONAL_TOKEN_CREATE_RATE="1000/hour",
)
class PersonalAccessTokenLifecycleTests(TestCase):
    endpoint = "/api/staff/auth/api-tokens/"

    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "personal-token-staff",
            "personal-token-staff@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.staff, {StaffRole.REVIEWER})
        self.client.force_login(self.staff)

    @staticmethod
    def token_auth(key):
        return {"HTTP_AUTHORIZATION": f"Token {key}"}

    def create_token(self, *, name="Claude Desktop", lifetime_days=30):
        return self.client.post(
            self.endpoint,
            {"name": name, "lifetime_days": lifetime_days},
            content_type="application/json",
            REMOTE_ADDR="203.0.113.20",
        )

    def test_create_list_rotate_and_revoke_keep_secrets_show_once(self):
        create = self.create_token()

        self.assertEqual(create.status_code, 201)
        self.assertEqual(create["Cache-Control"], "no-store")
        first_payload = create.json()
        first_key = first_payload.pop("token")
        self.assertTrue(first_key.startswith("prr_pat_"))
        self.assertEqual(
            set(first_payload),
            {
                "id",
                "name",
                "prefix",
                "created_at",
                "expires_at",
                "last_used_at",
                "revoked_at",
            },
        )

        first = PersonalAccessToken.objects.get(pk=first_payload["id"])
        self.assertEqual(first.user, self.staff)
        self.assertEqual(first.name, "Claude Desktop")
        self.assertNotEqual(first.token_hash, first_key)
        self.assertNotIn(first_key, str(first.__dict__))
        self.assertEqual(len(first.token_hash), 64)
        self.assertIn(first.prefix, first_key)

        listed = self.client.get(self.endpoint)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(listed.json()[0], first_payload)
        self.assertNotIn(first_key, listed.content.decode())
        self.assertNotIn("token_hash", listed.content.decode())

        token_client = Client()
        first_read = token_client.get(
            "/api/staff/requests/",
            **self.token_auth(first_key),
        )
        self.assertEqual(first_read.status_code, 200)

        rotate = self.client.post(
            f"{self.endpoint}{first.pk}/rotate",
            REMOTE_ADDR="203.0.113.21",
        )
        self.assertEqual(rotate.status_code, 200)
        second_payload = rotate.json()
        second_key = second_payload.pop("token")
        self.assertNotEqual(second_key, first_key)
        self.assertEqual(second_payload["name"], first.name)
        self.assertNotEqual(second_payload["id"], first.pk)

        old_read = token_client.get(
            "/api/staff/requests/",
            **self.token_auth(first_key),
        )
        new_read = token_client.get(
            "/api/staff/requests/",
            **self.token_auth(second_key),
        )
        self.assertEqual(old_read.status_code, 401)
        self.assertEqual(old_read.json()["code"], "token_revoked")
        self.assertEqual(new_read.status_code, 200)

        revoke = self.client.delete(
            f"{self.endpoint}{second_payload['id']}",
            REMOTE_ADDR="203.0.113.22",
        )
        self.assertEqual(revoke.status_code, 204)
        revoked_read = token_client.get(
            "/api/staff/requests/",
            **self.token_auth(second_key),
        )
        self.assertEqual(revoked_read.status_code, 401)
        self.assertEqual(revoked_read.json()["code"], "token_revoked")

        rows = self.client.get(self.endpoint).json()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["revoked_at"] for row in rows))
        serialized_rows = str(rows)
        self.assertNotIn(first_key, serialized_rows)
        self.assertNotIn(second_key, serialized_rows)

        audits = list(
            AuditLog.objects.filter(
                actor=self.staff,
                action__startswith="identity.personal_api_token_",
            ).order_by("at")
        )
        self.assertEqual(
            [row.action for row in audits],
            [
                "identity.personal_api_token_created",
                "identity.personal_api_token_rotated",
                "identity.personal_api_token_revoked",
            ],
        )
        self.assertEqual(
            [row.ip_address for row in audits],
            ["203.0.113.20", "203.0.113.21", "203.0.113.22"],
        )
        for row in audits:
            self.assertEqual(row.detail["name"], "Claude Desktop")
            self.assertTrue(row.detail["prefix"])
            self.assertNotIn(first_key, str(row.__dict__))
            self.assertNotIn(second_key, str(row.__dict__))

    @override_settings(
        PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS=30,
        PRR_PERSONAL_TOKEN_MAX_ACTIVE=2,
    )
    def test_lifetime_duplicate_name_and_active_limit_are_field_errors(self):
        too_long = self.create_token(name="Too long", lifetime_days=31)
        self.assertEqual(too_long.status_code, 400)
        self.assertIn("lifetime_days", too_long.json())

        first = self.create_token(name="Claude Desktop", lifetime_days=30)
        self.assertEqual(first.status_code, 201)

        duplicate = self.create_token(name="Claude Desktop", lifetime_days=10)
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("name", duplicate.json())

        second = self.create_token(name="Laptop CLI", lifetime_days=10)
        self.assertEqual(second.status_code, 201)

        over_limit = self.create_token(name="Automation test", lifetime_days=1)
        self.assertEqual(over_limit.status_code, 400)
        self.assertIn("non_field_errors", over_limit.json())
        self.assertIn("2", " ".join(over_limit.json()["non_field_errors"]))

        PersonalAccessToken.objects.filter(
            pk=first.json()["id"],
        ).update(expires_at=timezone.now() - timedelta(seconds=1))
        replacement = self.create_token(name="Automation test", lifetime_days=1)
        self.assertEqual(replacement.status_code, 201)

    @override_settings(PRR_PERSONAL_TOKEN_MAX_ACTIVE=1)
    def test_rotating_expired_token_cannot_bypass_active_limit(self):
        expired = self.create_token(name="Expired laptop", lifetime_days=1)
        PersonalAccessToken.objects.filter(pk=expired.json()["id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        active = self.create_token(name="Active desktop", lifetime_days=1)
        self.assertEqual(active.status_code, 201)

        response = self.client.post(
            f"{self.endpoint}{expired.json()['id']}/rotate",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("non_field_errors", response.json())
        self.assertIsNone(
            PersonalAccessToken.objects.get(pk=expired.json()["id"]).revoked_at,
        )
        self.assertEqual(
            PersonalAccessToken.objects.filter(
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).count(),
            1,
        )

    def test_expired_invalid_and_revoked_tokens_fail_distinctly(self):
        created = self.create_token(name="Expiry test", lifetime_days=1).json()
        key = created["token"]
        token_client = Client()

        PersonalAccessToken.objects.filter(pk=created["id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        expired = token_client.get(
            "/api/staff/requests/",
            **self.token_auth(key),
        )
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.json()["code"], "token_expired")

        invalid = token_client.get(
            "/api/staff/requests/",
            **self.token_auth("prr_pat_000000000000_not-a-real-secret"),
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["code"], "invalid_token")

        active = self.create_token(name="Revoke test", lifetime_days=1).json()
        self.client.delete(f"{self.endpoint}{active['id']}")
        revoked = token_client.get(
            "/api/staff/requests/",
            **self.token_auth(active["token"]),
        )
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.json()["code"], "token_revoked")

    def test_last_used_timestamp_is_write_throttled(self):
        created = self.create_token(name="Usage clock", lifetime_days=1).json()
        key = created["token"]
        row = PersonalAccessToken.objects.get(pk=created["id"])
        base = row.created_at + timedelta(minutes=1)
        token_client = Client()

        with mock.patch("apps.api.authentication.timezone.now", return_value=base):
            first = token_client.get(
                "/api/staff/requests/",
                **self.token_auth(key),
            )
        self.assertEqual(first.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.last_used_at, base)

        within_window = base + timedelta(seconds=30)
        with mock.patch(
            "apps.api.authentication.timezone.now",
            return_value=within_window,
        ):
            second = token_client.get(
                "/api/staff/requests/",
                **self.token_auth(key),
            )
        self.assertEqual(second.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.last_used_at, base)

        after_window = base + timedelta(seconds=61)
        with mock.patch(
            "apps.api.authentication.timezone.now",
            return_value=after_window,
        ):
            third = token_client.get(
                "/api/staff/requests/",
                **self.token_auth(key),
            )
        self.assertEqual(third.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.last_used_at, after_window)

    def test_session_mutations_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        me = csrf_client.get("/api/staff/auth/me")
        csrf_token = me.cookies["csrftoken"].value
        body = {"name": "CSRF test", "lifetime_days": 30}

        missing_csrf = csrf_client.post(
            self.endpoint,
            body,
            content_type="application/json",
        )
        with_csrf = csrf_client.post(
            self.endpoint,
            body,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(missing_csrf.status_code, 403)
        self.assertEqual(with_csrf.status_code, 201)

    def test_legacy_plaintext_self_service_route_is_removed(self):
        response = self.client.post("/api/staff/auth/api-token/")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Token.objects.filter(user=self.staff).exists())


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS=90,
    PRR_PERSONAL_TOKEN_MAX_ACTIVE=5,
    PRR_PERSONAL_TOKEN_CREATE_RATE="1000/hour",
)
class PersonalAccessTokenAuthorizationTests(TestCase):
    endpoint = "/api/staff/auth/api-tokens/"

    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "token-caller",
            "token-caller@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.staff, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(self.staff)
        issued = self.client.post(
            self.endpoint,
            {"name": "Agent", "lifetime_days": 30},
            content_type="application/json",
        ).json()
        self.token_id = issued["id"]
        self.key = issued["token"]
        self.auth = {"HTTP_AUTHORIZATION": f"Token {self.key}"}
        self.client.logout()

    def assert_handoff(self, response, capability_id):
        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["code"], "human_approval_required")
        self.assertEqual(payload["handoff"]["capability_id"], capability_id)
        self.assertEqual(
            payload["handoff"]["required_role"],
            "role_mapped_staff",
        )

    def test_personal_token_cannot_manage_personal_tokens(self):
        calls = (
            ("api_tokens.list", lambda: self.client.get(self.endpoint, **self.auth)),
            (
                "api_tokens.create",
                lambda: self.client.post(
                    self.endpoint,
                    {"name": "No persistence", "lifetime_days": 30},
                    content_type="application/json",
                    **self.auth,
                ),
            ),
            (
                "api_tokens.rotate",
                lambda: self.client.post(
                    f"{self.endpoint}{self.token_id}/rotate",
                    **self.auth,
                ),
            ),
            (
                "api_tokens.revoke",
                lambda: self.client.delete(
                    f"{self.endpoint}{self.token_id}",
                    **self.auth,
                ),
            ),
        )

        for capability_id, call in calls:
            with self.subTest(capability_id=capability_id):
                self.assert_handoff(call(), capability_id)

        self.assertIsNone(
            PersonalAccessToken.objects.get(pk=self.token_id).revoked_at,
        )

    def test_scoped_service_token_cannot_manage_personal_tokens(self):
        principal = get_user_model().objects.create_user("mcp-token-manager")
        principal.set_unusable_password()
        principal.save(update_fields=["password"])
        principal.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="records",
                codename="read_staff_api",
            )
        )
        token = Token.objects.create(user=principal)
        auth = {"HTTP_AUTHORIZATION": f"Token {token.key}"}

        self.assert_handoff(
            self.client.get(self.endpoint, **auth),
            "api_tokens.list",
        )

    def test_non_role_mapped_and_inactive_sessions_are_forbidden(self):
        no_role = get_user_model().objects.create_user(
            "no-role-token-user",
            "no-role-token-user@psd401.net",
            "pw",
            is_staff=True,
        )
        inactive = get_user_model().objects.create_user(
            "inactive-token-user",
            "inactive-token-user@psd401.net",
            "pw",
            is_staff=True,
            is_active=False,
        )
        apply_staff_roles(inactive, {StaffRole.REVIEWER})

        for user in (no_role, inactive):
            client = Client()
            client.force_login(user)
            with self.subTest(user=user.username):
                response = client.get(self.endpoint)
                self.assertEqual(response.status_code, 403)

    def test_personal_token_gets_handoff_for_human_only_denial(self):
        requester = Requester.objects.create(email="handoff@example.invalid")
        request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Records requested for the PAT handoff regression.",
        )

        response = self.client.post(
            f"/api/staff/requests/{request.number}/transition/",
            {"to_status": RequestStatus.DENIED, "note": "Draft only."},
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "human_approval_required")
        request.refresh_from_db()
        self.assertEqual(request.status, RequestStatus.RECEIVED)


class PersonalAccessTokenMigrationTests(TestCase):
    def test_upgrade_revokes_legacy_staff_tokens_but_keeps_service_tokens(self):
        staff = get_user_model().objects.create_user(
            "legacy-personal-token",
            is_staff=True,
        )
        service = get_user_model().objects.create_user("scoped-service-token")
        legacy = Token.objects.create(user=staff)
        scoped = Token.objects.create(user=service)
        migration = import_module(
            "apps.api.migrations.0004_personalaccesstoken",
        )

        migration.revoke_legacy_staff_tokens(django_apps, None)

        self.assertFalse(Token.objects.filter(pk=legacy.pk).exists())
        self.assertTrue(Token.objects.filter(pk=scoped.pk).exists())


@override_settings(
    PRR_AUTH_MODE="google",
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_GOOGLE_GROUP_RECHECK_SECONDS=60,
    PRR_GOOGLE_GROUP_MAX_STALE_SECONDS=120,
    PRR_PERSONAL_TOKEN_CREATE_RATE="1000/hour",
)
class PersonalAccessTokenRoleRevalidationTests(TestCase):
    @mock.patch(
        "apps.api.identity.GoogleGroupDirectory.roles_for_email",
        return_value=set(),
    )
    def test_personal_token_cannot_outlive_google_group_removal(self, roles):
        user = get_user_model().objects.create_user(
            "pat-google-user",
            email="pat-google-user@psd401.net",
            is_staff=True,
        )
        StaffIdentity.objects.create(
            user=user,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="pat-google-subject",
            last_group_check_at=timezone.now(),
        )
        apply_staff_roles(user, {StaffRole.LIAISON})
        self.client.force_login(user)
        issued = self.client.post(
            "/api/staff/auth/api-tokens/",
            {"name": "Google membership", "lifetime_days": 30},
            content_type="application/json",
        ).json()
        self.client.logout()
        identity = user.staff_identity
        identity.last_group_check_at = timezone.now() - timedelta(minutes=5)
        identity.save(update_fields=["last_group_check_at", "updated_at"])

        response = self.client.get(
            "/api/staff/collection-tasks/",
            HTTP_AUTHORIZATION=f"Token {issued['token']}",
        )

        self.assertEqual(response.status_code, 403)
        roles.assert_called_once_with("pat-google-user@psd401.net")
