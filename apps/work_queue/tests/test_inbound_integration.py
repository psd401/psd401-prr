from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.correspondence import services
from apps.work_queue.models import WorkItem
from apps.work_queue.tasks import reconcile_inbound_receipt_work_item

PLAIN_MESSAGE = (
    b"From: Requester <requester@example.org>\r\n"
    b"To: records@prr.psd401.net\r\n"
    b"Date: Sun, 23 Aug 2026 10:00:00 -0700\r\n"
    b"Message-ID: <work-item-integration@example.org>\r\n"
    b"Subject: Public records question\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Please provide the retained records.\r\n"
)


@override_settings(
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
class InboundWorkItemIntegrationTests(TestCase):
    def setUp(self):
        self.integration = get_user_model().objects.create_user(
            "work-item-inbound-transport",
            password=None,
        )
        self.integration.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="correspondence",
                codename="submit_inbound_correspondence",
            )
        )
        self.token = Token.objects.create(user=self.integration)
        self.received_at = timezone.now().replace(microsecond=0)

    def receive(self):
        return services.receive_inbound_message(
            raw_message=BytesIO(PLAIN_MESSAGE),
            provider="gmail",
            provider_account="records-inbox",
            provider_message_id="work-item-inbound-message",
            provider_thread_id="work-item-inbound-thread",
            envelope_sender="requester@example.org",
            envelope_recipients=["records@prr.psd401.net"],
            received_at=self.received_at,
            idempotency_key="gmail:work-item-integration",
            actor=self.integration,
            credential=self.token,
        )

    @patch("apps.secure_files.store_quarantined_object")
    def test_exact_transport_retry_repairs_a_missed_projection(self, _store):
        with patch(
            "apps.work_queue.integration.schedule_inbound_receipt_reconciliation"
        ):
            first = self.receive()
        self.assertFalse(WorkItem.objects.exists())

        with patch(
            "apps.work_queue.tasks.reconcile_inbound_receipt_work_item.delay"
        ) as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                retry = self.receive()

        self.assertTrue(retry.deduplicated)
        self.assertEqual(first.receipt_id, retry.receipt_id)
        self.assertFalse(WorkItem.objects.exists())
        enqueue.assert_called_once_with(str(first.receipt_id))

        reconcile_inbound_receipt_work_item.run(str(first.receipt_id))

        item = WorkItem.objects.get(
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
            source_id=str(first.receipt_id),
        )
        self.assertEqual(item.work_kind, WorkItem.WorkKind.INBOUND_UNMATCHED)
        self.assertEqual(item.events.count(), 1)
