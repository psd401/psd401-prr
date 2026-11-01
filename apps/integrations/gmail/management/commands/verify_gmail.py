"""Verify Gmail credentials and mailbox access without sending mail."""
from django.core.management.base import BaseCommand, CommandError

from apps.integrations.gmail import client


class Command(BaseCommand):
    help = "Verify Gmail credentials, delegation, scopes, and mailbox profile."

    def handle(self, *args, **options):
        try:
            report = client.verify_configuration()
        except client.GmailUnavailable as exc:
            raise CommandError(f"{exc}\nRemediation: {exc.remediation}") from exc

        self.stdout.write(self.style.SUCCESS("Credentials: OK"))
        self.stdout.write(self.style.SUCCESS(f"Subject: OK — {report.subject}"))
        for scope in report.granted_scopes:
            self.stdout.write(self.style.SUCCESS(f"Scope: OK — {scope}"))
        self.stdout.write(
            self.style.SUCCESS(f"Profile: OK — {report.profile_email}")
        )
        self.stdout.write(
            self.style.SUCCESS("Gmail verification passed; no message was sent.")
        )
