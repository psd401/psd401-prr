import datetime as dt
from email import policy
from email.parser import BytesParser
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from apps.correspondence import services, tasks
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
)
from apps.correspondence.tests.test_outbound_delivery import (
    OutboundDeliveryServiceTestCase,
)
from apps.integrations.gmail.client import GmailUnavailable, SentMessage
from apps.records import service_identities


@override_settings(
    PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True,
    PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True,
    PRR_GMAIL_ENABLED=True,
    PRR_DEPLOYMENT_MODE="production",
    PRR_ENVIRONMENT="production",
    PRR_GMAIL_MAILBOX="publicrecords@psd401.net",
)
class InternalGmailSenderTests(OutboundDeliveryServiceTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _queue_without_dispatch(self, **kwargs):
        with patch("apps.correspondence.tasks.enqueue_queued_delivery"):
            return self.queue_general(**kwargs)

    def test_queue_enqueues_internal_sender_after_commit(self):
        with (
            patch("apps.correspondence.tasks.enqueue_queued_delivery") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            _correspondence, delivery, _attachment = self.queue_general()

        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)

    def test_system_transport_principal_is_noninteractive_and_exact_scope(self):
        from apps.correspondence.transport import system_transport_principal

        actor = system_transport_principal()

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
            {("correspondence", "deliver_outbound_correspondence")},
        )

    def test_system_transport_principal_rejects_a_tokenized_row(self):
        from apps.correspondence.transport import system_transport_principal

        actor = system_transport_principal()
        Token.objects.create(user=actor)

        with self.assertRaisesMessage(
            PermissionDenied,
            "exact scope",
        ):
            system_transport_principal()

    def test_reserved_system_username_cannot_be_token_provisioned(self):
        with self.assertRaises(
            service_identities.IntegrationIdentityProvisionError,
        ) as raised:
            service_identities.provision_integration_identity(
                "mcp_readonly",
                service_identities.SYSTEM_OUTBOUND_TRANSPORT_USERNAME,
                rotate=True,
            )

        self.assertEqual(raised.exception.code, "reserved_system_identity")
        self.assertFalse(
            Token.objects.filter(
                user__username=(
                    service_identities.SYSTEM_OUTBOUND_TRANSPORT_USERNAME
                )
            ).exists()
        )

    def test_non_system_transport_requires_its_presented_token(self):
        _correspondence, delivery, _attachment = self._queue_without_dispatch()

        with self.assertRaisesMessage(
            PermissionDenied,
            "exact outbound-correspondence transport identity",
        ):
            services.claim_delivery(
                delivery,
                actor=self.other_integration,
            )

        credential = Token.objects.get(user=self.other_integration)
        claim = services.claim_delivery(
            delivery,
            actor=self.other_integration,
            credential=credential,
        )
        self.assertEqual(claim.attempt.actor, self.other_integration)

    def test_mime_builder_preserves_message_id_recipients_and_attachment(self):
        from apps.correspondence.transport import (
            build_delivery_message,
            system_transport_principal,
        )

        correspondence, delivery, attachment = self._queue_without_dispatch(
            with_attachment=True,
        )
        actor = system_transport_principal()
        claim = services.claim_delivery(delivery, actor=actor)
        opened = services.OpenedDeliveryAttachment(
            attachment=attachment,
            body=BytesIO(b"approved attachment bytes"),
        )

        with patch(
            "apps.correspondence.transport.services.open_claim_attachment",
            return_value=opened,
        ):
            raw = build_delivery_message(claim, actor=actor)

        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(message["Message-ID"], correspondence.rfc_message_id)
        self.assertEqual(message["Reply-To"], f"publicrecords+{correspondence.reply_token}@mail.prr.test")
        self.assertEqual(message["From"], "publicrecords@psd401.net")
        self.assertEqual(
            {address.addr_spec for address in message["To"].addresses},
            {"jane@example.org"},
        )
        self.assertEqual(
            {address.addr_spec for address in message["Cc"].addresses},
            {"authorized-copy@example.org"},
        )
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "approved-notice.pdf")
        self.assertEqual(attachments[0].get_payload(decode=True), b"approved attachment bytes")

    def test_worker_sends_and_reports_success_through_existing_services(self):
        correspondence, delivery, _attachment = self._queue_without_dispatch()

        with patch(
            "apps.correspondence.tasks.gmail_client.send_raw",
            return_value=SentMessage(
                message_id="gmail-provider-message",
                thread_id="gmail-provider-thread",
            ),
        ) as send_raw:
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(result["status"], CorrespondenceDelivery.Status.SENT)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertEqual(delivery.provider_message_id, "gmail-provider-message")
        raw = send_raw.call_args.args[0]
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(parsed["Message-ID"], correspondence.rfc_message_id)

    def test_manual_only_mode_does_not_claim_existing_automatic_delivery(self):
        request = self.prr
        with (
            patch("apps.correspondence.tasks.enqueue_queued_delivery"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            queued = services.queue_submission_receipt(request)

        delivery = queued.delivery
        with (
            override_settings(
                PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=False,
            ),
            patch("apps.correspondence.tasks.gmail_client.send_raw") as send_raw,
        ):
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )

        delivery.refresh_from_db()
        self.assertEqual(result["status"], "automated_outbound_disabled")
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertEqual(delivery.attempt_count, 0)
        self.assertFalse(delivery.attempts.exists())
        send_raw.assert_not_called()
        from apps.correspondence.transport import system_transport_principal

        with (
            override_settings(
                PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=False,
            ),
            self.assertRaises(services.AutomatedOutboundDisabled),
        ):
            services.claim_delivery(
                delivery,
                actor=system_transport_principal(),
            )

    def test_worker_success_survives_dedupe_marker_cleanup_failure(self):
        _correspondence, delivery, _attachment = self._queue_without_dispatch()

        with (
            patch(
                "apps.correspondence.tasks.gmail_client.send_raw",
                return_value=SentMessage(
                    message_id="gmail-provider-message",
                    thread_id="gmail-provider-thread",
                ),
            ) as send_raw,
            patch(
                "apps.correspondence.tasks.cache.delete",
                side_effect=RuntimeError("Redis unavailable"),
            ),
            self.assertLogs("apps.correspondence.tasks", level="WARNING") as logs,
        ):
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )

        delivery.refresh_from_db()
        self.assertEqual(result["status"], CorrespondenceDelivery.Status.SENT)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        send_raw.assert_called_once()
        self.assertTrue(
            any(
                "dedupe marker cleanup failed" in message
                for message in logs.output
            )
        )

    def test_retryable_and_permanent_gmail_failures_use_existing_categories(self):
        cases = (
            ("provider_unavailable", CorrespondenceDelivery.Status.RETRYABLE),
            ("message_rejected", CorrespondenceDelivery.Status.FAILED),
        )
        for category, expected_status in cases:
            with self.subTest(category=category):
                _correspondence, delivery, _attachment = self._queue_without_dispatch()
                with patch(
                    "apps.correspondence.tasks.gmail_client.send_raw",
                    side_effect=GmailUnavailable(
                        "sanitized Gmail failure",
                        category=category,
                    ),
                ):
                    result = tasks.send_queued_delivery.run(
                        str(delivery.id),
                        attempt_count=0,
                    )

                delivery.refresh_from_db()
                attempt = delivery.attempts.get(attempt_number=1)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(delivery.status, expected_status)
                self.assertEqual(attempt.error_category, category)

    @override_settings(
        PRR_DEPLOYMENT_MODE="production",
        PRR_ENVIRONMENT="development",
        PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST=("jane@example.org",),
    )
    def test_deployed_dev_refuses_any_recipient_outside_allowlist(self):
        _correspondence, delivery, _attachment = self._queue_without_dispatch()

        with patch("apps.correspondence.tasks.gmail_client.send_raw") as send_raw:
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )

        delivery.refresh_from_db()
        attempt = delivery.attempts.get(attempt_number=1)
        self.assertEqual(result["status"], CorrespondenceDelivery.Status.FAILED)
        self.assertEqual(
            attempt.error_category,
            "development_recipient_blocked",
        )
        send_raw.assert_not_called()

    def test_reconciliation_finds_prior_send_without_sending_again(self):
        from apps.correspondence.transport import system_transport_principal

        _correspondence, delivery, _attachment = self._queue_without_dispatch()
        claim = services.claim_delivery(
            delivery,
            actor=system_transport_principal(),
        )
        CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
            claim_expires_at=timezone.now() - dt.timedelta(seconds=1),
        )

        with (
            patch("apps.correspondence.tasks.gmail_client.send_raw") as send_raw,
            patch(
                "apps.correspondence.tasks.gmail_client.find_by_rfc822_message_id",
                return_value=SentMessage(
                    message_id="already-sent-message",
                    thread_id="already-sent-thread",
                ),
            ) as find_message,
        ):
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=claim.attempt.attempt_number,
            )

        delivery.refresh_from_db()
        self.assertEqual(result["action"], CorrespondenceDeliveryAttempt.Action.RECONCILE)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        send_raw.assert_not_called()
        find_message.assert_called_once()

    def test_reconciliation_not_found_uses_existing_retryable_state(self):
        from apps.correspondence.transport import system_transport_principal

        _correspondence, delivery, _attachment = self._queue_without_dispatch()
        claim = services.claim_delivery(
            delivery,
            actor=system_transport_principal(),
        )
        CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
            claim_expires_at=timezone.now() - dt.timedelta(seconds=1),
        )

        with (
            patch("apps.correspondence.tasks.gmail_client.send_raw") as send_raw,
            patch(
                "apps.correspondence.tasks.gmail_client.find_by_rfc822_message_id",
                return_value=None,
            ),
        ):
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=claim.attempt.attempt_number,
            )

        delivery.refresh_from_db()
        self.assertEqual(result["action"], CorrespondenceDeliveryAttempt.Action.RECONCILE)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.RETRYABLE)
        self.assertEqual(delivery.last_failure_category, "reconciliation_not_found")
        send_raw.assert_not_called()

    def test_crash_after_gmail_send_recovers_by_reconciliation_without_resend(self):
        _correspondence, delivery, _attachment = self._queue_without_dispatch()
        sent = SentMessage(
            message_id="sent-before-worker-crash",
            thread_id="sent-before-worker-crash-thread",
        )
        original_report = services.report_delivery_result

        with (
            patch(
                "apps.correspondence.tasks.gmail_client.send_raw",
                return_value=sent,
            ) as send_raw,
            patch(
                "apps.correspondence.tasks.services.report_delivery_result",
                side_effect=RuntimeError("worker stopped before reporting"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "worker stopped"):
                tasks.send_queued_delivery.run(
                    str(delivery.id),
                    attempt_count=0,
                )

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.CLAIMED)
        CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
            claim_expires_at=timezone.now() - dt.timedelta(seconds=1),
        )

        with (
            patch("apps.correspondence.tasks.gmail_client.send_raw") as resend,
            patch(
                "apps.correspondence.tasks.gmail_client.find_by_rfc822_message_id",
                return_value=sent,
            ),
            patch(
                "apps.correspondence.tasks.services.report_delivery_result",
                wraps=original_report,
            ),
        ):
            tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=1,
            )

        delivery.refresh_from_db()
        self.assertEqual(send_raw.call_count, 1)
        resend.assert_not_called()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)

    def _assert_effect_failure_surfaced(self, correspondence, delivery):
        from apps.audit.models import AuditLog
        from apps.work_queue.models import WorkItem

        delivery.refresh_from_db()
        correspondence.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.SENT)
        self.assertEqual(correspondence.status, Correspondence.Status.SENT)
        self.assertIsNone(correspondence.resulting_status_event_id)
        work_item = WorkItem.objects.get(
            source_type=WorkItem.SourceType.CORRESPONDENCE,
            source_id=str(correspondence.pk),
            work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
        )
        self.assertEqual(work_item.request_id, correspondence.request_id)
        self.assertEqual(
            work_item.disposition,
            WorkItem.Disposition.NEEDS_ATTENTION,
        )
        failure = AuditLog.objects.get(
            action="correspondence.delivery_effects_failed",
        )
        self.assertEqual(failure.detail["correspondence_id"], str(correspondence.pk))
        self.assertEqual(failure.detail["work_item_id"], str(work_item.pk))
        self.assertIn("stale snapshot", failure.detail["error"])
        return work_item

    def test_effect_failure_after_send_keeps_sent_evidence_and_surfaces(self):
        from django.core.exceptions import ValidationError

        correspondence, delivery, _attachment = self._queue_without_dispatch()

        with (
            patch(
                "apps.correspondence.tasks.gmail_client.send_raw",
                return_value=SentMessage(
                    message_id="sent-effect-failed",
                    thread_id="sent-effect-failed-thread",
                ),
            ),
            patch(
                "apps.correspondence.services.apply_successful_delivery_effects",
                side_effect=ValidationError("stale snapshot"),
            ),
        ):
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )

        self.assertEqual(result["status"], CorrespondenceDelivery.Status.SENT)
        self._assert_effect_failure_surfaced(correspondence, delivery)
        attempt = CorrespondenceDeliveryAttempt.objects.get(delivery=delivery)
        self.assertEqual(
            attempt.result_category,
            CorrespondenceDeliveryAttempt.ResultCategory.SENT,
        )
        self.assertEqual(attempt.provider_message_id, "sent-effect-failed")

    def test_effect_failure_alerts_staff_only_after_sent_evidence_commits(self):
        from django.core.exceptions import ValidationError

        correspondence, delivery, _attachment = self._queue_without_dispatch()

        with (
            patch(
                "apps.correspondence.tasks.gmail_client.send_raw",
                return_value=SentMessage(
                    message_id="sent-effect-alert",
                    thread_id="sent-effect-alert-thread",
                ),
            ),
            patch(
                "apps.correspondence.services.apply_successful_delivery_effects",
                side_effect=ValidationError("stale snapshot"),
            ),
            patch(
                "apps.records.notifications.notify_delivery_effects_failed",
            ) as notify,
        ):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                tasks.send_queued_delivery.run(
                    str(delivery.id),
                    attempt_count=0,
                )
            notify.assert_not_called()
            for callback in callbacks:
                callback()

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0].pk, correspondence.request_id)
        self.assertEqual(
            notify.call_args.kwargs["correspondence_id"],
            str(correspondence.pk),
        )
        self.assertEqual(
            notify.call_args.kwargs["correspondence_kind"],
            correspondence.kind,
        )
        self.assertIn("stale snapshot", notify.call_args.kwargs["error"])

    def test_effect_failure_alert_error_never_undoes_sent_evidence(self):
        from django.core.exceptions import ValidationError

        correspondence, delivery, _attachment = self._queue_without_dispatch()

        with (
            patch(
                "apps.correspondence.tasks.gmail_client.send_raw",
                return_value=SentMessage(
                    message_id="sent-effect-alert-broken",
                    thread_id="sent-effect-alert-broken-thread",
                ),
            ),
            patch(
                "apps.correspondence.services.apply_successful_delivery_effects",
                side_effect=ValidationError("stale snapshot"),
            ),
            patch(
                "apps.records.notifications.notify_delivery_effects_failed",
                side_effect=RuntimeError("alert transport broken"),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=0,
            )

        self.assertEqual(result["status"], CorrespondenceDelivery.Status.SENT)
        self._assert_effect_failure_surfaced(correspondence, delivery)

    def test_effect_failure_on_reconciliation_resolves_instead_of_wedging(self):
        from django.core.exceptions import ValidationError

        from apps.correspondence.transport import system_transport_principal

        correspondence, delivery, _attachment = self._queue_without_dispatch()
        claim = services.claim_delivery(
            delivery,
            actor=system_transport_principal(),
        )
        CorrespondenceDelivery.objects.filter(pk=delivery.pk).update(
            claim_expires_at=timezone.now() - dt.timedelta(seconds=1),
        )

        with (
            patch("apps.correspondence.tasks.gmail_client.send_raw") as send_raw,
            patch(
                "apps.correspondence.tasks.gmail_client.find_by_rfc822_message_id",
                return_value=SentMessage(
                    message_id="reconciled-effect-failed",
                    thread_id="reconciled-effect-failed-thread",
                ),
            ),
            patch(
                "apps.correspondence.services.apply_successful_delivery_effects",
                side_effect=ValidationError("stale snapshot"),
            ),
        ):
            result = tasks.send_queued_delivery.run(
                str(delivery.id),
                attempt_count=claim.attempt.attempt_number,
            )

        send_raw.assert_not_called()
        self.assertEqual(
            result["action"],
            CorrespondenceDeliveryAttempt.Action.RECONCILE,
        )
        self._assert_effect_failure_surfaced(correspondence, delivery)

    def test_enqueue_deduplicates_by_delivery_and_attempt_count(self):
        _correspondence, delivery, _attachment = self._queue_without_dispatch()

        with patch("apps.correspondence.tasks.send_queued_delivery.delay") as delay:
            first = tasks.enqueue_queued_delivery(
                str(delivery.id),
                attempt_count=0,
            )
            duplicate = tasks.enqueue_queued_delivery(
                str(delivery.id),
                attempt_count=0,
            )
            next_attempt = tasks.enqueue_queued_delivery(
                str(delivery.id),
                attempt_count=1,
            )

        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertTrue(next_attempt)
        self.assertEqual(delay.call_count, 2)

    def test_duplicate_worker_for_same_generation_sends_once(self):
        _correspondence, delivery, _attachment = self._queue_without_dispatch()

        with patch(
            "apps.correspondence.tasks.gmail_client.send_raw",
            return_value=SentMessage(
                message_id="single-provider-message",
                thread_id="single-provider-thread",
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

    def test_system_principal_row_is_created_by_migrations(self):
        user = get_user_model().objects.get(
            username="prr-gmail-outbound-transport",
        )
        self.assertFalse(user.has_usable_password())
