import datetime as dt
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.audit.models import AuditLog
from apps.correspondence import gmail_sync, receiving, tasks
from apps.correspondence.models import GmailSyncState, InboundCorrespondenceReceipt
from apps.integrations.gmail import client as gmail_client
from apps.records import notifications
from apps.records.models import PublicRecordsRequest
from apps.work_queue.models import WorkItem
from apps.work_queue.reconciliation import reconcile_inbound_receipt
from apps.work_queue.triage_alerts import alert_waiting_inbound_triage

from .test_internal_gmail_poller import MAILBOX, PROCESSED_LABEL, _plain_message

NOW = dt.datetime(2026, 9, 24, 20, 0, tzinfo=dt.UTC)

POLLER_SETTINGS = {
    "PRR_LOCAL_IMPLICIT_STAFF_ROLES": (),
    "PRR_GMAIL_ENABLED": True,
    "PRR_GMAIL_INBOUND_POLL_ENABLED": True,
    "PRR_GMAIL_MAILBOX": MAILBOX,
    "PRR_GMAIL_INBOUND_PROCESSED_LABEL": PROCESSED_LABEL,
    "PRR_GMAIL_INBOUND_LOOKBACK_DAYS": 7,
    "PRR_GMAIL_INBOUND_STALE_SECONDS": 300,
    "PRR_ENVIRONMENT": "production",
    "PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES": 1024 * 1024,
    "PRR_CORRESPONDENCE_INBOUND_BODY_MAX_BYTES": 64 * 1024,
    "PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS": 50,
    "PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES": 998,
    "PRR_CORRESPONDENCE_INBOUND_MAX_MIME_PARTS": 25,
    "PRR_CORRESPONDENCE_INBOUND_MAX_MIME_DEPTH": 10,
    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT": 5,
    "PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES": 256 * 1024,
    "PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES": 512 * 1024,
}


def _fetched(sequence, raw=None, *, received_at=None):
    return gmail_client.FetchedMessage(
        message_id=f"message-{sequence}",
        thread_id=f"thread-{sequence}",
        history_id=str(100 + sequence),
        received_at=received_at or NOW - dt.timedelta(hours=2),
        raw_bytes=raw if raw is not None else _plain_message(sequence),
        label_ids=("INBOX",),
    )


def _listed(fetched):
    return gmail_client.SentMessage(fetched.message_id, fetched.thread_id)


@override_settings(**POLLER_SETTINGS)
@patch("apps.secure_files.store_quarantined_object")
@patch("apps.correspondence.gmail_sync.gmail_client.add_label")
@patch("apps.correspondence.gmail_sync.gmail_client.get_message")
@patch("apps.correspondence.gmail_sync.gmail_client.list_history")
@patch("apps.records.notifications.notify_inbound_mailbox")
class OneBadMessageDoesNotStallThePollTests(TestCase):
    def _history(self, list_history, *fetched):
        GmailSyncState.objects.create(
            mailbox=MAILBOX, environment="production", history_id="100"
        )
        list_history.return_value = {
            "historyId": "200",
            "history": [
                {"messagesAdded": [{"message": {"id": f.message_id, "threadId": f.thread_id}}]}
                for f in fetched
            ],
        }

    def test_a_failing_message_is_recorded_and_the_rest_are_received(
        self, notify, list_history, get_message, add_label, store
    ):
        bad, good = _fetched(1), _fetched(2)
        self._history(list_history, bad, good)
        get_message.side_effect = [bad, good]
        real_receive = receiving.receive_inbound_message

        def receive(**kwargs):
            if kwargs["provider_message_id"] == bad.message_id:
                raise ValueError("unparseable")
            return real_receive(**kwargs)

        with (
            patch.object(receiving, "receive_inbound_message", side_effect=receive),
            self.assertLogs("apps.correspondence.gmail_sync", level="ERROR"),
        ):
            result = tasks.poll_inbound_gmail.run()

        self.assertEqual(result["received"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            list(
                InboundCorrespondenceReceipt.objects.values_list(
                    "provider_message_id", flat=True
                )
            ),
            [good.message_id],
        )
        # The failed message is never labelled, so reconciliation retries it.
        self.assertEqual(
            [call.args[0] for call in add_label.call_args_list], [good.message_id]
        )
        self.assertEqual(
            GmailSyncState.objects.get(mailbox=MAILBOX).history_id, "200"
        )
        failure = AuditLog.objects.get(action="gmail.inbound_message_failed")
        self.assertEqual(failure.target, bad.message_id)
        self.assertEqual(failure.detail["failure"], "ValueError")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["signal_type"], "inbound.message_failed")

    def test_a_whole_message_quarantine_alerts_the_records_team_at_once(
        self, notify, list_history, get_message, add_label, store
    ):
        held = _fetched(
            5,
            _plain_message(
                5,
                from_header='"Doe, Dana" <dana@psd401.net>',
                subject="Fwd: Data request",
            ),
        )
        self._history(list_history, held)
        get_message.side_effect = [held]

        with patch(
            "apps.correspondence.receiving.parse_inbound_message",
            side_effect=receiving.InboundMimeSecurityError("malformed_message"),
        ):
            tasks.poll_inbound_gmail.run()

        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(receipt.parsing_status, "quarantined")
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["signal_type"], "inbound.message_quarantined")
        self.assertEqual(
            kwargs["template_name"],
            "notifications/inbound_message_quarantined.txt",
        )
        self.assertTrue(kwargs["immediate"])
        self.assertTrue(kwargs["include_supervisor"])
        self.assertEqual(kwargs["condition_key"], str(receipt.pk))
        self.assertIn("Fwd: Data request", kwargs["subject"])
        self.assertEqual(kwargs["context"]["reason"], "malformed_message")
        self.assertEqual(
            kwargs["context"]["sender"], '"Doe, Dana" <dana@psd401.net>'
        )

    def test_a_parsed_message_sends_no_quarantine_alert(
        self, notify, list_history, get_message, add_label, store
    ):
        fine = _fetched(6)
        self._history(list_history, fine)
        get_message.side_effect = [fine]

        tasks.poll_inbound_gmail.run()

        self.assertNotIn(
            "inbound.message_quarantined",
            [call.kwargs["signal_type"] for call in notify.call_args_list],
        )

    def test_a_gmail_outage_mid_batch_still_leaves_the_checkpoint(
        self, notify, list_history, get_message, add_label, store
    ):
        first, second = _fetched(3), _fetched(4)
        self._history(list_history, first, second)
        get_message.side_effect = [
            first,
            gmail_client.GmailUnavailable(
                "Unavailable.", category="provider_unavailable", status_code=503
            ),
        ]

        with (
            self.assertLogs("apps.correspondence.gmail_sync", level="WARNING"),
            self.assertRaises(gmail_client.GmailUnavailable),
        ):
            tasks.poll_inbound_gmail.run()

        self.assertEqual(
            GmailSyncState.objects.get(mailbox=MAILBOX).history_id, "100"
        )
        notify.assert_not_called()


@override_settings(**POLLER_SETTINGS)
@patch("apps.secure_files.store_quarantined_object")
@patch("apps.correspondence.gmail_sync.gmail_client.add_label")
@patch("apps.correspondence.gmail_sync.gmail_client.get_message")
@patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
@patch("apps.records.notifications.notify_inbound_mailbox")
class MailboxReconciliationTests(TestCase):
    def test_the_query_does_not_depend_on_the_inbox_label(
        self, notify, list_messages, get_message, add_label, store
    ):
        list_messages.return_value = []

        gmail_sync.reconcile_inbound_mailbox(now=NOW)

        list_messages.assert_called_once_with(
            "-in:sent -in:drafts -in:chats -in:spam -in:trash "
            "-label:PRR/Processed newer_than:7d"
        )

    def test_a_missed_message_is_received_labelled_and_reported(
        self, notify, list_messages, get_message, add_label, store
    ):
        missed = _fetched(10)
        list_messages.return_value = [_listed(missed)]
        get_message.return_value = missed

        result = gmail_sync.reconcile_inbound_mailbox(now=NOW)

        self.assertEqual(result["received"], 1)
        # Fetched once: the pass reuses what it read to judge the message's age.
        get_message.assert_called_once_with(missed.message_id)
        add_label.assert_called_once()
        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(receipt.provider_message_id, missed.message_id)
        self.assertEqual(receipt.received_at, missed.received_at)
        notify.assert_called_once()
        self.assertEqual(
            notify.call_args.kwargs["signal_type"], "inbound.messages_recovered"
        )
        self.assertEqual(
            notify.call_args.kwargs["context"]["message_ids"], [missed.message_id]
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="gmail.inbound_reconciliation_recovered"
            ).exists()
        )

    def test_a_message_still_in_flight_is_left_to_the_minute_poll(
        self, notify, list_messages, get_message, add_label, store
    ):
        fresh = _fetched(11, received_at=NOW - dt.timedelta(seconds=60))
        list_messages.return_value = [_listed(fresh)]
        get_message.return_value = fresh

        result = gmail_sync.reconcile_inbound_mailbox(now=NOW)

        self.assertEqual(result["in_flight"], 1)
        self.assertEqual(result["received"], 0)
        self.assertFalse(InboundCorrespondenceReceipt.objects.exists())
        notify.assert_not_called()

    def test_an_already_received_but_unlabelled_message_is_relabelled_quietly(
        self, notify, list_messages, get_message, add_label, store
    ):
        seen = _fetched(12)
        list_messages.return_value = [_listed(seen)]
        get_message.return_value = seen
        gmail_sync.reconcile_inbound_mailbox(now=NOW)
        notify.reset_mock()
        add_label.reset_mock()

        result = gmail_sync.reconcile_inbound_mailbox(now=NOW)

        self.assertEqual(result["deduplicated"], 1)
        self.assertEqual(result["received"], 0)
        add_label.assert_called_once()
        notify.assert_not_called()
        self.assertEqual(InboundCorrespondenceReceipt.objects.count(), 1)

    @override_settings(PRR_GMAIL_INBOUND_POLL_ENABLED=False)
    def test_disabled_polling_disables_reconciliation(
        self, notify, list_messages, get_message, add_label, store
    ):
        self.assertEqual(
            gmail_sync.reconcile_inbound_mailbox(now=NOW), {"status": "disabled"}
        )
        list_messages.assert_not_called()

    def test_both_new_jobs_are_on_the_beat_schedule(
        self, notify, list_messages, get_message, add_label, store
    ):
        from config.celery import app

        schedule = app.conf.beat_schedule
        self.assertEqual(
            schedule["reconcile-inbound-gmail"]["task"],
            "apps.correspondence.tasks.reconcile_inbound_gmail",
        )
        self.assertEqual(
            schedule["alert-waiting-inbound-triage"]["task"],
            "apps.work_queue.tasks.alert_waiting_inbound_triage_daily",
        )


@override_settings(**POLLER_SETTINGS)
@patch("apps.secure_files.store_quarantined_object")
@patch("apps.correspondence.gmail_sync.gmail_client.add_label")
@patch("apps.correspondence.gmail_sync.gmail_client.get_message")
@patch("apps.correspondence.gmail_sync.gmail_client.list_messages")
class CatchUpCommandTests(TestCase):
    def _request_message(self, sequence, **kwargs):
        return _fetched(
            sequence,
            _plain_message(
                sequence,
                subject="Public records request",
                body="Please provide the board minutes.",
                **kwargs,
            ),
        )

    def test_the_dry_run_predicts_and_writes_nothing(
        self, list_messages, get_message, add_label, store
    ):
        request = self._request_message(20)
        old_address = self._request_message(21, delivered_to="former.officer@psd401.net")
        list_messages.return_value = [_listed(request), _listed(old_address)]
        get_message.side_effect = lambda message_id: {
            request.message_id: request,
            old_address.message_id: old_address,
        }[message_id]
        out = StringIO()

        call_command("catch_up_inbound_mailbox", "--after", "2026-01-01", stdout=out)

        output = out.getvalue()
        self.assertIn(f"NEW CASE | 2026-09-24 18:00 | {request.message_id}", output)
        self.assertIn("TRIAGE (sent to former.officer@psd401.net)", output)
        self.assertIn("nothing was written", output)
        self.assertIn("after:2026/01/01", list_messages.call_args.args[0])
        self.assertFalse(InboundCorrespondenceReceipt.objects.exists())
        add_label.assert_not_called()

    def test_commit_needs_named_messages(
        self, list_messages, get_message, add_label, store
    ):
        with self.assertRaises(CommandError):
            call_command("catch_up_inbound_mailbox", "--commit", "--after", "2026-01-01")

    def test_commit_receives_only_the_named_message_and_can_open_its_case(
        self, list_messages, get_message, add_label, store
    ):
        request = self._request_message(22)
        get_message.return_value = request

        call_command(
            "catch_up_inbound_mailbox",
            "--commit",
            "--message-id",
            request.message_id,
            stdout=StringIO(),
        )

        list_messages.assert_not_called()
        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(receipt.provider_message_id, request.message_id)
        self.assertIsNotNone(receipt.matched_request_id)
        self.assertEqual(
            PublicRecordsRequest.objects.get().received_at, request.received_at
        )

    def test_triage_only_receives_without_opening_a_case(
        self, list_messages, get_message, add_label, store
    ):
        request = self._request_message(23)
        get_message.return_value = request

        call_command(
            "catch_up_inbound_mailbox",
            "--commit",
            "--triage-only",
            "--message-id",
            request.message_id,
            stdout=StringIO(),
        )

        receipt = InboundCorrespondenceReceipt.objects.get()
        self.assertEqual(
            receipt.matching_status,
            InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        self.assertFalse(PublicRecordsRequest.objects.exists())
        add_label.assert_called_once()


@override_settings(**POLLER_SETTINGS, PRR_INBOX_TRIAGE_ALERT_BUSINESS_DAYS=1)
@patch("apps.secure_files.store_quarantined_object")
@patch("apps.records.notifications.notify_inbound_mailbox", return_value=True)
class WaitingTriageAlertTests(TestCase):
    def _receipt(self, sequence, received_at, *, delivered_to=MAILBOX):
        result = receiving.receive_inbound_message(
            raw_message=_plain_message(sequence, delivered_to=delivered_to),
            provider="gmail",
            provider_account=MAILBOX,
            provider_message_id=f"triage-{sequence}",
            provider_thread_id=f"triage-thread-{sequence}",
            envelope_sender="",
            envelope_recipients=[delivered_to],
            received_at=received_at,
            idempotency_key=f"triage:{sequence}",
            actor=gmail_sync.system_inbound_principal(),
        )
        reconcile_inbound_receipt(result.receipt_id)
        return result

    def test_mail_waiting_past_the_threshold_is_named_with_its_address(
        self, notify, store
    ):
        # Monday 2026-09-21 to Thursday 2026-09-24 is three business days.
        self._receipt(
            30,
            dt.datetime(2026, 9, 21, 20, 1, tzinfo=dt.UTC),
            delivered_to="former.officer@psd401.net",
        )
        # Received today: not yet overdue for triage.
        self._receipt(31, dt.datetime(2026, 9, 24, 16, 0, tzinfo=dt.UTC))

        result = alert_waiting_inbound_triage(today=dt.date(2026, 9, 24))

        self.assertEqual(result["waiting"], 1)
        context = notify.call_args.kwargs["context"]
        self.assertEqual(context["by_kind"], [("Unmatched inbound", 1)])
        self.assertEqual(context["by_address"], [("former.officer@psd401.net", 1)])
        self.assertEqual(context["oldest"], dt.date(2026, 9, 21))

    def test_triaged_mail_is_not_counted(self, notify, store):
        self._receipt(32, dt.datetime(2026, 9, 21, 20, 1, tzinfo=dt.UTC))
        WorkItem.objects.update(disposition=WorkItem.Disposition.WAITING)

        result = alert_waiting_inbound_triage(today=dt.date(2026, 9, 24))

        self.assertEqual(result, {"status": "ok", "waiting": 0, "alerted": False})
        notify.assert_not_called()


@override_settings(
    PRR_STAFF_NOTIFICATIONS_ENABLED=True,
    PRR_GMAIL_ENABLED=True,
    PRR_ENVIRONMENT="production",
    PRR_GMAIL_MAILBOX=MAILBOX,
    PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS=("officer@psd401.net",),
    PRA_SUPERVISOR_EMAIL="supervisor@psd401.net",
    PORTAL_BASE_URL="https://prr.psd401.net",
)
class MailboxAlertPayloadTests(TestCase):
    def test_a_mailbox_alert_needs_no_case_and_links_to_the_inbox(self):
        payload = notifications.build_notification(
            signal_type="inbound.triage_waiting",
            request=None,
            recipients=("officer@psd401.net",),
            subject="[INBOX TRIAGE] 1 inbound message(s) waiting",
            template_name="notifications/inbound_triage_waiting.txt",
            context={
                "waiting": 1,
                "threshold": 1,
                "oldest": dt.date(2026, 9, 21),
                "by_kind": [("Unmatched inbound", 1)],
                "by_address": [("former.officer@psd401.net", 1)],
            },
        )

        self.assertEqual(payload["request_id"], "")
        self.assertEqual(payload["request_number"], notifications.MAILBOX_ALERT_TARGET)
        self.assertIn("former.officer@psd401.net: 1", payload["body"])
        self.assertIn("https://prr.psd401.net/inbox", payload["body"])
        self.assertIn("2026-09-21", payload["body"])

    @patch("apps.records.notifications.deliver_staff_notification.delay")
    def test_a_mailbox_alert_goes_to_officers_and_optionally_the_supervisor(
        self, delay
    ):
        notifications.notify_inbound_mailbox(
            signal_type="inbound.message_failed",
            subject="[INBOUND MAIL FAILED] Gmail message m-1 was not received",
            template_name="notifications/inbound_message_failed.txt",
            context={"message_id": "m-1", "failure": "ValueError", "lookback_days": 7},
            condition_key="m-1:ValueError",
            include_supervisor=True,
        )

        payload = delay.call_args.args[0]
        self.assertEqual(
            payload["recipients"], ["officer@psd401.net", "supervisor@psd401.net"]
        )
        self.assertIn("m-1", payload["body"])
