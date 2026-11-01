# ABOUTME: One-time repair: retires requester-email Inbox rows still waiting on cases
# ABOUTME: that closed before closure started retiring them.
"""Closing a case now retires its Inbox rows; cases closed before that fix
still carry rows marked Needs attention, so they sit in the message queues
with nothing left to do. This applies the same disposition those closures
would have applied, through the same service, attributed to the officer who
runs it, with a reason that says it was a repair. Dry run by default.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.records.models import PublicRecordsRequest
from apps.records.services import CLOSED_STATUSES
from apps.work_queue.models import WorkItem
from apps.work_queue.services import handle_case_inbox_items

_INBOX_KINDS = (WorkItem.WorkKind.INBOUND_REPLY, WorkItem.WorkKind.EMAIL_INTAKE)


class Command(BaseCommand):
    help = "Retire waiting requester-email Inbox rows on cases that are already closed."

    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True, help="Username of the Records Officer applying the repair.")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        officer = get_user_model().objects.filter(username=options["actor"]).first()
        if officer is None:
            raise CommandError(f"No user named {options['actor']!r}.")
        stale = (
            PublicRecordsRequest.objects.filter(
                status__in=CLOSED_STATUSES,
                work_items__work_kind__in=_INBOX_KINDS,
                work_items__disposition__in=[
                    WorkItem.Disposition.NEEDS_ATTENTION,
                    WorkItem.Disposition.WAITING,
                ],
            )
            .distinct()
            .order_by("number")
        )
        cases = list(stale)
        self.stdout.write(f"Closed cases with waiting Inbox rows: {len(cases)}")
        for prr in cases:
            rows = prr.work_items.filter(
                work_kind__in=_INBOX_KINDS,
                disposition__in=[
                    WorkItem.Disposition.NEEDS_ATTENTION,
                    WorkItem.Disposition.WAITING,
                ],
            ).count()
            self.stdout.write(f"    {prr.number:16} {prr.status:20} {rows} row(s)")
        if not options["commit"]:
            self.stdout.write("Dry run; nothing written. Add --commit to retire these rows.")
            return
        retired = 0
        for prr in cases:
            try:
                with transaction.atomic():
                    retired += len(
                        handle_case_inbox_items(
                            prr,
                            actor=officer,
                            reason=f"request_closed_backfill:{prr.status}",
                        )
                    )
            except PermissionDenied as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write(f"Retired {retired} row(s) on {len(cases)} case(s).")
