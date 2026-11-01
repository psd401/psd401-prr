import datetime as dt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.configuration import values
from apps.configuration.registry import SETTINGS_BY_KEY
from apps.correspondence import services
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
)
from apps.records.models import PublicRecordsRequest, Requester
from config import settings as project_settings


@override_settings(
    PRR_ENVIRONMENT="contract-test",
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
)
class InternalCorrespondenceQueueTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.officer = user_model.objects.create_user(
            "event-contract-officer",
            "event-contract-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        requester = Requester.objects.create(
            name="Secret Contract Requester",
            email="secret.requester+event-contract@example.org",
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Sentinel legal request description that must stay out of task arguments.",
            estimated_completion=dt.date(2026, 8, 3),
        )

    def _approved_correspondence(self):
        draft = services.create_authored_draft(
            self.request,
            kind=Correspondence.Kind.GENERAL,
            subject="Sentinel approved subject",
            body="Sentinel approved body that must stay out of task arguments.",
            actor=self.officer,
        )
        return services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )

    @patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_queue_uses_internal_sender(self, enqueue):
        approved = self._approved_correspondence()

        with self.captureOnCommitCallbacks(execute=True):
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.QUEUED)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)

    @patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_queue_persists_delivery_atomically_but_schedules_only_after_commit(
        self,
        enqueue,
    ):
        approved = self._approved_correspondence()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        self.assertTrue(CorrespondenceDelivery.objects.filter(pk=delivery.pk).exists())
        enqueue.assert_not_called()
        self.assertEqual(len(callbacks), 1)

        callbacks[0]()

        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)

    @patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_outer_transaction_rollback_removes_delivery_and_enqueue(
        self,
        enqueue,
    ):
        approved = self._approved_correspondence()
        delivery_id = None

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                delivery = services.queue_approved(
                    approved,
                    actor=self.officer,
                    expected_version=approved.version,
                )
                delivery_id = delivery.id
                raise RuntimeError("force outer rollback")

        approved.refresh_from_db()
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)
        self.assertFalse(
            CorrespondenceDelivery.objects.filter(pk=delivery_id).exists()
        )
        enqueue.assert_not_called()

    @patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_enqueue_contains_only_operational_identifiers(
        self,
        enqueue,
    ):
        approved = self._approved_correspondence()

        with self.captureOnCommitCallbacks(execute=True):
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)
        serialized = str(enqueue.call_args)
        for sentinel in (
            self.request.requester.name,
            self.request.requester.email,
            self.request.description,
            approved.approved_subject,
            approved.approved_body,
        ):
            self.assertNotIn(sentinel, serialized)

    @patch(
        "apps.correspondence.tasks.enqueue_queued_delivery",
        side_effect=RuntimeError("broker unavailable"),
    )
    def test_broker_failure_after_commit_leaves_durable_pending_delivery(
        self,
        enqueue,
    ):
        approved = self._approved_correspondence()

        with self.captureOnCommitCallbacks(execute=True):
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)

    def test_celery_beat_runs_the_bounded_delivery_watchdog_each_minute(self):
        from config.celery import app

        entry = app.conf.beat_schedule["recover-stuck-correspondence-deliveries"]
        self.assertEqual(
            entry["task"],
            (
                "apps.correspondence.tasks."
                "watch_stuck_correspondence_deliveries"
            ),
        )
        self.assertEqual(entry["kwargs"], {"limit": 100})

    @patch("apps.correspondence.tasks.enqueue_queued_delivery")
    def test_retry_reenqueues_same_delivery_and_message_id(
        self,
        enqueue,
    ):
        approved = self._approved_correspondence()
        original_message_id = approved.rfc_message_id
        with self.captureOnCommitCallbacks(execute=True):
            delivery = services.queue_approved(
                approved,
                actor=self.officer,
                expected_version=approved.version,
            )
        approved.refresh_from_db()
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=0)

        failed_at = timezone.now()
        failure_category = "temporary_provider_failure"
        failure_description = services.RETRYABLE_DELIVERY_FAILURES[
            failure_category
        ]
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            claim_owner=str(self.officer.pk),
            claimed_at=failed_at,
            result_at=failed_at,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
            error_category=failure_category,
            result_fingerprint="a" * 64,
            actor=self.officer,
        )
        delivery.status = CorrespondenceDelivery.Status.RETRYABLE
        delivery.attempt_count = 1
        delivery.last_failure_category = failure_category
        delivery.last_failure_description = failure_description
        delivery.completed_at = failed_at
        delivery.save()
        approved.status = Correspondence.Status.FAILED
        approved.failed_at = failed_at
        approved.failure_category = failure_category
        approved.failure_description = failure_description
        approved.save()
        enqueue.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            retried = services.retry_delivery(
                delivery,
                actor=self.officer,
                expected_version=delivery.version,
            )

        approved.refresh_from_db()
        self.assertEqual(retried.id, delivery.id)
        self.assertEqual(approved.id, delivery.correspondence_id)
        self.assertEqual(approved.rfc_message_id, original_message_id)
        enqueue.assert_called_once_with(str(delivery.id), attempt_count=1)


class CorrespondenceReplySettingsValidationTests(TestCase):
    def test_outbound_transport_defaults_off_before_test_overrides(self):
        self.assertFalse(
            project_settings.PRR_CORRESPONDENCE_OUTBOUND_ENABLED
        )

    # The reply-address pairing, the throttle-rate form, and the email
    # allowlist parsing used to be tested here against the private helpers in
    # config/settings.py. Phase B moved the settings those helpers parsed
    # into the store and deleted the helpers with them; the same guarantees
    # are asserted against the save path in
    # apps/configuration/tests/test_phase_b_settings.py
    # (ReplyAddressRuleTests, ShapedValueTests). What is left here are the
    # two helpers config/settings.py still uses.

    def test_the_strict_boolean_helper_accepts_only_true_or_false(self):
        self.assertTrue(
            project_settings._parse_strict_boolean("PRR_TEST_BOOLEAN", "TRUE")
        )
        self.assertFalse(
            project_settings._parse_strict_boolean("PRR_TEST_BOOLEAN", "false")
        )
        for value in ("tru", "1", "yes", ""):
            with self.subTest(boolean=value):
                with self.assertRaises(ImproperlyConfigured):
                    project_settings._parse_strict_boolean(
                        "PRR_TEST_BOOLEAN",
                        value,
                    )

    def test_development_recipient_allowlist_is_validated_and_normalized(self):
        spec = SETTINGS_BY_KEY["PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST"]
        self.assertEqual(
            values.coerce(
                spec, "Jane@Example.org, copy@example.org,jane@example.org"
            ),
            ("jane@example.org", "copy@example.org"),
        )
        # Empty is legal for this one, and means "deliver nowhere" rather
        # than "deliver anywhere" -- it only applies outside production.
        self.assertEqual(values.coerce(spec, ""), ())
        with self.assertRaises(ValueError):
            values.coerce(spec, "not an address")
