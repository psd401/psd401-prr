# ABOUTME: Foundation tests for the staff API — session auth endpoints,
# ABOUTME: the staff-only permission gate, and SPA/static serving wiring.
from django.contrib.auth import get_user_model
from django.test import TestCase


class AuthEndpointTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.civilian = get_user_model().objects.create_user(
            "civilian", "someone@example.com", "pw", is_staff=False)

    def test_login_sets_session_and_returns_user(self):
        r = self.client.post("/api/staff/auth/login",
                             {"username": "officer", "password": "pw"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "officer")
        me = self.client.get("/api/staff/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "officer")

    def test_login_rejects_bad_credentials(self):
        r = self.client.post("/api/staff/auth/login",
                             {"username": "officer", "password": "wrong"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_login_rejects_non_staff_user(self):
        r = self.client.post("/api/staff/auth/login",
                             {"username": "civilian", "password": "pw"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"detail": "Invalid username or password."})

    def test_logout_clears_session(self):
        self.client.post("/api/staff/auth/login",
                         {"username": "officer", "password": "pw"},
                         content_type="application/json")
        r = self.client.post("/api/staff/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/staff/auth/me").status_code, 403)

    def test_me_requires_auth(self):
        self.assertEqual(self.client.get("/api/staff/auth/me").status_code, 403)


class StaffGateTests(TestCase):
    """Staff endpoints refuse anonymous and non-staff callers."""

    def test_staff_endpoint_refuses_anonymous(self):
        r = self.client.get("/api/staff/exemptions/")
        self.assertEqual(r.status_code, 403)

    def test_staff_endpoint_refuses_non_staff_session(self):
        get_user_model().objects.create_user(
            "civilian2", "c2@example.com", "pw", is_staff=False)
        self.client.login(username="civilian2", password="pw")
        r = self.client.get("/api/staff/exemptions/")
        self.assertEqual(r.status_code, 403)

class SpaServingTests(TestCase):
    def test_root_serves_spa_shell(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        shell = (b"".join(r.streaming_content)
                 if getattr(r, "streaming", False) else r.content)
        self.assertIn(b"<div id=\"root\"", shell)
        self.assertIn(b'data-auth-mode="local"', shell)

    def test_client_route_serves_spa_shell(self):
        r = self.client.get("/requests/PRR-2026-0001")
        self.assertEqual(r.status_code, 200)

    def test_admin_and_api_not_swallowed_by_catchall(self):
        self.assertIn(self.client.get("/admin/").status_code, (301, 302))
        self.assertEqual(self.client.get("/api/staff/auth/me").status_code, 403)
