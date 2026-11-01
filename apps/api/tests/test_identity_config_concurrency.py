# ABOUTME: PostgreSQL race coverage for the last active Application Admin mapping.
# ABOUTME: Concurrent removals serialize and recheck the invariant under row locks.
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.models import GoogleGroupRoleMapping
from apps.api.resources.identity_config import GoogleGroupRoleMappingSerializer


@skipUnless(
    connection.vendor == "postgresql",
    "Role-mapping concurrency requires PostgreSQL row-level locks.",
)
@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class GoogleGroupRoleMappingConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "concurrent-mapping-admin",
            "concurrent-mapping-admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.staff, {StaffRole.ADMIN})
        self.mappings = (
            GoogleGroupRoleMapping.objects.create(
                role=StaffRole.ADMIN,
                google_group="concurrent-admin-a@psd401.net",
            ),
            GoogleGroupRoleMapping.objects.create(
                role=StaffRole.ADMIN,
                google_group="concurrent-admin-b@psd401.net",
            ),
        )

    def test_concurrent_deactivations_preserve_one_active_admin_mapping(self):
        validated = threading.Barrier(2)
        original_validate = GoogleGroupRoleMappingSerializer.validate

        def synchronized_validate(serializer, attrs):
            result = original_validate(serializer, attrs)
            validated.wait(timeout=10)
            return result

        def deactivate(mapping_id):
            close_old_connections()
            try:
                client = Client()
                user = get_user_model().objects.get(pk=self.staff.pk)
                client.force_login(user)
                response = client.patch(
                    f"/api/staff/google-role-mappings/{mapping_id}/",
                    {"active": False},
                    content_type="application/json",
                )
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with patch.object(
            GoogleGroupRoleMappingSerializer,
            "validate",
            synchronized_validate,
        ), patch(
            "apps.api.resources.identity_config._expire_google_authorizations"
        ), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(deactivate, (self.mappings[0].pk, self.mappings[1].pk))
            )

        self.assertEqual(sorted(status for status, _payload in results), [200, 400])
        rejected = next(payload for status, payload in results if status == 400)
        self.assertIn("last active Application Admin mapping", str(rejected))
        self.assertEqual(
            GoogleGroupRoleMapping.objects.filter(
                role=StaffRole.ADMIN,
                active=True,
            ).count(),
            1,
        )
