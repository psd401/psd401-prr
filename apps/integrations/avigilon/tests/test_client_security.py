import datetime as dt
from unittest import mock

import requests
from django.test import TestCase, override_settings

from apps.integrations.avigilon import client
from apps.integrations.avigilon.tests import fakes


class AltaAddressTests(TestCase):
    @override_settings(
        ALTA_API_BASE="http://alta.example.org",
        ALTA_API_USERNAME=fakes.USERNAME,
        ALTA_API_PASSWORD=fakes.PASSWORD,
    )
    @mock.patch("apps.integrations.avigilon.client.requests.Session")
    def test_plaintext_base_url_is_rejected_before_session_creation(self, session):
        # The stored setting refuses a plaintext address, so one arriving from
        # the deployed environment resolves to the safe default: not
        # configured. Either way no request is built and no password sent.
        with self.assertRaises(client.AltaError):
            client.list_device_cameras()

        session.assert_not_called()

    def test_the_client_refuses_an_unsafe_address_on_its_own(self):
        """Defense in depth below the registry pattern: if a value ever
        reached the client without passing it, the client still refuses."""
        for unsafe in (
            "http://alta.example.org",
            "https://user:pass@alta.example.org",
            "https://alta.example.org/?token=x",
            "https://alta.example.org/#fragment",
            # The address is the origin; the client adds /api/v1 itself.
            "https://alta.example.org/api/v1",
            "https://alta.example.org/anything",
        ):
            with self.subTest(address=unsafe), mock.patch.object(
                client.runtime, "get", return_value=unsafe
            ):
                with self.assertRaisesRegex(client.AltaError, "HTTPS"):
                    client._validated_base_url()

    def test_an_origin_is_normalized(self):
        for supplied, expected in (
            ("https://Example.US4.aware.avasecurity.com", "https://example.us4.aware.avasecurity.com"),
            ("https://example.us4.aware.avasecurity.com/", "https://example.us4.aware.avasecurity.com"),
            ("https://example.org:8443", "https://example.org:8443"),
        ):
            with self.subTest(address=supplied), mock.patch.object(
                client.runtime, "get", return_value=supplied
            ):
                self.assertEqual(client._validated_base_url(), expected)

    def test_each_setting_is_required(self):
        for missing in ("ALTA_API_BASE", "ALTA_API_USERNAME", "ALTA_API_PASSWORD"):
            configured = {**fakes.ALTA_SETTINGS, missing: ""}
            with self.subTest(missing=missing), override_settings(**configured):
                with mock.patch.object(client.requests, "Session") as session:
                    with self.assertRaises(client.AltaNotConfigured):
                        client.list_device_groups()
                session.assert_not_called()

    @override_settings(**fakes.ALTA_SETTINGS)
    def test_the_config_repr_never_shows_the_password(self):
        config = client._config()

        self.assertEqual(config.api, f"{fakes.BASE}/api/v1")
        self.assertNotIn(fakes.PASSWORD, repr(config))
        self.assertNotIn(fakes.PASSWORD, str(config))


class AltaTransportSecurityTests(fakes.TenantTestCase):
    def test_every_request_disables_redirects(self):
        # FakeSession raises if any request leaves redirects on.
        self.tenant.on("POST", "/bookmarks", fakes.response(201, {"id": "bm-1"}))
        self.tenant.on("POST", "/store/timelineInformation", {"stores": []})
        self.tenant.on("DELETE", "/bookmarks/bm-1", {})
        camera = client.list_device_cameras()[0]
        end = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)

        client.create_bookmark_hold(
            camera, end - dt.timedelta(minutes=5), end, name="PRR hold", description="d"
        )
        client.delete_bookmark("bm-1")

        self.assertTrue(self.tenant.calls)
        for call in self.tenant.calls:
            self.assertIs(call["kwargs"]["allow_redirects"], False)

    def test_every_session_ignores_environment_proxies_and_netrc(self):
        """trust_env off: an HTTPS_PROXY or a .netrc on the host must not
        route or authenticate Alta traffic, the sign-in included."""
        client.list_device_groups()
        self.tenant.expire_all_sessions()
        client.list_device_cameras()

        self.assertTrue(self.tenant.sessions)
        for session in self.tenant.sessions:
            self.assertIs(session.trust_env, False)
            self.assertTrue(session.trust_env_at_request)
            self.assertNotIn(True, session.trust_env_at_request)

    def test_redirect_response_is_rejected_without_following(self):
        self.tenant.on(
            "GET",
            "/deviceGroups",
            fakes.response(302, headers={"Location": "https://elsewhere.example.org/"}),
        )

        with self.assertRaisesRegex(client.AltaError, "redirect"):
            client.list_device_groups()

        self.assertEqual(len(self.tenant.calls_to("GET", "/deviceGroups")), 1)

    def test_a_redirect_on_sign_in_is_rejected(self):
        self.tenant.login_response = fakes.response(
            302, headers={"Location": "https://elsewhere.example.org/"}
        )

        with self.assertRaisesRegex(client.AltaError, "redirect"):
            client.list_device_groups()

    def test_the_session_jar_never_carries_a_second_copy_of_the_cookie(self):
        """The cookie is sent explicitly from the shared cache; a cookie the
        login left in the per-call jar would be sent alongside it."""
        client.list_device_groups()

        for session in self.tenant.sessions:
            self.assertEqual(len(session.cookies), 0)
            self.assertTrue(session.closed)


class AltaSecretsNeverLeakTests(fakes.TenantTestCase):
    """Neither the password nor the session cookie may reach a log record or
    an exception, whatever goes wrong."""

    def _assert_clean(self, logs, exc, cookies):
        text = logs.text() + "\n" + fakes.exception_text(exc)
        self.assertNotIn(fakes.PASSWORD, text)
        for cookie in cookies:
            self.assertNotIn(cookie, text)

    def _failure(self, arrange, act=client.list_device_groups):
        logs = self.capture_logs()
        arrange()
        with self.assertRaises(client.AltaError) as caught:
            act()
        cookies = set(self.tenant.valid_cookies) | {
            c["cookie"] for c in self.tenant.calls if c["cookie"]
        }
        self._assert_clean(logs, caught.exception, cookies)
        return caught.exception

    def test_a_refused_sign_in(self):
        self.tenant.login_response = fakes.response(401)
        self._failure(lambda: None)

    def test_a_transport_error_quoting_the_secrets(self):
        def boom():
            cookie = self.tenant.issue_cookie("leaky-cookie-value")
            self.tenant.on(
                "GET",
                "/deviceGroups",
                requests.ConnectionError(
                    f"connection reset; va={cookie}; password={fakes.PASSWORD}"
                ),
            )

        exc = self._failure(boom)
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__suppress_context__)

    def test_a_sign_in_transport_error_quoting_the_password(self):
        def boom():
            self.tenant.login_response = requests.Timeout(
                f"timed out sending password={fakes.PASSWORD}"
            )

        self._failure(boom)

    def test_a_permission_denial(self):
        self._failure(
            lambda: self.tenant.on(
                "GET", "/deviceGroups", fakes.response(403, {"kind": "PERMISSION_DENIED"})
            )
        )

    def test_a_server_error_on_create(self):
        def arrange():
            self.tenant.on("POST", "/bookmarks", fakes.response(500, {"kind": "INTERNAL"}))

        def act():
            camera = client.list_device_cameras()[0]
            end = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)
            client.create_bookmark_hold(
                camera, end - dt.timedelta(minutes=5), end, name="n", description="d"
            )

        self._failure(arrange, act)

    def test_an_undecodable_body(self):
        self._failure(
            lambda: self.tenant.on(
                "GET",
                "/deviceGroups",
                fakes.response(200, b"{not json", content_type="application/json"),
            )
        )

    def test_a_successful_session_logs_no_secret(self):
        logs = self.capture_logs()
        client.list_device_cameras()
        self.tenant.expire_all_sessions()
        client.list_device_cameras()

        text = logs.text()
        self.assertNotIn(fakes.PASSWORD, text)
        for call in self.tenant.calls:
            if call["cookie"]:
                self.assertNotIn(call["cookie"], text)
