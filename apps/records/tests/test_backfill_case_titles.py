"""The one-time pass that names cases which predate automatic case titles."""

from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.records.advisory_ai import request_automatic_case_title_advisory
from apps.records.models import (
    AdvisoryRun,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)

ADVISORY_ON = override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)


def make_case(description, **kw):
    requester = Requester.objects.create(email="requester@example.org")
    return PublicRecordsRequest.objects.create(
        requester=requester, description=description, **kw
    )


@ADVISORY_ON
class BackfillCaseTitlesTests(TestCase):
    def setUp(self):
        self.open_case = make_case("Bus camera video from route 12")
        self.closed_case = make_case(
            "Budget spreadsheets", status=RequestStatus.FULFILLED
        )

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command(
            "backfill_case_titles", *args, stdout=out, stderr=err, pause=0
        )
        return out.getvalue(), err.getvalue()

    def test_a_dry_run_queues_nothing(self):
        with patch("apps.records.tasks.execute_advisory_analysis.delay") as delay:
            out, _ = self.run_command()

        delay.assert_not_called()
        self.assertEqual(AdvisoryRun.objects.count(), 0)
        self.assertIn(self.open_case.number, out)
        self.assertIn("Re-run with --commit", out)

    def test_a_dry_run_leaves_closed_cases_out_unless_asked(self):
        out, _ = self.run_command()
        self.assertNotIn(self.closed_case.number, out)

        out, _ = self.run_command("--all")
        self.assertIn(self.closed_case.number, out)

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_commit_queues_one_title_run_per_open_case(self, delay):
        self.run_command("--commit")

        runs = AdvisoryRun.objects.all()
        self.assertEqual(
            [run.request_id for run in runs], [self.open_case.pk]
        )
        run = runs.get()
        self.assertEqual(run.kind, AdvisoryRun.Kind.CASE_TITLE)
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)
        self.assertIsNone(run.requested_by)
        self.assertFalse(run.applied)
        delay.assert_called_once_with(str(run.pk))

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_running_it_twice_queues_nothing_the_second_time(self, delay):
        self.run_command("--commit")
        delay.reset_mock()

        out, _ = self.run_command("--commit")

        self.assertEqual(AdvisoryRun.objects.count(), 1)
        delay.assert_not_called()
        self.assertIn("0 of 0 cases", out)

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_limit_bounds_one_pass(self, delay):
        make_case("Another request")
        make_case("A third request")

        self.run_command("--commit", "--limit", "2")

        self.assertEqual(AdvisoryRun.objects.count(), 2)

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_the_pass_changes_nothing_about_the_cases(self, delay):
        before = list(PublicRecordsRequest.objects.order_by("pk").values())

        self.run_command("--commit", "--all")

        self.assertEqual(
            list(PublicRecordsRequest.objects.order_by("pk").values()), before
        )

    @override_settings(PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED=False)
    def test_commit_refuses_while_the_switch_is_off(self):
        with self.assertRaises(CommandError):
            self.run_command("--commit")
        self.assertEqual(AdvisoryRun.objects.count(), 0)

    @override_settings(PRR_AI_ADVISORY_ENABLED=False)
    def test_commit_refuses_while_advisory_ai_is_off(self):
        with self.assertRaises(CommandError):
            self.run_command("--commit")

    def test_a_zero_batch_size_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command("--commit", "--batch-size", "0")

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_one_unqueueable_case_does_not_strand_the_rest(self, delay):
        """A backlog pass has to finish. One case the service refuses is
        named on stderr and counted, and every later case still gets its
        run."""
        later = make_case("A later request")

        def refuse_the_first(request_id):
            if request_id == self.open_case.pk:
                raise ValidationError("not queueable")
            return request_automatic_case_title_advisory(request_id)

        with patch(
            "apps.records.management.commands.backfill_case_titles"
            ".request_automatic_case_title_advisory",
            side_effect=refuse_the_first,
        ):
            out, err = self.run_command("--commit")

        self.assertIn(self.open_case.number, err)
        self.assertEqual(
            [run.request_id for run in AdvisoryRun.objects.all()], [later.pk]
        )
        self.assertIn("1 could not be queued", out)

    def test_the_pause_follows_new_runs_not_cases_already_titled(self):
        """--pause spaces out calls that reach the provider. A case the
        service hands back already complete made no call, so it must not
        sleep again just because the in-flight count still sits on a batch
        boundary."""
        make_case("A second open request")
        make_case("A third open request")
        states = iter([
            AdvisoryRun.State.PENDING,
            AdvisoryRun.State.COMPLETE,
            AdvisoryRun.State.COMPLETE,
        ])

        def fake_request(request_id):
            return AdvisoryRun(state=next(states))

        module = "apps.records.management.commands.backfill_case_titles"
        with patch(f"{module}.request_automatic_case_title_advisory",
                   side_effect=fake_request), \
                patch(f"{module}.time.sleep") as sleep:
            call_command(
                "backfill_case_titles", "--commit", "--batch-size", "1",
                stdout=StringIO(), stderr=StringIO(), pause=5,
            )

        self.assertEqual(sleep.call_count, 1)
