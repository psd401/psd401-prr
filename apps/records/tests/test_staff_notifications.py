import datetime as dt
import os
import subprocess
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.formats import date_format

from apps.audit.models import AuditLog
from apps.configuration import runtime
from apps.configuration.models import AppSetting
from apps.correspondence.models import Correspondence
from apps.integrations.avigilon.client import (
    AltaNotConfigured,
    BookmarkHold,
    Camera,
    HeadCoverage,
)
from apps.integrations.gmail.client import GmailUnavailable, SentMessage
from apps.records import notifications, tasks, triage
from apps.records.models import (
    FootagePreservation,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
)


class StaffNotificationSettingsTests(TestCase):
    """What the environment still decides about staff alerts.

    Phase B moved the recipient list and the suppression window into the
    settings store. Their bounds -- addresses only, 60 to 604800 seconds --
    are asserted against the save path in
    ``apps/configuration/tests/test_phase_b_settings.py``, and the "at least
    one recipient before you may turn alerts on" rule is the
    ``staff_notification_recipients_configured`` prerequisite. What is left
    here is the switch itself, which is still a bootstrap seed.
    """

    def _import_settings(self, *, python="import config.settings", **environment):
        process_environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(settings.BASE_DIR)),
            "PYTHON_DOTENV_DISABLED": "1",
            "DJANGO_DEBUG": "true",
            **environment,
        }
        return subprocess.run(
            [sys.executable, "-c", python],
            cwd=settings.BASE_DIR,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_safe_defaults(self):
        self.assertFalse(settings.PRR_STAFF_NOTIFICATIONS_ENABLED)
        self.assertEqual(
            runtime.get("PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS"),
            ("publicrecords@psd401.net",),
        )
        self.assertEqual(
            runtime.get("PRR_STAFF_NOTIFICATION_SUPPRESSION_SECONDS"),
            86400,
        )

    def test_notifications_default_off_in_production_until_explicitly_enabled(self):
        result = self._import_settings(
            python=(
                "import config.settings as settings; "
                "print(settings.PRR_STAFF_NOTIFICATIONS_ENABLED)"
            ),
            PRR_DEPLOYMENT_MODE="production",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_an_empty_recipient_list_sends_nowhere_rather_than_anywhere(self):
        """No recipient means no alert, and never a broadcast.

        The recipient list is a stored setting now, and the store lets it be
        emptied -- the same asymmetry every prerequisite has. What must hold
        is that an emptied list makes alerts inert rather than falling back to
        some other address.
        """
        AppSetting.objects.create(
            key="PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS", value=[]
        )
        runtime.invalidate_all()
        self.assertEqual(notifications._records_officer_recipients(), ())

    def test_the_switch_is_a_strict_boolean(self):
        result = self._import_settings(PRR_STAFF_NOTIFICATIONS_ENABLED="yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PRR_STAFF_NOTIFICATIONS_ENABLED must be exactly true or false.",
            result.stderr,
        )


def make_request(**kwargs):
    requester = Requester.objects.create(email="requester@example.org")
    defaults = {
        "requester": requester,
        "description": "Records about the district transportation program.",
    }
    defaults.update(kwargs)
    return PublicRecordsRequest.objects.create(**defaults)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    PRR_STAFF_NOTIFICATIONS_ENABLED=True,
    PRR_STAFF_NOTIFICATION_SUPPRESSION_SECONDS=86400,
    PRR_GMAIL_ENABLED=True,
    PRR_DEPLOYMENT_MODE="production",
    PRR_ENVIRONMENT="production",
    PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS=("records.officer@psd401.net",),
    PRA_OFFICER_EMAIL="records.officer@psd401.net",
    PRA_SUPERVISOR_EMAIL="records.supervisor@psd401.net",
    FACILITIES_EMAIL="facilities@psd401.net",
)
class StaffNotificationServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.officer = get_user_model().objects.create_user(
            username="notification-officer",
            email="assigned.officer@psd401.net",
        )

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_deadline_notification_uses_gmail_audits_and_is_not_correspondence(
        self,
        send_raw,
    ):
        send_raw.return_value = SentMessage("gmail-1", "thread-1")
        due = timezone.localdate() + dt.timedelta(days=1)
        request = make_request(
            assigned_officer=self.officer,
            statutory_response_due=due,
        )

        queued = notifications.notify_deadline(
            request,
            level="URGENT",
            include_supervisor=True,
        )

        self.assertTrue(queued)
        send_raw.assert_called_once()
        message = BytesParser(policy=policy.default).parsebytes(
            send_raw.call_args.args[0]
        )
        self.assertEqual(
            {str(address) for address in message["To"].addresses},
            {"records.officer@psd401.net", "records.supervisor@psd401.net"},
        )
        self.assertIn(request.number, message["Subject"])
        self.assertIn(
            date_format(due, "DATE_FORMAT"),
            message.get_body().get_content(),
        )
        self.assertIn(
            f"/requests/{request.number}",
            message.get_body().get_content(),
        )
        self.assertFalse(Correspondence.objects.exists())
        request.refresh_from_db()
        self.assertFalse(request.initial_response_delivered)
        sent_audit = AuditLog.objects.get(
            action="staff_notification.sent",
            target=request.number,
        )
        self.assertEqual(sent_audit.detail["signal_type"], "deadline.critical")
        self.assertIn(
            "records.officer@psd401.net",
            sent_audit.detail["recipients"],
        )

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_four_unchanged_hourly_deadline_alerts_send_once(self, send_raw):
        send_raw.return_value = SentMessage("gmail-1", "thread-1")
        request = make_request(
            assigned_officer=self.officer,
            statutory_response_due=timezone.localdate(),
        )

        results = [
            notifications.notify_deadline(
                request,
                level="OVERDUE",
                include_supervisor=True,
            )
            for _ in range(4)
        ]

        self.assertEqual(results, [True, None, None, None])
        send_raw.assert_called_once()

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_manual_hold_is_one_immediate_urgent_gmail_message(self, send_raw):
        send_raw.return_value = SentMessage("gmail-urgent", "thread-urgent")
        request = make_request(description="Camera footage from yesterday")
        preservation = FootagePreservation.objects.create(
            request=request,
            window_start=timezone.now() - dt.timedelta(days=1),
            window_end=timezone.now(),
        )

        queued = notifications.notify_footage_manual_hold(
            preservation,
            error="Alta is unavailable",
        )

        self.assertTrue(queued)
        send_raw.assert_called_once()
        message = BytesParser(policy=policy.default).parsebytes(
            send_raw.call_args.args[0]
        )
        self.assertIn("URGENT", message["Subject"])
        self.assertEqual(
            {str(address) for address in message["To"].addresses},
            {"facilities@psd401.net", "records.supervisor@psd401.net"},
        )
        body = message.get_body().get_content()
        self.assertIn('"Manually deleted"', body)
        self.assertIn("ALL cameras district-wide", body)
        self.assertNotIn("saved clips", body)
        self.assertFalse(Correspondence.objects.exists())

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_unapplied_delivery_effect_is_one_immediate_urgent_staff_alert(
        self, send_raw
    ):
        send_raw.return_value = SentMessage("gmail-effect", "thread-effect")
        request = make_request()

        queued = notifications.notify_delivery_effects_failed(
            request,
            correspondence_id="3f1c7c1e-0000-4000-8000-000000000001",
            correspondence_kind="denial",
            error="Approved denial-basis snapshot integrity check failed.",
        )

        self.assertTrue(queued)
        send_raw.assert_called_once()
        message = BytesParser(policy=policy.default).parsebytes(
            send_raw.call_args.args[0]
        )
        self.assertIn("URGENT", message["Subject"])
        self.assertIn(request.number, message["Subject"])
        self.assertEqual(
            {str(address) for address in message["To"].addresses},
            {"records.officer@psd401.net", "records.supervisor@psd401.net"},
        )
        body = message.get_body().get_content()
        self.assertIn("denial", body)
        self.assertIn("integrity check failed", body)
        self.assertIn("was delivered", body)
        self.assertEqual(message["Auto-Submitted"], "auto-generated")
        self.assertFalse(Correspondence.objects.exists())

    @override_settings(PRR_STAFF_NOTIFICATIONS_ENABLED=False)
    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_notifications_can_be_disabled_without_a_gmail_call(self, send_raw):
        request = make_request(statutory_response_due=timezone.localdate())

        self.assertFalse(
            notifications.notify_deadline(
                request,
                level="WARNING",
                include_supervisor=False,
            )
        )

        send_raw.assert_not_called()
        blocked = AuditLog.objects.get(
            action="staff_notification.blocked",
            target=request.number,
        )
        self.assertEqual(
            blocked.detail["reason"],
            "staff_notifications_disabled",
        )

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_queued_notification_rechecks_runtime_kill_switches(self, send_raw):
        send_raw.return_value = SentMessage("gmail-queued", "thread-queued")
        request = make_request(statutory_response_due=timezone.localdate())
        payload = notifications.build_notification(
            signal_type="deadline.warning",
            request=request,
            recipients=("records.officer@psd401.net",),
            subject=f"[WARNING] {request.number}",
            template_name="notifications/deadline.txt",
            context={"level": "WARNING"},
        )
        cases = (
            (
                {"PRR_STAFF_NOTIFICATIONS_ENABLED": False},
                "staff_notifications_disabled",
            ),
            ({"PRR_GMAIL_ENABLED": False}, "gmail_disabled"),
            (
                {
                    "PRR_ENVIRONMENT": "development",
                    "PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST": (),
                },
                "development_recipient_blocked",
            ),
        )

        for overrides, expected_reason in cases:
            with self.subTest(reason=expected_reason), self.settings(**overrides):
                cache.set(
                    notifications._dedupe_key(payload),
                    True,
                    timeout=86400,
                )

                result = notifications.deliver_staff_notification.run(payload)

                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["reason"], expected_reason)
                self.assertIsNone(cache.get(notifications._dedupe_key(payload)))

        send_raw.assert_not_called()
        self.assertEqual(
            list(
                AuditLog.objects.filter(
                    action="staff_notification.blocked",
                    target=request.number,
                )
                .order_by("id")
                .values_list("detail__reason", flat=True)
            ),
            [reason for _, reason in cases],
        )

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    @mock.patch(
        "apps.integrations.avigilon.client.list_device_cameras",
        side_effect=AltaNotConfigured("not configured"),
    )
    def test_repeated_manual_hold_records_suppression_not_delivery_failure(
        self,
        _list_device_cameras,
        send_raw,
    ):
        send_raw.return_value = SentMessage("gmail-urgent", "thread-urgent")
        request = make_request(description="Camera footage from yesterday")
        preservation = FootagePreservation.objects.create(
            request=request,
            window_start=timezone.now() - dt.timedelta(days=1),
            window_end=timezone.now(),
        )

        tasks.execute_footage_hold(preservation.id)
        tasks.execute_footage_hold(preservation.id)

        send_raw.assert_called_once()
        latest_note = request.events.order_by("-occurred_at", "-pk").first().note
        self.assertIn("duplicate alert suppressed", latest_note)
        self.assertNotIn("delivery failed", latest_note)

    @override_settings(
        PRR_DEPLOYMENT_MODE="production",
        PRR_ENVIRONMENT="development",
        PRR_CORRESPONDENCE_DEV_RECIPIENT_ALLOWLIST=("safe@psd401.net",),
    )
    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_deployed_dev_refuses_non_allowlisted_staff_recipient(self, send_raw):
        request = make_request(
            assigned_officer=self.officer,
            statutory_response_due=timezone.localdate(),
        )

        queued = notifications.notify_deadline(
            request,
            level="WARNING",
            include_supervisor=False,
        )

        self.assertFalse(queued)
        send_raw.assert_not_called()
        self.assertTrue(
            AuditLog.objects.filter(
                action="staff_notification.blocked",
                target=request.number,
                detail__signal_type="deadline.warning",
            ).exists()
        )

    @mock.patch(
        "apps.records.notifications.deliver_staff_notification.delay",
        side_effect=OSError("broker unavailable"),
    )
    def test_enqueue_failure_isolated_from_originating_work(self, _delay):
        request = make_request(
            assigned_officer=self.officer,
            statutory_response_due=timezone.localdate(),
        )

        with self.assertLogs("apps.records.notifications", level="WARNING"):
            queued = notifications.notify_deadline(
                request,
                level="WARNING",
                include_supervisor=False,
            )

        self.assertFalse(queued)

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_gmail_failure_never_breaks_originating_deadline_watchdog(
        self,
        send_raw,
    ):
        send_raw.side_effect = GmailUnavailable(
            "Gmail unavailable",
            category="provider_unavailable",
        )
        request = make_request(
            assigned_officer=self.officer,
            statutory_response_due=timezone.localdate() - dt.timedelta(days=1),
        )

        with self.assertLogs("apps.records.notifications", level="WARNING"):
            tasks.deadline_watchdog()

        self.assertTrue(PublicRecordsRequest.objects.filter(pk=request.pk).exists())
        self.assertEqual(send_raw.call_count, 6)
        self.assertTrue(
            AuditLog.objects.filter(
                action="staff_notification.failed",
                target=request.number,
                detail__signal_type="deadline.critical",
            ).exists()
        )

    @mock.patch("apps.records.notifications.gmail_client.send_raw")
    def test_persistent_gmail_failure_is_logged_and_does_not_escape_task(
        self,
        send_raw,
    ):
        send_raw.side_effect = GmailUnavailable(
            "Gmail unavailable",
            category="provider_unavailable",
        )
        request = make_request(statutory_response_due=timezone.localdate())
        payload = notifications.build_notification(
            signal_type="deadline.critical",
            request=request,
            recipients=("records.officer@psd401.net",),
            subject=f"[OVERDUE] {request.number}",
            template_name="notifications/deadline.txt",
            context={"level": "OVERDUE"},
        )

        notifications.deliver_staff_notification.push_request(retries=5)
        try:
            with self.assertLogs("apps.records.notifications", level="ERROR"):
                result = notifications.deliver_staff_notification.run(payload)
        finally:
            notifications.deliver_staff_notification.pop_request()

        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            AuditLog.objects.filter(
                action="staff_notification.failed",
                target=request.number,
                detail__signal_type="deadline.critical",
            ).exists()
        )


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    PRR_STAFF_NOTIFICATIONS_ENABLED=True,
    PRR_GMAIL_ENABLED=True,
    PRR_DEPLOYMENT_MODE="production",
    PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS=("records.officer@psd401.net",),
    PRA_OFFICER_EMAIL="records.officer@psd401.net",
    PRA_SUPERVISOR_EMAIL="records.supervisor@psd401.net",
    FACILITIES_EMAIL="facilities@psd401.net",
    CAMERA_RETENTION_DAYS=10,
)
class StaffNotificationSignalTests(TestCase):
    def setUp(self):
        cache.clear()

    @mock.patch("apps.records.notifications.enqueue_staff_notification")
    def test_estimate_watchdog_emits_overdue_and_approaching_signals(self, enqueue):
        today = timezone.localdate()
        make_request(estimated_completion=today - dt.timedelta(days=1))
        make_request(estimated_completion=today + dt.timedelta(days=1))

        tasks.estimate_watchdog()

        self.assertEqual(
            {call.kwargs["signal_type"] for call in enqueue.call_args_list},
            {"estimate.overdue", "estimate.approaching"},
        )

    @mock.patch("apps.records.notifications.enqueue_staff_notification")
    @mock.patch(
        "apps.integrations.avigilon.client.create_bookmark_hold",
        return_value=BookmarkHold(id="bm-1", coverage=(HeadCoverage("0", 1.0),)),
    )
    @mock.patch("apps.integrations.avigilon.client.list_device_cameras")
    def test_manual_hold_and_completed_hold_each_emit_one_signal(
        self,
        list_cameras,
        _create_bookmark_hold,
        enqueue,
    ):
        list_cameras.side_effect = AltaNotConfigured("not configured")
        request = make_request(description="camera video from yesterday")
        preservation = FootagePreservation.objects.create(
            request=request,
            window_start=timezone.now() - dt.timedelta(days=1),
            window_end=timezone.now(),
        )

        tasks.execute_footage_hold(preservation.id)

        manual_calls = [
            call
            for call in enqueue.call_args_list
            if call.kwargs["signal_type"] == "footage.manual_hold_required"
        ]
        self.assertEqual(len(manual_calls), 1)
        self.assertTrue(manual_calls[0].kwargs["immediate"])

        enqueue.reset_mock()
        list_cameras.side_effect = None
        # Zero cameras is a manual hold, never a completed one (rule 7).
        list_cameras.return_value = [
            Camera(
                id="cam-1", name="Gym", site="PHS", device_group_id="group-PHS",
                server_group_id="server-1", status="CONNECTED",
                pruned_to=timezone.now() - dt.timedelta(days=20), sources=("0",),
            )
        ]
        second_request = make_request(description="second camera request")
        second = FootagePreservation.objects.create(
            request=second_request,
            window_start=timezone.now() - dt.timedelta(days=1),
            window_end=timezone.now(),
        )

        tasks.execute_footage_hold(second.id)

        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.kwargs["signal_type"],
            "footage.hold_complete",
        )

    @mock.patch("apps.records.notifications.enqueue_staff_notification")
    def test_expired_footage_emits_adequate_search_review_signal(self, enqueue):
        old = (timezone.localdate() - dt.timedelta(days=30)).isoformat()
        request = make_request(description=f"camera footage on {old}")

        triage.triage_footage(request)

        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.kwargs["signal_type"],
            "footage.adequate_search_review_queued",
        )

    @mock.patch("apps.records.notifications.enqueue_staff_notification")
    def test_collection_task_signal_uses_custodian_email(self, enqueue):
        system = RecordSystem.objects.create(
            slug="routing-test",
            name="Routing Test System",
            custodian_email="custodian@psd401.net",
            triage_keywords=["routing-sentinel"],
            search_checklist="Search the system.",
        )
        request = make_request(description="routing-sentinel")

        triage.route_to_systems(request)

        enqueue.assert_called_once()
        self.assertEqual(
            enqueue.call_args.kwargs["signal_type"],
            "collection.task_created",
        )
        self.assertEqual(
            enqueue.call_args.kwargs["recipients"],
            [system.custodian_email],
        )


class InternalFailureLoggingTests(TestCase):
    """The failure log has to carry the traceback, not just a sentence.

    `_record_internal_failure` used to log with `exc_info=True`, which reads
    the ambient exception out of `sys.exc_info()`. That works only while the
    `except` frame it was called from is still on the stack -- true today,
    and silently false the moment somebody defers the call or logs from a
    callback. It now takes the exception its caller already caught, and this
    is the test that says the traceback actually arrives.
    """

    def setUp(self):
        cache.clear()

    def test_the_internal_failure_log_carries_the_exception(self):
        payload = {"request_number": "PRR-2026-0001", "signal_type": "deadline"}
        failure = RuntimeError("the database went away")

        with self.assertLogs("apps.records.notifications", level="ERROR") as logged:
            result = notifications._record_internal_failure(payload, failure)

        record = next(
            entry
            for entry in logged.records
            if "failed before Gmail accepted it" in entry.getMessage()
        )
        self.assertIsNotNone(record.exc_info)
        self.assertIs((record.exc_info or (None, None, None))[1], failure)
        self.assertIn("the database went away", logged.output[-1])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["request_number"], "PRR-2026-0001")

    @mock.patch("apps.records.notifications._send_notification")
    @mock.patch("apps.records.notifications._block_notification", return_value=None)
    def test_the_task_hands_down_the_exception_it_caught(self, _blocked, send):
        """The unit test above proves the parameter is logged. This proves
        the task passes the exception it actually caught, rather than one
        the helper would have had to find on the stack for itself."""
        failure = RuntimeError("storage refused the write")
        send.side_effect = failure
        payload = {"request_number": "PRR-2026-0002", "signal_type": "deadline"}

        with self.assertLogs("apps.records.notifications", level="ERROR") as logged:
            result = notifications.deliver_staff_notification(payload)

        record = next(
            entry
            for entry in logged.records
            if "failed before Gmail accepted it" in entry.getMessage()
        )
        self.assertIsNotNone(record.exc_info)
        self.assertIs((record.exc_info or (None, None, None))[1], failure)
        self.assertEqual(result["status"], "failed")
