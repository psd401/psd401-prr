from django.core.management.base import BaseCommand, CommandError

from apps.records import service_identities


class Command(BaseCommand):
    help = "Provision a noninteractive, read-only token for the MCP server."

    def add_arguments(self, parser):
        parser.add_argument("username", help="For example mcp-production.")
        parser.add_argument(
            "--rotate",
            action="store_true",
            help="Revoke the current token and print a replacement.",
        )

    def handle(self, *args, **options):
        try:
            provisioned = service_identities.provision_integration_identity(
                "mcp_readonly",
                options["username"],
                rotate=options["rotate"],
            )
        except service_identities.IntegrationIdentityProvisionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Provisioned {provisioned.user.get_username()}. Store this token "
                "in the MCP client now; it will not be shown again:\n"
                f"{provisioned.token.key}"
            )
        )
