# ABOUTME: Phase C — write-only secrets, encryption at rest, lockout
# ABOUTME: protection, live credential checks, and runtime sign-in behaviour.
"""What Phase C has to be true about, in the order it matters.

A secret that can be read back is not a secret; a sign-in change that
strands every administrator cannot be undone; a credential that fails
silently is worse than one that fails loudly. Each of those is a class
below.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from django.conf import settings as django_settings
from django.contrib.auth import authenticate, get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.models import DirectStaffRoleGrant
from apps.audit.models import AuditLog
from apps.configuration import runtime, secrets, services, signin, values
from apps.configuration.models import AppSecret, AppSettingChange
from apps.configuration.registry import SECRET_KEYS, SETTINGS_BY_KEY

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

REASON = "District rotated the credential after the annual access review."

#: A sign-in world in which Google works: a client, a secret, and an admin in
#: the accepted domain. Tests that change one thing start from here.
GOOGLE_SIGN_IN = override_settings(
    PRR_AUTH_MODE="google",
    PRR_GOOGLE_OIDC_CLIENT_ID="prr-client.apps.googleusercontent.com",
    PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
)


def override_env(**values):
    """A deployed environment, for the pre-import bootstrap path.

    A secret has no Django settings attribute to override: the bootstrap
    reads the process environment, because that is where a deployment keeps
    it before anything is stored.
    """
    return patch.dict(os.environ, values, clear=False)


def _admin(username="app-admin", email="app-admin@psd401.net", **extra):
    user = get_user_model().objects.create_user(
        username, email, "district-password", is_staff=True, **extra
    )
    apply_staff_roles(user, {StaffRole.ADMIN})
    return user


class SecretStorageTests(TestCase):
    """A stored secret is encrypted, unreadable, and identifiable."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = _admin()

    def test_the_stored_row_does_not_contain_the_value(self):
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-super-secret-value",
            actor=self.admin,
            reason=REASON,
        )
        row = AppSecret.objects.get(key="PRR_GOOGLE_OIDC_CLIENT_SECRET")

        self.assertNotIn("GOCSPX-super-secret-value", row.ciphertext)
        self.assertNotIn("GOCSPX-super-secret-value", row.fingerprint)

    def test_a_database_dump_alone_reveals_nothing(self):
        """The whole requirement, stated as a test.

        Everything the database holds about this secret is the ciphertext and
        the fingerprint. Neither opens without the key, which lives in the
        deployment's environment and is in no backup of the database.
        """
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-super-secret-value",
            actor=self.admin,
            reason=REASON,
        )
        dumped = "".join(
            f"{row.ciphertext}{row.fingerprint}" for row in AppSecret.objects.all()
        ) + "".join(
            f"{change.old_value}{change.new_value}{change.reason}"
            for change in AppSettingChange.objects.all()
        )

        self.assertNotIn("GOCSPX-super-secret-value", dumped)

    def test_the_value_round_trips_for_the_application(self):
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-super-secret-value",
            actor=self.admin,
            reason=REASON,
        )

        self.assertEqual(
            runtime.get_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET"),
            "GOCSPX-super-secret-value",
        )

    def test_a_secret_cannot_be_read_through_the_ordinary_read_path(self):
        with self.assertRaises(ValueError):
            runtime.get("PRR_GOOGLE_OIDC_CLIENT_SECRET")

    def test_a_secret_cannot_be_written_through_the_ordinary_write_path(self):
        with self.assertRaises(ValidationError):
            services.set_value(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                "GOCSPX-would-be-plaintext",
                actor=self.admin,
                reason=REASON,
            )
        self.assertFalse(AppSecret.objects.exists())

    def test_a_secret_has_no_json_representation(self):
        spec = SETTINGS_BY_KEY["PRR_GOOGLE_OIDC_CLIENT_SECRET"]

        with self.assertRaises(ValueError):
            values.to_json(spec, "GOCSPX-would-be-plaintext")

    def test_the_history_records_the_change_and_not_the_value(self):
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-first",
            actor=self.admin,
            reason=REASON,
        )
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-second",
            actor=self.admin,
            reason=REASON,
        )
        changes = list(
            AppSettingChange.objects.filter(key="PRR_GOOGLE_OIDC_CLIENT_SECRET")
        )

        self.assertEqual(len(changes), 2)
        latest = changes[0]
        self.assertTrue(latest.new_value["set"])
        self.assertTrue(latest.old_value["set"])
        self.assertNotEqual(
            latest.new_value["fingerprint"], latest.old_value["fingerprint"]
        )
        self.assertEqual(latest.actor, self.admin)
        self.assertEqual(latest.reason, REASON)

    def test_clearing_a_secret_is_its_own_recorded_change(self):
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-first",
            actor=self.admin,
            reason=REASON,
        )
        with GOOGLE_SIGN_IN:
            # Clearing the client secret would lock everyone out of Google
            # sign-in, so the break-glass door has to be open for it.
            services.set_value(
                "PRR_ENABLE_ADMIN", True, actor=self.admin, reason=REASON
            )
            self.admin.is_superuser = True
            self.admin.save(update_fields=["is_superuser"])
            services.set_value(
                "PRR_BREAK_GLASS_PASSWORD_AUTH",
                True,
                actor=self.admin,
                reason=REASON,
            )
            services.set_secret(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                "",
                actor=self.admin,
                reason=REASON,
            )

        self.assertEqual(runtime.get_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET"), "")
        latest = AppSettingChange.objects.filter(
            key="PRR_GOOGLE_OIDC_CLIENT_SECRET"
        ).first()
        self.assertFalse(latest.new_value["set"])

    def test_clearing_a_secret_does_not_resurrect_the_deployed_one(self):
        """The bug a deleted row would have caused.

        A key with no row resolves to what the deployment is running, which
        is the whole point of the bootstrap -- and on the deployment that
        matters, that is the very credential the administrator just revoked.
        Clearing has to leave a record that says "cleared", not an absence
        that says "nobody has decided yet".
        """
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-stored",
            actor=self.admin,
            reason=REASON,
        )
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "",
            actor=self.admin,
            reason=REASON,
        )

        with override_env(PRR_GOOGLE_OIDC_CLIENT_SECRET="GOCSPX-still-deployed"):
            self.assertEqual(
                runtime.get_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET"), ""
            )

    def test_clearing_a_secret_twice_is_refused(self):
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-stored",
            actor=self.admin,
            reason=REASON,
        )
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET", "", actor=self.admin, reason=REASON
        )

        with self.assertRaises(ValidationError):
            services.set_secret(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                "",
                actor=self.admin,
                reason=REASON,
            )

    def test_an_unset_secret_still_reads_the_deployed_value(self):
        """The other half of the same rule: no row means no decision yet."""
        with override_env(PRR_GOOGLE_OIDC_CLIENT_SECRET="GOCSPX-still-deployed"):
            self.assertEqual(
                runtime.get_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET"),
                "GOCSPX-still-deployed",
            )

    def test_setting_the_same_value_again_is_refused(self):
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-same",
            actor=self.admin,
            reason=REASON,
        )
        with self.assertRaises(ValidationError):
            services.set_secret(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                "GOCSPX-same",
                actor=self.admin,
                reason=REASON,
            )

    def test_a_reason_is_required(self):
        with self.assertRaises(ValidationError):
            services.set_secret(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                "GOCSPX-value",
                actor=self.admin,
                reason="why",
            )

    def test_an_over_long_secret_is_refused_without_echoing_it(self):
        too_long = "G" * 256
        with self.assertRaises(ValidationError) as raised:
            services.set_secret(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                too_long,
                actor=self.admin,
                reason=REASON,
            )

        self.assertNotIn(too_long, " ".join(raised.exception.messages))
        self.assertFalse(AppSecret.objects.exists())

    def test_only_an_application_admin_may_set_one(self):
        officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})

        with self.assertRaises(PermissionDenied):
            services.set_secret(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                "GOCSPX-value",
                actor=officer,
                reason=REASON,
            )
        self.assertFalse(AppSecret.objects.exists())

    def test_the_change_is_audited_without_the_value(self):
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-audited",
            actor=self.admin,
            reason=REASON,
        )
        entry = AuditLog.objects.get(action="configuration.secret_changed")

        self.assertNotIn("GOCSPX-audited", str(entry.detail))
        self.assertEqual(entry.actor, self.admin)

    def test_an_unreadable_ciphertext_fails_closed(self):
        """A rotated key is the honest cause, and the honest answer is "unset".

        Raising here would break sign-in with a stack trace instead of the
        refusal an administrator can act on, and pretending the old value is
        still installed would be worse than either.
        """
        services.set_secret(
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "GOCSPX-value",
            actor=self.admin,
            reason=REASON,
        )
        AppSecret.objects.filter(key="PRR_GOOGLE_OIDC_CLIENT_SECRET").update(
            ciphertext="not-a-fernet-token"
        )

        self.assertEqual(runtime.get_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET"), "")


class EncryptionKeyTests(TestCase):
    def test_a_malformed_key_reads_every_secret_as_unset(self):
        """A typo in PRR_SETTINGS_ENCRYPTION_KEY must fail closed like a
        rotated key does, not turn every sign-in into a server error."""
        cache.clear()
        AppSecret.objects.create(
            key="PRR_GOOGLE_OIDC_CLIENT_SECRET",
            ciphertext=secrets.encrypt("GOCSPX-value"),
            fingerprint=secrets.fingerprint("GOCSPX-value"),
        )

        with override_settings(PRR_SETTINGS_ENCRYPTION_KEY="not-a-fernet-key"):
            self.assertEqual(
                runtime.get_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET"), ""
            )
            self.assertFalse(
                runtime.secret_is_set("PRR_GOOGLE_OIDC_CLIENT_SECRET")
            )

    def test_an_explicit_key_and_a_derived_key_are_both_usable(self):
        derived = secrets.encrypt("value")
        self.assertEqual(secrets.decrypt(derived), "value")

        explicit = secrets.Fernet.generate_key().decode("ascii")
        with override_settings(PRR_SETTINGS_ENCRYPTION_KEY=explicit):
            under_explicit = secrets.encrypt("value")
            self.assertEqual(secrets.decrypt(under_explicit), "value")

        self.assertNotEqual(derived, under_explicit)

    def test_a_secret_written_under_one_key_does_not_open_under_another(self):
        ciphertext = secrets.encrypt("value")
        other = secrets.Fernet.generate_key().decode("ascii")

        with override_settings(PRR_SETTINGS_ENCRYPTION_KEY=other):
            with self.assertRaises(secrets.SecretUnreadable):
                secrets.decrypt(ciphertext)

    def test_the_deployed_key_reaches_the_settings_module(self):
        """The documented operator control: a key in ``.env.app`` is read.
        ``override_settings`` alone would pass even if nothing read it."""
        key = secrets.Fernet.generate_key().decode("ascii")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import config.settings as s; "
                    "print(s.PRR_SETTINGS_ENCRYPTION_KEY)"
                ),
            ],
            cwd=django_settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "PRR_SETTINGS_ENCRYPTION_KEY": key,
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), key)

    def test_the_fingerprint_identifies_a_value_without_revealing_it(self):
        self.assertEqual(secrets.fingerprint("a"), secrets.fingerprint("a"))
        self.assertNotEqual(secrets.fingerprint("a"), secrets.fingerprint("b"))
        self.assertNotIn("a", secrets.fingerprint("a"))

    def test_the_fingerprint_is_key_bound(self):
        """A plain hash of a short secret is guessable by whoever holds the
        database. An HMAC under the encryption key is not."""
        under_derived = secrets.fingerprint("GOCSPX-value")
        other = secrets.Fernet.generate_key().decode("ascii")

        with override_settings(PRR_SETTINGS_ENCRYPTION_KEY=other):
            self.assertNotEqual(secrets.fingerprint("GOCSPX-value"), under_derived)


def _as_google_admin(user):
    """An admin the way Google sign-in makes one: the managed admin group
    written by a Google login, with an admin group mapping to write it."""
    from apps.api.models import GoogleGroupRoleMapping, StaffIdentity

    GoogleGroupRoleMapping.objects.get_or_create(
        role=StaffRole.ADMIN, google_group="prr-admins@psd401.net"
    )
    StaffIdentity.objects.update_or_create(
        user=user,
        defaults={
            "source": StaffIdentity.Source.GOOGLE,
            "google_subject": f"google-sub-{user.pk}",
        },
    )
    return user


class LockoutProtectionTests(TestCase):
    """The one refusal that exists because the mistake cannot be undone."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = _as_google_admin(_admin())

    def _install_client_secret(self):
        AppSecret.objects.create(
            key="PRR_GOOGLE_OIDC_CLIENT_SECRET",
            ciphertext=secrets.encrypt("GOCSPX-value"),
            fingerprint=secrets.fingerprint("GOCSPX-value"),
        )

    @GOOGLE_SIGN_IN
    def test_a_domain_no_administrator_is_in_is_refused(self):
        self._install_client_secret()

        with self.assertRaises(ValidationError) as raised:
            services.set_value(
                "PRR_GOOGLE_HOSTED_DOMAIN",
                "someone-elses-district.org",
                actor=self.admin,
                reason=REASON,
            )

        self.assertIn("nobody able to sign in", " ".join(raised.exception.messages))
        self.assertIn(
            "someone-elses-district.org", " ".join(raised.exception.messages)
        )

    @GOOGLE_SIGN_IN
    def test_clearing_the_sign_in_client_is_refused(self):
        self._install_client_secret()

        with self.assertRaises(ValidationError):
            services.set_value(
                "PRR_GOOGLE_OIDC_CLIENT_ID",
                "",
                actor=self.admin,
                reason=REASON,
            )

    @GOOGLE_SIGN_IN
    def test_clearing_the_client_secret_is_refused(self):
        self._install_client_secret()

        with self.assertRaises(ValidationError):
            services.set_secret(
                "PRR_GOOGLE_OIDC_CLIENT_SECRET",
                "",
                actor=self.admin,
                reason=REASON,
            )
        self.assertTrue(AppSecret.objects.exists())

    def _local_mode_with_google_configured(self):
        """Local sign-in today, Google fully configured and ready to switch
        to. The only thing left to decide is who Google would let in."""
        self._install_client_secret()
        return override_settings(
            PRR_AUTH_MODE="local",
            PRR_GOOGLE_OIDC_CLIENT_ID="prr-client.apps.googleusercontent.com",
            PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
        )

    def _switch_to_google(self, actor):
        return services.set_value(
            "PRR_AUTH_MODE", "google", actor=actor, reason=REASON
        )

    def test_a_sign_in_mode_outside_the_two_choices_is_refused(self):
        with self.assertRaises(ValidationError):
            services.set_value(
                "PRR_AUTH_MODE", "saml", actor=self.admin, reason=REASON
            )

    def test_a_superuser_alone_is_not_a_way_in_through_google(self):
        """Google sign-in resolves roles from Workspace groups and direct
        grants at every login. ``is_superuser`` plays no part, so a superuser
        with neither is refused at the Google screen."""
        apply_staff_roles(self.admin, {StaffRole.RECORDS_OFFICER})
        superuser = get_user_model().objects.create_user(
            "root", "root@psd401.net", "district-password",
            is_staff=True, is_superuser=True,
        )
        with self._local_mode_with_google_configured():
            with self.assertRaises(ValidationError) as raised:
                self._switch_to_google(superuser)

        self.assertIn("nobody able to sign in", " ".join(raised.exception.messages))

    def test_a_locally_granted_admin_group_is_not_a_way_in_through_google(self):
        """The managed admin group is rewritten from Google at each login. A
        membership that Google never granted is not what Google will grant."""
        from apps.api.models import StaffIdentity

        StaffIdentity.objects.filter(user=self.admin).update(
            source=StaffIdentity.Source.LOCAL, google_subject=None
        )
        with self._local_mode_with_google_configured():
            with self.assertRaises(ValidationError):
                self._switch_to_google(self.admin)

    def test_an_admin_whose_role_came_from_a_mapped_google_group_counts(self):
        with self._local_mode_with_google_configured():
            self._switch_to_google(self.admin)

        self.assertEqual(runtime.get("PRR_AUTH_MODE"), "google")

    def test_a_mapped_group_that_was_removed_no_longer_counts(self):
        from apps.api.models import GoogleGroupRoleMapping

        GoogleGroupRoleMapping.objects.all().delete()
        with self._local_mode_with_google_configured():
            with self.assertRaises(ValidationError):
                self._switch_to_google(self.admin)

    def test_a_direct_admin_grant_counts_as_a_way_in_through_google(self):
        from apps.api.models import GoogleGroupRoleMapping

        GoogleGroupRoleMapping.objects.all().delete()
        DirectStaffRoleGrant.objects.create(
            email="app-admin@psd401.net",
            role=StaffRole.ADMIN,
            reason="Named Application Admin outside any Workspace group.",
            granted_by=self.admin,
        )
        with self._local_mode_with_google_configured():
            self._switch_to_google(self.admin)

        self.assertEqual(runtime.get("PRR_AUTH_MODE"), "google")

    @GOOGLE_SIGN_IN
    def test_break_glass_makes_the_otherwise_refused_change_possible(self):
        """The escape hatch is what turns a refusal into a decision."""
        self._install_client_secret()
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_superuser"])
        services.set_value(
            "PRR_ENABLE_ADMIN", True, actor=self.admin, reason=REASON
        )
        services.set_value(
            "PRR_BREAK_GLASS_PASSWORD_AUTH", True, actor=self.admin, reason=REASON
        )

        services.set_value(
            "PRR_GOOGLE_HOSTED_DOMAIN",
            "someone-elses-district.org",
            actor=self.admin,
            reason=REASON,
        )

        self.assertEqual(
            runtime.get("PRR_GOOGLE_HOSTED_DOMAIN"), "someone-elses-district.org"
        )

    @GOOGLE_SIGN_IN
    def test_turning_break_glass_off_is_refused_while_google_is_broken(self):
        """The asymmetry Phase A established does not hold here.

        A prerequisite is asked only on enable. This is asked in both
        directions, because turning the last working door off is exactly the
        change that strands everyone.
        """
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_superuser"])
        services.set_value(
            "PRR_ENABLE_ADMIN", True, actor=self.admin, reason=REASON
        )
        services.set_value(
            "PRR_BREAK_GLASS_PASSWORD_AUTH", True, actor=self.admin, reason=REASON
        )
        # No client secret was ever installed, so Google sign-in cannot work.

        with self.assertRaises(ValidationError):
            services.set_value(
                "PRR_BREAK_GLASS_PASSWORD_AUTH",
                False,
                actor=self.admin,
                reason=REASON,
            )

    @GOOGLE_SIGN_IN
    def test_a_direct_grant_counts_before_its_holder_has_an_account(self):
        """Handing the district to a new administrator is not a lockout.

        Provisioning is just-in-time: the grant is what decides, so refusing
        until the person has signed in would refuse the change that lets
        them.
        """
        self._install_client_secret()
        DirectStaffRoleGrant.objects.create(
            email="new-admin@newdistrict.org",
            role=StaffRole.ADMIN,
            reason="Incoming records officer with no syncable group.",
            granted_by=self.admin,
        )

        services.set_value(
            "PRR_GOOGLE_HOSTED_DOMAIN",
            "newdistrict.org",
            actor=self.admin,
            reason=REASON,
        )

        self.assertEqual(runtime.get("PRR_GOOGLE_HOSTED_DOMAIN"), "newdistrict.org")

    @GOOGLE_SIGN_IN
    def test_a_working_change_is_allowed(self):
        self._install_client_secret()

        services.set_value(
            "PRR_GOOGLE_OIDC_CLIENT_ID",
            "rotated-client.apps.googleusercontent.com",
            actor=self.admin,
            reason=REASON,
        )

        self.assertEqual(
            runtime.get("PRR_GOOGLE_OIDC_CLIENT_ID"),
            "rotated-client.apps.googleusercontent.com",
        )

    def test_a_non_sign_in_setting_is_not_subject_to_the_rule(self):
        services.set_value(
            "PRR_OFFICE_CONVERTER_BINARY",
            "/usr/local/bin/soffice",
            actor=self.admin,
            reason=REASON,
        )

        self.assertEqual(
            runtime.get("PRR_OFFICE_CONVERTER_BINARY"), "/usr/local/bin/soffice"
        )


class DeployTimeSignInTests(TestCase):
    """The system checks that failed a deploy with no working sign-in are
    gone, because those values now live in the store. The import says it
    instead, on the deploy that would come up with nobody able to sign in."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _as_google_admin(_admin())

    @override_settings(
        PRR_AUTH_MODE="google",
        PRR_GOOGLE_OIDC_CLIENT_ID="",
        PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
    )
    def test_the_import_reports_a_deployment_nobody_can_sign_in_to(self):
        with self.assertLogs("apps.configuration.signin", "ERROR") as logged:
            call_command("import_environment_settings", verbosity=0)

        self.assertIn("nobody able to sign in", "\n".join(logged.output))

    @GOOGLE_SIGN_IN
    def test_the_import_is_quiet_when_sign_in_works(self):
        with override_env(PRR_GOOGLE_OIDC_CLIENT_SECRET="GOCSPX-value"):
            with self.assertNoLogs("apps.configuration.signin", "ERROR"):
                call_command("import_environment_settings", verbosity=0)


class BreakGlassSignInTests(TestCase):
    """The password door answers to the store, not to the boot."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = _admin(is_superuser=True)

    @override_settings(PRR_AUTH_MODE="local")
    def test_an_unreadable_store_closes_the_password_door(self):
        """Even in local mode: a gate that cannot be read is a shut gate."""
        from apps.api.auth_backends import GatedPasswordBackend

        with patch.object(runtime, "get", side_effect=RuntimeError("store down")):
            with self.assertLogs("apps.api.auth_backends", "ERROR"):
                self.assertIsNone(
                    GatedPasswordBackend().authenticate(
                        None, username="app-admin", password="district-password"
                    )
                )

    @override_settings(
        PRR_AUTH_MODE="google", PRR_BREAK_GLASS_PASSWORD_AUTH=False
    )
    def test_a_password_is_refused_while_break_glass_is_off(self):
        self.assertIsNone(
            authenticate(username="app-admin", password="district-password")
        )

    @override_settings(
        PRR_AUTH_MODE="google", PRR_BREAK_GLASS_PASSWORD_AUTH=True
    )
    def test_a_password_works_while_break_glass_is_on(self):
        self.assertEqual(
            authenticate(username="app-admin", password="district-password"),
            self.admin,
        )

    @override_settings(PRR_AUTH_MODE="local")
    def test_local_sign_in_accepts_a_password_without_break_glass(self):
        self.assertEqual(
            authenticate(username="app-admin", password="district-password"),
            self.admin,
        )

    @override_settings(
        PRR_AUTH_MODE="google",
        PRR_BREAK_GLASS_PASSWORD_AUTH=True,
        PRR_GOOGLE_OIDC_CLIENT_ID="",
    )
    def test_break_glass_survives_a_broken_oidc_configuration(self):
        """The point of the door: a wrong OAuth client does not close it."""
        self.assertEqual(
            authenticate(username="app-admin", password="district-password"),
            self.admin,
        )


class SignInRouteTests(TestCase):
    """Both doors are wired; the store decides which one answers."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(PRR_AUTH_MODE="local")
    def test_local_mode_answers_the_local_login_and_hides_the_oidc_routes(self):
        self.assertNotEqual(
            self.client.post(
                "/api/staff/auth/login",
                {"username": "nobody", "password": "wrong"},
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/staff/auth/oidc/authenticate/").status_code, 404
        )

    @override_settings(PRR_AUTH_MODE="google")
    def test_google_mode_hides_the_local_login(self):
        self.assertEqual(
            self.client.post(
                "/api/staff/auth/login",
                {"username": "nobody", "password": "wrong"},
                content_type="application/json",
            ).status_code,
            404,
        )


class AdminRouteTests(TestCase):
    @override_settings(PRR_ENABLE_ADMIN=False)
    def test_the_admin_route_is_absent_while_the_switch_is_off(self):
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        self.assertEqual(self.client.get("/admin/login/").status_code, 404)

    @override_settings(PRR_ENABLE_ADMIN=True)
    def test_the_admin_route_answers_while_the_switch_is_on(self):
        self.assertNotEqual(self.client.get("/admin/login/").status_code, 404)


class VerificationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = _admin()

    def _save_vault_identity(self):
        # The live check runs after commit, so a slow provider cannot hold a
        # write transaction open. In a TestCase that means running the
        # callbacks explicitly.
        with self.captureOnCommitCallbacks(execute=True):
            services.set_value(
                "PRR_VAULT_IMPERSONATION_TARGET",
                "prr-vault@example.iam.gserviceaccount.com",
                actor=self.admin,
                reason=REASON,
            )

    def test_a_credential_that_cannot_authenticate_is_saved_but_unverified(self):
        self._save_vault_identity()

        self.assertEqual(
            runtime.get("PRR_VAULT_IMPERSONATION_TARGET"),
            "prr-vault@example.iam.gserviceaccount.com",
        )
        state = services.verification.state_for(
            ["PRR_VAULT_IMPERSONATION_TARGET"]
        )["PRR_VAULT_IMPERSONATION_TARGET"]
        self.assertIsNotNone(state)
        self.assertFalse(state["ok"])
        self.assertTrue(state["detail"])

    def test_the_recorded_detail_is_a_sentence_and_not_a_traceback(self):
        self._save_vault_identity()
        detail = services.verification.state_for(
            ["PRR_VAULT_IMPERSONATION_TARGET"]
        )["PRR_VAULT_IMPERSONATION_TARGET"]["detail"]

        self.assertNotIn("Traceback", detail)
        self.assertLessEqual(len(detail), 500)

    def test_a_setting_with_no_live_check_records_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            services.set_value(
                "PRR_OFFICE_CONVERTER_BINARY",
                "/usr/local/bin/soffice",
                actor=self.admin,
                reason=REASON,
            )

        self.assertIsNone(
            services.verification.state_for(["PRR_OFFICE_CONVERTER_BINARY"])[
                "PRR_OFFICE_CONVERTER_BINARY"
            ]
        )


class RegistryContractTests(TestCase):
    def test_the_secrets_are_the_sign_in_secret_and_the_alta_password(self):
        """A key file path is not a secret, and neither is an identity.

        Deployed Google access is keyless, so the values this system holds
        that would be dangerous to read back are the OAuth client secret and
        the Alta service account's password. Anything else added to this
        tuple needs the same write-only treatment, which is why the tuple is
        asserted rather than assumed.
        """
        self.assertEqual(
            set(SECRET_KEYS), {"PRR_GOOGLE_OIDC_CLIENT_SECRET", "ALTA_API_PASSWORD"}
        )

    def test_no_secret_carries_a_default(self):
        for key in SECRET_KEYS:
            self.assertEqual(SETTINGS_BY_KEY[key].default, "", key)

    def test_break_glass_requires_the_admin_route(self):
        """api.E010, as a registry dependency instead of a startup check."""
        self.assertIn(
            "PRR_ENABLE_ADMIN",
            SETTINGS_BY_KEY["PRR_BREAK_GLASS_PASSWORD_AUTH"].requires,
        )

    def test_every_phase_c_key_is_registered(self):
        for key in (
            "PRR_GMAIL_IMPERSONATION_TARGET",
            "PRR_GMAIL_CREDENTIALS_FILE",
            "PRR_VAULT_IMPERSONATION_TARGET",
            "PRR_VAULT_ADMIN_USER",
            "PRR_VAULT_CREDENTIALS_FILE",
            "PRR_GOOGLE_GROUP_CREDENTIALS_FILE",
            "PRR_AUTH_MODE",
            "PRR_GOOGLE_OIDC_CLIENT_ID",
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "PRR_GOOGLE_HOSTED_DOMAIN",
            "PRR_GOOGLE_OIDC_TIMEOUT_SECONDS",
            "PRR_ENABLE_ADMIN",
            "PRR_BREAK_GLASS_PASSWORD_AUTH",
            "PRR_CLOUDWATCH_REGION",
            "PRR_OFFICE_CONVERTER_BINARY",
        ):
            self.assertIn(key, SETTINGS_BY_KEY, key)

    def test_every_sign_in_key_is_covered_by_the_lockout_rule(self):
        """A new sign-in setting that skipped the rule would be the bug."""
        sign_in_keys = {
            spec.key
            for spec in SETTINGS_BY_KEY.values()
            if spec.category == "Staff sign-in"
        }

        self.assertEqual(sign_in_keys - signin.SIGN_IN_KEYS, set())

    def test_no_production_module_reads_a_secret_at_import_time(self):
        """A secret resolved at import is a secret that never rotates."""
        findings = []
        for path in sorted(
            (REPOSITORY_ROOT / "apps").rglob("*.py")
        ) + sorted((REPOSITORY_ROOT / "config").rglob("*.py")):
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text())
            for node in tree.body:
                # A def is not import-time work; only what runs while the
                # module is being imported can freeze a secret.
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "get_secret"
                    ):
                        findings.append(  # noqa: PERF401 - reads clearer as a loop
                            f"{path.relative_to(REPOSITORY_ROOT)}:{inner.lineno}"
                        )
        self.assertEqual(sorted(set(findings)), [])
