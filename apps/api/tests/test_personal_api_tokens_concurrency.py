# ABOUTME: PostgreSQL race coverage for the personal-token active limit.
# ABOUTME: Concurrent creates serialize on the staff row before counting.
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.models import PersonalAccessToken


@skipUnless(
    connection.vendor == "postgresql",
    "Personal-token concurrency requires PostgreSQL row-level locks.",
)
@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_PERSONAL_TOKEN_MAX_ACTIVE=1,
    PRR_PERSONAL_TOKEN_CREATE_RATE="1000/hour",
)
class PersonalAccessTokenConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "concurrent-personal-token-user",
            "concurrent-personal-token-user@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.staff, {StaffRole.REVIEWER})

    def test_concurrent_creates_cannot_exceed_active_limit(self):
        ready = threading.Barrier(2)

        def create(name):
            close_old_connections()
            try:
                client = Client()
                user = get_user_model().objects.get(pk=self.staff.pk)
                client.force_login(user)
                ready.wait(timeout=10)
                response = client.post(
                    "/api/staff/auth/api-tokens/",
                    {"name": name, "lifetime_days": 30},
                    content_type="application/json",
                )
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(create, ("Claude Desktop", "Laptop CLI")),
            )

        self.assertEqual(sorted(status for status, _payload in results), [201, 400])
        rejected = next(payload for status, payload in results if status == 400)
        self.assertIn("non_field_errors", rejected)
        self.assertEqual(
            PersonalAccessToken.objects.filter(
                user=self.staff,
                revoked_at__isnull=True,
            ).count(),
            1,
        )
