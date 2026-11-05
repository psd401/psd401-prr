"""The shared Aware session: one login serves every process.

Aware allows three sessions per account and evicts the oldest, so a client
that logged in per task would sign its own peers out. Each test's separate
client calls build separate ``requests.Session`` objects, exactly as separate
web and Celery processes would; the only thing they share is the Django
cache.
"""

from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from apps.configuration import secrets
from apps.integrations.avigilon import client
from apps.integrations.avigilon.tests import fakes


class SharedSessionTests(fakes.TenantTestCase):
    def test_one_login_serves_every_later_call(self):
        client.list_device_groups()
        client.list_device_groups()
        client.list_device_cameras()

        self.assertEqual(self.tenant.logins, 1)
        self.assertGreaterEqual(len(self.tenant.sessions), 3)
        cookies = {c["cookie"] for c in self.tenant.api_calls()}
        self.assertEqual(len(cookies), 1)

    def test_the_login_posts_the_service_account_credentials(self):
        client.list_device_groups()

        (login,) = self.tenant.calls_to("POST", "/dologin")
        self.assertEqual(
            login["json"], {"username": fakes.USERNAME, "password": fakes.PASSWORD}
        )

    def test_a_cookie_stored_by_another_process_is_used_without_a_login(self):
        """What a Celery worker sees after the web process signed in."""
        cookie = self.tenant.issue_cookie("signed-in-elsewhere")
        cache.set(client._session_cache_key(client._config()), secrets.encrypt(cookie))

        client.list_device_groups()

        self.assertEqual(self.tenant.logins, 0)
        self.assertEqual(self.tenant.api_calls()[0]["cookie"], "signed-in-elsewhere")

    def test_the_cached_cookie_is_sealed_and_expires_inside_a_day(self):
        with mock.patch.object(client.cache, "set", wraps=cache.set) as setter:
            client.list_device_groups()

        key = client._session_cache_key(client._config())
        stored = cache.get(key)
        cookie = self.tenant.api_calls()[0]["cookie"]
        self.assertTrue(stored)
        self.assertNotIn(cookie, str(stored))
        self.assertEqual(secrets.decrypt(stored), cookie)
        (call,) = [c for c in setter.call_args_list if c.args[0] == key]
        timeout = call.kwargs.get("timeout", call.args[2] if len(call.args) > 2 else None)
        self.assertEqual(timeout, client.SESSION_CACHE_TTL_SECONDS)
        self.assertLess(client.SESSION_CACHE_TTL_SECONDS, 24 * 60 * 60)

    def test_the_cache_key_never_contains_the_password_or_username(self):
        key = client._session_cache_key(client._config())

        self.assertNotIn(fakes.PASSWORD, key)
        self.assertNotIn(fakes.USERNAME, key)

    def test_a_password_change_does_not_reuse_the_old_session(self):
        client.list_device_groups()
        with override_settings(ALTA_API_PASSWORD="a-rotated-password"):
            new_key = client._session_cache_key(client._config())
        old_key = client._session_cache_key(client._config())

        self.assertNotEqual(new_key, old_key)

    def test_a_sealed_cookie_this_key_cannot_open_is_a_miss(self):
        cache.set(client._session_cache_key(client._config()), "not-a-fernet-token")

        client.list_device_groups()

        self.assertEqual(self.tenant.logins, 1)

    def test_an_unreachable_cache_fails_closed(self):
        with mock.patch.object(client.cache, "get", side_effect=ConnectionError("redis down")):
            with self.assertRaisesRegex(client.AltaError, "cache"):
                client.list_device_groups()

        self.assertEqual(self.tenant.logins, 0)


class ExpiredSessionTests(fakes.TenantTestCase):
    def test_a_401_logs_in_again_once_and_retries(self):
        client.list_device_groups()
        self.tenant.expire_all_sessions()

        groups = client.list_device_groups()

        self.assertEqual(len(groups), 3)
        self.assertEqual(self.tenant.logins, 2)
        first, retried = self.tenant.calls_to("GET", "/deviceGroups")[1:]
        self.assertNotEqual(first["cookie"], retried["cookie"])
        self.assertIn(retried["cookie"], self.tenant.valid_cookies)

    def test_the_new_session_replaces_the_old_one_for_everyone(self):
        client.list_device_groups()
        self.tenant.expire_all_sessions()
        client.list_device_groups()

        client.list_device_groups()

        self.assertEqual(self.tenant.logins, 2)

    def test_a_401_right_after_a_fresh_login_is_not_retried_again(self):
        self.tenant.on("GET", "/deviceGroups", fakes.response(401))

        with self.assertRaises(client.AltaAuthenticationFailed):
            client.list_device_groups()

        self.assertEqual(self.tenant.logins, 2)
        self.assertEqual(len(self.tenant.calls_to("GET", "/deviceGroups")), 2)

    def test_a_refused_login_raises_without_retrying(self):
        self.tenant.login_response = fakes.response(401)

        with self.assertRaisesRegex(client.AltaAuthenticationFailed, "ALTA_API_USERNAME"):
            client.list_device_groups()

        self.assertEqual(self.tenant.logins, 1)
        self.assertEqual(self.tenant.api_calls(), [])

    def test_a_login_without_a_session_cookie_raises(self):
        self.tenant.login_response = fakes.response(200)

        with self.assertRaisesRegex(client.AltaError, "cookie"):
            client.list_device_groups()

    def test_a_403_is_a_permission_error_and_is_never_retried(self):
        self.tenant.on(
            "GET", "/deviceGroups", fakes.response(403, {"kind": "PERMISSION_DENIED"})
        )

        with self.assertRaises(client.AltaPermissionDenied):
            client.list_device_groups()

        self.assertEqual(self.tenant.logins, 1)
        self.assertEqual(len(self.tenant.calls_to("GET", "/deviceGroups")), 1)
        self.sleep.assert_not_called()


class LoginLockTests(fakes.TenantTestCase):
    def _hold_lock(self):
        config = client._config()
        self.assertTrue(cache.add(client._login_lock_key(config), "held", 60))
        return config

    def test_a_waiter_uses_the_session_the_lock_holder_stores(self):
        config = self._hold_lock()

        def other_process_finishes(_seconds):
            cookie = self.tenant.issue_cookie("from-the-lock-holder")
            cache.set(client._session_cache_key(config), secrets.encrypt(cookie))
            cache.delete(client._login_lock_key(config))

        self.sleep.side_effect = other_process_finishes

        client.list_device_groups()

        self.assertEqual(self.tenant.logins, 0)
        self.assertEqual(self.tenant.api_calls()[0]["cookie"], "from-the-lock-holder")

    def test_a_waiter_takes_over_when_the_holder_dies(self):
        config = self._hold_lock()

        def lock_expires(_seconds):
            cache.delete(client._login_lock_key(config))

        self.sleep.side_effect = lock_expires

        client.list_device_groups()

        self.assertEqual(self.tenant.logins, 1)

    def test_a_waiter_gives_up_with_an_error_not_a_second_login(self):
        self._hold_lock()
        clock = iter(range(0, 10_000, 5))

        with mock.patch.object(client, "_monotonic", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(client.AltaError, "sign"):
                client.list_device_groups()

        self.assertEqual(self.tenant.logins, 0)

    def test_the_lock_outlives_a_login_and_the_wait_outlives_the_lock(self):
        self.assertGreater(client.LOGIN_LOCK_TTL_SECONDS, client.LOGIN_TIMEOUT_SECONDS)
        self.assertGreater(client.LOGIN_WAIT_SECONDS, client.LOGIN_LOCK_TTL_SECONDS)

    def test_the_lock_is_released_after_a_failed_login(self):
        self.tenant.login_response = fakes.response(500)

        with self.assertRaises(client.AltaError):
            client.list_device_groups()

        self.assertIsNone(cache.get(client._login_lock_key(client._config())))

    def test_the_lock_is_released_after_a_login(self):
        client.list_device_groups()

        self.assertIsNone(cache.get(client._login_lock_key(client._config())))
