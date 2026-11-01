# ABOUTME: Queue the advisory case-title proposal for cases that predate it.
# ABOUTME: Dry run by default; --commit queues, in bounded batches.
"""Give existing cases the one-line title new cases now get at intake.

The case list's Subject column falls back to the opening of the request text,
which for an emailed request is usually the sender's signature block rather
than anything about the records. New cases get a proposed title at intake once
an Application Admin turns "Name new cases automatically" on. This is the
one-time pass that gives the cases already in the system the same thing.

It queues advisory runs and nothing else. It sends nothing, changes no status,
touches no deadline, runs no search or Vault operation, and writes nothing to
the request: ``AdvisoryRun.applied`` stays false by database constraint, and
the description remains the untouched legal record of what was asked (AGENTS.md
rule 9). A case whose title cannot be proposed keeps the description snippet it
shows today.

Safe to run twice. The advisory service reuses a pending, running, or complete
run over the same request text and model rather than paying the provider for a
second copy of the same answer, and this pass also skips those cases up front
so the numbers it reports are the work genuinely left.

Open cases by default -- a closed case is not what anyone is scanning a work
queue for -- and ``--all`` includes closed ones. ``--limit`` bounds one pass;
``--batch-size`` and ``--pause`` bound how fast runs reach the model provider,
so a backlog of several thousand cases does not arrive all at once.

Dry run by default; ``--commit`` queues.
"""

import time

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.audit.models import audit
from apps.configuration import runtime
from apps.records.advisory_ai import request_automatic_case_title_advisory
from apps.records.models import AdvisoryRun, PublicRecordsRequest
from apps.records.services import CLOSED_STATUSES

#: A run in one of these states already answers this case, or is about to.
_SETTLED_STATES = (
    AdvisoryRun.State.PENDING,
    AdvisoryRun.State.RUNNING,
    AdvisoryRun.State.COMPLETE,
)


class Command(BaseCommand):
    help = "Queue advisory case titles for cases created before the feature."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Queue the runs. Without this the command only reports.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="include_closed",
            help="Include closed cases, not only open ones.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after this many cases. 0 means every matching case.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=25,
            help="How many runs to queue before pausing.",
        )
        parser.add_argument(
            "--pause",
            type=float,
            default=1.0,
            help="Seconds to wait between batches.",
        )

    def handle(self, *args, **options):
        commit = bool(options["commit"])
        limit = max(0, int(options["limit"]))
        batch_size = int(options["batch_size"])
        if batch_size < 1:
            raise CommandError("--batch-size must be at least 1.")
        pause = max(0.0, float(options["pause"]))
        if commit and not (
            runtime.get("PRR_AI_ADVISORY_ENABLED")
            and runtime.get("PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED")
        ):
            # The same two switches the intake path needs. Saying so here is
            # plainer than queueing runs the worker then fails one at a time
            # as automatic_disabled.
            raise CommandError(
                "Advisory AI and automatic case titles must both be on before "
                "this pass can queue anything."
            )

        cases = PublicRecordsRequest.objects.order_by("received_at", "pk")
        if not options["include_closed"]:
            cases = cases.exclude(status__in=CLOSED_STATUSES)
        # Both conditions must hold on the same advisory run, which is what
        # excluding on a multi-valued relation in a single call means.
        cases = cases.exclude(
            advisory_runs__kind=AdvisoryRun.Kind.CASE_TITLE,
            advisory_runs__state__in=_SETTLED_STATES,
        )
        # The identifiers are read before anything is queued: the loop creates
        # advisory runs, which are exactly the rows the exclusion above filters
        # on, so a streaming cursor would be walking a result set its own work
        # is changing underneath it.
        targets = list(cases.values_list("number", "pk"))
        if limit:
            targets = targets[:limit]

        examined = 0
        in_flight = 0
        already_complete = 0
        failed = 0
        for number, pk in targets:
            examined += 1
            if not commit:
                self.stdout.write(f"{number}: would queue a case-title run")
                continue
            try:
                run = request_automatic_case_title_advisory(pk)
            except (ValidationError, PermissionDenied) as exc:
                # One unqueueable case must not strand every later one. The
                # case number is named so a human can look at it; no request
                # text and no model output is ever printed.
                failed += 1
                self.stderr.write(f"{number}: not queued ({type(exc).__name__})")
                continue
            if run.state == AdvisoryRun.State.COMPLETE:
                # The service found an identical completed run and handed it
                # back. Nothing was queued and the provider was not called.
                already_complete += 1
                continue
            in_flight += 1
            # Only a newly queued run reaches the provider, so only a new run
            # advances the batch; an already-titled case must not sleep again.
            if in_flight % batch_size == 0 and pause:
                time.sleep(pause)

        if commit:
            audit(
                "advisory.case_titles.backfilled",
                examined=examined,
                in_flight=in_flight,
                already_complete=already_complete,
                failed=failed,
                included_closed=bool(options["include_closed"]),
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"{in_flight} of {examined} cases now have a title run in "
                    f"flight ({already_complete} already had a completed one, "
                    f"{failed} could not be queued)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Would queue a case-title run for {examined} cases. "
                    "Re-run with --commit to do it."
                )
            )
