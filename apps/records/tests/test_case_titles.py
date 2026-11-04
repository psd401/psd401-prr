"""The advisory case title: a label for one column, and nothing else.

A case list that falls back to the first 140 characters of the request text
shows, for an emailed request, whatever the sender's mail client put at the
top -- a name, a doctorate, a job title, a pronoun line -- before it says
anything about records. The case-title advisory proposes the missing sentence:
a neutral noun phrase naming what was asked for.

It is a new advisory *kind* rather than a reuse of request intent, and this
module is mostly about why that distinction has to hold at runtime. A
completed automatic request-intent proposal can start an unattended Google
Vault search count (AGENTS.md rule 11). A title proposes no query, names no
record system, and reads no attachment, so there is nothing for a count to be
built from -- which is what makes running it unattended on every new case a
different decision, on its own switch, from running intent unattended.

Nothing here reaches a requester, moves a status, or touches a deadline, and
``AdvisoryRun.applied`` stays false by database constraint, exactly as it does
for every other kind.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.correspondence.models import Correspondence
from apps.records.advisory_ai import (
    CASE_TITLE_MAX_LENGTH,
    _advisory_system_message,
    _validate_proposal,
    defer_automatic_case_title_advisory,
    execute_advisory_run,
    request_automatic_case_title_advisory,
    request_case_title_advisory,
)
from apps.records.models import (
    AdvisoryRun,
    Channel,
    PublicRecordsRequest,
    Requester,
)

ADVISORY_ON = override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
    PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED=True,
)

#: The shape of an emailed request as it actually arrives: the person before
#: the records. This is the text the description snippet was showing.
EMAIL_BODY = (
    "Records Request\n"
    "Jordan Reyes, Ed.D. (she/her)\n"
    "Chief of Schools\n"
    "Somewhere School District\n"
    "\n"
    "Please provide every email between the superintendent and the stadium "
    "contractor between January and March 2026."
)


class CaseTitleProposalTests(TestCase):
    """Validation, the instruction text, and what the model may return."""

    def test_the_proposal_carries_one_bounded_string_and_nothing_else(self):
        validated = _validate_proposal(
            AdvisoryRun.Kind.CASE_TITLE, {"title": "  Stadium contractor emails  "}
        )
        self.assertEqual(validated, {"title": "Stadium contractor emails"})

    def test_an_extra_key_is_refused(self):
        with self.assertRaises(ValueError):
            _validate_proposal(
                AdvisoryRun.Kind.CASE_TITLE,
                {"title": "Stadium emails", "summary": "…"},
            )

    def test_a_missing_title_is_refused(self):
        with self.assertRaises(ValueError):
            _validate_proposal(AdvisoryRun.Kind.CASE_TITLE, {})

    def test_a_slightly_long_title_is_shortened_at_a_word(self):
        """A model that ran a few words past the bound still named the case.
        Deterministic decoding returns the same over-long title every time,
        so refusing it would leave the case unnamed for good."""
        long_title = (
            "Emails and text messages between the superintendent and board "
            "members about the 2026 bond measure"
        )
        self.assertGreater(len(long_title), CASE_TITLE_MAX_LENGTH)

        title = _validate_proposal(
            AdvisoryRun.Kind.CASE_TITLE, {"title": long_title}
        )["title"]

        self.assertLessEqual(len(title), CASE_TITLE_MAX_LENGTH)
        self.assertTrue(title.endswith("…"))
        # Cut between words, never through one.
        self.assertTrue(long_title.startswith(title[:-1].rstrip()))
        self.assertEqual(
            long_title[len(title[:-1].rstrip())], " ",
        )

    def test_a_title_within_the_bound_is_left_alone(self):
        title = "x" * CASE_TITLE_MAX_LENGTH
        self.assertEqual(
            _validate_proposal(AdvisoryRun.Kind.CASE_TITLE, {"title": title}),
            {"title": title},
        )

    def test_a_paragraph_instead_of_a_title_is_refused(self):
        """Far past the bound, the model ignored the instruction; the start
        of a paragraph is not a title."""
        with self.assertRaises(ValueError):
            _validate_proposal(
                AdvisoryRun.Kind.CASE_TITLE, {"title": "word " * 60},
            )

    def test_one_unbroken_overlong_token_is_refused(self):
        with self.assertRaises(ValueError):
            _validate_proposal(
                AdvisoryRun.Kind.CASE_TITLE,
                {"title": "x" * (CASE_TITLE_MAX_LENGTH + 1)},
            )

    def test_an_empty_title_is_refused(self):
        with self.assertRaises(ValueError):
            _validate_proposal(AdvisoryRun.Kind.CASE_TITLE, {"title": "   "})

    def test_the_instruction_forbids_describing_the_requester_or_an_outcome(self):
        message = _advisory_system_message(AdvisoryRun.Kind.CASE_TITLE)
        self.assertIn("advisory-only", message)
        self.assertIn("never include or describe the requester", message.lower())
        self.assertIn("never state or imply an outcome", message.lower())
        for forbidden in ("exemption", "denial", "fee", "deadline"):
            self.assertIn(forbidden, message.lower())


@ADVISORY_ON
class CaseTitleRunTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "title-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = User.objects.create_user(
            "title-reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description=EMAIL_BODY,
            channel=Channel.EMAIL,
        )

    def test_an_automatic_run_needs_no_accountable_actor(self):
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_case_title_advisory(self.prr.pk)
        self.assertEqual(run.kind, AdvisoryRun.Kind.CASE_TITLE)
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)
        self.assertIsNone(run.requested_by)
        self.assertIsNone(run.record_item_id)
        self.assertFalse(run.applied)

    def test_a_manual_run_is_records_officer_only(self):
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_case_title_advisory(self.prr.pk, actor=self.officer)
        self.assertEqual(run.requested_by, self.officer)
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.MANUAL)
        with self.assertRaises(PermissionDenied):
            request_case_title_advisory(self.prr.pk, actor=self.reviewer)

    @override_settings(PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED=False)
    def test_the_automatic_path_refuses_while_its_own_switch_is_off(self):
        with self.assertRaises(ValidationError):
            request_automatic_case_title_advisory(self.prr.pk)

    @override_settings(PRR_AI_ADVISORY_ENABLED=False)
    def test_nothing_runs_while_advisory_ai_itself_is_off(self):
        with self.assertRaises(ValidationError):
            request_automatic_case_title_advisory(self.prr.pk)
        self.assertFalse(defer_automatic_case_title_advisory(self.prr.pk))

    def test_the_intent_switch_does_not_govern_case_titles(self):
        """Two switches, two decisions. Turning intent off must not silently
        turn titles off too: intent is the one whose completed proposal can
        start an unattended Vault count, and that is the decision the district
        approved separately."""
        with override_settings(PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED=False):
            with patch(
                "apps.records.advisory_ai._queue", side_effect=lambda run: run
            ):
                run = request_automatic_case_title_advisory(self.prr.pk)
        self.assertEqual(run.kind, AdvisoryRun.Kind.CASE_TITLE)

    def test_the_same_request_text_reuses_one_run(self):
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            first = request_automatic_case_title_advisory(self.prr.pk)
            second = request_automatic_case_title_advisory(self.prr.pk)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            AdvisoryRun.objects.filter(kind=AdvisoryRun.Kind.CASE_TITLE).count(), 1
        )

    def test_the_input_is_the_request_text_and_the_inbound_subject(self):
        Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Public records request - stadium contractor",
            received_body=EMAIL_BODY,
            received_at=timezone.now(),
        )
        captured = {}

        def fake_invoke(kind, payload, *, attachment_blocks=None, model_identifier=None):
            captured["kind"] = kind
            captured["payload"] = payload
            captured["attachment_blocks"] = attachment_blocks
            return {"title": "Superintendent emails with the stadium contractor"}

        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_case_title_advisory(self.prr.pk)
        with patch(
            "apps.records.advisory_ai.invoke_advisory_model", side_effect=fake_invoke
        ):
            execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE)
        self.assertEqual(
            run.proposal,
            {"title": "Superintendent emails with the stadium contractor"},
        )
        self.assertFalse(run.applied)
        payload = captured["payload"]
        self.assertEqual(payload["request_description"], EMAIL_BODY)
        self.assertEqual(
            payload["request_email_subject"],
            "Public records request - stadium contractor",
        )
        # No attachments, no record systems, no exemption taxonomy: there is
        # nothing in this payload a search query could be assembled from.
        self.assertEqual(captured["attachment_blocks"], [])
        self.assertNotIn("available_record_systems", payload)

    def test_a_case_with_no_inbound_email_sends_an_empty_subject(self):
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_case_title_advisory(self.prr.pk)
        from apps.records.advisory_ai import _build_advisory_input

        payload = _build_advisory_input(run).payload
        self.assertEqual(payload["request_email_subject"], "")

    def test_a_completed_run_starts_no_vault_count(self):
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_case_title_advisory(self.prr.pk)
        with (
            patch(
                "apps.records.advisory_ai.invoke_advisory_model",
                return_value={"title": "Stadium contractor emails"},
            ),
            patch(
                "apps.records.vault_counts.defer_automatic_vault_count_from_intent"
            ) as vault_count,
        ):
            execute_advisory_run(run.pk)
        vault_count.assert_not_called()

    def test_a_completed_run_sends_nothing_and_moves_no_status(self):
        before = self.prr.status
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_case_title_advisory(self.prr.pk)
        with patch(
            "apps.records.advisory_ai.invoke_advisory_model",
            return_value={"title": "Stadium contractor emails"},
        ):
            execute_advisory_run(run.pk)
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, before)
        self.assertEqual(self.prr.description, EMAIL_BODY)
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertEqual(
            Correspondence.objects.filter(request=self.prr).count(), 0
        )

    def test_an_overlong_title_fails_the_run_rather_than_landing(self):
        """Validation is the worker's, not the model's. Output far past the
        bound (not a title at all) leaves the case with the description
        snippet it had."""
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_case_title_advisory(self.prr.pk)
        with patch(
            "apps.records.advisory_ai.invoke_advisory_model",
            return_value={"title": "x" * 500},
        ):
            execute_advisory_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.FAILED)
        self.assertEqual(run.failure_code, "invalid_model_output")
        self.assertEqual(run.proposal, {})

    def test_the_database_refuses_an_applied_title_run(self):
        """The architectural boundary is a constraint, not a convention: no
        model output is ever an applied legal outcome, for this kind either."""
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_case_title_advisory(self.prr.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdvisoryRun.objects.filter(pk=run.pk).update(applied=True)

    def test_a_case_title_run_may_not_name_a_record_item(self):
        """The database, not a convention, keeps the kind pointed at a case."""
        from apps.records.models import RecordItem

        item = RecordItem.objects.create(
            request=self.prr,
            source_system="gmail",
            original_filename="message.pdf",
            storage_key="records/source.pdf",
            content_type="application/pdf",
            content_length=10,
            sha256="0" * 64,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.CASE_TITLE,
                request=self.prr,
                record_item=item,
                requested_by=self.officer,
                model_identifier="m",
                input_sha256="0" * 64,
            )


@ADVISORY_ON
class CaseTitleIntakeTests(TestCase):
    """Every channel gets a title; none of them waits on a file scan."""

    def test_intake_defers_a_title_run_for_a_web_form_request(self):
        from apps.records.intake import submit_intake

        with patch(
            "apps.records.intake.defer_automatic_case_title_advisory"
        ) as deferred:
            prr = submit_intake(
                {"email": "requester@example.org", "description": EMAIL_BODY},
                channel=Channel.PORTAL,
            )
        deferred.assert_called_once_with(prr.pk)

    def test_the_deferred_call_is_a_no_op_while_the_switch_is_off(self):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="r@example.org"),
            description=EMAIL_BODY,
        )
        with override_settings(PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED=False):
            self.assertFalse(defer_automatic_case_title_advisory(prr.pk))
        self.assertFalse(AdvisoryRun.objects.exists())


# The ``backfill_case_titles`` command has its own module,
# ``test_backfill_case_titles.py``: dry run, scope, idempotency, bounds, the
# switches it refuses without, and that a pass changes nothing about a case.
