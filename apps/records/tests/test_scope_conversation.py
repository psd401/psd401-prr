"""A new case answers "how big is this?" before anyone clicks anything.

Phase 3 of docs/roadmap-usability.md. The intent proposal gains a date range
and a narrowing menu, and both it and the Vault count it enables run at
intake. None of that touches the requester: a count estimates a search
universe, and an option list is drafting material for an officer.
"""

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.correspondence.automatic_templates import MAX_NARROWING_OPTIONS
from apps.records import advisory_ai, vault_counts
from apps.records.advisory_ai import (
    _validate_proposal,
    execute_advisory_run,
)
from apps.records.intake import submit_intake
from apps.records.models import (
    AdvisoryRun,
    Channel,
    IntakeAttachment,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
    VaultCountRun,
)
from apps.records.tasks import scan_intake_attachment

RECEIVED = dt.date(2026, 6, 15)


def intent_proposal(**overrides):
    proposal = {
        "summary": "Emails about the field renovation contract.",
        "required_search_terms": ["field renovation"],
        "search_terms": ["contract", "bid"],
        "record_system_slugs": [],
        "clarification_questions": [],
        "complexity_flags": [],
        "date_range": {"start": "2026-01-01", "end": "2026-06-01"},
        "narrowing_options": [],
    }
    proposal.update(overrides)
    return proposal


class IntentContractTests(TestCase):
    def validate(self, proposal, *, received_date=RECEIVED):
        return _validate_proposal(
            AdvisoryRun.Kind.REQUEST_INTENT,
            proposal,
            received_date=received_date,
        )

    def test_the_date_range_and_options_come_back_bounded(self):
        result = self.validate(
            intent_proposal(
                narrowing_options=[
                    {
                        "terms": ["field renovation", "bid"],
                        "rationale": "Only the bid correspondence.",
                    }
                ]
            )
        )

        self.assertEqual(
            result["date_range"],
            {"start": "2026-01-01", "end": "2026-06-01"},
        )
        self.assertEqual(
            result["narrowing_options"],
            [
                {
                    "terms": ["field renovation", "bid"],
                    "rationale": "Only the bid correspondence.",
                }
            ],
        )

    def test_an_end_date_past_receipt_is_pulled_back_to_receipt(self):
        """Only records in existence at receipt can be responsive.

        A model that proposes a window running into the future is not merely
        wrong, it is proposing to search for records that did not exist when
        the request arrived.
        """
        result = self.validate(
            intent_proposal(date_range={"start": None, "end": "2026-12-31"})
        )

        self.assertEqual(result["date_range"]["end"], RECEIVED.isoformat())

    def test_a_missing_end_date_defaults_to_receipt(self):
        result = self.validate(
            intent_proposal(date_range={"start": None, "end": None})
        )

        self.assertEqual(result["date_range"]["end"], RECEIVED.isoformat())

    def test_a_start_after_receipt_is_dropped_not_fatal(self):
        """A future start is the model echoing an event, not a bad proposal.

        Refusing it would fail the whole run, so the officer would see no
        proposal and no automatic count. Dropping the start leaves a window
        that runs up to receipt, which the officer then edits.
        """
        result = self.validate(
            intent_proposal(date_range={"start": "2026-09-01", "end": None})
        )

        self.assertEqual(
            result["date_range"],
            {"start": None, "end": RECEIVED.isoformat()},
        )

    def test_a_start_after_the_end_is_refused(self):
        with self.assertRaisesMessage(ValueError, "on or before"):
            self.validate(
                intent_proposal(
                    date_range={"start": "2026-06-10", "end": "2026-01-01"}
                )
            )

    def test_a_malformed_date_is_refused(self):
        with self.assertRaisesMessage(ValueError, "ISO calendar date"):
            self.validate(
                intent_proposal(date_range={"start": "June 2026", "end": None})
            )

    def test_the_date_range_must_have_exactly_start_and_end(self):
        with self.assertRaisesMessage(ValueError, "exactly start and end"):
            self.validate(intent_proposal(date_range={"end": None}))

    def test_a_fifth_narrowing_option_is_refused(self):
        with self.assertRaisesMessage(ValueError, "bounded list"):
            self.validate(
                intent_proposal(
                    narrowing_options=[
                        {"terms": [f"term {index}"], "rationale": "Because."}
                        for index in range(MAX_NARROWING_OPTIONS + 1)
                    ]
                )
            )

    def test_an_option_with_extra_fields_is_refused(self):
        with self.assertRaisesMessage(ValueError, "exactly terms and rationale"):
            self.validate(
                intent_proposal(
                    narrowing_options=[
                        {
                            "terms": ["bid"],
                            "rationale": "Because.",
                            "apply": True,
                        }
                    ]
                )
            )

    def test_an_option_with_no_terms_is_dropped_not_fatal(self):
        # PRR-2026-0038: Nova answered a vague request with no search terms,
        # three clarification questions, and a termless narrowing option.
        # Refusing the whole proposal cost the officer the questions.
        validated = self.validate(
            intent_proposal(
                narrowing_options=[
                    {"terms": [], "rationale": "Ask which school."},
                    {"terms": ["bid"], "rationale": "Only bids."},
                ]
            )
        )

        self.assertEqual(
            validated["narrowing_options"],
            [{"terms": ["bid"], "rationale": "Only bids."}],
        )

    def test_a_proposal_missing_the_new_fields_is_refused(self):
        proposal = intent_proposal()
        del proposal["narrowing_options"]

        with self.assertRaisesMessage(ValueError, "unexpected fields"):
            self.validate(proposal)

    def test_the_prompt_states_the_new_contract(self):
        instruction = advisory_ai._advisory_system_message(
            AdvisoryRun.Kind.REQUEST_INTENT
        )

        self.assertIn("date_range", instruction)
        self.assertIn("narrowing_options", instruction)
        self.assertIn("never propose a denial", instruction)


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED=True,
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class AutomaticIntentAtIntakeTests(TestCase):
    def intake(self):
        # The intent run is deliberately deferred to after commit: intake is
        # the legal record and must be durable before any AI work starts.
        with self.captureOnCommitCallbacks(execute=True):
            return submit_intake(
                {
                    "requester_name": "Jane Public",
                    "requester_email": "jane@example.org",
                    "description": (
                        "Emails about the field renovation contract."
                    ),
                },
                channel=Channel.PORTAL,
            )

    @patch("apps.records.advisory_ai._queue")
    def test_a_new_case_starts_its_own_intent_proposal(self, queue):
        queue.side_effect = lambda run: run

        prr = self.intake()

        run = AdvisoryRun.objects.get(request=prr)
        self.assertEqual(run.kind, AdvisoryRun.Kind.REQUEST_INTENT)
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)
        self.assertIsNone(run.requested_by_id)
        self.assertFalse(run.applied)

    @override_settings(PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED=False)
    @patch("apps.records.advisory_ai._queue")
    def test_nothing_runs_until_the_district_switches_it_on(self, queue):
        queue.side_effect = lambda run: run

        self.intake()

        self.assertFalse(AdvisoryRun.objects.exists())

    @patch("apps.records.advisory_ai._queue")
    def test_intake_survives_an_advisory_that_cannot_be_queued(self, queue):
        """The request is the legal record. AI preparation is not.

        A queue outage must never cost the district a request it received.
        """
        queue.side_effect = ValidationError("The advisory worker is down.")

        prr = self.intake()

        self.assertTrue(
            PublicRecordsRequest.objects.filter(pk=prr.pk).exists()
        )

    def intake_with_a_pending_file(self):
        """Portal intake whose one attachment is still awaiting its scan."""

        def persist(prr, attachments):
            return [
                IntakeAttachment.objects.create(
                    request=prr,
                    original_filename="map.pdf",
                    content_type="application/pdf",
                    size=42,
                    sha256="a" * 64,
                    storage_key=f"quarantine/intake/{prr.id}/map",
                )
            ]

        with patch("apps.records.intake.persist_intake_attachments", persist):
            with self.captureOnCommitCallbacks(execute=True):
                return submit_intake(
                    {
                        "requester_name": "Jane Public",
                        "requester_email": "jane@example.org",
                        "description": "Emails about the field renovation contract.",
                    },
                    channel=Channel.PORTAL,
                    attachments=[object()],
                )

    @patch("apps.records.advisory_ai._queue")
    def test_a_case_with_an_unscanned_file_waits_for_the_scan(self, queue):
        """The proposal reads the requester's files, so it cannot start first.

        Starting before the scan would fingerprint an input the worker then
        sees change, failing every attachment-bearing request as
        source_changed.
        """
        queue.side_effect = lambda run: run

        self.intake_with_a_pending_file()

        self.assertFalse(AdvisoryRun.objects.exists())

    @patch("apps.records.advisory_ai._queue")
    def test_the_last_scan_verdict_starts_the_proposal(self, queue):
        queue.side_effect = lambda run: run
        prr = self.intake_with_a_pending_file()
        attachment = prr.intake_attachments.get()

        with (
            patch(
                "apps.records.attachments.scan_object_for_malware",
                return_value=(True, "clean"),
            ),
            patch("apps.records.attachments.promote_clean_attachment"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            scan_intake_attachment(str(attachment.id))

        run = AdvisoryRun.objects.get(request=prr)
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)

    @patch("apps.records.advisory_ai._queue")
    def test_a_repeated_trigger_reuses_the_same_proposal(self, queue):
        queue.side_effect = lambda run: run
        prr = self.intake()

        with self.captureOnCommitCallbacks(execute=True):
            advisory_ai.defer_automatic_request_intent_advisory(prr.pk)

        self.assertEqual(AdvisoryRun.objects.filter(request=prr).count(), 1)

    def test_the_automatic_path_refuses_when_the_switch_is_off(self):
        prr = self.intake()
        with override_settings(PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED=False):
            with self.assertRaisesMessage(ValidationError, "not enabled"):
                advisory_ai.request_automatic_request_intent_advisory(prr.pk)


class VaultQueryFromIntentTests(TestCase):
    """The automatic count and the officer's Count button build one query.

    The staff workspace builds the same query in TypeScript
    (frontend/src/vaultQuery.ts). Both suites read one case file, so a
    change to either side that the other does not share fails loudly.
    """

    def test_every_shared_case_builds_the_expected_query(self):
        cases = json.loads(
            (
                Path(settings.BASE_DIR) / "frontend/src/vaultQueryCases.json"
            ).read_text()
        )
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case["name"]):
                self.assertEqual(
                    vault_counts.vault_query_from_intent(
                        case["required"], case["alternative"]
                    ),
                    case["expected"],
                )


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED=True,
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_VAULT_ADMIN_USER="vault-admin@example.org",
    PRR_VAULT_IMPERSONATION_TARGET=(
        "prr-vault@psd401-prr-identity.iam.gserviceaccount.com"
    ),
    PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
)
class AutomaticVaultCountTests(TestCase):
    def setUp(self):
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="jane@example.org"),
            description="Emails about the field renovation contract.",
            received_at=timezone.make_aware(
                dt.datetime(2026, 6, 15, 9, 0),
            ),
        )
        self.vault_system = RecordSystem.objects.create(
            slug="google-workspace",
            name="Google Workspace",
            integration=RecordSystem.Integration.VAULT,
            active=True,
        )
        RecordSystem.objects.create(
            slug="avigilon-alta",
            name="Cameras",
            integration=RecordSystem.Integration.AVIGILON,
            active=True,
        )

    def count(self, **overrides):
        proposal = intent_proposal(
            record_system_slugs=["google-workspace"],
            **overrides,
        )
        with patch("apps.records.tasks.execute_vault_count.delay"):
            return vault_counts.request_automatic_vault_count_from_intent(
                self.request.pk,
                proposal,
            )

    def test_the_count_searches_the_whole_district_through_receipt(self):
        run = self.count()

        self.assertEqual(run.trigger, VaultCountRun.Trigger.AUTOMATIC)
        self.assertIsNone(run.requested_by_id)
        self.assertEqual(run.search_scope, VaultCountRun.SearchScope.ENTIRE_ORG)
        self.assertEqual(run.custodian_emails, [])
        self.assertEqual(
            run.terms,
            '"field renovation" AND ("contract" OR "bid")',
        )
        self.assertEqual(run.date_range_start, dt.date(2026, 1, 1))
        self.assertEqual(run.date_range_end, dt.date(2026, 6, 1))

    def test_an_end_date_past_receipt_never_reaches_vault(self):
        run = self.count(date_range={"start": None, "end": "2026-12-31"})

        self.assertEqual(run.date_range_end, dt.date(2026, 6, 15))

    def test_no_count_runs_when_the_proposal_names_no_vault_system(self):
        with patch("apps.records.tasks.execute_vault_count.delay"):
            run = vault_counts.request_automatic_vault_count_from_intent(
                self.request.pk,
                intent_proposal(record_system_slugs=["avigilon-alta"]),
            )

        self.assertIsNone(run)
        self.assertFalse(VaultCountRun.objects.exists())

    def test_no_count_runs_when_the_vault_system_is_retired(self):
        self.vault_system.active = False
        self.vault_system.save(update_fields=["active"])

        self.assertIsNone(self.count())

    def test_no_count_runs_without_a_query_to_run(self):
        self.assertIsNone(
            self.count(required_search_terms=[], search_terms=[])
        )

    @override_settings(PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED=False)
    def test_no_count_runs_until_the_district_switches_it_on(self):
        self.assertIsNone(self.count())

    def test_an_officer_scoped_count_still_needs_an_accountable_actor(self):
        with self.assertRaisesMessage(ValidationError, "accountable actor"):
            vault_counts.request_vault_count(
                self.request.pk,
                actor=None,
                terms='"bid"',
                custodian_emails=[],
                search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
            )

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_a_completed_automatic_proposal_chains_straight_into_a_count(
        self,
        invoke,
    ):
        invoke.return_value = intent_proposal(
            record_system_slugs=["google-workspace"],
        )
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="",
        )
        run.input_sha256 = advisory_ai.advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        with patch("apps.records.tasks.execute_vault_count.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                execute_advisory_run(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE, run.failure_code)
        count = VaultCountRun.objects.get(request=self.request)
        self.assertEqual(count.trigger, VaultCountRun.Trigger.AUTOMATIC)

    @patch("apps.records.advisory_ai.invoke_advisory_model")
    def test_an_officer_run_does_not_silently_start_a_count(self, invoke):
        """A Run button is a request for a proposal, not for a Vault search.

        The officer already has a Count button, and it is theirs to press with
        their own edited terms.
        """
        from django.contrib.auth import get_user_model

        from apps.api.identity import apply_staff_roles
        from apps.api.models import StaffRole

        officer = get_user_model().objects.create_user(
            "scope-officer",
            "scope-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        invoke.return_value = intent_proposal(
            record_system_slugs=["google-workspace"],
        )
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            trigger=AdvisoryRun.Trigger.MANUAL,
            requested_by=officer,
            model_identifier="district-approved-model-v1",
            input_sha256="",
        )
        run.input_sha256 = advisory_ai.advisory_input_sha256(run)
        run.save(update_fields=["input_sha256"])

        with patch("apps.records.tasks.execute_vault_count.delay"):
            with self.captureOnCommitCallbacks(execute=True):
                execute_advisory_run(run.pk)

        self.assertFalse(VaultCountRun.objects.exists())
