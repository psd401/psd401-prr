# ABOUTME: Staff inbound-correspondence queue and manual-association API contract.
# ABOUTME: Proves officer-only access, PII-minimal queues, and human-only actions.
import hashlib
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryNotification,
    InboundCorrespondenceReceipt,
)
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class InboundCorrespondenceReceiptApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "inbound-queue-officer",
            "inbound-queue-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.admin = User.objects.create_user(
            "inbound-queue-admin",
            "inbound-queue-admin@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.liaison = User.objects.create_user(
            "inbound-queue-liaison",
            "inbound-queue-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.integration = User.objects.create_user(
            "inbound-queue-integration",
            password=None,
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Sensitive Requester",
                email="sensitive.requester@example.org",
            ),
            description="Inbound correspondence queue contract.",
        )
        self.other_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="Second inbound correspondence destination.",
        )
        self.sequence = 0

    def login(self, user=None):
        self.client.force_login(user or self.officer)

    def open_clarification_fixture(self, outbound, *, opened_at):
        PublicRecordsRequest.objects.filter(pk=self.request.pk).update(
            status=RequestStatus.CLARIFICATION,
        )
        self.request.refresh_from_db()
        opened_event = StatusEvent.objects.create(
            request=self.request,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered for API association test.",
            visible_to_requester=True,
            occurred_at=opened_at,
        )
        return ClarificationCycle.objects.create(
            request=self.request,
            outbound_correspondence=outbound,
            opened_at=opened_at,
            opened_status_event=opened_event,
        )

    def make_receipt(
        self,
        *,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        candidate_request=None,
        matched_request=None,
        quarantined=False,
        quarantine_reason="",
        parsing_error="",
        correspondence_kind=Correspondence.Kind.GENERAL,
        match_method=None,
        matched_delivery=None,
        delivery_status_original_message_id="",
    ):
        self.sequence += 1
        now = timezone.now()
        if (
            candidate_request is not None
            and matching_status
            == InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
        ):
            matching_status = (
                InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
            )
        is_matched = (
            matching_status
            == InboundCorrespondenceReceipt.MatchingStatus.MATCHED
        )
        correspondence = None
        if parsing_status == InboundCorrespondenceReceipt.ParsingStatus.PARSED:
            correspondence = Correspondence.objects.create(
                request=matched_request if is_matched else None,
                direction=Correspondence.Direction.INBOUND,
                kind=correspondence_kind,
                status=(
                    Correspondence.Status.RECEIVED
                    if is_matched
                    else Correspondence.Status.UNMATCHED
                ),
                received_subject="Reply from Sensitive Requester",
                received_body=(
                    ""
                    if correspondence_kind
                    == Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
                    else "My student identification number is private."
                ),
                received_sender=(
                    "Sensitive Requester <sensitive.requester@example.org>"
                ),
                received_recipients=["publicrecords@psd401.net"],
                received_headers={
                    "date": "Thu, 23 Jul 2026 10:00:00 -0700",
                    "x-unsafe": "must not escape the parser allowlist",
                },
                received_at=now,
                matched_at=now if is_matched else None,
                matched_by=(
                    self.officer
                    if is_matched
                    and match_method
                    != InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS
                    else None
                ),
                provider="gmail",
                provider_account="records-inbox",
                provider_message_id=f"correspondence-{self.sequence}",
                provider_thread_id=f"thread-{self.sequence}",
            )
        return InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id=f"receipt-{self.sequence}",
            provider_thread_id=f"thread-{self.sequence}",
            rfc_message_id=f"<inbound-{self.sequence}@example.org>",
            in_reply_to="<outbound@prr.psd401.net>",
            references=["<earlier@prr.psd401.net>"],
            received_at=now,
            envelope_sender="sensitive.requester@example.org",
            envelope_recipients=["publicrecords@psd401.net"],
            parsing_status=parsing_status,
            matching_status=matching_status,
            match_method=match_method or (
                InboundCorrespondenceReceipt.MatchMethod.MANUAL
                if is_matched
                else (
                    InboundCorrespondenceReceipt.MatchMethod.SUBJECT
                    if candidate_request
                    else ""
                )
            ),
            delivery_status_original_message_id=(
                delivery_status_original_message_id
            ),
            candidate_request=candidate_request,
            matched_request=matched_request if is_matched else None,
            matched_delivery=matched_delivery,
            matched_correspondence=correspondence,
            matched_at=now if is_matched else None,
            matched_by=(
                self.officer
                if is_matched
                and match_method
                != InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS
                else None
            ),
            quarantined=quarantined,
            quarantine_reason=quarantine_reason,
            parsing_error=parsing_error,
            raw_message_size=512,
            raw_message_sha256=f"{self.sequence:064x}",
            payload_sha256=f"{self.sequence + 1000:064x}",
            idempotency_key=f"inbound-queue:{self.sequence}",
            submitted_by=self.integration,
            completed_at=now,
        )

    def add_attachment(
        self,
        receipt,
        *,
        filename,
        size=123,
        sha256=None,
        scan_status=CorrespondenceAttachment.ScanStatus.PENDING,
        quarantined=True,
        quarantine_reason="",
    ):
        attachment = CorrespondenceAttachment.objects.create(
            correspondence=receipt.matched_correspondence,
            inbound_receipt=receipt,
            original_filename=filename,
            content_type="application/pdf",
            size=size,
            sha256=sha256 or f"{self.sequence + 2000:064x}",
            uploaded_by=self.integration,
        )
        if scan_status == CorrespondenceAttachment.ScanStatus.PENDING:
            if not quarantined or quarantine_reason:
                raise AssertionError("Pending fixture state must be canonical.")
            return attachment
        return CorrespondenceAttachment.objects.filter(
            pk=attachment.pk
        ).finalize_locked_scan(
            expected_storage_key=attachment.storage_key,
            scan_status=scan_status,
            scanned_at=timezone.now(),
            quarantine_reason=quarantine_reason,
            promoted_storage_key=(
                f"correspondence/attachments/"
                f"{receipt.matched_correspondence_id}/{attachment.pk}"
                if scan_status == CorrespondenceAttachment.ScanStatus.CLEAN
                else None
            ),
        )

    def test_queue_is_records_officer_only_and_requires_an_explicit_queue(self):
        self.make_receipt()

        self.assertEqual(
            self.client.get(
                "/api/staff/inbound-correspondence-receipts/?queue=unmatched"
            ).status_code,
            403,
        )
        for user in (self.admin, self.liaison):
            with self.subTest(user=user.username):
                self.login(user)
                response = self.client.get(
                    "/api/staff/inbound-correspondence-receipts/?queue=unmatched"
                )
                self.assertEqual(response.status_code, 403)
                self.client.logout()

        self.login()
        missing = self.client.get(
            "/api/staff/inbound-correspondence-receipts/"
        )
        invalid = self.client.get(
            "/api/staff/inbound-correspondence-receipts/?queue=all"
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["code"], "queue_required")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["code"], "invalid_queue")

    def test_unmatched_queue_includes_candidates_with_sender_and_subject_only(self):
        unmatched = self.make_receipt()
        candidate = self.make_receipt(candidate_request=self.request)
        self.make_receipt(
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            matched_request=self.other_request,
        )
        self.login()

        response = self.client.get(
            "/api/staff/inbound-correspondence-receipts/?queue=unmatched"
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertSetEqual(
            {row["id"] for row in rows},
            {str(unmatched.id), str(candidate.id)},
        )
        candidate_row = next(
            row for row in rows if row["id"] == str(candidate.id)
        )
        self.assertEqual(
            candidate_row["candidate_request_number"],
            self.request.number,
        )
        self.assertIsNone(candidate_row["matched_request_number"])
        self.assertEqual(candidate_row["matching_status"], "candidate")
        self.assertEqual(candidate_row["match_method"], "subject")
        # Officers triage from the list, so each row says who wrote and what
        # about (the same officer-only audience as the detail view). The body
        # and transport identifiers stay in the detail view only.
        self.assertEqual(
            candidate_row["received_sender"],
            "Sensitive Requester <sensitive.requester@example.org>",
        )
        self.assertEqual(
            candidate_row["received_subject"], "Reply from Sensitive Requester"
        )
        serialized = str(rows)
        for secret in (
            "student identification",
            "received_body",
            "envelope_sender",
            "provider_message_id",
            "raw_storage_key",
            "raw_message_sha256",
            "idempotency_key",
        ):
            self.assertNotIn(secret, serialized)

    def test_rows_carry_their_open_work_item_for_a_no_action_decision(self):
        from apps.work_queue.models import WorkItem
        from apps.work_queue.reconciliation import reconcile_inbound_receipt

        receipt = self.make_receipt()
        reconcile_inbound_receipt(receipt.pk)
        work_item = WorkItem.objects.get(source_id=str(receipt.pk))
        self.login()

        response = self.client.get(
            "/api/staff/inbound-correspondence-receipts/?queue=unmatched"
        )

        row = response.json()["results"][0]
        self.assertEqual(row["work_item_id"], str(work_item.pk))
        self.assertEqual(row["work_item_version"], work_item.version)

    def test_a_dismissed_receipt_leaves_the_needs_a_case_queue(self):
        from apps.work_queue.models import WorkItem
        from apps.work_queue.reconciliation import reconcile_inbound_receipt
        from apps.work_queue.services import set_no_action_required

        dismissed = self.make_receipt()
        kept = self.make_receipt()
        for receipt in (dismissed, kept):
            reconcile_inbound_receipt(receipt.pk)
        work_item = WorkItem.objects.get(source_id=str(dismissed.pk))
        set_no_action_required(
            work_item,
            category=WorkItem.NoActionCategory.NOT_A_RECORDS_REQUEST,
            reason="Vendor marketing sent to the former officer address.",
            actor=self.officer,
            expected_version=work_item.version,
            idempotency_key="no-action:dismissed-receipt",
        )
        self.login()

        response = self.client.get(
            "/api/staff/inbound-correspondence-receipts/?queue=unmatched"
        )

        self.assertEqual(
            [row["id"] for row in response.json()["results"]],
            [str(kept.id)],
        )

    def test_quarantine_queue_exposes_only_sanitized_reason_codes(self):
        receipt = self.make_receipt(
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
            ),
            parsing_status=(
                InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED
            ),
            quarantined=True,
            quarantine_reason=(
                "mime_depth_exceeded: sensitive.requester@example.org"
            ),
            parsing_error=(
                "Traceback /private/storage sensitive.requester@example.org"
            ),
        )
        self.login()

        response = self.client.get(
            "/api/staff/inbound-correspondence-receipts/?queue=quarantined"
        )

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["id"], str(receipt.id))
        self.assertEqual(
            row["quarantine_reason_code"],
            "mime_depth_exceeded",
        )
        self.assertEqual(row["parsing_error_code"], "parsing_failed")
        serialized = str(row)
        self.assertNotIn("sensitive.requester@example.org", serialized)
        self.assertNotIn("Traceback", serialized)
        self.assertNotIn("/private/storage", serialized)

    def test_quarantined_detail_has_a_stable_nullable_content_shape(self):
        receipt = self.make_receipt(
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
            ),
            parsing_status=(
                InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED
            ),
            quarantined=True,
            quarantine_reason="malformed_message",
            parsing_error="malformed_message",
        )
        self.login()

        response = self.client.get(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["correspondence_id"])
        for field in (
            "received_subject",
            "received_body",
            "received_sender",
            "received_recipients",
            "correspondence_status",
            "correspondence_kind",
        ):
            with self.subTest(field=field):
                self.assertIn(field, body)
                self.assertIsNone(body[field])
        self.assertEqual(body["received_headers"], {})
        self.assertEqual(body["attachments"], [])

    def test_parser_unavailable_remains_a_stable_sanitized_reason_code(self):
        receipt = self.make_receipt(
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
            ),
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.FAILED,
            quarantined=True,
            quarantine_reason="parser_unavailable",
            parsing_error="parser_unavailable",
        )
        self.login()

        response = self.client.get(
            "/api/staff/inbound-correspondence-receipts/?queue=quarantined"
        )

        self.assertEqual(response.status_code, 200)
        row = next(
            item
            for item in response.json()["results"]
            if item["id"] == str(receipt.id)
        )
        self.assertEqual(
            row["quarantine_reason_code"],
            "parser_unavailable",
        )
        self.assertEqual(row["parsing_error_code"], "parser_unavailable")

    def test_detail_is_officer_only_and_excludes_transport_secrets(self):
        receipt = self.make_receipt(candidate_request=self.request)
        clean = self.add_attachment(
            receipt,
            filename="student-details.pdf",
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            quarantined=False,
        )
        pending = self.add_attachment(
            receipt,
            filename="pending-evidence.pdf",
        )

        self.login(self.admin)
        forbidden = self.client.get(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/"
        )
        self.assertEqual(forbidden.status_code, 403)

        self.login()
        response = self.client.get(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/"
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["received_subject"], "Reply from Sensitive Requester")
        self.assertEqual(
            body["received_sender"],
            "Sensitive Requester <sensitive.requester@example.org>",
        )
        self.assertEqual(
            body["candidate_request_number"],
            self.request.number,
        )
        self.assertEqual(body["downloadable_attachment_count"], 1)
        attachments = {row["id"]: row for row in body["attachments"]}
        self.assertTrue(attachments[str(clean.id)]["is_downloadable"])
        self.assertFalse(attachments[str(pending.id)]["is_downloadable"])
        self.assertNotIn("url", str(body).lower())
        self.assertNotIn("x-unsafe", body["received_headers"])
        for secret_field in (
            "raw_storage_key",
            "raw_message_sha256",
            "payload_sha256",
            "idempotency_key",
            "ip_address",
            "submitted_by",
            "storage_key",
            "sha256",
            "parsing_error",
            "quarantine_reason",
        ):
            self.assertNotIn(secret_field, body)
            for attachment in body["attachments"]:
                self.assertNotIn(secret_field, attachment)

    def test_detail_exposes_only_sanitized_delivery_status_evidence(self):
        now = timezone.now()
        outbound = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.QUEUED,
            draft_subject="Sensitive outbound subject",
            draft_body="Sensitive outbound body",
            approved_subject="Sensitive outbound subject",
            approved_body="Sensitive outbound body",
            created_by=self.officer,
            approved_by=self.officer,
            queued_by=self.officer,
            approved_at=now,
            queued_at=now,
        )
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=outbound,
            status=CorrespondenceDelivery.Status.PENDING,
            idempotency_key="api-dsn-detail",
            queued_at=now,
        )
        receipt = self.make_receipt(
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            matched_request=self.request,
            correspondence_kind=(
                Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
            ),
            match_method=(
                InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS
            ),
            matched_delivery=delivery,
            delivery_status_original_message_id=outbound.rfc_message_id,
        )
        notification = CorrespondenceDeliveryNotification.objects.create(
            inbound_receipt=receipt,
            delivery=delivery,
            recipient_index=1,
            recipient_match_status=(
                CorrespondenceDeliveryNotification
                .RecipientMatchStatus.UNMATCHED
            ),
            action=CorrespondenceDeliveryNotification.Action.FAILED,
            enhanced_status="5.1.1",
        )
        self.login()

        response = self.client.get(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/"
        )

        self.assertEqual(response.status_code, 200)
        response_body = response.json()
        self.assertEqual(
            response_body["delivery_status_original_message_id"],
            outbound.rfc_message_id,
        )
        self.assertEqual(
            response_body["matched_delivery_id"],
            str(delivery.id),
        )
        notices = response_body["delivery_notifications"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(
            {
                key: value
                for key, value in notices[0].items()
                if key != "created_at"
            },
            {
                "id": str(notification.id),
                "delivery_id": str(delivery.id),
                "recipient_index": 1,
                "recipient_match_status": "unmatched",
                "action": "failed",
                "enhanced_status": "5.1.1",
            },
        )
        self.assertTrue(notices[0]["created_at"])
        serialized = str(notices).lower()
        for forbidden in (
            "email",
            "recipient_address",
            "diagnostic",
            "sensitive.requester@example.org",
            "private provider prose",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_unmatched_content_cannot_bypass_the_officer_queue_resource(self):
        receipt = self.make_receipt()
        correspondence_id = str(receipt.matched_correspondence_id)

        self.login(self.officer)
        listing = self.client.get("/api/staff/correspondence/")
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn(
            correspondence_id,
            {
                row["id"]
                for row in listing.json()["results"]
            },
        )
        detail = self.client.get(
            f"/api/staff/correspondence/{correspondence_id}/"
        )
        self.assertEqual(detail.status_code, 404)

        self.login(self.admin)
        self.assertEqual(
            self.client.get("/api/staff/correspondence/").status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                f"/api/staff/correspondence/{correspondence_id}/"
            ).status_code,
            403,
        )

    def test_matched_correspondence_exposes_opaque_receipt_link_only_to_officer(
        self,
    ):
        receipt = self.make_receipt(
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            matched_request=self.request,
        )
        path = "/api/staff/correspondence/?direction=inbound"

        self.login()
        officer_row = self.client.get(path).json()["results"][0]
        self.assertEqual(officer_row["id"], str(receipt.matched_correspondence_id))
        self.assertEqual(officer_row["inbound_receipt_id"], str(receipt.id))

        self.login(self.admin)
        self.assertEqual(self.client.get(path).status_code, 403)

    def test_only_an_officer_can_authorize_a_clean_unmatched_attachment(self):
        receipt = self.make_receipt()
        attachment = self.add_attachment(
            receipt,
            filename="matching-evidence.pdf",
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            quarantined=False,
        )
        path = (
            f"/api/staff/correspondence-attachments/{attachment.id}/download/"
        )

        self.login(self.admin)
        self.assertEqual(self.client.post(path).status_code, 403)

        self.login()
        response = self.client.post(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expires_in"], 300)
        self.assertIn(
            f"/api/staff/correspondence-attachments/{attachment.id}/content/",
            response.json()["url"],
        )

    @patch("apps.correspondence.services.open_clean_attachment")
    def test_clean_zero_byte_inbound_attachment_downloads_after_authorization(
        self,
        open_file,
    ):
        receipt = self.make_receipt()
        attachment = self.add_attachment(
            receipt,
            filename="empty-evidence.pdf",
            size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            quarantined=False,
        )
        open_file.return_value = BytesIO(b"")
        path = (
            f"/api/staff/correspondence-attachments/{attachment.id}/download/"
        )
        self.login()

        authorized = self.client.post(path)
        downloaded = self.client.get(authorized.json()["url"])

        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(b"".join(downloaded.streaming_content), b"")
        open_file.assert_called_once()

    @patch("apps.correspondence.services.open_clean_attachment")
    def test_zero_byte_inbound_download_still_verifies_stored_bytes(
        self,
        open_file,
    ):
        receipt = self.make_receipt()
        attachment = self.add_attachment(
            receipt,
            filename="tampered-empty-evidence.pdf",
            size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            scan_status=CorrespondenceAttachment.ScanStatus.CLEAN,
            quarantined=False,
        )
        open_file.return_value = BytesIO(b"not empty")
        self.login()

        authorized = self.client.post(
            f"/api/staff/correspondence-attachments/{attachment.id}/download/"
        )
        downloaded = self.client.get(authorized.json()["url"])

        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(downloaded.status_code, 404)
        self.assertEqual(downloaded.json()["code"], "not_found")
        open_file.assert_called_once()

    @patch(
        "apps.api.resources.inbound_correspondence."
        "inbound.match_inbound_receipt"
    )
    def test_match_delegates_exactly_to_the_locked_domain_service(self, match):
        receipt = self.make_receipt(candidate_request=self.request)
        match.return_value = receipt
        reason = "The message headers and case history confirm this request."
        self.login()

        response = self.client.post(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/match/",
            {
                "request_number": self.request.number,
                "reason": reason,
                "expected_version": receipt.version,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        match.assert_called_once_with(
            receipt,
            request=self.request,
            actor=self.officer,
            reason=reason,
            expected_version=receipt.version,
            ip_address="127.0.0.1",
        )

    @patch(
        "apps.api.resources.inbound_correspondence."
        "inbound.rematch_inbound_receipt"
    )
    def test_rematch_delegates_and_stale_version_is_a_conflict(self, rematch):
        receipt = self.make_receipt(
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            matched_request=self.request,
        )
        reason = "A forwarded header belonged to a different request record."
        self.login()
        rematch.side_effect = services.VersionConflict(
            "Inbound receipt version conflict."
        )

        response = self.client.post(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/rematch/",
            {
                "request_number": self.other_request.number,
                "reason": reason,
                "expected_version": receipt.version,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "version_conflict")
        rematch.assert_called_once_with(
            receipt,
            request=self.other_request,
            actor=self.officer,
            reason=reason,
            expected_version=receipt.version,
            ip_address="127.0.0.1",
        )

    def test_manual_match_and_rematch_audits_capture_request_ip(self):
        receipt = self.make_receipt(candidate_request=self.request)
        self.login()

        matched = self.client.post(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/match/",
            {
                "request_number": self.request.number,
                "reason": (
                    "The retained headers and case history identify this case."
                ),
                "expected_version": receipt.version,
            },
            content_type="application/json",
            REMOTE_ADDR="203.0.113.41",
        )
        self.assertEqual(matched.status_code, 200)

        rematched = self.client.post(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/rematch/",
            {
                "request_number": self.other_request.number,
                "reason": (
                    "The complete thread proves the first association was wrong."
                ),
                "expected_version": matched.json()["version"],
            },
            content_type="application/json",
            REMOTE_ADDR="203.0.113.42",
        )
        self.assertEqual(rematched.status_code, 200)

        match_audit = AuditLog.objects.get(
            action="correspondence.inbound_matched"
        )
        rematch_audit = AuditLog.objects.get(
            action="correspondence.inbound_rematched"
        )
        self.assertEqual(str(match_audit.ip_address), "203.0.113.41")
        self.assertEqual(str(rematch_audit.ip_address), "203.0.113.42")

    def test_officer_explicitly_associates_a_matched_reply_with_open_cycle(self):
        opened_at = timezone.now() - timezone.timedelta(minutes=5)
        outbound = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.SENT,
            draft_subject="Clarification requested",
            draft_body="Please provide the requested clarification.",
            approved_subject="Clarification requested",
            approved_body="Please provide the requested clarification.",
            approved_by=self.officer,
            approved_at=opened_at,
            queued_by=self.officer,
            queued_at=opened_at,
            sent_at=opened_at,
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="clarification-api-outbound",
        )
        cycle = self.open_clarification_fixture(
            outbound,
            opened_at=opened_at,
        )
        receipt = self.make_receipt(
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            matched_request=self.request,
        )
        events_before = StatusEvent.objects.filter(request=self.request).count()
        reason = (
            "The requester confirmed this fresh-thread message answers the "
            "open clarification."
        )
        self.login()

        response = self.client.post(
            (
                "/api/staff/inbound-correspondence-receipts/"
                f"{receipt.id}/associate-clarification/"
            ),
            {
                "clarification_cycle_id": str(cycle.id),
                "reason": reason,
                "expected_version": receipt.version,
            },
            content_type="application/json",
            REMOTE_ADDR="203.0.113.43",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["correspondence_kind"],
            Correspondence.Kind.CLARIFICATION_RESPONSE,
        )
        self.assertEqual(response.json()["version"], receipt.version + 1)
        cycle.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(
            cycle.inbound_response_correspondence_id,
            receipt.matched_correspondence_id,
        )
        self.assertEqual(cycle.responded_at, receipt.received_at)
        self.assertIsNone(cycle.resolved_at)
        self.assertEqual(self.request.status, RequestStatus.CLARIFICATION)
        self.assertEqual(
            StatusEvent.objects.filter(request=self.request).count(),
            events_before,
        )
        event = AuditLog.objects.get(
            action="correspondence.clarification_response_associated"
        )
        self.assertEqual(str(event.ip_address), "203.0.113.43")
        self.assertEqual(event.detail["reason"], reason)

    @patch(
        "apps.api.resources.inbound_correspondence."
        "inbound.match_inbound_receipt"
    )
    def test_match_is_human_only_for_officer_tokens(self, match):
        receipt = self.make_receipt()
        token = Token.objects.create(user=self.officer)
        reason = "The message headers and case history confirm this request."

        response = self.client.post(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/match/",
            {
                "request_number": self.request.number,
                "reason": reason,
                "expected_version": receipt.version,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertEqual(
            response.json()["handoff"]["capability_id"],
            "inbound_correspondence.match",
        )
        match.assert_not_called()

    def test_readonly_agent_is_denied_sensitive_queue_detail(self):
        receipt = self.make_receipt()
        agent = get_user_model().objects.create_user(
            "inbound-queue-readonly-agent",
            is_staff=False,
        )
        agent.user_permissions.add(
            Permission.objects.get(codename="read_staff_api")
        )
        token = Token.objects.create(user=agent)

        response = self.client.get(
            f"/api/staff/inbound-correspondence-receipts/{receipt.id}/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 403)

    def test_openapi_contract_types_queue_and_manual_association_shapes(self):
        self.login()

        response = self.client.get(
            "/api/schema/",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        schema = response.json()
        paths = schema["paths"]
        collection = paths[
            "/api/staff/inbound-correspondence-receipts/"
        ]["get"]
        queue_parameter = next(
            item
            for item in collection["parameters"]
            if item["name"] == "queue"
        )
        self.assertTrue(queue_parameter["required"])
        self.assertEqual(
            queue_parameter["schema"]["enum"],
            ["quarantined", "unmatched"],
        )

        detail_operation = paths[
            "/api/staff/inbound-correspondence-receipts/{id}/"
        ]["get"]
        detail_reference = detail_operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        detail_component = schema["components"]["schemas"][
            detail_reference.rsplit("/", 1)[1]
        ]
        self.assertEqual(
            detail_component["properties"]["received_headers"]["type"],
            "object",
        )
        self.assertEqual(
            detail_component["properties"]["quarantine_reason_code"]["type"],
            "string",
        )

        for action_name in ("match", "rematch"):
            operation = paths[
                "/api/staff/inbound-correspondence-receipts/"
                f"{{id}}/{action_name}/"
            ]["post"]
            request_reference = operation["requestBody"]["content"][
                "application/json"
            ]["schema"]["$ref"]
            request_component = schema["components"]["schemas"][
                request_reference.rsplit("/", 1)[1]
            ]
            self.assertSetEqual(
                set(request_component["required"]),
                {"request_number", "reason", "expected_version"},
            )
