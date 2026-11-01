"""Smoke-test the Avigilon Alta API config: run this first once the district
API token is in .env, and fix any VERIFY-marked endpoint paths in
apps/integrations/avigilon/client.py that 404."""
from django.core.management.base import BaseCommand, CommandError

from apps.integrations.avigilon import client as alta


class Command(BaseCommand):
    help = "Verify Alta Aware API connectivity and permissions."

    def handle(self, *args, **opts):
        try:
            result = alta.checks()
        except alta.AltaNotConfigured as e:
            raise CommandError(f"Not configured: {e}") from e
        except Exception as e:
            raise CommandError(
                f"API error: {e}\nCheck ALTA_API_BASE, token scopes, and the "
                "VERIFY-marked endpoint paths in avigilon/client.py."
            ) from e
        self.stdout.write(self.style.SUCCESS(
            f"OK — {result['camera_count']} cameras visible. Sample: {result['sample']}"))
