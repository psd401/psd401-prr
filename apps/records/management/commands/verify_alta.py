"""Check the Avigilon Alta (Aware) connection footage holds depend on.

Set ALTA_API_BASE, ALTA_API_USERNAME, and ALTA_API_PASSWORD in the
application's settings, then run this. The plain run signs in once and only
reads: it counts active and inactive devices, recording heads, and device
groups, and names the groups with no active device. ``--smoke-bookmark <device guid>``
also creates one 10-minute bookmark on that device ending an hour ago, reads
it back, reports its coverage, and deletes it, deleting it whatever fails in
between. Output is ids, counts, and times only.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.integrations.avigilon import client as alta


def _run(operation, *args):
    try:
        return operation(*args)
    except alta.AltaNotConfigured as e:
        raise CommandError(f"Not configured: {e}") from None
    except alta.AltaWriteUncertain as e:
        # Only the smoke create raises this: its bookmark may exist.
        raise CommandError(
            f"API error: {e} Look for a bookmark named {alta.SMOKE_NAME!r} and "
            "delete it by hand."
        ) from None
    except alta.AltaError as e:
        raise CommandError(f"API error: {e}") from None
    except Exception as e:  # noqa: BLE001 - never echo an unknown error's text
        raise CommandError(f"API error: {type(e).__name__}") from None


class Command(BaseCommand):
    help = "Verify Alta Aware API sign-in and read access; optionally smoke-test one bookmark."

    def add_arguments(self, parser):
        parser.add_argument(
            "--smoke-bookmark",
            metavar="DEVICE_GUID",
            help="Create, check, and delete one 10-minute bookmark on this device.",
        )

    def handle(self, *args, **opts):
        result = _run(alta.checks)
        self.stdout.write(self.style.SUCCESS(
            f"OK: signed in; {result['device_count']} active devices with "
            f"{result['head_count']} recording heads "
            f"({result['devices_without_heads']} with no recording head) in "
            f"{result['group_count']} device groups; "
            f"{result['inactive_devices']} inactive device(s) not listed."
        ))
        if result["empty_groups"]:
            self.stdout.write(
                "Device groups with no active device: " + ", ".join(result["empty_groups"])
            )
        if opts.get("smoke_bookmark"):
            self._smoke(opts["smoke_bookmark"])

    def _smoke(self, device_guid: str) -> None:
        result = _run(alta.smoke_test_bookmark, device_guid)
        self.stdout.write(
            f"Created bookmark {result['bookmark_id']} on device {result['device_id']} "
            f"for {result['heads']} heads, {result['start']} to {result['end']}; "
            f"{result['stored_entries']} entries read back."
        )
        for head in result["coverage"]:
            line = f"  head {head['source']}: {head['covered_fraction']:.0%} covered"
            self.stdout.write(line + (f" ({head['error']})" if head["error"] else ""))
        if result["confirmed_gone"]:
            self.stdout.write(
                f"Deleted {result['bookmark_id']}; GET now returns NOT_FOUND."
            )
        if result["failures"]:
            raise CommandError(
                f"Smoke bookmark {result['bookmark_id']} failed: "
                + "; ".join(result["failures"])
            )
        self.stdout.write(self.style.SUCCESS("Smoke bookmark OK."))
