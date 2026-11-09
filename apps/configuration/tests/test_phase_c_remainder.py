# ABOUTME: Phase C, second half: the malware scanner, Alta, the portal
# ABOUTME: address, district addresses, holidays, and case thresholds.
"""The last district decisions that still lived in ``.env.app``.

Each class names what would go wrong if the move were only cosmetic: a
value that is registered but still read from the environment, a holiday the
deadline engine keeps ignoring until a restart, a token that can be read
back, a scanner address nobody can tell is wrong.
"""

import datetime as dt
import subprocess
import sys
from unittest.mock import patch

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.api.identity import StaffRole, apply_staff_roles
from apps.configuration import runtime, services, values, verification
from apps.configuration.models import AppSecret, AppSetting, SettingVerification
from apps.configuration.registry import SECRET_KEYS, SETTINGS_BY_KEY

REASON = "District confirmed the value with the system's owner in writing."

#: Every key this half moves, with the value the deployment ran before it
#: moved. The first deploy after the move must change nothing.
MOVED_DEFAULTS = {
    "CLAMAV_HOST": "",
    "CLAMAV_PORT": 3310,
    "CLAMAV_TIMEOUT_SECONDS": 30,
    "ALTA_API_BASE": "",
    "ALTA_API_USERNAME": "",
    "ALTA_API_PASSWORD": "",
    "PORTAL_BASE_URL": "https://prr.psd401.net",
    "PRA_SUPERVISOR_EMAIL": "publicrecords@psd401.net",
    "PRA_OFFICER_EMAIL": "publicrecords@psd401.net",
    "FACILITIES_EMAIL": "publicrecords@psd401.net",
    "DISTRICT_HOLIDAYS": (),
    "DEFAULT_ESTIMATE_BUSINESS_DAYS": 60,
    "STALL_THRESHOLD_DAYS": 30,
}


def _admin():
    user = get_user_model().objects.create_user(
        "app-admin", "app-admin@psd401.net", "pw", is_staff=True
    )
    apply_staff_roles(user, {StaffRole.ADMIN})
    return user


class _StoreTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = _admin()

    def _set(self, key, value):
        return services.set_value(key, value, actor=self.admin, reason=REASON)


class RegistrationTests(TestCase):
    def test_every_remaining_district_value_is_registered(self):
        self.assertEqual(set(MOVED_DEFAULTS) - set(SETTINGS_BY_KEY), set())

    def test_every_default_is_what_the_deployment_already_ran(self):
        for key, default in MOVED_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(SETTINGS_BY_KEY[key].default, default)

    def test_the_alta_password_is_write_only_and_the_account_is_not(self):
        self.assertIn("ALTA_API_PASSWORD", SECRET_KEYS)
        self.assertNotIn("ALTA_API_USERNAME", SECRET_KEYS)

    def test_the_retired_alta_token_is_no_longer_a_setting(self):
        """Aware has no API tokens; holds sign in with an account."""
        self.assertNotIn("ALTA_API_TOKEN", SETTINGS_BY_KEY)

    def test_the_from_address_is_not_an_environment_setting(self):
        """Production refuses every generic Django send, so the from address
        has no production effect and is not a decision worth a setting."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import config.settings as s; print(s.DEFAULT_FROM_EMAIL)",
            ],
            cwd=django_settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "DEFAULT_FROM_EMAIL": "someone@example.org",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "publicrecords@psd401.net")


class ShapeTests(_StoreTestCase):
    def test_holidays_are_iso_dates(self):
        spec = SETTINGS_BY_KEY["DISTRICT_HOLIDAYS"]

        self.assertEqual(
            values.coerce(spec, "2026-06-22, 2026-12-24,2026-06-22"),
            ("2026-06-22", "2026-12-24"),
        )
        with self.assertRaisesMessage(ValueError, "2026-13-01"):
            values.coerce(spec, "2026-13-01")
        with self.assertRaises(ValueError):
            values.coerce(spec, "June 22")
        # Ten characters and valid ISO 8601, but a week date: Python's
        # fromisoformat would read it as Wednesday 2026-06-24.
        with self.assertRaisesMessage(ValueError, "2026-W26-3"):
            values.coerce(spec, "2026-W26-3")

    def test_the_portal_address_is_an_https_origin(self):
        spec = SETTINGS_BY_KEY["PORTAL_BASE_URL"]

        self.assertEqual(
            values.coerce(spec, "https://prr.psd401.net"),
            "https://prr.psd401.net",
        )
        for refused in (
            "http://prr.psd401.net",
            "https://prr.psd401.net/requests",
            "prr.psd401.net",
        ):
            with self.subTest(value=refused), self.assertRaises(ValueError):
                values.coerce(spec, refused)

    def test_a_developer_portal_may_be_plain_http_on_localhost(self):
        spec = SETTINGS_BY_KEY["PORTAL_BASE_URL"]

        self.assertEqual(
            values.coerce(spec, "http://localhost:5173"),
            "http://localhost:5173",
        )

    def test_the_alta_address_must_be_https(self):
        with self.assertRaises(ValidationError):
            self._set("ALTA_API_BASE", "http://example.us4.aware.avasecurity.com")

        self._set("ALTA_API_BASE", "https://example.us4.aware.avasecurity.com")

    def test_the_alta_address_is_the_origin_only(self):
        """The client adds /api/v1; a stored path would double it."""
        for refused in (
            "https://example.us4.aware.avasecurity.com/api/v1",
            "https://example.us4.aware.avasecurity.com/",
            "https://user:pw@example.us4.aware.avasecurity.com",
            "https://example.us4.aware.avasecurity.com?x=1",
        ):
            with self.subTest(value=refused), self.assertRaises(ValidationError):
                self._set("ALTA_API_BASE", refused)

    def test_the_alta_account_has_no_spaces(self):
        with self.assertRaises(ValidationError):
            self._set("ALTA_API_USERNAME", "prr holds@example.org")

        self._set("ALTA_API_USERNAME", "prr-holds@example.org")
        self.assertEqual(runtime.get("ALTA_API_USERNAME"), "prr-holds@example.org")

    def test_holds_have_no_share_group_setting(self):
        from apps.configuration.registry import SETTINGS_BY_KEY

        self.assertNotIn("ALTA_BOOKMARK_SHARE_GROUP_ID", SETTINGS_BY_KEY)

    def test_the_scanner_port_is_a_real_port(self):
        with self.assertRaises(ValidationError):
            self._set("CLAMAV_PORT", 70000)

    def test_the_scan_timeout_is_bounded_between_one_and_120_seconds(self):
        """The bound config/settings.py used to enforce at process start."""
        for refused in (0, 121):
            with self.subTest(value=refused):
                with self.assertRaisesMessage(ValidationError, "120"):
                    self._set("CLAMAV_TIMEOUT_SECONDS", refused)

        self._set("CLAMAV_TIMEOUT_SECONDS", 120)
        self.assertEqual(runtime.get("CLAMAV_TIMEOUT_SECONDS"), 120)


class TakesEffectWithoutARestartTests(_StoreTestCase):
    def test_a_stored_holiday_closes_the_deadline_clock_that_day(self):
        from apps.records import deadlines

        monday = dt.date(2026, 6, 22)
        self.assertTrue(deadlines.is_business_day(monday))

        self._set("DISTRICT_HOLIDAYS", ["2026-06-22"])
        self.assertFalse(deadlines.is_business_day(monday))
        # Juneteenth (Friday 6/19) and the closure (Monday 6/22): five
        # business days from Thursday 6/18 land on Monday 6/29, not the
        # Friday 6/26 they would without the closure.
        self.assertEqual(
            deadlines.add_business_days(dt.date(2026, 6, 18), 5),
            dt.date(2026, 6, 29),
        )

        self._set("DISTRICT_HOLIDAYS", [])
        self.assertTrue(deadlines.is_business_day(monday))

    def test_the_scanner_is_reached_at_the_stored_address(self):
        from apps.secure_files import probe_clamav

        self._set("CLAMAV_HOST", "clamav.internal")
        self._set("CLAMAV_PORT", 3311)
        reached = []

        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def sendall(self, _data):
                pass

            def recv(self, _size):
                return b"PONG\0"

        def factory(address, timeout):
            reached.append(address)
            return _Connection()

        self.assertTrue(probe_clamav(_connection_factory=factory))
        self.assertEqual(reached, [("clamav.internal", 3311)])

    def test_the_stored_alta_account_is_the_one_that_signs_in(self):
        from apps.integrations.avigilon import client

        self._set("ALTA_API_BASE", "https://example.us4.aware.avasecurity.com")
        self._set("ALTA_API_USERNAME", "prr-holds@example.org")
        services.set_secret(
            "ALTA_API_PASSWORD", "alta-password", actor=self.admin, reason=REASON
        )

        config = client._config()

        self.assertEqual(config.base, "https://example.us4.aware.avasecurity.com")
        self.assertEqual(config.username, "prr-holds@example.org")
        self.assertEqual(config.password, "alta-password")

    def test_a_stored_stall_threshold_is_the_one_the_watchdog_uses(self):
        self._set("STALL_THRESHOLD_DAYS", 45)
        self.assertEqual(runtime.get("STALL_THRESHOLD_DAYS"), 45)


class BootstrapTests(TestCase):
    """Before the import, each key answers with what the deployment ran."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_officer_and_facilities_addresses_inherit_the_supervisor(self):
        with patch.dict(
            "os.environ", {"PRA_SUPERVISOR_EMAIL": "records@psd401.net"}
        ):
            self.assertEqual(
                runtime.get("PRA_OFFICER_EMAIL"), "records@psd401.net"
            )
            self.assertEqual(
                runtime.get("FACILITIES_EMAIL"), "records@psd401.net"
            )

    def test_the_deployed_alta_password_is_carried_until_one_is_stored(self):
        with patch.dict("os.environ", {"ALTA_API_PASSWORD": "deployed-password"}):
            self.assertEqual(
                runtime.get_secret("ALTA_API_PASSWORD"), "deployed-password"
            )
        self.assertFalse(AppSecret.objects.exists())

    def test_the_deployed_holiday_list_is_parsed(self):
        with patch.dict(
            "os.environ", {"DISTRICT_HOLIDAYS": "2026-06-22,2026-12-24"}
        ):
            self.assertEqual(
                runtime.get("DISTRICT_HOLIDAYS"), ("2026-06-22", "2026-12-24")
            )


class ScannerVerificationTests(_StoreTestCase):
    def test_an_unreachable_scanner_is_saved_and_marked_unverified(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._set("CLAMAV_HOST", "no-such-scanner.invalid")

        self.assertTrue(AppSetting.objects.filter(key="CLAMAV_HOST").exists())
        record = SettingVerification.objects.get(key="CLAMAV_HOST")
        self.assertFalse(record.ok)
        self.assertIn("ClamAV", record.detail)

    def test_a_scanner_that_answers_is_marked_verified(self):
        with patch("apps.secure_files.probe_clamav", return_value=True):
            with self.captureOnCommitCallbacks(execute=True):
                self._set("CLAMAV_HOST", "clamav")

        self.assertTrue(SettingVerification.objects.get(key="CLAMAV_HOST").ok)


class AltaVerificationTests(_StoreTestCase):
    """Saving the Alta address, account, or password signs in and reads the
    device groups once; the verdict is recorded under the address."""

    def setUp(self):
        super().setUp()
        from apps.integrations.avigilon.tests import fakes

        self.fakes = fakes
        self.tenant = fakes.FakeAware()
        self.tenant.on("GET", "/deviceGroups", fakes.groups())
        session = patch(
            "apps.integrations.avigilon.client.requests.Session",
            side_effect=self.tenant.new_session,
        )
        session.start()
        self.addCleanup(session.stop)

    def _configure(self, password):
        with self.captureOnCommitCallbacks(execute=True):
            self._set("ALTA_API_BASE", self.fakes.BASE)
            self._set("ALTA_API_USERNAME", self.fakes.USERNAME)
        with self.captureOnCommitCallbacks(execute=True):
            services.set_secret(
                "ALTA_API_PASSWORD", password, actor=self.admin, reason=REASON
            )
        return SettingVerification.objects.get(key="ALTA_API_BASE")

    def test_an_address_without_an_account_is_unverified_without_a_request(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._set("ALTA_API_BASE", self.fakes.BASE)

        record = SettingVerification.objects.get(key="ALTA_API_BASE")
        self.assertFalse(record.ok)
        self.assertIn("account", record.detail)
        self.assertEqual(self.tenant.calls, [])

    def test_a_password_that_signs_in_is_marked_verified(self):
        record = self._configure(self.fakes.PASSWORD)

        self.assertTrue(record.ok)
        self.assertEqual(record.detail, "")
        self.assertEqual(self.tenant.logins, 1)
        self.assertEqual(
            {c["method"] for c in self.tenant.api_calls()}, {"GET"}
        )

    def test_a_wrong_password_is_saved_and_marked_unverified(self):
        record = self._configure("not-the-password")

        self.assertTrue(AppSecret.objects.filter(key="ALTA_API_PASSWORD").exists())
        self.assertFalse(record.ok)
        self.assertIn("ALTA_API_PASSWORD", record.detail)
        self.assertNotIn("not-the-password", record.detail)

    def test_every_alta_key_shares_one_verdict(self):
        state = verification.state_for(
            ["ALTA_API_BASE", "ALTA_API_USERNAME", "ALTA_API_PASSWORD"]
        )
        self.assertEqual(set(state), {"ALTA_API_BASE", "ALTA_API_USERNAME", "ALTA_API_PASSWORD"})

        self._configure(self.fakes.PASSWORD)

        state = verification.state_for(["ALTA_API_USERNAME", "ALTA_API_PASSWORD"])
        self.assertTrue(state["ALTA_API_USERNAME"]["ok"])
        self.assertTrue(state["ALTA_API_PASSWORD"]["ok"])
