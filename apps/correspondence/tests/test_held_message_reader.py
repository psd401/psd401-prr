# ABOUTME: An officer can read a held (unparseable) email as plain text.
# ABOUTME: Headers and text parts only; attachments are named, never opened.
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TransactionTestCase

from apps.api.identity import StaffRole, apply_staff_roles
from apps.audit.models import AuditLog
from apps.correspondence import held_messages
from apps.correspondence.tests.test_inbound_reparse import (
    REPARSE_SETTINGS,
    ReparseFixture,
)


@REPARSE_SETTINGS
class HeldMessageReaderTests(ReparseFixture):
    def setUp(self):
        super().setUp()
        self.officer = get_user_model().objects.create_user(
            "held-reader", "held-reader@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

    def read(self, receipt, actor=None):
        with patch("apps.secure_files.open_private_object", side_effect=self._open):
            return held_messages.readable_held_message(
                receipt.pk, actor=actor or self.officer
            )

    def test_shows_sender_subject_text_and_names_attachments(self):
        receipt = self._quarantined_receipt()

        message = self.read(receipt)

        headers = dict(message["headers"])
        self.assertEqual(headers["From"], '"Doe, Dana" <dana@psd401.net>')
        self.assertEqual(headers["Subject"], "Fwd: Data request")
        self.assertIn("Please see the forwarded request.", message["body"])
        self.assertEqual(message["attachments"], ["image001.png", "questionnaire.csv"])
        self.assertFalse(message["raw_source"])
        self.assertTrue(
            AuditLog.objects.filter(
                action="inbound.held_message_read", target=str(receipt.pk)
            ).exists()
        )

    def test_unparseable_bytes_fall_back_to_the_raw_source_as_text(self):
        receipt = self._quarantined_receipt(
            raw=b"Subject: broken\r\n\r\n\x00garbled \xff body\x07",
            message_id="m-2",
        )

        message = self.read(receipt)

        self.assertIn("garbled", message["body"])
        self.assertNotIn("\x00", message["body"])
        self.assertNotIn("\x07", message["body"])

    def test_only_a_records_officer_may_read_it(self):
        receipt = self._quarantined_receipt()
        reviewer = get_user_model().objects.create_user(
            "held-reviewer", "held-reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})

        with self.assertRaises(PermissionDenied):
            self.read(receipt, actor=reviewer)

    def test_a_parsed_message_is_not_a_held_message(self):
        parsed = SimpleNamespace(pk="receipt-1", parsing_status="parsed")

        with (
            patch.object(
                held_messages.InboundCorrespondenceReceipt.objects,
                "get",
                return_value=parsed,
            ),
            self.assertRaisesMessage(ValidationError, "not held"),
        ):
            self.read(parsed)

    def test_the_staff_api_returns_the_readable_email(self):
        receipt = self._quarantined_receipt()
        self.client.force_login(self.officer)

        with patch("apps.secure_files.open_private_object", side_effect=self._open):
            response = self.client.get(
                f"/api/staff/inbound-correspondence-receipts/{receipt.pk}/readable/"
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Please see the forwarded request.", response.json()["body"])


class HeldMessageReaderTransactionTests(TransactionTestCase):
    """Postgres refuses select_for_update outside a transaction; SQLite and
    the ordinary TestCase (one big transaction) hide that, so check it."""

    def test_the_officer_check_runs_inside_a_transaction(self):
        officer = get_user_model().objects.create_user(
            "held-tx", "held-tx@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        seen = []

        def spy(actor, **kwargs):
            seen.append(transaction.get_connection().in_atomic_block)
            raise PermissionDenied("stop here")

        with (
            patch.object(held_messages, "require_current_human_staff", spy),
            self.assertRaises(PermissionDenied),
        ):
            held_messages.readable_held_message("unused", actor=officer)
        self.assertEqual(seen, [True])
