"""Intake classification: the model labels a new case for a person to confirm.

It may say new request, follow-up, duplicate, related, non-request, or spam,
and point at an earlier case it was shown. It changes nothing: the officer
applies a label through the ordinary merge, close, or add-to-case action, and
what they decided is recorded so agreement can be measured.
"""

import datetime as dt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.advisory_ai import (
    AdvisoryOutputInvalid,
    _build_advisory_input,
    _validate_proposal,
    defer_automatic_intake_classification,
    execute_advisory_run,
    request_intake_classification,
)
from apps.records.models import (
    AdvisoryFeedback,
    AdvisoryRun,
    MatterKind,
    PublicRecordsRequest,
    Requester,
)

KIND = AdvisoryRun.Kind.INTAKE_CLASSIFICATION


def _user(username, role):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [role])
    return user


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class _Fixture(TestCase):
    def setUp(self):
        queue = patch("apps.records.tasks.execute_advisory_analysis.delay")
        queue.start()
        self.addCleanup(queue.stop)
        self.officer = _user("intake-officer", StaffRole.RECORDS_OFFICER)
        self.reviewer = _user("intake-reviewer", StaffRole.REVIEWER)
        self.now = timezone.now()

    def _case(self, *, email="a@example.org", days_ago=0, description="Records.",
              matter_kind=MatterKind.PUBLIC_RECORDS):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email=email),
            description=description,
            matter_kind=matter_kind,
        )
        PublicRecordsRequest.objects.filter(pk=prr.pk).update(
            received_at=self.now - dt.timedelta(days=days_ago),
        )
        prr.refresh_from_db()
        return prr


class IntakeClassificationInputTests(_Fixture):
    def test_candidates_are_earlier_cases_of_the_same_matter_kind(self):
        earlier = self._case(email="b@example.org", days_ago=10, description="Bus video.")
        self._case(email="c@example.org", days_ago=10, matter_kind=MatterKind.LITIGATION_DISCOVERY)
        self._case(email="d@example.org", days_ago=400)
        new = self._case(email="b@example.org", days_ago=0)
        later = self._case(email="e@example.org", days_ago=-1)

        run = AdvisoryRun(kind=KIND, request=new, model_identifier="m", input_sha256="")
        payload = _build_advisory_input(run).payload

        numbers = [row["number"] for row in payload["candidate_cases"]]
        self.assertEqual(numbers, [earlier.number])
        self.assertNotIn(later.number, numbers)
        self.assertNotIn(new.number, numbers)
        self.assertTrue(payload["candidate_cases"][0]["same_requester_email"])
        self.assertEqual(payload["candidate_cases"][0]["description"], "Bus video.")
        self.assertEqual(payload["request_channel"], new.channel)

    def test_the_input_is_stable_while_later_cases_arrive(self):
        self._case(days_ago=5)
        new = self._case(days_ago=0)
        run = AdvisoryRun(kind=KIND, request=new, model_identifier="m", input_sha256="")
        before = _build_advisory_input(run).fingerprint
        self._case(days_ago=-1)
        self.assertEqual(_build_advisory_input(run).fingerprint, before)


class IntakeClassificationOutputTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.earlier = self._case(days_ago=3)
        self.new = self._case(days_ago=0)
        self.candidates = {self.earlier.number}

    def _validate(self, proposal):
        return _validate_proposal(KIND, proposal, candidate_numbers=self.candidates)

    def test_a_duplicate_naming_a_shown_case_is_accepted(self):
        result = self._validate({
            "label": "duplicate_of",
            "target_case_number": self.earlier.number,
            "confidence": 0.91,
            "evidence": "Same records, same requester, two days apart.",
        })
        self.assertEqual(result["label"], "duplicate_of")
        self.assertEqual(result["target_case_number"], self.earlier.number)
        self.assertEqual(result["confidence"], 0.91)

    def test_a_case_the_model_was_not_shown_is_refused(self):
        with self.assertRaises(AdvisoryOutputInvalid):
            self._validate({
                "label": "follow_up",
                "target_case_number": "PRR-2099-9999",
                "confidence": 0.8,
                "evidence": "Invented.",
            })

    def test_labels_that_point_at_a_case_need_one_and_others_must_not(self):
        with self.assertRaises(AdvisoryOutputInvalid):
            self._validate({
                "label": "related_to", "target_case_number": None,
                "confidence": 0.5, "evidence": "x",
            })
        with self.assertRaises(AdvisoryOutputInvalid):
            self._validate({
                "label": "spam", "target_case_number": self.earlier.number,
                "confidence": 0.5, "evidence": "x",
            })

    def test_unknown_labels_and_bad_confidence_are_refused(self):
        for proposal in (
            {"label": "deny", "target_case_number": None, "confidence": 0.5, "evidence": "x"},
            {"label": "spam", "target_case_number": None, "confidence": 1.5, "evidence": "x"},
            {"label": "spam", "target_case_number": None, "confidence": 0.5,
             "evidence": "x", "extra": 1},
        ):
            with self.subTest(proposal=proposal), self.assertRaises(AdvisoryOutputInvalid):
                self._validate(proposal)

    def test_a_completed_run_changes_nothing_about_the_case(self):
        run = request_intake_classification(self.new.pk, actor=self.officer)
        reply = {
            "label": "duplicate_of",
            "target_case_number": self.earlier.number,
            "confidence": 0.95,
            "evidence": "Identical text.",
        }
        with patch("apps.records.advisory_ai.invoke_advisory_model", return_value=reply):
            execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE)
        self.assertEqual(run.proposal["label"], "duplicate_of")
        self.assertFalse(run.applied)
        self.new.refresh_from_db()
        self.assertIsNone(self.new.closed_at)
        self.assertIsNone(self.new.merged_into)


class AutomaticIntakeClassificationTests(_Fixture):
    @override_settings(PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED=True)
    def test_a_new_case_is_classified_after_it_commits(self):
        new = self._case()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(defer_automatic_intake_classification(new.pk))
        run = AdvisoryRun.objects.get(request=new, kind=KIND)
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)
        self.assertIsNone(run.requested_by)

    def test_nothing_runs_while_the_switch_is_off(self):
        new = self._case()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertFalse(defer_automatic_intake_classification(new.pk))
        self.assertFalse(AdvisoryRun.objects.filter(kind=KIND).exists())

    @override_settings(
        PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED=True,
        PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED=False,
    )
    def test_intake_defers_the_classification(self):
        from apps.records.intake import submit_intake

        with self.captureOnCommitCallbacks(execute=True), patch(
            "apps.records.intake.defer_automatic_intake_classification",
        ) as defer:
            prr = submit_intake(
                {"email": "new@example.org", "description": "Board minutes."},
            )
        defer.assert_called_once_with(prr.pk)


class IntakeClassificationApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.earlier = self._case(days_ago=3)
        self.new = self._case(days_ago=0)
        self.api = APIClient()
        self.url = f"/api/staff/requests/{self.new.number}/advisory-intake/"

    def _complete_run(self):
        run = request_intake_classification(self.new.pk, actor=self.officer)
        reply = {
            "label": "duplicate_of",
            "target_case_number": self.earlier.number,
            "confidence": 0.9,
            "evidence": "Same text.",
        }
        with patch("apps.records.advisory_ai.invoke_advisory_model", return_value=reply):
            execute_advisory_run(run.pk)
        return run

    def test_an_officer_requests_and_reads_the_classification(self):
        self.api.force_authenticate(self.officer)
        created = self.api.post(self.url)
        self.assertEqual(created.status_code, 202, created.content)
        self.assertEqual(created.json()["kind"], KIND)
        read = self.api.get(self.url)
        self.assertEqual(read.status_code, 200)
        self.assertTrue(read.json()["enabled"])
        self.assertEqual(read.json()["latest"]["id"], created.json()["id"])
        self.assertIsNone(read.json()["feedback"])

    def test_a_reviewer_may_not(self):
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(self.url).status_code, 403)
        self.assertEqual(self.api.post(self.url).status_code, 403)

    def test_feedback_is_recorded_once_per_run(self):
        run = self._complete_run()
        self.api.force_authenticate(self.officer)
        body = {"outcome": "confirmed", "final_label": "duplicate_of", "reason": ""}

        first = self.api.post(self.url + "feedback/", body, format="json")
        second = self.api.post(self.url + "feedback/", body, format="json")

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 409)
        feedback = AdvisoryFeedback.objects.get(advisory_run=run)
        self.assertEqual(feedback.actor, self.officer)
        self.assertEqual(feedback.final_label, "duplicate_of")
        self.assertEqual(self.api.get(self.url).json()["feedback"]["outcome"], "confirmed")

    def test_a_dismissal_records_what_the_officer_judged_instead(self):
        run = self._complete_run()
        self.api.force_authenticate(self.officer)
        response = self.api.post(self.url + "feedback/", {
            "outcome": "dismissed", "final_label": "new_request",
            "reason": "Different school year.",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        feedback = AdvisoryFeedback.objects.get(advisory_run=run)
        self.assertEqual(feedback.outcome, AdvisoryFeedback.Outcome.DISMISSED)
        self.assertEqual(feedback.final_label, "new_request")

    def test_feedback_needs_a_completed_run(self):
        request_intake_classification(self.new.pk, actor=self.officer)
        self.api.force_authenticate(self.officer)
        response = self.api.post(self.url + "feedback/", {
            "outcome": "confirmed", "final_label": "duplicate_of",
        }, format="json")
        self.assertEqual(response.status_code, 400)
