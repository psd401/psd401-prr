# ABOUTME: Streams the append-only audit ledger as canonical JSON Lines.
# ABOUTME: Supplies independently readable legal evidence beside database backups.
import datetime as dt
import json

from django.core.management.base import BaseCommand

from apps.audit.models import AuditLog


def _utc_iso(value):
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


class Command(BaseCommand):
    help = "Export the complete append-only audit ledger as canonical JSON Lines."

    def handle(self, *args, **options):
        rows = (
            AuditLog.objects.order_by("pk")
            .values(
                "id",
                "at",
                "actor_id",
                "action",
                "target",
                "detail",
                "ip_address",
            )
            .iterator(chunk_size=2_000)
        )
        for row in rows:
            row["at"] = _utc_iso(row["at"])
            payload = json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self.stdout.write(payload)
