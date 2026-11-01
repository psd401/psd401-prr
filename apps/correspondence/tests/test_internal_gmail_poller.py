import datetime as dt
from unittest.mock import ANY, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.audit.models import AuditLog
from apps.configuration import runtime
from apps.correspondence import gmail_sync, receiving, tasks
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryNotification,
    CorrespondenceRecipient,
    GmailSyncState,
    InboundCorrespondenceReceipt,
)
from apps.integrations.gmail import client as gmail_client
from apps.records import attachments as intake_attachment_services
from apps.records import service_identities
from apps.records.email_intake import reconcile_stored_unmatched_email_intake
from apps.records.models import (
    AdvisoryRun,
    Channel,
    CollectionTask,
    IntakeAttachment,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
)
from apps.records.service_identities import require_exact_inbound_identity

from .test_delivery_status_receiving import delivery_status_message
from .test_inbound_receiving import ATTACHMENT_MESSAGE

MAILBOX = "publicrecords@psd401.net"
PROCESSED_LABEL = "PRR/Processed"
RECEIVED_AT = dt.datetime(2026, 7, 23, 19, 20, 30, tzinfo=dt.UTC)


def _plain_message(
    sequence,
    *,
    delivered_to=MAILBOX,
    in_reply_to="",
    from_header="Jane Requester <jane.requester@example.org>",
    subject="Re: records request",
    body=None,
    extra_headers=(),
):
    reply_header = (
        f"In-Reply-To: {in_reply_to}\r\n".encode()
        if in_reply_to
        else b""
    )
    message_body = body if body is not None else f"Reply body {sequence}.\r\n"
    return b"".join(
        [
            f"Delivered-To: {delivered_to}\r\n".encode(),
            f"From: {from_header}\r\n".encode(),
            f"To: {delivered_to}\r\n".encode(),
            b"Date: Thu, 23 Jul 2026 12:20:30 -0700\r\n",
            f"Message-ID: <requester-reply-{sequence}@example.org>\r\n".encode(),
            reply_header,
            f"Subject: {subject}\r\n".encode(),
            *(f"{header}\r\n".encode() for header in extra_headers),
            b"MIME-Version: 1.0\r\n",
            b"Content-Type: text/plain; charset=utf-8\r\n",
            b"\r\n",
            message_body.encode(),
        ]
    )


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_GMAIL_ENABLED=True,
    PRR_GMAIL_INBOUND_POLL_ENABLED=True,
    PRR_GMAIL_MAILBOX=MAILBOX,
    PRR_GMAIL_INBOUND_PROCESSED_LABEL=PROCESSED_LABEL,
    PRR_GMAIL_INBOUND_LOOKBACK_DAYS=7,
    PRR_ENVIRONMENT="production",
    PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES=1024 * 1024,
    PRR_CORRESPONDENCE_INBOUND_BODY_MAX_BYTES=64 * 1024,
    PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS=50,
    PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES=998,
    PRR_CORRESPONDENCE_INBOUND_MAX_MIME_PARTS=25,
    PRR_CORRESPONDENCE_INBOUND_MAX_MIME_DEPTH=10,
    PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT=5,
    PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES=256 * 1024,
    PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES=512 * 1024,
)
class InternalGmailPollerTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "gmail-poller-officer",
            "gmail-poller-officer@psd401.net",
            "pw",
            is_staff=True,
        )

    def _outbound(self, sequence, *, provider_thread_id=""):
        requester = Requester.objects.create(
            name=f"Requester {sequence}",
            email=f"requester-{sequence}@example.org",
        )
        request = PublicRecordsRequest.objects.create(
            requester=requester,
            description=f"Request {sequence}",
        )
        correspondence = Correspondence.objects.create(
            request=request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject=f"Outbound {sequence}",
            draft_body="Outbound body",
            created_by=self.officer,
        )
        CorrespondenceRecipient.objects.create(
            correspondence=correspondence,
            recipient_type=CorrespondenceRecipient.RecipientType.TO,
            email=requester.email,
            display_name=requester.name,
            requester=requester,
        )
        correspondence.status = Correspondence.Status.SENT
        correspondence.approved_subject = f"Outbound {sequence}"
        correspondence.approved_body = "Outbound body"
        correspondence.approved_by = self.officer
        correspondence.queued_by = self.officer
        correspondence.approved_at = RECEIVED_AT - dt.timedelta(days=1)
        correspondence.queued_at = RECEIVED_AT - dt.timedelta(days=1)
        correspondence.sent_at = RECEIVED_AT - dt.timedelta(days=1)
        correspondence.provider = "gmail"
        correspondence.provider_account = MAILBOX
        correspondence.provider_message_id = f"outbound-{sequence}"
        correspondence.provider_thread_id = provider_thread_id
        correspondence.save()
        return correspondence

    @patch("apps.secure_files.store_quarantined_object")
    def test_reconciliation_converts_stored_eligible_receipts_once(self, store):
        actor = gmail_sync.system_inbound_principal()
        result = receiving.receive_inbound_message(
            raw_message=_plain_message(
                41,
                subject="Stored public records request",
                body="Please provide the superintendent's correspondence.",
            ),
            provider="gmail",
            provider_account=MAILBOX,
            provider_message_id="stored-message-41",
            provider_thread_id="stored-thread-41",
            envelope_sender="",
            envelope_recipients=[MAILBOX],
            received_at=RECEIVED_AT,
            idempotency_key="gmail-inbound:stored-message-41",
            actor=actor,
        )
        receipt = InboundCorrespondenceReceipt.objects.get(pk=result.receipt_id)
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        other_result = receiving.receive_inbound_message(
            raw_message=_plain_message(
                42,
                subject="Other-provider public records request",
                body="Please provide the board's correspondence.",
            ),
            provider="gmail",
            provider_account="other-account",
            provider_message_id="stored-message-42",
            provider_thread_id="stored-thread-42",
            envelope_sender="",
            envelope_recipients=[MAILBOX],
            received_at=RECEIVED_AT,
            idempotency_key="other-mailbox:stored-message-42",
            actor=actor,
        )

        first = reconcile_stored_unmatched_email_intake(actor=actor)
        second = reconcile_stored_unmatched_email_intake(actor=actor)

        self.assertEqual(
            first,
            {
                "status": "ok",
                "examined": 1,
                "converted": 1,
                "remaining_eligible": 0,
            },
        )
        self.assertEqual(
            second,
            {
                "status": "ok",
                "examined": 0,
                "converted": 0,
                "remaining_eligible": 0,
            },
        )
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
        )
        other_receipt = InboundCorrespondenceReceipt.objects.get(
            pk=other_result.receipt_id
        )
        self.assertEqual(
            other_receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        store.assert_called()

    @staticmethod
    def _fetched(sequence, raw, *, thread_id=None, history_id=None, label_ids=()):
        return gmail_client.FetchedMessage(
            message_id=f"message-{sequence}",
            thread_id=thread_id or f"thread-{sequence}",
            history_id=history_id or str(100 + sequence),
            received_at=RECEIVED_AT + dt.timedelta(seconds=sequence),
            raw_bytes=raw,
            label_ids=tuple(label_ids),
        )

    def _poll_one(self, fetched, *, list_messages, get_message):
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched
        return tasks.poll_inbound_gmail.run()

    def _staff_alert(self, sequence, **kwargs):
        request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="alert-case@example.org"),
            description="Records named in a staff alert subject.",
        )
        raw = _plain_message(
            sequence,
            from_header=MAILBOX,
            subject=f"[URGENT] {request.number}: denial delivered",
            body="Internal staff alert.\r\n",
            extra_headers=("Auto-Submitted: auto-generated",),
        )
        return request, self._fetched(sequence, raw, **kwargs)

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="160",
    )
    def test_own_auto_generated_mail_is_labeled_but_never_received(
        self,
        _current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        _request, fetched = self._staff_alert(
            60,
            label_ids=("SENT", "INBOX"),
        )
        request_count = PublicRecordsRequest.objects.count()

        result = self._poll_one(
            fetched,
            list_messages=list_messages,
            get_message=get_message,
        )

        self.assertEqual(result["received"], 0)
        self.assertEqual(result["self_generated_skipped"], 1)
        self.assertFalse(InboundCorrespondenceReceipt.objects.exists())
        self.assertEqual(PublicRecordsRequest.objects.count(), request_count)
        store.assert_not_called()
        add_label.assert_called_once_with(
            fetched.message_id,
            PROCESSED_LABEL,
            archive=False,
        )
        self.assertEqual(
            GmailSyncState.objects.get(mailbox=MAILBOX).history_id,
            "160",
        )
        skipped = AuditLog.objects.get(
            action="gmail.inbound_self_generated_skipped",
        )
        self.assertEqual(skipped.target, fetched.message_id)
        self.assertEqual(skipped.actor, gmail_sync.system_inbound_principal())

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="163",
    )
    def test_ambiguous_repeated_auto_submitted_header_is_still_received(
        self,
        _current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        fetched = self._fetched(
            63,
            _plain_message(
                63,
                from_header=MAILBOX,
                subject="Two markers",
                extra_headers=(
                    "Auto-Submitted: auto-generated",
                    "Auto-Submitted: no",
                ),
            ),
            label_ids=("SENT", "INBOX"),
        )

        result = self._poll_one(
            fetched,
            list_messages=list_messages,
            get_message=get_message,
        )

        self.assertEqual(result["received"], 1)
        self.assertEqual(result["self_generated_skipped"], 0)

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="161",
    )
    def test_forged_own_address_without_sent_label_is_still_received(
        self,
        _current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        _request, fetched = self._staff_alert(61, label_ids=("INBOX",))

        result = self._poll_one(
            fetched,
            list_messages=list_messages,
            get_message=get_message,
        )

        self.assertEqual(result["received"], 1)
        self.assertEqual(result["self_generated_skipped"], 0)
        self.assertTrue(InboundCorrespondenceReceipt.objects.exists())

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="162",
    )
    def test_human_mail_sent_to_own_mailbox_is_still_received(
        self,
        _current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        fetched = self._fetched(
            62,
            _plain_message(
                62,
                from_header=MAILBOX,
                subject="Note to self",
                extra_headers=("Auto-Submitted: no",),
            ),
            label_ids=("SENT", "INBOX"),
        )

        result = self._poll_one(
            fetched,
            list_messages=list_messages,
            get_message=get_message,
        )

        self.assertEqual(result["received"], 1)
        self.assertEqual(result["self_generated_skipped"], 0)
        self.assertTrue(InboundCorrespondenceReceipt.objects.exists())

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="150",
    )
    def test_unmatched_bare_email_creates_request_through_canonical_intake(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        system = RecordSystem.objects.create(
            slug="bond-records",
            name="Bond records",
            triage_keywords=["bond levy"],
            search_checklist="Search the bond project files.",
            needs_verification=False,
        )
        body = "Please provide bond levy records.\r\nSecond line exactly.\r\n"
        fetched = self._fetched(
            40,
            _plain_message(
                40,
                from_header='"Jane Q. Requester" <jane.requester@example.org>',
                subject="New public records request",
                body=body,
            ),
        )
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 1)
        request = PublicRecordsRequest.objects.get()
        self.assertEqual(request.channel, Channel.EMAIL)
        self.assertEqual(request.requester.email, "jane.requester@example.org")
        self.assertEqual(request.requester.name, "Jane Q. Requester")
        self.assertEqual(request.description, body)
        self.assertEqual(request.received_at, fetched.received_at)
        self.assertEqual(request.statutory_response_due.isoformat(), "2026-07-30")
        self.assertEqual(
            CollectionTask.objects.get(request=request).system,
            system,
        )
        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(receipt.matched_request, request)
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
        )
        self.assertEqual(receipt.matched_correspondence.request, request)
        self.assertTrue(receipt.raw_storage_key)
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.created",
                target=request.number,
                detail__integration_source="email",
            ).exists()
        )
        request.description = "Rewritten wording"
        with self.assertRaisesMessage(
            ValidationError,
            "Request descriptions are immutable after intake",
        ):
            request.save()
        store.assert_called_once()
        add_label.assert_called_once_with(
            fetched.message_id,
            PROCESSED_LABEL,
            archive=False,
        )
        current_history_id.assert_called_once_with()

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="160",
    )
    def test_forwarded_request_opens_under_the_original_sender_and_date(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        body = (
            "This came in for us.\r\n"
            "\r\n"
            "---------- Forwarded message ---------\r\n"
            "From: Pat Analyst <pat.analyst@salarysurvey.example>\r\n"
            "Date: Mon, Jul 20, 2026 at 9:05 AM\r\n"
            "Subject: Data request\r\n"
            "To: <erin@psd401.net>\r\n"
            "\r\n"
            "Please complete the attached salary questionnaire.\r\n"
        )
        fetched = self._fetched(
            45,
            _plain_message(
                45,
                from_header='"Doe, Dana" <dana@psd401.net>',
                subject="Fwd: Data request",
                body=body,
            ),
        )
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        tasks.poll_inbound_gmail.run()

        request = PublicRecordsRequest.objects.get()
        self.assertEqual(
            request.requester.email, "pat.analyst@salarysurvey.example"
        )
        self.assertEqual(request.requester.name, "Pat Analyst")
        self.assertEqual(request.description, body)
        self.assertEqual(
            request.received_at,
            dt.datetime(2026, 7, 20, 16, 5, tzinfo=dt.UTC),
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="request.email_forward_attributed",
                target=request.number,
                detail__forwarded_by="dana@psd401.net",
            ).exists()
        )

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="161",
    )
    def test_forward_dated_after_arrival_keeps_the_arrival_time(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        body = (
            "---------- Forwarded message ---------\r\n"
            "From: Pat Analyst <pat.analyst@salarysurvey.example>\r\n"
            "Date: Mon, Jul 27, 2026 at 9:05 AM\r\n"
            "\r\n"
            "Please send the minutes.\r\n"
        )
        fetched = self._fetched(
            46,
            _plain_message(
                46,
                from_header="Staff <staff@psd401.net>",
                subject="Fwd: request",
                body=body,
            ),
        )
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        tasks.poll_inbound_gmail.run()

        request = PublicRecordsRequest.objects.get()
        self.assertEqual(
            request.requester.email, "pat.analyst@salarysurvey.example"
        )
        self.assertEqual(request.received_at, fetched.received_at)

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="175",
    )
    def test_tokenized_automated_invalid_sender_and_empty_mail_stay_in_review(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        tokenized = f"publicrecords+{timezone.now().timestamp()}@psd401.net"
        raw_messages = [
            _plain_message(51, delivered_to=tokenized),
            _plain_message(52, extra_headers=("Auto-Submitted: auto-replied",)),
            _plain_message(53, extra_headers=("Precedence: bulk",)),
            _plain_message(54, extra_headers=("Precedence: list",)),
            _plain_message(55, extra_headers=("Precedence: junk",)),
            _plain_message(56, extra_headers=("List-Id: notices.example.org",)),
            _plain_message(57, from_header="not-an-address"),
            _plain_message(58, body=" \r\n\t"),
        ]
        fetched = [
            self._fetched(index + 50, raw)
            for index, raw in enumerate(raw_messages, start=1)
        ]
        list_messages.return_value = [
            gmail_client.SentMessage(item.message_id, item.thread_id)
            for item in fetched
        ]
        get_message.side_effect = fetched

        tasks.poll_inbound_gmail.run()

        self.assertFalse(PublicRecordsRequest.objects.exists())
        receipts = list(InboundCorrespondenceReceipt.objects.order_by("received_at"))
        self.assertEqual(len(receipts), len(raw_messages))
        self.assertTrue(
            all(
                receipt.matching_status
                != InboundCorrespondenceReceipt.MatchingStatus.MATCHED
                for receipt in receipts
            )
        )
        self.assertEqual(add_label.call_count, len(raw_messages))
        self.assertEqual(store.call_count, len(raw_messages))

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="190",
    )
    def test_blocked_sender_stays_in_review_without_auto_match_or_intake(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        target = self._outbound(
            "nextrequest",
            provider_thread_id="nextrequest-thread",
        )
        fetched = self._fetched(
            90,
            _plain_message(
                90,
                from_header="NextRequest <notices@updates.nextrequest.com>",
                in_reply_to=target.rfc_message_id,
                subject=f"Re: {target.request.number}",
            ),
            thread_id="nextrequest-thread",
        )
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 1)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)
        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        self.assertEqual(receipt.match_method, "")
        self.assertIsNone(receipt.matched_request)
        self.assertFalse(
            InboundCorrespondenceReceipt.objects.filter(
                match_method=InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="correspondence.inbound_automatic_processing_blocked",
                target=str(receipt.pk),
                detail__reason_code="blocked_sender_domain",
            ).exists()
        )
        store.assert_called_once()
        add_label.assert_called_once_with(
            fetched.message_id,
            PROCESSED_LABEL,
            archive=False,
        )
        current_history_id.assert_called_once_with()

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="200",
    )
    def test_poller_matches_reply_token_in_reply_to_and_provider_thread(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        token_target = self._outbound("token")
        reply_target = self._outbound("reply")
        thread_target = self._outbound(
            "thread",
            provider_thread_id="known-provider-thread",
        )
        token_address = (
            f"{runtime.get('PRR_CORRESPONDENCE_REPLY_LOCAL_PART')}"
            f"+{token_target.reply_token}"
            f"@{runtime.get('PRR_CORRESPONDENCE_REPLY_DOMAIN')}"
        )
        fetched = [
            self._fetched(1, _plain_message(1, delivered_to=token_address)),
            self._fetched(
                2,
                _plain_message(2, in_reply_to=reply_target.rfc_message_id),
            ),
            self._fetched(
                3,
                _plain_message(3),
                thread_id="known-provider-thread",
            ),
        ]
        list_messages.return_value = [
            gmail_client.SentMessage(item.message_id, item.thread_id)
            for item in fetched
        ]
        get_message.side_effect = fetched

        result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 3)
        receipts = {
            receipt.provider_message_id: receipt
            for receipt in InboundCorrespondenceReceipt.objects.all()
        }
        self.assertEqual(
            receipts["message-1"].match_method,
            InboundCorrespondenceReceipt.MatchMethod.REPLY_TOKEN,
        )
        self.assertEqual(receipts["message-1"].matched_request, token_target.request)
        self.assertEqual(receipts["message-1"].received_at, fetched[0].received_at)
        self.assertEqual(receipts["message-1"].envelope_sender, "")
        self.assertEqual(
            receipts["message-2"].match_method,
            InboundCorrespondenceReceipt.MatchMethod.IN_REPLY_TO,
        )
        self.assertEqual(receipts["message-2"].matched_request, reply_target.request)
        self.assertEqual(
            receipts["message-3"].match_method,
            InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD,
        )
        self.assertEqual(receipts["message-3"].matched_request, thread_target.request)
        self.assertEqual(add_label.call_count, 3)
        current_history_id.assert_called_once_with()
        self.assertEqual(
            GmailSyncState.objects.get(
                mailbox=MAILBOX,
                environment="production",
            ).history_id,
            "200",
        )

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="300",
    )
    def test_reset_checkpoint_replays_without_duplicate_receipt(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        raw = _plain_message("replay")
        fetched = self._fetched(10, raw, history_id="299")
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.side_effect = [fetched, fetched]

        first = tasks.poll_inbound_gmail.run()
        state = GmailSyncState.objects.get(
            mailbox=MAILBOX,
            environment="production",
        )
        state.history_id = ""
        state.save(update_fields=["history_id", "updated_at"])
        second = tasks.poll_inbound_gmail.run()

        self.assertEqual(first["received"], 1)
        self.assertEqual(second["deduplicated"], 1)
        self.assertEqual(InboundCorrespondenceReceipt.objects.count(), 1)
        self.assertEqual(Correspondence.objects.filter(direction="inbound").count(), 1)
        self.assertEqual(PublicRecordsRequest.objects.count(), 1)
        request = PublicRecordsRequest.objects.get()
        self.assertEqual(request.inbound_correspondence_receipts.count(), 1)
        self.assertEqual(store.call_count, 1)
        self.assertEqual(add_label.call_count, 2)
        self.assertEqual(current_history_id.call_count, 2)

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.tasks.scan_correspondence_attachment.delay")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="400",
    )
    def test_inbound_attachment_stays_quarantined_and_scan_is_enqueued(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        scan_delay,
        store,
    ):
        fetched = self._fetched(20, ATTACHMENT_MESSAGE)
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        with self.captureOnCommitCallbacks(execute=True):
            tasks.poll_inbound_gmail.run()

        attachment = CorrespondenceAttachment.objects.get()
        self.assertTrue(attachment.quarantined)
        self.assertEqual(attachment.scan_status, "pending")
        intake_attachment = IntakeAttachment.objects.get()
        self.assertEqual(intake_attachment.request, PublicRecordsRequest.objects.get())
        self.assertEqual(
            intake_attachment.source_correspondence_attachment,
            attachment,
        )
        self.assertEqual(
            intake_attachment.scan_status,
            IntakeAttachment.ScanStatus.PENDING,
        )
        self.assertEqual(intake_attachment.storage_key, attachment.storage_key)
        with self.assertRaisesMessage(
            ValueError,
            "Only malware-scanned attachments can be downloaded",
        ):
            intake_attachment_services.open_clean_attachment(intake_attachment)
        scan_delay.assert_called_once_with(str(attachment.id))
        self.assertEqual(store.call_count, 2)

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.tasks.scan_correspondence_attachment.delay")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="425",
    )
    def test_email_attachment_reuses_correspondence_scan_verdict(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        scan_delay,
        store,
    ):
        fetched = self._fetched(21, ATTACHMENT_MESSAGE)
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched
        tasks.poll_inbound_gmail.run()
        source = CorrespondenceAttachment.objects.get()

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ) as scan,
            patch("apps.correspondence.tasks.secure_files.copy_clean_object") as copy,
        ):
            result = tasks.scan_correspondence_attachment.run(str(source.pk))

        self.assertEqual(result, CorrespondenceAttachment.ScanStatus.CLEAN)
        source.refresh_from_db()
        intake_attachment = IntakeAttachment.objects.get()
        self.assertEqual(
            intake_attachment.scan_status,
            IntakeAttachment.ScanStatus.CLEAN,
        )
        self.assertEqual(intake_attachment.storage_key, source.storage_key)
        self.assertEqual(intake_attachment.scanned_at, source.scanned_at)
        scan.assert_called_once()
        copy.assert_called_once()

        with patch(
            "apps.records.attachments.secure_files.open_private_object"
        ) as open_private_object:
            intake_attachment_services.open_clean_attachment(intake_attachment)
        open_private_object.assert_called_once_with(
            source.storage_key,
            _client=ANY,
        )

        tasks._persist_clean_revalidation_failure_barrier(
            source,
            scanned_at=timezone.now(),
        )
        intake_attachment.refresh_from_db()
        self.assertEqual(
            intake_attachment.scan_status,
            IntakeAttachment.ScanStatus.CLEAN,
        )
        with self.assertRaisesMessage(
            ValueError,
            "Only malware-scanned attachments can be downloaded",
        ):
            intake_attachment_services.open_clean_attachment(intake_attachment)

    @override_settings(
        PRR_AI_ADVISORY_ENABLED=True,
        PRR_AI_AWS_REGION="us-west-2",
        PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED=True,
        PRR_AI_MODEL="district-approved-model-v1",
    )
    @patch("apps.records.advisory_ai._queue", side_effect=lambda run: run)
    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.tasks.scan_correspondence_attachment.delay")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="430",
    )
    def test_emailed_request_proposal_waits_for_its_attachment_verdict(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        scan_delay,
        store,
        queue,
    ):
        """The email's files attach after the request exists, then get scanned.

        Starting the intent proposal at request creation would fingerprint
        an input with no files, which the worker then sees change.
        """
        fetched = self._fetched(22, ATTACHMENT_MESSAGE)
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched
        with self.captureOnCommitCallbacks(execute=True):
            tasks.poll_inbound_gmail.run()
        self.assertFalse(AdvisoryRun.objects.exists())
        source = CorrespondenceAttachment.objects.get()

        with (
            patch(
                "apps.correspondence.tasks.secure_files.scan_object_for_malware",
                return_value=(True, "OK"),
            ),
            patch("apps.correspondence.tasks.secure_files.copy_clean_object"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            tasks.scan_correspondence_attachment.run(str(source.pk))

        run = AdvisoryRun.objects.get()
        self.assertEqual(run.request, PublicRecordsRequest.objects.get())
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="450",
    )
    def test_rejected_mime_is_retained_quarantined_before_checkpoint_advance(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        fetched = self._fetched(25, b"not a valid RFC822 message\r\n")
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        result = tasks.poll_inbound_gmail.run()

        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(result["received"], 1)
        self.assertEqual(receipt.parsing_status, "quarantined")
        self.assertTrue(receipt.quarantined)
        self.assertIsNotNone(receipt.completed_at)
        store.assert_called_once()
        add_label.assert_called_once_with(
            fetched.message_id,
            PROCESSED_LABEL,
            archive=False,
        )
        self.assertEqual(
            GmailSyncState.objects.get(
                mailbox=MAILBOX,
                environment="production",
            ).history_id,
            "450",
        )

    @override_settings(PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED=True)
    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="460",
    )
    def test_processed_message_leaves_the_inbox_when_the_flag_is_on(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        """Durable receipt still comes first; archiving is the last step."""
        fetched = self._fetched(26, b"not a valid RFC822 message\r\n")
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 1)
        self.assertTrue(InboundCorrespondenceReceipt.objects.exists())
        add_label.assert_called_once_with(
            fetched.message_id,
            PROCESSED_LABEL,
            archive=True,
        )

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="500",
    )
    def test_delivery_status_report_is_handed_to_existing_receiving_pipeline(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        outbound = self._outbound("dsn")
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=outbound,
            status=CorrespondenceDelivery.Status.PENDING,
            provider="gmail",
            provider_account=MAILBOX,
            idempotency_key="poller-dsn-delivery",
            queued_at=RECEIVED_AT - dt.timedelta(days=1),
        )
        dsn = delivery_status_message(outbound.rfc_message_id)
        fetched = self._fetched(30, dsn)
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched

        result = tasks.poll_inbound_gmail.run()

        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(result["received"], 1)
        self.assertEqual(
            receipt.match_method,
            InboundCorrespondenceReceipt.MatchMethod.DELIVERY_STATUS,
        )
        self.assertEqual(receipt.matched_delivery, delivery)
        self.assertEqual(receipt.matched_request, outbound.request)
        self.assertEqual(
            receipt.matched_correspondence.kind,
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        )
        self.assertTrue(
            CorrespondenceDeliveryNotification.objects.filter(
                inbound_receipt=receipt,
                delivery=delivery,
            ).exists()
        )
        store.assert_called_once()
        add_label.assert_called_once_with(
            "message-30",
            PROCESSED_LABEL,
            archive=False,
        )

    def test_disabled_poll_is_noop_before_principal_or_gmail_client_lookup(self):
        with (
            override_settings(PRR_GMAIL_INBOUND_POLL_ENABLED=False),
            patch("apps.correspondence.gmail_sync.system_inbound_principal") as actor,
            patch("apps.correspondence.gmail_sync.gmail_client.list_history") as history,
            patch("apps.correspondence.gmail_sync.gmail_client.list_messages") as messages,
            patch("apps.correspondence.gmail_sync.gmail_client.get_message") as get_message,
        ):
            result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result, {"status": "disabled"})
        actor.assert_not_called()
        history.assert_not_called()
        messages.assert_not_called()
        get_message.assert_not_called()

    def test_routing_uses_first_provider_header_and_ignores_mime_to(self):
        token = "11111111-1111-4111-8111-111111111111"
        token_address = f"publicrecords+{token}@psd401.net"
        raw = (
            f"Delivered-To: {MAILBOX}\r\n"
            f"To: {token_address}\r\n"
            f"X-Original-To: {token_address}\r\n"
            "\r\nBody"
        ).encode()

        self.assertEqual(gmail_sync._routing_recipients(raw), [MAILBOX])

    def test_excess_untrusted_routing_headers_fall_back_to_pinned_mailbox(self):
        headers = "".join(
            f"X-Original-To: forged-{index}@example.org\r\n"
            for index in range(
                settings.PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS + 1
            )
        )

        self.assertEqual(
            gmail_sync._routing_recipients(
                f"{headers}\r\nBody".encode()
            ),
            [MAILBOX],
        )

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="700",
    )
    @patch("apps.correspondence.gmail_sync.gmail_client.list_history")
    def test_expired_history_uses_bounded_resync_without_gaps_or_duplicates(
        self,
        list_history,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        GmailSyncState.objects.create(
            mailbox=MAILBOX,
            environment="production",
            history_id="600",
        )
        list_history.side_effect = gmail_client.GmailUnavailable(
            "History expired.",
            category="permanent_provider_failure",
            status_code=404,
        )
        fetched = [
            self._fetched(40, _plain_message(40)),
            self._fetched(41, _plain_message(41)),
        ]
        list_messages.return_value = [
            gmail_client.SentMessage(item.message_id, item.thread_id)
            for item in fetched
        ]
        get_message.side_effect = fetched

        result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 2)
        self.assertEqual(
            set(
                InboundCorrespondenceReceipt.objects.values_list(
                    "provider_message_id",
                    flat=True,
                )
            ),
            {"message-40", "message-41"},
        )
        list_messages.assert_called_once_with(
            "in:inbox -label:PRR/Processed newer_than:7d"
        )
        self.assertEqual(
            GmailSyncState.objects.get(
                mailbox=MAILBOX,
                environment="production",
            ).history_id,
            "700",
        )

    @override_settings(PRR_ENVIRONMENT="development")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.list_messages",
        return_value=[],
    )
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="800",
    )
    def test_nonproduction_resync_query_requires_its_environment_label(
        self,
        current_history_id,
        list_messages,
        get_message,
    ):
        tasks.poll_inbound_gmail.run()

        list_messages.assert_called_once_with(
            "in:inbox label:PRR/development -label:PRR/Processed newer_than:7d"
        )
        get_message.assert_not_called()

    @override_settings(PRR_ENVIRONMENT="development")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_history")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.list_messages",
        return_value=[],
    )
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="850",
    )
    def test_nonproduction_checkpoint_never_bypasses_environment_query(
        self,
        current_history_id,
        list_messages,
        get_message,
        list_history,
    ):
        GmailSyncState.objects.create(
            mailbox=MAILBOX,
            environment="development",
            history_id="840",
        )

        tasks.poll_inbound_gmail.run()

        list_history.assert_not_called()
        list_messages.assert_called_once_with(
            "in:inbox label:PRR/development -label:PRR/Processed newer_than:7d"
        )
        get_message.assert_not_called()

    @patch("apps.correspondence.gmail_sync.gmail_client.list_history")
    def test_gmail_outage_leaves_checkpoint_unadvanced_and_logs_context(
        self,
        list_history,
    ):
        state = GmailSyncState.objects.create(
            mailbox=MAILBOX,
            environment="production",
            history_id="900",
        )
        list_history.side_effect = gmail_client.GmailUnavailable(
            "Provider unavailable.",
            category="provider_unavailable",
            status_code=503,
        )

        with (
            self.assertLogs("apps.correspondence.gmail_sync", level="WARNING") as logs,
            self.assertRaises(gmail_client.GmailUnavailable),
        ):
            tasks.poll_inbound_gmail.run()

        state.refresh_from_db()
        self.assertEqual(state.history_id, "900")
        self.assertIn(MAILBOX, " ".join(logs.output))
        self.assertIn("900", " ".join(logs.output))

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
    @patch(
        "apps.correspondence.gmail_sync.gmail_client.current_history_id",
        return_value="1000",
    )
    def test_label_failure_after_durable_receipt_does_not_rewind_checkpoint(
        self,
        current_history_id,
        list_messages,
        get_message,
        add_label,
        store,
    ):
        fetched = self._fetched(50, _plain_message(50))
        list_messages.return_value = [
            gmail_client.SentMessage(fetched.message_id, fetched.thread_id)
        ]
        get_message.return_value = fetched
        add_label.side_effect = gmail_client.GmailUnavailable(
            "Label unavailable.",
            category="provider_unavailable",
            status_code=503,
        )

        with self.assertLogs(
            "apps.correspondence.gmail_sync",
            level="WARNING",
        ):
            result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 1)
        self.assertEqual(result["label_failures"], 1)
        self.assertEqual(InboundCorrespondenceReceipt.objects.count(), 1)
        self.assertEqual(
            GmailSyncState.objects.get(
                mailbox=MAILBOX,
                environment="production",
            ).history_id,
            "1000",
        )

    @patch("apps.secure_files.store_quarantined_object")
    @patch("apps.correspondence.gmail_sync.gmail_client.add_label")
    @patch("apps.correspondence.gmail_sync.gmail_client.get_message")
    @patch("apps.correspondence.gmail_sync.gmail_client.list_history")
    def test_incremental_history_processes_only_added_messages(
        self,
        list_history,
        get_message,
        add_label,
        store,
    ):
        GmailSyncState.objects.create(
            mailbox=MAILBOX,
            environment="production",
            history_id="1100",
        )
        fetched = self._fetched(60, _plain_message(60))
        list_history.return_value = {
            "historyId": "1200",
            "history": [
                {
                    "id": "1150",
                    "messages": [
                        {"id": "label-only", "threadId": "label-thread"}
                    ],
                },
                {
                    "id": "1160",
                    "messagesAdded": [
                        {
                            "message": {
                                "id": fetched.message_id,
                                "threadId": fetched.thread_id,
                            }
                        }
                    ],
                },
            ],
        }
        get_message.return_value = fetched

        result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 1)
        get_message.assert_called_once_with(fetched.message_id)
        self.assertEqual(
            GmailSyncState.objects.get(
                mailbox=MAILBOX,
                environment="production",
            ).history_id,
            "1200",
        )

    def test_checkpoint_advance_is_monotonic(self):
        state = GmailSyncState.objects.create(
            mailbox=MAILBOX,
            environment="production",
            history_id="1300",
        )

        gmail_sync._advance_checkpoint(state.pk, "1400")
        gmail_sync._advance_checkpoint(state.pk, "1350")

        state.refresh_from_db()
        self.assertEqual(state.history_id, "1400")


class GmailSyncStateModelTests(TestCase):
    def test_mailbox_and_environment_are_unique(self):
        GmailSyncState.objects.create(
            mailbox=MAILBOX,
            environment="production",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            GmailSyncState.objects.create(
                mailbox=MAILBOX,
                environment="production",
            )

    def test_history_identifier_is_numeric_or_blank(self):
        GmailSyncState.objects.create(
            mailbox=MAILBOX,
            environment="production",
            history_id="",
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            GmailSyncState.objects.create(
                mailbox=MAILBOX,
                environment="development",
                history_id="not-history",
            )


class SystemInboundPrincipalTests(TestCase):
    def test_system_inbound_principal_is_noninteractive_and_exact_scope(self):
        actor = gmail_sync.system_inbound_principal()

        self.assertTrue(actor.is_active)
        self.assertFalse(actor.is_staff)
        self.assertFalse(actor.is_superuser)
        self.assertFalse(actor.has_usable_password())
        self.assertFalse(actor.groups.exists())
        self.assertFalse(Token.objects.filter(user=actor).exists())
        self.assertEqual(
            set(
                actor.user_permissions.values_list(
                    "content_type__app_label",
                    "codename",
                )
            ),
            {("correspondence", "submit_inbound_correspondence")},
        )

    def test_system_inbound_principal_rejects_a_tokenized_row(self):
        actor = gmail_sync.system_inbound_principal()
        Token.objects.create(user=actor)

        with self.assertRaisesMessage(PermissionDenied, "exact scope"):
            gmail_sync.system_inbound_principal()

    def test_non_system_inbound_identity_requires_its_presented_token(self):
        actor = get_user_model().objects.create_user(
            "legacy-inbound-transport",
            password=None,
        )
        actor.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="correspondence",
                codename="submit_inbound_correspondence",
            )
        )
        credential = Token.objects.create(user=actor)

        with self.assertRaisesMessage(
            PermissionDenied,
            "exact inbound-correspondence transport identity",
        ):
            require_exact_inbound_identity(actor)

        self.assertEqual(
            require_exact_inbound_identity(actor, credential=credential),
            actor,
        )

    def test_reserved_system_username_cannot_be_token_provisioned(self):
        with self.assertRaises(
            service_identities.IntegrationIdentityProvisionError,
        ) as raised:
            service_identities.provision_integration_identity(
                "mcp_readonly",
                service_identities.SYSTEM_INBOUND_TRANSPORT_USERNAME,
                rotate=True,
            )

        self.assertEqual(raised.exception.code, "reserved_system_identity")


class GmailPollerBeatTests(TestCase):
    def test_minute_beat_entry_is_registered(self):
        from config.celery import app

        entry = app.conf.beat_schedule["poll-inbound-gmail"]

        self.assertEqual(
            entry["task"],
            "apps.correspondence.tasks.poll_inbound_gmail",
        )
        self.assertEqual(
            str(entry["schedule"]),
            "<crontab: * * * * * (m/h/dM/MY/d)>",
        )
