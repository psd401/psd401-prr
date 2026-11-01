# ABOUTME: PostgreSQL race coverage for the settings store's first write to a
# ABOUTME: key: the deploy-time import and an administrator's change collide.
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.models import GoogleGroupRoleMapping, StaffIdentity
from apps.configuration import runtime, secrets, services, signin
from apps.configuration.models import AppSecret, AppSetting, AppSettingChange

KEY = "PRR_CLOUDWATCH_METRICS_ENABLED"


@skipUnless(
    connection.vendor == "postgresql",
    "First-write races require PostgreSQL's concurrent unique-index behaviour.",
)
class FirstWriteRaceTests(TransactionTestCase):
    """Neither side of a first-write collision may crash on the unique key.

    ``set_value`` locks an existing row, but a key with no row yet has
    nothing to lock, so the import and an administrator's first change can
    both decide to insert.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = get_user_model().objects.create_user(
            "race-admin", "race-admin@psd401.net", "pw", is_staff=True
        )

    def _in_thread(self, work):
        close_old_connections()
        try:
            return work()
        except ValidationError as error:
            return error
        finally:
            close_old_connections()

    def test_an_import_that_commits_first_refuses_the_change_cleanly(self):
        import_done = threading.Event()
        original = runtime.get_uncached

        def admin_read_then_wait(key):
            value = original(key)
            import_done.wait(timeout=10)
            return value

        def change():
            return services.set_value(
                KEY,
                False,
                actor=self.admin,
                reason="Turning metrics off while the region is fixed.",
            )

        def importer():
            try:
                return services.import_environment_value(KEY, True)
            finally:
                import_done.set()

        with patch.object(runtime, "get_uncached", admin_read_then_wait), \
                ThreadPoolExecutor(max_workers=2) as executor:
            change_result = executor.submit(self._in_thread, change)
            time.sleep(0.2)
            import_result = executor.submit(self._in_thread, importer)
            changed, imported = change_result.result(), import_result.result()

        self.assertEqual(imported[1], True)
        self.assertIsInstance(changed, ValidationError)
        self.assertIn("changed at the same time", str(changed))
        self.assertTrue(AppSetting.objects.get(key=KEY).value)
        self.assertEqual(AppSettingChange.objects.filter(key=KEY).count(), 1)

    def test_an_import_racing_a_change_in_flight_leaves_the_change_standing(self):
        inserted = threading.Event()
        original_create = AppSettingChange.objects.create

        def admin_insert_then_hold(**kwargs):
            row = original_create(**kwargs)
            if kwargs.get("source") == AppSettingChange.Source.ADMIN:
                inserted.set()
                # Hold the uncommitted insert so the import's own insert has
                # to wait on the unique index and then lose.
                time.sleep(0.5)
            return row

        def change():
            return services.set_value(
                KEY,
                True,
                actor=self.admin,
                reason="Metrics approved for the production account.",
            )

        def importer():
            inserted.wait(timeout=10)
            return services.import_environment_value(KEY, False)

        with patch.object(
            AppSettingChange.objects, "create", admin_insert_then_hold
        ), patch.object(runtime, "get_uncached", return_value=False), \
                patch("apps.configuration.services._check_enable_allowed"), \
                ThreadPoolExecutor(max_workers=2) as executor:
            change_result = executor.submit(self._in_thread, change)
            import_result = executor.submit(self._in_thread, importer)
            changed, imported = change_result.result(), import_result.result()

        self.assertNotIsInstance(changed, ValidationError)
        self.assertEqual(imported[1], False)
        self.assertTrue(AppSetting.objects.get(key=KEY).value)
        self.assertEqual(
            list(
                AppSettingChange.objects.filter(key=KEY).values_list(
                    "source", flat=True
                )
            ),
            [AppSettingChange.Source.ADMIN],
        )


#: A sign-in world in which Google and break-glass both work, before
#: anything is stored: the environment the deploy-time import would read.
_WORKING_SIGN_IN = {
    "PRR_AUTH_MODE": "google",
    "PRR_GOOGLE_OIDC_CLIENT_ID": "prr-client.apps.googleusercontent.com",
    "PRR_GOOGLE_HOSTED_DOMAIN": "psd401.net",
    "PRR_GOOGLE_OIDC_TIMEOUT_SECONDS": 10,
    "PRR_BREAK_GLASS_PASSWORD_AUTH": True,
    "PRR_ENABLE_ADMIN": True,
}


class _SignInLockoutRace:
    """Two admins, two sign-in keys, each save judged alone as safe.

    One breaks the Google door while the other closes break-glass. Judged
    against each other's pre-commit state, both pass and nobody can sign in.
    Every sign-in save takes the same lock first, so the second is judged
    against the first's committed result.
    """

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        User = get_user_model()
        self.breaker = User.objects.create_user(
            "breaker", "breaker@psd401.net", "pw", is_staff=True,
            is_superuser=True,
        )
        self.closer = User.objects.create_user(
            "closer", "closer@psd401.net", "pw", is_staff=True
        )
        # Admins the way Google sign-in makes them, so the Google door is
        # genuinely open before the race and only the race can close it.
        GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN, google_group="prr-admins@psd401.net"
        )
        for user in (self.breaker, self.closer):
            apply_staff_roles(user, {StaffRole.ADMIN})
            StaffIdentity.objects.update_or_create(
                user=user,
                defaults={
                    "source": StaffIdentity.Source.GOOGLE,
                    "google_subject": f"google-sub-{user.pk}",
                },
            )

    def _in_thread(self, work):
        close_old_connections()
        try:
            return work()
        except ValidationError as error:
            return error
        finally:
            close_old_connections()

    def test_the_second_sign_in_change_sees_the_first(self):
        # Alone, closing break-glass is safe: Google still works. Only the
        # other admin's concurrent change can make it unsafe.
        self.assertIsNone(
            signin.lockout_blocker(
                services._resolver("PRR_ENABLE_ADMIN", False),
                services._secret_resolver(),
            )
        )
        judged = threading.Event()
        original_create = AppSettingChange.objects.create

        def hold_the_domain_change(**kwargs):
            row = original_create(**kwargs)
            if kwargs.get("key") == "PRR_GOOGLE_HOSTED_DOMAIN":
                judged.set()
                # Hold the uncommitted, already-judged change open.
                time.sleep(0.5)
            return row

        def break_google():
            return services.set_value(
                "PRR_GOOGLE_HOSTED_DOMAIN",
                "someone-elses-district.org",
                actor=self.breaker,
                reason="Moving sign-in while break-glass stays open.",
            )

        def close_break_glass():
            judged.wait(timeout=10)
            return services.set_value(
                "PRR_ENABLE_ADMIN",
                False,
                actor=self.closer,
                reason="Closing the admin route after the incident.",
            )

        with patch.object(
            AppSettingChange.objects, "create", hold_the_domain_change
        ), ThreadPoolExecutor(max_workers=2) as executor:
            broke = executor.submit(self._in_thread, break_google)
            closed = executor.submit(self._in_thread, close_break_glass)
            broke, closed = broke.result(), closed.result()

        self.assertNotIsInstance(broke, ValidationError)
        self.assertIsInstance(closed, ValidationError)
        self.assertIn("nobody able to sign in", str(closed))
        self.assertTrue(runtime.get_uncached("PRR_ENABLE_ADMIN"))


@skipUnless(
    connection.vendor == "postgresql",
    "Serializing sign-in changes needs PostgreSQL locks.",
)
class SignInLockoutRaceTests(_SignInLockoutRace, TransactionTestCase):
    """Every sign-in setting already has a stored row."""

    def setUp(self):
        super().setUp()
        for key, value in _WORKING_SIGN_IN.items():
            AppSetting.objects.create(key=key, value=value)
        AppSecret.objects.create(
            key="PRR_GOOGLE_OIDC_CLIENT_SECRET",
            ciphertext=secrets.encrypt("GOCSPX-value"),
            fingerprint=secrets.fingerprint("GOCSPX-value"),
        )


@skipUnless(
    connection.vendor == "postgresql",
    "Serializing sign-in changes needs PostgreSQL locks.",
)
@override_settings(
    PRR_GOOGLE_OIDC_CLIENT_SECRET="GOCSPX-value", **_WORKING_SIGN_IN
)
class SignInLockoutRaceWithNothingStoredTests(
    _SignInLockoutRace, TransactionTestCase
):
    """No sign-in row exists yet, so there is no row either save can lock.

    Every value is still the deployed one; both saves are first inserts.
    """
