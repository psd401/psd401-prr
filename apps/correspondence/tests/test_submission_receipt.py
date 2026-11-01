from email import policy
from email.parser import BytesParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.correspondence import services, tasks
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
)
from apps.integrations.gmail.client import SentMessage
from apps.records import tasks as record_tasks
from apps.records.intake import submit_intake
from apps.records.models import (
    Channel,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)


@override_settings(
    PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True,
    PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True,
    PRR_GMAIL_ENABLED=True,
    PRR_DEPLOYMENT_MODE="production",
    PRR_ENVIRONMENT="production",
    PRR_GMAIL_MAILBOX="publicrecords@psd401.net",
    PRR_CORRESPONDENCE_REPLY_LOCAL_PART="publicrecords",
    PRR_CORRESPONDENCE_REPLY_DOMAIN="psd401.net",
    AUTO_ACK_SAFETY_NET=True,
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
)
class SubmissionReceiptTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "receipt-officer",
            "receipt-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.liaison = User.objects.create_user(
            "receipt-liaison",
            "receipt-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, {StaffRole.LIAISON})
        self.prr = self.make_request(Channel.PORTAL)

    @staticmethod
    def make_request(channel):
        return PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                name="Jamie Requester",
                email="jamie@example.org",
            ),
            description="Emails concerning the 2026 transportation plan.",
            channel=channel,
            statutory_response_due=timezone.localdate(),
        )

    def test_queue_is_versioned_nonstatutory_and_idempotent(self):
        with self.captureOnCommitCallbacks(execute=False):
            first = services.queue_submission_receipt(self.prr)
            second = services.queue_submission_receipt(self.prr)

        correspondence = first.delivery.correspondence
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.delivery.pk, second.delivery.pk)
        self.assertEqual(
            correspondence.kind,
            Correspondence.Kind.SUBMISSION_RECEIPT,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertTrue(correspondence.automated)
        self.assertIsNone(correspondence.created_by)
        self.assertIsNone(correspondence.approved_by)
        self.assertIsNone(correspondence.queued_by)
        self.assertEqual(correspondence.template_identifier, "submission_receipt")
        self.assertEqual(correspondence.template_version, "1")
        self.assertIn(
            "This confirmation is not the District's five-business-day "
            "response required by RCW 42.56.520.",
            correspondence.approved_body,
        )
        self.assertEqual(Correspondence.objects.count(), 1)
        self.assertEqual(CorrespondenceDelivery.objects.count(), 1)

    @override_settings(PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=False)
    def test_manual_only_mode_does_not_create_automatic_receipt_work(self):
        with self.assertRaises(services.AutomatedOutboundDisabled):
            services.queue_submission_receipt(self.prr)

        self.assertFalse(Correspondence.objects.exists())
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_partial_existing_receipt_requires_human_review(self):
        services.create_submission_receipt_draft(self.prr)

        with self.assertRaisesMessage(
            ValidationError,
            "The existing submission receipt delivery requires human review.",
        ):
            services.queue_submission_receipt(self.prr)

    def test_receipt_and_lifecycle_messages_do_not_block_each_other(self):
        with self.captureOnCommitCallbacks(execute=False):
            services.queue_submission_receipt(self.prr)
        acknowledgment = services.create_acknowledgment_draft(
            self.prr,
            actor=self.liaison,
        )
        acknowledgment = services.approve_draft(
            acknowledgment,
            actor=self.officer,
            expected_version=acknowledgment.version,
        )
        with self.captureOnCommitCallbacks(execute=False):
            services.queue_approved(
                acknowledgment,
                actor=self.officer,
                expected_version=acknowledgment.version,
            )

        other = self.make_request(Channel.EMAIL)
        other_acknowledgment = services.create_acknowledgment_draft(
            other,
            actor=self.liaison,
        )
        other_acknowledgment = services.approve_draft(
            other_acknowledgment,
            actor=self.officer,
            expected_version=other_acknowledgment.version,
        )
        with self.captureOnCommitCallbacks(execute=False):
            services.queue_approved(
                other_acknowledgment,
                actor=self.officer,
                expected_version=other_acknowledgment.version,
            )
            receipt = services.queue_submission_receipt(other)

        self.assertEqual(
            receipt.delivery.correspondence.kind,
            Correspondence.Kind.SUBMISSION_RECEIPT,
        )

    def test_gmail_delivery_is_once_only_nonstatutory_and_safety_net_still_fires(self):
        with self.captureOnCommitCallbacks(execute=False):
            queued = services.queue_submission_receipt(self.prr)
        delivery = queued.delivery
        correspondence = delivery.correspondence

        with patch(
            "apps.correspondence.tasks.gmail_client.send_raw",
            return_value=SentMessage(
                message_id="submission-receipt-message",
                thread_id="submission-receipt-thread",
            ),
        ) as send_raw:
            first = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )
            duplicate = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )

        self.assertEqual(first["status"], CorrespondenceDelivery.Status.SENT)
        self.assertEqual(duplicate["status"], "stale")
        send_raw.assert_called_once()
        message = BytesParser(policy=policy.default).parsebytes(
            send_raw.call_args.args[0]
        )
        self.assertEqual(
            message["Reply-To"],
            f"publicrecords+{correspondence.reply_token}@psd401.net",
        )

        self.prr.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)
        self.assertFalse(self.prr.initial_response_delivered)
        self.assertIsNone(self.prr.acknowledgment_sent_at)
        self.assertIsNone(correspondence.resulting_status_event_id)
        self.assertFalse(StatusEvent.objects.filter(request=self.prr).exists())

        with (
            self.captureOnCommitCallbacks(execute=False),
            patch("apps.records.tasks._notify"),
        ):
            record_tasks.auto_acknowledge_safety_net()

        acknowledgment = Correspondence.objects.get(
            request=self.prr,
            kind=Correspondence.Kind.ACKNOWLEDGMENT,
        )
        self.assertEqual(acknowledgment.status, Correspondence.Status.QUEUED)
        self.assertTrue(acknowledgment.automated)

    @patch("apps.records.intake.persist_intake_attachments")
    @patch("apps.records.intake.route_to_systems")
    @patch("apps.records.intake.triage_footage")
    @patch("apps.records.intake.compute_statutory_deadline.delay")
    def test_portal_and_email_intake_queue_receipts_but_other_channels_do_not(
        self,
        _deadline,
        _triage,
        _routing,
        _attachments,
    ):
        created = []
        for channel in (Channel.PORTAL, Channel.EMAIL, Channel.MAIL):
            with self.captureOnCommitCallbacks(execute=False):
                created.append(
                    submit_intake(
                        {
                            "email": f"{channel}@example.org",
                            "description": f"Request submitted by {channel}.",
                        },
                        channel=channel,
                    )
                )

        receipt_request_ids = set(
            Correspondence.objects.filter(
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            ).values_list("request_id", flat=True)
        )
        self.assertSetEqual(
            receipt_request_ids,
            {created[0].pk, created[1].pk},
        )
