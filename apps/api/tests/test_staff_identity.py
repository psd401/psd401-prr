# ABOUTME: Security contract for Google staff identity, configurable role
# ABOUTME: mappings, local-only passwords, and periodic access revalidation.
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.sessions.models import Session
from django.core.exceptions import SuspiciousOperation
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied

from apps.api.identity import (
    GoogleDirectoryUnavailable,
    GoogleGroupDirectory,
    StaffRole,
    apply_staff_roles,
    provision_google_user,
    refresh_google_roles,
    resolved_roles_for_email,
    staff_roles,
    validate_google_claims,
)
from apps.api.middleware import StaffIdentityRevalidationMiddleware
from apps.api.models import (
    DirectStaffRoleGrant,
    GoogleGroupRoleMapping,
    StaffIdentity,
)
from apps.api.oidc import StaffOIDCBackend, runtime_oidc_setting
from apps.api.resources.identity_config import (
    DirectStaffRoleGrantSerializer,
    GoogleGroupRoleMappingSerializer,
    GoogleGroupRoleMappingViewSet,
)
from apps.audit.models import AuditLog


class GoogleClaimValidationTests(TestCase):
    def valid_claims(self, **changes):
        claims = {
            "sub": "google-subject-123",
            "email": "officer@psd401.net",
            "email_verified": True,
            "hd": "psd401.net",
            "given_name": "Pat",
            "family_name": "Officer",
        }
        claims.update(changes)
        return claims

    def test_valid_claims_return_the_immutable_subject_and_normalized_email(self):
        identity = validate_google_claims(self.valid_claims(), "psd401.net")

        self.assertEqual(identity.subject, "google-subject-123")
        self.assertEqual(identity.email, "officer@psd401.net")

    def test_domain_hint_is_not_treated_as_authorization(self):
        with self.assertRaises(PermissionDenied):
            validate_google_claims(self.valid_claims(hd="gmail.com"), "psd401.net")

    def test_unverified_email_and_missing_subject_are_rejected(self):
        for changes in ({"email_verified": False}, {"sub": ""}):
            with self.subTest(changes=changes), self.assertRaises(PermissionDenied):
                validate_google_claims(self.valid_claims(**changes), "psd401.net")

    def test_oidc_flow_uses_nonce_pkce_and_minimal_identity_scopes(self):
        self.assertTrue(settings.OIDC_USE_NONCE)
        self.assertTrue(settings.OIDC_USE_PKCE)
        self.assertTrue(settings.OIDC_VERIFY_SSL)
        # The timeout is a district setting now, read through the same hook
        # the library reads every other value through, so it is asserted
        # where it is answered rather than on a Django settings attribute
        # that no longer exists.
        self.assertEqual(runtime_oidc_setting("OIDC_TIMEOUT"), 10)
        self.assertEqual(settings.OIDC_RP_SCOPES, "openid email profile")
        self.assertFalse(settings.OIDC_STORE_ACCESS_TOKEN)
        self.assertFalse(settings.OIDC_STORE_ID_TOKEN)

    def test_only_the_two_required_oidc_routes_are_exposed(self):
        source = (settings.BASE_DIR / "apps" / "api" / "urls.py").read_text()

        self.assertIn('"auth/oidc/authenticate/"', source)
        self.assertIn('"auth/oidc/callback/"', source)
        self.assertNotIn('include("mozilla_django_oidc.urls")', source)
        self.assertNotIn('path("auth/oidc/logout/"', source)


class RoleMappingTests(TestCase):
    def test_external_group_names_are_admin_configurable(self):
        mapping = GoogleGroupRoleMapping.objects.create(
            role=StaffRole.RECORDS_OFFICER,
            google_group="district-prr-team@psd401.net",
        )

        self.assertEqual(mapping.role, StaffRole.RECORDS_OFFICER)
        self.assertEqual(mapping.google_group, "district-prr-team@psd401.net")

    def test_applying_roles_only_replaces_managed_role_groups(self):
        user = get_user_model().objects.create_user("mapped", is_staff=True)
        unrelated = Group.objects.create(name="unrelated")
        user.groups.add(unrelated)
        apply_staff_roles(user, {StaffRole.LIAISON, StaffRole.REVIEWER})

        self.assertSetEqual(
            set(user.groups.values_list("name", flat=True)),
            {"unrelated", "prr-role:liaison", "prr-role:reviewer"},
        )

    @override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
    def test_application_admin_can_configure_mappings_through_the_staff_api(self):
        user = get_user_model().objects.create_user("app-admin", is_staff=True)
        apply_staff_roles(user, {StaffRole.ADMIN})
        google_user = get_user_model().objects.create_user(
            "google-staff", email="google-staff@psd401.net", is_staff=True
        )
        identity = StaffIdentity.objects.create(
            user=google_user,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="google-staff-subject",
            last_group_check_at=timezone.now(),
        )
        self.client.force_login(user)

        response = self.client.post(
            "/api/staff/google-role-mappings/",
            {
                "role": StaffRole.LIAISON,
                "google_group": "district-records-liaisons@psd401.net",
                "active": True,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            AuditLog.objects.filter(action="identity.role_mapping_created", actor=user).exists()
        )
        identity.refresh_from_db()
        self.assertIsNone(identity.last_group_check_at)

    @override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
    def test_records_officer_cannot_change_identity_configuration(self):
        user = get_user_model().objects.create_user("officer-only", is_staff=True)
        apply_staff_roles(user, {StaffRole.RECORDS_OFFICER})
        self.client.force_login(user)

        response = self.client.get("/api/staff/google-role-mappings/")

        self.assertEqual(response.status_code, 403)

    @override_settings(
        PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
        PRR_GOOGLE_GROUP_ALLOWED_DOMAINS=("psd401.net",),
    )
    def test_external_group_cannot_become_an_authorization_source(self):
        user = get_user_model().objects.create_user("app-admin", is_staff=True)
        apply_staff_roles(user, {StaffRole.ADMIN})
        self.client.force_login(user)

        response = self.client.post(
            "/api/staff/google-role-mappings/",
            {
                "role": StaffRole.REVIEWER,
                "google_group": "attacker-controlled@example.org",
                "active": True,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(GoogleGroupRoleMapping.objects.exists())

    @override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
    def test_last_active_application_admin_mapping_cannot_be_deactivated(self):
        user = get_user_model().objects.create_user("app-admin", is_staff=True)
        apply_staff_roles(user, {StaffRole.ADMIN})
        mapping = GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="only-prr-admins@psd401.net",
        )
        self.client.force_login(user)

        response = self.client.patch(
            f"/api/staff/google-role-mappings/{mapping.pk}/",
            {"active": False},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        mapping.refresh_from_db()
        self.assertTrue(mapping.active)

    @override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
    def test_locked_update_rechecks_last_admin_after_validation(self):
        user = get_user_model().objects.create_user("app-admin", is_staff=True)
        apply_staff_roles(user, {StaffRole.ADMIN})
        target = GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="first-prr-admins@psd401.net",
        )
        other = GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="second-prr-admins@psd401.net",
        )
        serializer = GoogleGroupRoleMappingSerializer(
            target,
            data={"active": False},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        other.delete()
        view = GoogleGroupRoleMappingViewSet()
        view.request = SimpleNamespace(user=user)

        with patch(
            "apps.api.resources.identity_config._expire_google_authorizations"
        ):
            with self.assertRaisesMessage(
                serializers.ValidationError,
                "last active Application Admin mapping",
            ):
                view.perform_update(serializer)

        target.refresh_from_db()
        self.assertTrue(target.active)


class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeGroupsApi:
    def __init__(self, member_email):
        self.member_email = member_email
        self.membership_lookups = []

    def groups(self):
        return self

    def lookup(self, **kwargs):
        if "groupKey_id" in kwargs:
            return _FakeRequest({"name": f"groups/{kwargs['groupKey_id']}"})
        self.membership_lookups.append(kwargs)
        if kwargs["memberKey_id"] != self.member_email:
            raise AssertionError("unexpected test member")
        return _FakeRequest({"name": "groups/group/memberships/member"})

    def memberships(self):
        return self

    def list(self, **kwargs):
        raise AssertionError("authorization must not enumerate every group member")


class GoogleGroupDirectoryTests(TestCase):
    def test_only_direct_membership_in_configured_groups_grants_roles(self):
        GoogleGroupRoleMapping.objects.create(
            role=StaffRole.REVIEWER,
            google_group="custom-review@psd401.net",
        )
        api = _FakeGroupsApi("reviewer@psd401.net")

        roles = GoogleGroupDirectory(api=api).roles_for_email("reviewer@psd401.net")

        self.assertSetEqual(roles, {StaffRole.REVIEWER})
        self.assertEqual(
            api.membership_lookups,
            [{
                "parent": "groups/custom-review@psd401.net",
                "memberKey_id": "reviewer@psd401.net",
            }],
        )

    @override_settings(PRR_GOOGLE_GROUP_CREDENTIALS_FILE="/run/secrets/bad.json")
    @patch(
        "apps.api.identity.google.auth.load_credentials_from_file",
        side_effect=ValueError("malformed external account config"),
    )
    def test_malformed_runtime_credentials_fail_as_directory_unavailable(self, load):
        with self.assertRaises(GoogleDirectoryUnavailable):
            GoogleGroupDirectory()._api()


class GoogleUserProvisioningTests(TestCase):
    def claims(self, **changes):
        claims = {
            "sub": "immutable-google-sub",
            "email": "new.user@psd401.net",
            "email_verified": True,
            "hd": "psd401.net",
            "given_name": "New",
            "family_name": "User",
        }
        claims.update(changes)
        return claims

    @override_settings(PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
    def test_authorized_user_is_created_without_a_password(self):
        user = provision_google_user(self.claims(), {StaffRole.LIAISON})

        self.assertTrue(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.staff_identity.google_subject, "immutable-google-sub")
        self.assertEqual(user.staff_identity.source, StaffIdentity.Source.GOOGLE)
        self.assertTrue(user.groups.filter(name="prr-role:liaison").exists())

    @override_settings(PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
    def test_user_without_a_mapped_role_is_not_provisioned(self):
        with self.assertRaises(PermissionDenied):
            provision_google_user(self.claims(), set())

        self.assertFalse(get_user_model().objects.filter(email="new.user@psd401.net").exists())

    @override_settings(PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
    def test_existing_email_is_never_silently_linked(self):
        existing = get_user_model().objects.create_user(
            "existing", email="new.user@psd401.net", is_staff=True
        )
        StaffIdentity.objects.create(user=existing, source=StaffIdentity.Source.LOCAL)

        with self.assertRaises(PermissionDenied):
            provision_google_user(self.claims(), {StaffRole.RECORDS_OFFICER})

        existing.refresh_from_db()
        self.assertFalse(existing.staff_identity.google_subject)

    @override_settings(PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
    def test_preapproved_existing_account_can_be_linked_once(self):
        existing = get_user_model().objects.create_user(
            "existing", email="new.user@psd401.net", is_staff=True
        )
        StaffIdentity.objects.create(
            user=existing,
            source=StaffIdentity.Source.LOCAL,
            allow_next_google_link=True,
        )

        user = provision_google_user(self.claims(), {StaffRole.RECORDS_OFFICER})

        self.assertEqual(user, existing)
        user.staff_identity.refresh_from_db()
        self.assertEqual(user.staff_identity.google_subject, "immutable-google-sub")
        self.assertFalse(user.staff_identity.allow_next_google_link)


class StaffOIDCBackendTests(TestCase):
    @override_settings(PRR_GOOGLE_OIDC_CLIENT_ID="prr-client.apps.googleusercontent.com")
    @patch(
        "mozilla_django_oidc.auth.OIDCAuthenticationBackend.verify_token",
        return_value={
            "iss": "https://accounts.google.com",
            "aud": "prr-client.apps.googleusercontent.com",
            "sub": "subject",
        },
    )
    def test_id_token_must_be_issued_for_this_google_client(self, verify_token):
        payload = StaffOIDCBackend().verify_token("signed-token", nonce="nonce")

        self.assertEqual(payload["sub"], "subject")

    @override_settings(PRR_GOOGLE_OIDC_CLIENT_ID="prr-client.apps.googleusercontent.com")
    @patch(
        "mozilla_django_oidc.auth.OIDCAuthenticationBackend.verify_token",
        return_value={
            "iss": "https://accounts.google.com",
            "aud": "another-client.apps.googleusercontent.com",
            "sub": "subject",
        },
    )
    def test_id_token_for_another_client_is_rejected(self, verify_token):
        with self.assertRaises(SuspiciousOperation):
            StaffOIDCBackend().verify_token("signed-token", nonce="nonce")

    @override_settings(PRR_GOOGLE_OIDC_CLIENT_ID="prr-client.apps.googleusercontent.com")
    @patch(
        "mozilla_django_oidc.auth.OIDCAuthenticationBackend.verify_token",
        return_value={
            "iss": "https://attacker.example",
            "aud": "prr-client.apps.googleusercontent.com",
            "sub": "subject",
        },
    )
    def test_id_token_from_another_issuer_is_rejected(self, verify_token):
        with self.assertRaises(SuspiciousOperation):
            StaffOIDCBackend().verify_token("signed-token", nonce="nonce")

    @override_settings(PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
    @patch(
        "apps.api.identity.GoogleGroupDirectory.roles_for_email",
        return_value={StaffRole.REVIEWER},
    )
    def test_backend_provisions_only_after_group_authorization(self, roles_for_email):
        claims = {
            "sub": "backend-subject",
            "email": "backend.user@psd401.net",
            "email_verified": True,
            "hd": "psd401.net",
        }

        user = StaffOIDCBackend().get_or_create_user(None, None, claims)

        self.assertEqual(user.staff_identity.google_subject, "backend-subject")
        roles_for_email.assert_called_once_with("backend.user@psd401.net")

    @override_settings(PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
    @patch("apps.api.identity.GoogleGroupDirectory.roles_for_email", return_value=set())
    def test_backend_denies_a_domain_user_without_a_mapped_role(self, roles_for_email):
        claims = {
            "sub": "unauthorized-subject",
            "email": "not-prr-staff@psd401.net",
            "email_verified": True,
            "hd": "psd401.net",
        }

        with self.assertRaises(SuspiciousOperation):
            StaffOIDCBackend().get_or_create_user(None, None, claims)

    @override_settings(PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
    @patch("apps.api.identity.GoogleGroupDirectory.roles_for_email")
    def test_backend_rejects_untrusted_claims_before_any_directory_lookup(
        self, roles_for_email
    ):
        claims = {
            "sub": "attacker-subject",
            "email": "attacker@example.com",
            "email_verified": True,
            "hd": "example.com",
        }

        with self.assertRaises(SuspiciousOperation):
            StaffOIDCBackend().get_or_create_user(None, None, claims)

        roles_for_email.assert_not_called()


@override_settings(
    PRR_AUTH_MODE="google",
    PRR_GOOGLE_GROUP_RECHECK_SECONDS=60,
    PRR_GOOGLE_GROUP_MAX_STALE_SECONDS=120,
)
class StaffIdentityRevalidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "oidc-user", email="oidc-user@psd401.net", is_staff=True
        )
        self.identity = StaffIdentity.objects.create(
            user=self.user,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="subject",
            last_group_check_at=timezone.now() - timedelta(minutes=5),
        )
        apply_staff_roles(self.user, {StaffRole.LIAISON})
        self.request = RequestFactory().get("/api/staff/collection-tasks/")
        SessionMiddleware(lambda request: None).process_request(self.request)
        self.request.session.save()
        login(
            self.request,
            self.user,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        self.request.user = self.user

    @patch("apps.api.middleware.refresh_google_roles", return_value=set())
    def test_removed_group_access_logs_the_session_out(self, refresh):
        response = StaffIdentityRevalidationMiddleware(lambda request: None)(self.request)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.request.session.get("_auth_user_id"))
        refresh.assert_called_once_with(self.user)

    @patch(
        "apps.api.middleware.refresh_google_roles",
        side_effect=GoogleDirectoryUnavailable("directory unavailable"),
    )
    def test_stale_access_fails_closed_when_google_is_unavailable(self, refresh):
        response = StaffIdentityRevalidationMiddleware(lambda request: None)(self.request)

        self.assertEqual(response.status_code, 503)
        refresh.assert_called_once_with(self.user)


@override_settings(
    PRR_AUTH_MODE="google",
    PRR_GOOGLE_GROUP_RECHECK_SECONDS=60,
    PRR_GOOGLE_GROUP_MAX_STALE_SECONDS=120,
)
class StaffTokenRevalidationTests(TestCase):
    @patch("apps.api.identity.GoogleGroupDirectory.roles_for_email", return_value=set())
    def test_attributed_staff_token_cannot_outlive_google_group_removal(self, roles):
        user = get_user_model().objects.create_user(
            "token-user", email="token-user@psd401.net", is_staff=True
        )
        StaffIdentity.objects.create(
            user=user,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="token-subject",
            last_group_check_at=timezone.now() - timedelta(minutes=5),
        )
        apply_staff_roles(user, {StaffRole.LIAISON})
        token = Token.objects.create(user=user)

        response = self.client.get(
            "/api/staff/collection-tasks/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 403)
        roles.assert_called_once_with("token-user@psd401.net")


@override_settings(PRR_AUTH_MODE="google", PRR_GOOGLE_HOSTED_DOMAIN="psd401.net")
class DirectStaffRoleGrantTests(TestCase):
    """A named person who belongs to no syncable group still needs to work."""

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "grant-admin", "grant-admin@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})

    def grant(self, email, role, **kwargs):
        return DirectStaffRoleGrant.objects.create(
            email=email,
            role=role,
            reason=kwargs.pop("reason", "Not a member of any syncable group."),
            granted_by=self.admin,
            **kwargs,
        )

    def test_a_granted_person_in_no_group_can_sign_in(self):
        """The whole point: no group, no login before this existed."""
        self.grant("dana@psd401.net", StaffRole.ADMIN)

        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            return_value=set(),
        ):
            roles = resolved_roles_for_email("dana@psd401.net")

        self.assertEqual(roles, {StaffRole.ADMIN})

    def test_an_ungranted_person_in_no_group_is_still_denied(self):
        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            return_value=set(),
        ):
            self.assertEqual(resolved_roles_for_email("stranger@psd401.net"), set())

    def test_grants_add_to_group_roles_and_never_subtract(self):
        self.grant("both@psd401.net", StaffRole.ADMIN)

        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            return_value={StaffRole.LIAISON},
        ):
            roles = resolved_roles_for_email("both@psd401.net")

        self.assertEqual(roles, {StaffRole.LIAISON, StaffRole.ADMIN})

    def test_an_inactive_grant_confers_nothing(self):
        self.grant("former@psd401.net", StaffRole.ADMIN, active=False)

        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            return_value=set(),
        ):
            self.assertEqual(resolved_roles_for_email("former@psd401.net"), set())

    def test_a_google_outage_does_not_lock_out_a_directly_granted_person(self):
        """Their authority was never Google's to answer for."""
        self.grant("dana@psd401.net", StaffRole.ADMIN)

        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            side_effect=GoogleDirectoryUnavailable("directory down"),
        ):
            roles = resolved_roles_for_email("dana@psd401.net")

        self.assertEqual(roles, {StaffRole.ADMIN})

    def test_a_google_outage_still_fails_closed_for_everyone_else(self):
        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            side_effect=GoogleDirectoryUnavailable("directory down"),
        ):
            with self.assertRaises(GoogleDirectoryUnavailable):
                resolved_roles_for_email("group-only@psd401.net")

    def test_a_revoked_grant_removes_the_role_at_the_next_refresh(self):
        user = get_user_model().objects.create_user(
            "dana", "dana@psd401.net", "pw", is_staff=True
        )
        StaffIdentity.objects.create(
            user=user,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="google-dana",
        )
        record = self.grant("dana@psd401.net", StaffRole.ADMIN)

        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            return_value=set(),
        ):
            refresh_google_roles(user)
            self.assertEqual(staff_roles(user), {StaffRole.ADMIN})

            record.active = False
            record.save(update_fields=["active"])
            refresh_google_roles(user)

        self.assertEqual(staff_roles(user), set())

    def test_the_grant_email_must_be_in_the_district_domain(self):
        serializer = DirectStaffRoleGrantSerializer(data={
            "email": "outsider@example.org",
            "role": StaffRole.ADMIN,
            "reason": "Contractor who needs to look at the queue.",
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("email", serializer.errors)

    def test_the_grant_requires_a_substantive_written_reason(self):
        serializer = DirectStaffRoleGrantSerializer(data={
            "email": "dana@psd401.net",
            "role": StaffRole.ADMIN,
            "reason": "needed",
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("reason", serializer.errors)

    def test_the_last_admin_cannot_be_removed_from_either_source(self):
        """Lockout protection has to count both ways in, not just groups."""
        record = self.grant("only-admin@psd401.net", StaffRole.ADMIN)

        serializer = DirectStaffRoleGrantSerializer(
            record,
            data={"active": False},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())

        # A group mapping is an equally valid admin, so with one present the
        # same grant may be retired.
        GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="prr-admin@psd401.net",
            active=True,
        )
        allowed = DirectStaffRoleGrantSerializer(
            record,
            data={"active": False},
            partial=True,
        )
        self.assertTrue(allowed.is_valid(), allowed.errors)

    def test_a_grant_alone_can_hold_the_last_admin_mapping_open(self):
        """The converse: a grant must count when the last mapping is deleted."""
        mapping = GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="prr-admin@psd401.net",
            active=True,
        )
        self.grant("standing-admin@psd401.net", StaffRole.ADMIN)

        serializer = GoogleGroupRoleMappingSerializer(
            mapping,
            data={"active": False},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


def sessions_for(user):
    return sum(
        1
        for session in Session.objects.all()
        if str(session.get_decoded().get("_auth_user_id")) == str(user.pk)
    )


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class GrantChangeKeepsTheActingAdminSignedInTests(TestCase):
    """Saving several roles for a person is several grant writes in a row.

    Every write expires Google authorizations so changed roles take effect
    now. That used to delete the acting admin's own session too, so the
    second write of one save failed with "Authentication credentials were not
    provided." and the admin was signed out mid-edit.
    """

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            "avery", "avery@psd401.net", is_staff=True
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        StaffIdentity.objects.create(
            user=self.admin,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="admin-subject",
            last_group_check_at=timezone.now(),
        )
        self.other = User.objects.create_user(
            "other", "other@psd401.net", is_staff=True
        )
        apply_staff_roles(self.other, {StaffRole.LIAISON})
        StaffIdentity.objects.create(
            user=self.other,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="other-subject",
            last_group_check_at=timezone.now(),
        )

    def post_grant(self, role):
        return self.client.post(
            "/api/staff/direct-role-grants/",
            {
                "email": "dana@psd401.net",
                "role": role,
                "reason": "District Records Officer; same access as the administrator.",
                "active": True,
            },
            content_type="application/json",
        )

    def test_consecutive_grant_writes_succeed_in_one_session(self):
        other_client = self.client_class()
        other_client.force_login(self.other)
        self.client.force_login(self.admin)

        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            side_effect=lambda email: (
                {StaffRole.ADMIN} if email == "avery@psd401.net" else set()
            ),
        ):
            first = self.post_grant(StaffRole.LIAISON)
            second = self.post_grant(StaffRole.REVIEWER)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(
            set(
                DirectStaffRoleGrant.objects.filter(
                    email="dana@psd401.net"
                ).values_list("role", flat=True)
            ),
            {StaffRole.LIAISON, StaffRole.REVIEWER},
        )
        # Everyone else is still signed out so their roles apply now.
        self.assertEqual(sessions_for(self.other), 0)
        self.assertEqual(sessions_for(self.admin), 1)

    def test_the_acting_admin_is_still_rechecked_on_the_next_request(self):
        self.client.force_login(self.admin)

        with patch(
            "apps.api.identity.GoogleGroupDirectory.roles_for_email",
            return_value={StaffRole.ADMIN},
        ):
            self.post_grant(StaffRole.LIAISON)

        self.admin.staff_identity.refresh_from_db()
        # Cleared, so the revalidation middleware recomputes their roles.
        self.assertIsNone(self.admin.staff_identity.last_group_check_at)


@override_settings(
    PRR_AUTH_MODE="google",
    PRR_GOOGLE_OIDC_CLIENT_ID="prr-client.apps.googleusercontent.com",
)
class GoogleSignInReturnPathTests(TestCase):
    """A deep link survives the Google round trip; an off-site one does not."""

    def test_same_site_next_is_kept_for_the_callback(self):
        response = self.client.get(
            "/api/staff/auth/oidc/authenticate/",
            {"next": "/work/42?tab=records"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.session["oidc_login_next"], "/work/42?tab=records"
        )

    def test_off_site_next_is_dropped(self):
        self.client.get(
            "/api/staff/auth/oidc/authenticate/",
            {"next": "https://attacker.example/phish"},
        )

        self.assertIsNone(self.client.session["oidc_login_next"])
