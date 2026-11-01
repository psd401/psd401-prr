# ABOUTME: Tests for adding an auto-created email case's message to an
# ABOUTME: existing case in one atomic, audited, human-only officer action.
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence.models import (
    Correspondence,
    InboundCorrespondenceReceipt,
)
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
)

REASON = "The requester sent this as a follow-up to the open stadium case."


def _make_request(number_seed, *, status=RequestStatus.RECEIVED, **kwargs):
    requester = Requester.objects.create(
        email=f"{number_seed}@example.org",
        name=f"Requester {number_seed}",
    )
    return PublicRecordsRequest.objects.create(
        requester=requester,
        description=f"Records sought by {number_seed}.",
        status=status,
        **kwargs,
    )


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class EmailIntakeRedirectTests(TestCase):
    """Moving one auto-created email onto the case it actually belongs to."""

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.login(username="officer", password="pw")

        self.source = _make_request("auto", channel="email")
        self.destination = _make_request(
            "existing",
            status=RequestStatus.GATHERING,
        )
        self.receipt = self._email_intake_receipt(self.source)

    def _email_intake_receipt(
        self,
        prr,
        *,
        provider_message_id="email-intake-message",
        kind=Correspondence.Kind.GENERAL,
        match_method=InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
    ):
        matched_at = timezone.now()
        correspondence = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.INBOUND,
            kind=kind,
            status=Correspondence.Status.RECEIVED,
            received_subject="Follow-up about the stadium project",
            received_body=prr.description,
            received_sender="Jane <jane@example.org>",
            received_recipients=["publicrecords@psd401.net"],
            received_at=prr.received_at,
            matched_at=matched_at,
            provider="gmail",
            provider_account="publicrecords@psd401.net",
            provider_message_id=provider_message_id,
            created_by=self.officer,
        )
        return InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="publicrecords@psd401.net",
            provider_message_id=provider_message_id,
            provider_thread_id=f"{provider_message_id}-thread",
            received_at=prr.received_at,
            envelope_recipients=["publicrecords@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.MATCHED
            ),
            match_method=match_method,
            matched_request=prr,
            matched_correspondence=correspondence,
            matched_at=matched_at,
            # A manual match is attributed to the officer who made it; every
            # machine match method is required to have no attributed matcher.
            matched_by=(
                self.officer
                if match_method
                == InboundCorrespondenceReceipt.MatchMethod.MANUAL
                else None
            ),
            quarantined=False,
            raw_message_size=128,
            raw_message_sha256="a" * 64,
            payload_sha256="b" * 64,
            idempotency_key=f"gmail:poll:{provider_message_id}",
            submitted_by=self.officer,
            completed_at=matched_at,
        )

    def _redirect(self, *, receipt=None, number=None, version=None, **extra):
        receipt = receipt or self.receipt
        body = {
            "request_number": number or self.destination.number,
            "reason": REASON,
            "expected_version": (
                receipt.version if version is None else version
            ),
        }
        body.update(extra)
        return self.client.post(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/redirect/",
            body,
            content_type="application/json",
        )

    def test_message_moves_and_the_leftover_case_closes_as_non_request(self):
        response = self._redirect()

        self.assertEqual(response.status_code, 200)
        self.receipt.refresh_from_db()
        self.source.refresh_from_db()
        self.destination.refresh_from_db()
        correspondence = self.receipt.matched_correspondence
        correspondence.refresh_from_db()

        self.assertEqual(self.receipt.matched_request_id, self.destination.pk)
        self.assertEqual(correspondence.request_id, self.destination.pk)
        self.assertEqual(self.source.status, RequestStatus.CLOSED_NON_REQUEST)
        self.assertIsNotNone(self.source.closed_at)
        self.assertEqual(self.destination.status, RequestStatus.GATHERING)
        # Nothing is deleted: the leftover case and its timeline stay.
        self.assertTrue(
            PublicRecordsRequest.objects.filter(pk=self.source.pk).exists()
        )
        closure = self.source.events.get(
            to_status=RequestStatus.CLOSED_NON_REQUEST,
        )
        self.assertIn(self.destination.number, closure.note)
        self.assertEqual(closure.actor, self.officer)

    def test_the_redirect_is_audited_with_both_case_numbers(self):
        self._redirect()

        entry = AuditLog.objects.get(action="records.email_intake_redirected")
        self.assertEqual(entry.actor, self.officer)
        self.assertEqual(entry.target, self.destination.number)
        self.assertEqual(
            entry.detail["source_request"],
            self.source.number,
        )
        self.assertEqual(
            entry.detail["destination_request"],
            self.destination.number,
        )
        self.assertTrue(entry.detail["source_closed"])
        self.assertEqual(entry.detail["reason"], REASON)

    def test_a_stale_version_rolls_the_whole_action_back(self):
        response = self._redirect(version=self.receipt.version + 5)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "version_conflict")
        self.receipt.refresh_from_db()
        self.source.refresh_from_db()
        correspondence = self.receipt.matched_correspondence
        correspondence.refresh_from_db()
        # The closure is not allowed to survive the failed move: one action.
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)
        self.assertIsNone(self.source.closed_at)
        self.assertFalse(self.source.events.exists())
        self.assertEqual(self.receipt.matched_request_id, self.source.pk)
        self.assertEqual(correspondence.request_id, self.source.pk)

    def test_a_failed_rematch_rolls_the_closure_back(self):
        with mock.patch(
            "apps.correspondence.inbound.rematch_inbound_receipt",
            side_effect=ValidationError("Rematch refused."),
        ):
            response = self._redirect()

        self.assertEqual(response.status_code, 400)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)
        self.assertFalse(self.source.events.exists())
        self.assertFalse(
            AuditLog.objects.filter(
                action="records.email_intake_redirected",
            ).exists()
        )

    def test_a_closed_destination_case_is_refused(self):
        """A closed case is nobody's work; a follow-up must not be buried there."""
        from apps.records import services

        services.close_case(
            self.destination,
            RequestStatus.CLOSED_WITHDRAWN,
            actor=self.officer,
            reason="Requester withdrew this one by phone earlier today.",
        )

        response = self._redirect()

        self.assertEqual(response.status_code, 400)
        self.source.refresh_from_db()
        self.receipt.refresh_from_db()
        # The source must not be closed as a side effect of the refused move.
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)
        self.assertFalse(self.source.events.exists())
        self.assertEqual(self.receipt.matched_request_id, self.source.pk)

    def test_the_audit_entry_carries_no_requester_identity(self):
        self._redirect()

        entry = AuditLog.objects.get(action="records.email_intake_redirected")
        recorded = str(entry.detail)
        for private in (
            self.source.requester.name,
            self.source.requester.email,
            self.source.description,
            self.destination.requester.name,
            self.destination.requester.email,
        ):
            self.assertNotIn(private, recorded)

    def test_the_same_case_is_not_a_destination(self):
        response = self._redirect(number=self.source.number)

        self.assertEqual(response.status_code, 400)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)

    def test_a_receipt_that_was_not_auto_created_is_refused(self):
        manual = _make_request("manual")
        receipt = self._email_intake_receipt(
            manual,
            provider_message_id="manual-message",
            match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
        )

        response = self._redirect(receipt=receipt)

        self.assertEqual(response.status_code, 400)
        receipt.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(receipt.matched_request_id, manual.pk)
        self.assertEqual(manual.status, RequestStatus.RECEIVED)

    def test_delivery_status_evidence_is_refused_and_closes_nothing(self):
        """A bounce is evidence about a message, never a requester's follow-up."""
        bounce_case = _make_request("bounce", channel="email")
        receipt = self._email_intake_receipt(
            bounce_case,
            provider_message_id="bounce-message",
            kind=Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        )

        response = self._redirect(receipt=receipt)

        self.assertEqual(response.status_code, 400)
        receipt.refresh_from_db()
        bounce_case.refresh_from_db()
        self.assertEqual(receipt.matched_request_id, bounce_case.pk)
        # The refused move must not leave the source case closed either.
        self.assertEqual(bounce_case.status, RequestStatus.RECEIVED)
        self.assertFalse(bounce_case.events.exists())

    def test_an_already_closed_source_case_is_left_exactly_as_it_is(self):
        from apps.records import services

        services.close_email_non_request(
            self.source,
            actor=self.officer,
            reason="Closed earlier as an unrelated vendor solicitation.",
        )
        self.source.refresh_from_db()
        closed_at = self.source.closed_at
        event_count = self.source.events.count()
        self.receipt.refresh_from_db()

        response = self._redirect()

        self.assertEqual(response.status_code, 200)
        self.source.refresh_from_db()
        self.receipt.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.CLOSED_NON_REQUEST)
        self.assertEqual(self.source.closed_at, closed_at)
        self.assertEqual(self.source.events.count(), event_count)
        self.assertEqual(self.receipt.matched_request_id, self.destination.pk)
        self.assertFalse(
            AuditLog.objects.get(
                action="records.email_intake_redirected",
            ).detail["source_closed"]
        )

    def test_a_substantive_reason_is_required(self):
        response = self._redirect(reason="short")

        self.assertEqual(response.status_code, 400)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)

    def test_a_missing_destination_case_is_a_not_found(self):
        response = self._redirect(number="PRR-9999-9999")

        self.assertEqual(response.status_code, 404)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, RequestStatus.RECEIVED)

    def test_a_role_without_officer_authority_cannot_redirect(self):
        for role in (
            StaffRole.LIAISON,
            StaffRole.REVIEWER,
            StaffRole.ADMIN,
            StaffRole.INTAKE_CLERK,
        ):
            with self.subTest(role=role):
                actor = get_user_model().objects.create_user(
                    f"{role}-user",
                    f"{role}@psd401.net",
                    "pw",
                    is_staff=True,
                )
                apply_staff_roles(actor, {role})
                self.client.logout()
                self.client.login(username=f"{role}-user", password="pw")

                response = self._redirect()

                self.assertEqual(response.status_code, 403)
                self.source.refresh_from_db()
                self.assertEqual(self.source.status, RequestStatus.RECEIVED)

    def test_a_token_caller_gets_the_human_handoff_instead(self):
        token = Token.objects.create(user=self.officer)
        self.client.logout()

        response = self.client.post(
            (
                "/api/staff/inbound-correspondence-receipts/"
                f"{uuid.uuid4()}/redirect/"
            ),
            {
                "request_number": self.destination.number,
                "reason": REASON,
                "expected_version": 1,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertEqual(
            response.json()["handoff"]["capability_id"],
            "inbound_correspondence.redirect_email_intake",
        )
