"""The fixture cases that need real PDFs (#40, #46) seed only with the e2e
filesystem store, and then seed records the review services can open."""

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.correspondence.management.commands.seed_casework_test_fixture import (
    FIXTURE_FLIGHT_RECORDS,
    FIXTURE_FLIGHT_REQUEST_NUMBER,
    FIXTURE_REDACTION_RECORD_COUNT,
    FIXTURE_REDACTION_REQUEST_NUMBER,
    _seed_redaction_proposal_case,
    _seed_review_flight_case,
)
from apps.records.models import RecordItem, RedactionProposal
from apps.records.preview import preview_pdf_metadata

AI = {
    "PRR_AI_ADVISORY_ENABLED": True,
    "PRR_AI_AWS_REGION": "us-west-2",
    "PRR_AI_MODEL": "district-approved-model-v1",
    "PRR_AI_MAX_INPUT_CHARACTERS": 50_000,
}


class FixturePdfCaseTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(
            username="fixture-pdf-officer", email="fpo@psd401.net", is_staff=True,
        )
        self.root = tempfile.mkdtemp(prefix="prr-fixture-pdf-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_without_the_local_store_nothing_is_seeded(self):
        self.assertIsNone(_seed_review_flight_case(self.officer))
        self.assertIsNone(_seed_redaction_proposal_case(self.officer))
        self.assertFalse(RecordItem.objects.exists())

    def test_with_the_local_store_the_pdfs_open_and_seeding_is_repeatable(self):
        with override_settings(
            PRR_E2E_FILESYSTEM_OBJECT_STORE=self.root, PRR_TEST_FIXTURE_SEED_ENABLED=True, **AI,
        ):
            flight = _seed_review_flight_case(self.officer)
            redaction = _seed_redaction_proposal_case(self.officer)
            assert flight is not None and redaction is not None
            self.assertEqual(flight.number, FIXTURE_FLIGHT_REQUEST_NUMBER)
            self.assertEqual(redaction.number, FIXTURE_REDACTION_REQUEST_NUMBER)
            items = RecordItem.objects.filter(request=flight)
            self.assertEqual(items.count(), len(FIXTURE_FLIGHT_RECORDS))
            self.assertEqual(preview_pdf_metadata(items[0].pk)["page_count"], 1)
            self.assertEqual(
                RedactionProposal.objects.filter(record_item__request=redaction).count(),
                FIXTURE_REDACTION_RECORD_COUNT,
            )
            # A second seed finds the cases and adds nothing.
            _seed_review_flight_case(self.officer)
            _seed_redaction_proposal_case(self.officer)
            self.assertEqual(
                RecordItem.objects.count(), len(FIXTURE_FLIGHT_RECORDS) + FIXTURE_REDACTION_RECORD_COUNT,
            )
