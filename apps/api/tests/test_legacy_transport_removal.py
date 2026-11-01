from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import get_commands
from django.test import TestCase

from apps.configuration import runtime
from apps.records.service_identities import INTEGRATION_IDENTITY_DEFINITIONS
from config.celery import app as celery_app
from integrations.mcp.client import PRRClient
from integrations.mcp.tools import READ_TOOL_NAMES


class LegacyTransportRemovalTests(TestCase):
    retired_edge = "n" + "8n"

    def test_retired_http_endpoints_return_not_found(self):
        delivery_id = "11111111-1111-4111-8111-111111111111"
        attempt_id = "22222222-2222-4222-8222-222222222222"
        attachment_id = "33333333-3333-4333-8333-333333333333"
        prefix = f"/api/integrations/{self.retired_edge}"
        requests = (
            ("post", f"{prefix}/requests/"),
            ("post", f"{prefix}/correspondence/inbound/"),
            (
                "post",
                f"{prefix}/correspondence-deliveries/{delivery_id}/claim/",
            ),
            (
                "post",
                f"{prefix}/correspondence-deliveries/{delivery_id}/result/",
            ),
            (
                "get",
                (
                    f"{prefix}/correspondence-deliveries/{delivery_id}/attempts/"
                    f"{attempt_id}/attachments/{attachment_id}/"
                ),
            ),
        )

        for method, path in requests:
            with self.subTest(path=path):
                response = getattr(self.client, method)(
                    path,
                    {},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 404)

    def test_retired_commands_settings_identity_and_receipt_surface_are_absent(self):
        command_names = {
            f"provision_{self.retired_edge}_intake",
            f"provision_{self.retired_edge}_correspondence_inbound",
            f"provision_{self.retired_edge}_correspondence_outbound",
        }
        self.assertTrue(command_names.isdisjoint(get_commands()))
        self.assertEqual(
            [definition.id for definition in INTEGRATION_IDENTITY_DEFINITIONS],
            ["mcp_readonly"],
        )
        self.assertFalse(
            any(
                name.startswith(f"PRR_{self.retired_edge.upper()}_")
                for name in dir(settings)
            )
        )
        # The in-process delivery lease is an application-managed setting now.
        self.assertEqual(runtime.get("PRR_CORRESPONDENCE_LEASE_SECONDS"), 300)
        with self.assertRaises(LookupError):
            apps.get_model("records", "IntegratedIntakeSubmission")

        response = self.client.get(
            "/api/staff/integrated-intake-submissions/",
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("list_integrated_intake_submissions", READ_TOOL_NAMES)
        self.assertFalse(
            hasattr(PRRClient, "list_integrated_intake_submissions")
        )

    def test_retired_name_is_absent_from_application_source(self):
        repository_root = Path(__file__).resolve().parents[3]
        source_roots = (
            repository_root / "apps",
            repository_root / "config",
            repository_root / "frontend" / "src",
        )
        source_suffixes = {".py", ".js", ".jsx", ".ts", ".tsx"}
        matches = []
        for source_root in source_roots:
            for path in source_root.rglob("*"):
                if path.suffix not in source_suffixes:
                    continue
                if self.retired_edge in path.read_text().casefold():
                    matches.append(str(path.relative_to(repository_root)))

        self.assertEqual(matches, [])

    def test_webhook_bus_runtime_surface_is_absent(self):
        self.assertFalse(apps.is_installed("apps.events"))
        with self.assertRaises(LookupError):
            apps.get_model("events", "OutboundEvent")
        with self.assertRaises(LookupError):
            apps.get_model("events", "WebhookEndpoint")

        self.assertNotIn(
            "recover-pending-outbound-events",
            celery_app.conf.beat_schedule,
        )
        for setting_name in (
            "PRR_EVENT_OUTBOX_STALE_SECONDS",
            "PRR_WEBHOOK_ALLOWED_HOSTS",
            "PRR_WEBHOOK_REQUIRE_HTTPS",
        ):
            with self.subTest(setting=setting_name):
                self.assertFalse(hasattr(settings, setting_name))

        for path in (
            "/api/staff/webhook-endpoints/",
            "/api/staff/outbound-events/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_webhook_bus_source_and_production_imports_are_absent(self):
        repository_root = Path(__file__).resolve().parents[3]
        self.assertFalse((repository_root / "apps" / "events").exists())
        self.assertFalse(
            (repository_root / "apps" / "api" / "resources" / "webhooks.py").exists()
        )

        import_marker = "apps" + ".events"
        matches = []
        for source_root in (
            repository_root / "apps",
            repository_root / "config",
        ):
            for path in source_root.rglob("*.py"):
                if "tests" in path.parts:
                    continue
                if import_marker in path.read_text():
                    matches.append(str(path.relative_to(repository_root)))

        self.assertEqual(matches, [])
