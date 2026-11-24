# ABOUTME: An officer releases a held email and files it: to a case or as a new request.
# ABOUTME: Strict parser first; when it still refuses, the plain text the officer read.
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import TransactionTestCase
from rest_framework.authtoken.models import Token

from apps.api.identity import StaffRole, apply_staff_roles
from apps.audit.models import AuditLog
from apps.correspondence import held_messages, inbound
from apps.correspondence.mime import InboundMimeSecurityError
from apps.correspondence.models import (
    CorrespondenceAttachment,
    InboundCorrespondenceReceipt,
)
from apps.correspondence.services import VersionConflict
from apps.correspondence.tests.test_inbound_reparse import (
    GMAIL_FORWARD,
    REPARSE_SETTINGS,
    ReparseFixture,
)
from apps.records import email_intake
from apps.records.models import (
    Channel,
    IntakeAttachment,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)

REASON = "The requester replied to the closed case; this is readable."
STRICT_REFUSES = patch(
    "apps.correspondence.receiving.parse_inbound_message",
    side_effect=InboundMimeSecurityError("malformed_message"),
)


@REPARSE_SETTINGS
class HeldMessageReleaseTests(ReparseFixture):
    def setUp(self):
        super().setUp()
        self.officer = User.objects.create_user(
            "held-release", "held-release@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

    def _storage(self):
        return (
            patch(
                "apps.secure_files.store_quarantined_object",
                side_effect=self._store,
            ),
            patch(
                "apps.secure_files.open_private_object",
                side_effect=self._open,
            ),
            patch("apps.correspondence.receiving._enqueue_attachment_scan"),
        )

    def release(self, receipt, *, actor=None, reason=REASON, version=None):
        store, open_, scan = self._storage()
        with store, open_, scan, self.captureOnCommitCallbacks(execute=True):
            return held_messages.release_held_message(
                receipt.pk,
                actor=actor or self.officer,
                reason=reason,
                expected_version=(
                    receipt.version if version is None else version
                ),
            )

    def test_parser_release_keeps_attachments_and_waits_for_the_officer(self):
        receipt = self._quarantined_receipt()

        released = self.release(receipt)

        self.assertEqual(released.parsing_status, "parsed")
        self.assertFalse(released.quarantined)
        # The officer files it; nothing matches it automatically.
        self.assertEqual(released.matching_status, "unmatched")
        self.assertIsNone(released.matched_request)
        self.assertEqual(
            released.matched_correspondence.received_subject,
            "Fwd: Data request",
        )
        self.assertEqual(
            CorrespondenceAttachment.objects.filter(
                inbound_receipt=released
            ).count(),
            2,
        )
        event = AuditLog.objects.get(action="inbound.held_message_released")
        self.assertEqual(event.target, str(receipt.pk))
        self.assertEqual(event.detail["reason"], REASON)
        self.assertEqual(event.detail["read_as"], "parser")
        self.assertEqual(event.detail["previous_reason"], "malformed_message")

    def test_text_release_when_the_parser_still_refuses(self):
        receipt = self._quarantined_receipt()

        with STRICT_REFUSES:
            released = self.release(receipt)

        self.assertEqual(released.parsing_status, "parsed")
        self.assertEqual(released.matching_status, "unmatched")
        correspondence = released.matched_correspondence
        self.assertEqual(correspondence.received_subject, "Fwd: Data request")
        self.assertEqual(
            correspondence.received_sender, '"Doe, Dana" <dana@psd401.net>'
        )
        self.assertIn(
            "Please see the forwarded request.", correspondence.received_body
        )
        # Attachments are never opened on a text release; they stay in the
        # held original.
        self.assertFalse(
            CorrespondenceAttachment.objects.filter(
                inbound_receipt=released
            ).exists()
        )
        event = AuditLog.objects.get(action="inbound.held_message_released")
        self.assertEqual(event.detail["read_as"], "text")
        self.assertEqual(event.detail["attachments_left_held"], 2)

    def test_only_a_records_officer_may_release(self):
        receipt = self._quarantined_receipt()
        reviewer = User.objects.create_user(
            "held-release-reviewer", "r@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})

        with self.assertRaises(PermissionDenied):
            self.release(receipt, actor=reviewer)
        receipt.refresh_from_db()
        self.assertEqual(receipt.parsing_status, "quarantined")

    def test_a_reason_is_required(self):
        receipt = self._quarantined_receipt()

        with self.assertRaises(ValidationError):
            self.release(receipt, reason="ok")

    def test_a_stale_version_is_refused(self):
        receipt = self._quarantined_receipt()

        with self.assertRaises(VersionConflict):
            self.release(receipt, version=receipt.version + 5)

    def test_a_released_message_cannot_be_released_twice(self):
        receipt = self._quarantined_receipt()
        released = self.release(receipt)

        with self.assertRaisesMessage(ValidationError, "not held"):
            self.release(released)

    def test_tampered_evidence_is_refused(self):
        receipt = self._quarantined_receipt()
        self.stored[receipt.raw_storage_key] = GMAIL_FORWARD + b"tampered"

        with self.assertRaisesMessage(ValidationError, "fingerprint"):
            self.release(receipt)

    def test_a_released_message_can_be_added_to_a_closed_case(self):
        requester = Requester.objects.create(email="jane@example.org")
        closed = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Records sought.",
            status=RequestStatus.CLOSED_WITHDRAWN,
        )
        receipt = self._quarantined_receipt()
        released = self.release(receipt)

        with self.captureOnCommitCallbacks(execute=True):
            matched = inbound.match_inbound_receipt(
                released,
                request=closed,
                actor=self.officer,
                reason=REASON,
                expected_version=released.version,
            )

        self.assertEqual(matched.matched_request, closed)

    # -- Opening a new request from a released (or any unmatched) email --

    def open_request(self, receipt, *, actor=None, version=None):
        with (
            patch("apps.correspondence.receiving._enqueue_attachment_scan"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            return email_intake.open_request_from_email(
                receipt,
                actor=actor or self.officer,
                reason=REASON,
                expected_version=(
                    receipt.version if version is None else version
                ),
            )

    def test_open_a_new_request_from_a_released_email(self):
        receipt = self._quarantined_receipt()
        released = self.release(receipt)

        request = self.open_request(released)

        self.assertEqual(request.channel, Channel.EMAIL)
        self.assertEqual(request.requester.email, "dana@psd401.net")
        self.assertIn("Please see the forwarded request.", request.description)
        released.refresh_from_db()
        self.assertEqual(released.matching_status, "matched")
        self.assertEqual(released.match_method, "email_intake")
        self.assertEqual(released.matched_request, request)
        self.assertEqual(
            IntakeAttachment.objects.filter(request=request).count(), 2
        )
        event = AuditLog.objects.get(action="request.opened_from_email")
        self.assertEqual(event.target, request.number)
        self.assertEqual(event.detail["reason"], REASON)
        self.assertEqual(event.detail["receipt_id"], str(released.pk))

    def test_a_forward_opens_under_the_original_sender(self):
        raw = GMAIL_FORWARD.replace(
            b"Please see the forwarded request.\r\n",
            (
                b"---------- Forwarded message ---------\r\n"
                b"From: Pat Analyst <pat.analyst@salarysurvey.example>\r\n"
                b"Date: Wed, Oct 14, 2026 at 9:05 AM\r\n\r\n"
                b"Please complete the questionnaire.\r\n"
            ),
        )
        released = self.release(self._quarantined_receipt(raw=raw))

        request = self.open_request(released)

        self.assertEqual(
            request.requester.email, "pat.analyst@salarysurvey.example"
        )

    def test_a_held_email_cannot_open_a_request(self):
        receipt = self._quarantined_receipt()

        with self.assertRaises(ValidationError):
            self.open_request(receipt)
        self.assertFalse(PublicRecordsRequest.objects.exists())

    def test_only_a_records_officer_may_open_a_request(self):
        released = self.release(self._quarantined_receipt())
        reviewer = User.objects.create_user(
            "open-reviewer", "o@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})

        with self.assertRaises(PermissionDenied):
            self.open_request(released, actor=reviewer)
        self.assertFalse(PublicRecordsRequest.objects.exists())

    def test_a_matched_email_cannot_open_a_second_request(self):
        released = self.release(self._quarantined_receipt())
        self.open_request(released)
        released.refresh_from_db()

        with self.assertRaises(ValidationError):
            self.open_request(released)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)

    # -- Staff API --

    def test_the_staff_api_releases_then_opens_a_request(self):
        receipt = self._quarantined_receipt()
        self.client.force_login(self.officer)
        store, open_, scan = self._storage()
        base = f"/api/staff/inbound-correspondence-receipts/{receipt.pk}"

        with store, open_, scan, self.captureOnCommitCallbacks(execute=True):
            released = self.client.post(
                f"{base}/release/",
                {"reason": REASON, "expected_version": receipt.version},
                content_type="application/json",
            )
        self.assertEqual(released.status_code, 200, released.content)
        body = released.json()
        self.assertEqual(body["parsing_status"], "parsed")
        self.assertEqual(body["matching_status"], "unmatched")

        with scan, self.captureOnCommitCallbacks(execute=True):
            opened = self.client.post(
                f"{base}/open-request/",
                {"reason": REASON, "expected_version": body["version"]},
                content_type="application/json",
            )
        self.assertEqual(opened.status_code, 200, opened.content)
        self.assertEqual(opened.json()["matching_status"], "matched")
        self.assertTrue(opened.json()["matched_request_number"])

    def test_the_staff_api_reports_a_stale_version_as_a_conflict(self):
        receipt = self._quarantined_receipt()
        self.client.force_login(self.officer)
        store, open_, scan = self._storage()

        with store, open_, scan:
            response = self.client.post(
                f"/api/staff/inbound-correspondence-receipts/{receipt.pk}/release/",
                {"reason": REASON, "expected_version": receipt.version + 9},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 409, response.content)

    def test_token_callers_are_handed_off_to_a_person(self):
        receipt = self._quarantined_receipt()
        token = Token.objects.create(user=self.officer)

        for action in ("release", "open-request"):
            response = self.client.post(
                f"/api/staff/inbound-correspondence-receipts/{receipt.pk}/{action}/",
                {"reason": REASON, "expected_version": receipt.version},
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Token {token.key}",
            )
            self.assertEqual(response.status_code, 403, response.content)
            self.assertEqual(
                response.json()["code"], "human_approval_required"
            )
        receipt.refresh_from_db()
        self.assertEqual(receipt.parsing_status, "quarantined")


class HeldMessageReleaseTransactionTests(TransactionTestCase):
    """Postgres refuses select_for_update outside a transaction; SQLite and
    the ordinary TestCase (one big transaction) hide that, so check it."""

    def _officer(self, name):
        officer = User.objects.create_user(
            name, f"{name}@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        return officer

    def _spy(self, seen):
        def spy(actor, **kwargs):
            seen.append(transaction.get_connection().in_atomic_block)
            raise PermissionDenied("stop here")

        return spy

    def test_release_checks_the_officer_inside_a_transaction(self):
        seen = []
        with (
            patch.object(
                held_messages, "require_current_human_staff", self._spy(seen)
            ),
            self.assertRaises(PermissionDenied),
        ):
            held_messages.release_held_message(
                "unused",
                actor=self._officer("release-tx"),
                reason=REASON,
                expected_version=1,
            )
        self.assertEqual(seen, [True])

    def test_open_request_checks_the_officer_inside_a_transaction(self):
        seen = []
        receipt = InboundCorrespondenceReceipt(pk="unused")
        with (
            patch.object(
                email_intake, "require_current_human_staff", self._spy(seen)
            ),
            self.assertRaises(PermissionDenied),
        ):
            email_intake.open_request_from_email(
                receipt,
                actor=self._officer("open-tx"),
                reason=REASON,
                expected_version=1,
            )
        self.assertEqual(seen, [True])
