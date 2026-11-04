"""Compliance automation tests — the anti-'kick it every 90 days' machinery."""
import datetime as dt
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.configuration import runtime
from apps.correspondence import services as correspondence_services
from apps.correspondence.models import Correspondence, CorrespondenceDelivery
from apps.records import advisory_ai, letters, tasks
from apps.records.models import (
    EstimateRevision,
    LegacyImportBatch,
    LegacyRequestImport,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
    awaiting_initial_response,
)


def make_request(**kw):
    r = Requester.objects.create(email="jane@example.org", name="Jane Q. Public")
    defaults = {"requester": r, "description": "All emails about the stadium project"}
    defaults.update(kw)
    return PublicRecordsRequest.objects.create(**defaults)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class StaffNotificationTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="private-officer",
            email="private.officer@psd401.net",
        )

    def test_deadline_notification_reaches_configured_records_officer(self):
        due = timezone.localdate() + dt.timedelta(days=2)
        prr = make_request(
            assigned_officer=self.officer,
            statutory_response_due=due,
            description="PRIVATE deadline request narrative",
        )

        with mock.patch(
            "apps.records.notifications.enqueue_staff_notification",
            return_value=True,
        ) as enqueue:
            tasks._notify(prr, level="WARNING", cc_supervisor=False)

        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["signal_type"], "deadline.warning")
        self.assertEqual(
            enqueue.call_args.kwargs["recipients"],
            runtime.get("PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS"),
        )
        self.assertIn(prr.number, enqueue.call_args.kwargs["subject"])

    def test_auto_ack_blocked_notifies_officer_and_supervisor(self):
        due = timezone.localdate()
        prr = make_request(
            assigned_officer=self.officer,
            statutory_response_due=due,
            description="PRIVATE blocked safety-net narrative",
        )

        with mock.patch(
            "apps.records.notifications.enqueue_staff_notification",
            return_value=True,
        ) as enqueue:
            tasks._notify(
                prr,
                level="AUTO_ACK_BLOCKED",
                cc_supervisor=True,
            )

        self.assertSetEqual(
            set(enqueue.call_args.kwargs["recipients"]),
            {
                *runtime.get("PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS"),
                runtime.get("PRA_SUPERVISOR_EMAIL"),
            },
        )

    def test_estimate_watchdog_preserves_overdue_human_alert(self):
        overdue_date = timezone.localdate() - dt.timedelta(days=4)
        approaching_date = timezone.localdate() + dt.timedelta(days=1)
        make_request(
            assigned_officer=self.officer,
            estimated_completion=overdue_date,
            description="PRIVATE overdue estimate reason",
        )
        make_request(
            assigned_officer=self.officer,
            estimated_completion=approaching_date,
            description="PRIVATE approaching estimate reason",
        )

        with mock.patch(
            "apps.records.notifications.enqueue_staff_notification",
        ) as enqueue:
            tasks.estimate_watchdog()

        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(
            {call.kwargs["signal_type"] for call in enqueue.call_args_list},
            {"estimate.overdue", "estimate.approaching"},
        )


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    AUTO_ACK_SAFETY_NET=True,
    PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True,
)
class SafetyNetTests(TestCase):
    def test_an_unreadable_settings_store_fails_the_run_instead_of_skipping_it(self):
        # "Switch off" and "switch unreadable" must not look the same: a
        # silent skip on the due date is a late acknowledgment. A failed run
        # is visible to monitoring and the next hourly tick tries again.
        make_request(statutory_response_due=timezone.localdate())
        cache.clear()
        self.addCleanup(cache.clear)
        with mock.patch.object(
            runtime, "_stored_value", side_effect=DatabaseError("store down")
        ):
            with self.assertRaises(DatabaseError):
                tasks.auto_acknowledge_safety_net()

    def test_queues_on_last_day_without_claiming_delivery_or_legal_effect(self):
        prr = make_request(statutory_response_due=timezone.localdate())
        with (
            self.captureOnCommitCallbacks(execute=False),
            mock.patch("apps.records.tasks._notify") as mocked_notify,
            mock.patch("apps.integrations.gmail.client.send_raw")
            as send_raw,
        ):
            tasks.auto_acknowledge_safety_net()
        prr.refresh_from_db()

        correspondence = Correspondence.objects.get(request=prr)
        delivery = CorrespondenceDelivery.objects.get(
            correspondence=correspondence
        )
        self.assertEqual(
            correspondence.kind,
            Correspondence.Kind.ACKNOWLEDGMENT,
        )
        self.assertEqual(correspondence.status, Correspondence.Status.QUEUED)
        self.assertTrue(correspondence.automated)
        self.assertEqual(delivery.status, CorrespondenceDelivery.Status.PENDING)
        self.assertIsNone(prr.acknowledgment_sent_at)
        self.assertIsNone(prr.estimated_completion)
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.assertFalse(EstimateRevision.objects.exists())
        send_raw.assert_not_called()
        mocked_notify.assert_called_once_with(
            prr,
            level="AUTO_ACK_QUEUED",
            cc_supervisor=True,
        )

    def test_batched_awaiting_set_agrees_with_the_per_request_property(self):
        """The set predicate and the per-request property are one rule.

        ``awaiting_initial_response`` selects the cases the day-5 safety net
        still owes a response, and ``initial_response_delivered`` answers the
        same question one case at a time. The cross-case Inbox reads the set
        form so a 50-row page costs one query instead of a hundred, which is
        only safe while the two cannot disagree. Each branch of the property
        -- a sent acknowledgment, an imported legacy response, an
        acknowledged or clarification status event -- is exercised here
        against a case that has none of them.
        """
        undelivered = make_request(description="Nothing delivered yet.")
        acknowledged = make_request(acknowledgment_sent_at=timezone.now())
        batch = LegacyImportBatch.objects.create(
            source_sha256="a" * 64,
            mapping_sha256="b" * 64,
            plan_sha256="c" * 64,
            row_count=2,
            mapping_snapshot={},
            imported_by=get_user_model().objects.create_user(
                "importer",
                is_staff=True,
            ),
        )
        legacy = make_request(description="Answered before the migration.")
        LegacyRequestImport.objects.create(
            batch=batch,
            request=legacy,
            source_id="legacy-1",
            source_status="open",
            source_row_sha256="d" * 64,
            legacy_initial_response_at=timezone.now(),
        )
        legacy_without_response = make_request(description="Imported, unanswered.")
        LegacyRequestImport.objects.create(
            batch=batch,
            request=legacy_without_response,
            source_id="legacy-2",
            source_status="open",
            source_row_sha256="e" * 64,
            legacy_initial_response_at=None,
        )
        clarified = make_request(description="Clarification went out.")
        StatusEvent.objects.create(
            request=clarified,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered.",
        )
        non_initial_event = make_request(description="Moved, but never answered.")
        StatusEvent.objects.create(
            request=non_initial_event,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.GATHERING,
            note="Gathering started without a statutory response.",
        )

        every_request = [
            undelivered,
            acknowledged,
            legacy,
            legacy_without_response,
            clarified,
            non_initial_event,
        ]
        awaiting = set(
            awaiting_initial_response(
                PublicRecordsRequest.objects.filter(
                    pk__in=[prr.pk for prr in every_request]
                )
            ).values_list("pk", flat=True)
        )

        self.assertEqual(
            awaiting,
            {
                undelivered.pk,
                legacy_without_response.pk,
                non_initial_event.pk,
            },
        )
        for prr in every_request:
            with self.subTest(request=prr.description):
                self.assertEqual(
                    prr.pk not in awaiting,
                    prr.initial_response_delivered,
                )

    def test_does_not_fire_early(self):
        prr = make_request(
            statutory_response_due=timezone.localdate() + dt.timedelta(days=3)
        )
        tasks.auto_acknowledge_safety_net()
        prr.refresh_from_db()
        self.assertIsNone(prr.acknowledgment_sent_at)

    def test_does_not_fire_when_already_acknowledged(self):
        prr = make_request(
            statutory_response_due=timezone.localdate(),
            acknowledgment_sent_at=timezone.now(),
        )
        with mock.patch(
            "apps.correspondence.services.queue_automated_acknowledgment"
        ) as queue_acknowledgment:
            tasks.auto_acknowledge_safety_net()

        queue_acknowledgment.assert_not_called()
        self.assertFalse(Correspondence.objects.filter(request=prr).exists())

    def test_blocked_delivery_escalates_and_does_not_abort_later_requests(self):
        blocked = make_request(statutory_response_due=timezone.localdate())
        later = make_request(statutory_response_due=timezone.localdate())

        def queue(prr):
            if prr.pk == blocked.pk:
                raise ValidationError("Correspondence transport is unavailable.")
            return mock.Mock(created=True)

        with (
            mock.patch(
                "apps.correspondence.services.queue_automated_acknowledgment",
                side_effect=queue,
            ) as mocked_queue,
            mock.patch("apps.records.tasks._notify") as mocked_notify,
        ):
            tasks.auto_acknowledge_safety_net()

        self.assertEqual(mocked_queue.call_count, 2)
        mocked_notify.assert_any_call(
            blocked,
            level="AUTO_ACK_BLOCKED",
            cc_supervisor=True,
        )
        mocked_notify.assert_any_call(
            later,
            level="AUTO_ACK_QUEUED",
            cc_supervisor=True,
        )

    def test_acknowledgment_success_race_does_not_emit_blocked_alert(self):
        prr = make_request(statutory_response_due=timezone.localdate())

        def queue(stale_prr):
            PublicRecordsRequest.objects.filter(pk=stale_prr.pk).update(
                acknowledgment_sent_at=timezone.now()
            )
            raise ValidationError("This request has already been acknowledged.")

        with (
            mock.patch(
                "apps.correspondence.services.queue_automated_acknowledgment",
                side_effect=queue,
            ) as mocked_queue,
            mock.patch("apps.records.tasks._notify") as mocked_notify,
        ):
            tasks.auto_acknowledge_safety_net()

        mocked_queue.assert_called_once_with(prr)
        mocked_notify.assert_not_called()

    def test_concurrent_close_does_not_emit_blocked_alert(self):
        prr = make_request(statutory_response_due=timezone.localdate())

        def queue(stale_prr):
            PublicRecordsRequest.objects.filter(pk=stale_prr.pk).update(
                status=RequestStatus.CLOSED_WITHDRAWN
            )
            raise ValidationError("This request is already closed.")

        with (
            mock.patch(
                "apps.correspondence.services.queue_automated_acknowledgment",
                side_effect=queue,
            ) as mocked_queue,
            mock.patch("apps.records.tasks._notify") as mocked_notify,
        ):
            tasks.auto_acknowledge_safety_net()

        mocked_queue.assert_called_once_with(prr)
        mocked_notify.assert_not_called()

    def test_concurrent_clarification_response_does_not_queue_or_alert(self):
        prr = make_request(statutory_response_due=timezone.localdate())
        real_queue = correspondence_services.queue_automated_acknowledgment

        def queue(stale_prr):
            StatusEvent.objects.create(
                request_id=stale_prr.pk,
                from_status=RequestStatus.RECEIVED,
                to_status=RequestStatus.CLARIFICATION,
                note="Clarification delivered concurrently.",
            )
            return real_queue(stale_prr)

        with (
            self.captureOnCommitCallbacks(execute=False),
            mock.patch(
                "apps.correspondence.services.queue_automated_acknowledgment",
                side_effect=queue,
            ) as mocked_queue,
            mock.patch("apps.records.tasks._notify") as mocked_notify,
        ):
            tasks.auto_acknowledge_safety_net()

        mocked_queue.assert_called_once_with(prr)
        mocked_notify.assert_not_called()
        self.assertFalse(Correspondence.objects.exists())
        self.assertFalse(CorrespondenceDelivery.objects.exists())

    def test_blocked_notification_failure_does_not_starve_later_request(self):
        first_received_at = timezone.now() - dt.timedelta(minutes=1)
        blocked = make_request(
            statutory_response_due=timezone.localdate(),
            received_at=first_received_at,
        )
        later = make_request(
            statutory_response_due=timezone.localdate(),
            received_at=first_received_at + dt.timedelta(seconds=1),
        )

        def queue(prr):
            if prr.pk == blocked.pk:
                raise ValidationError("Correspondence transport is unavailable.")
            return mock.Mock(created=True)

        def notify(prr, **kwargs):
            if prr.pk == blocked.pk:
                raise OSError("Internal mail transport unavailable.")

        with (
            mock.patch(
                "apps.correspondence.services.queue_automated_acknowledgment",
                side_effect=queue,
            ) as mocked_queue,
            mock.patch("apps.records.tasks._notify", side_effect=notify),
            self.assertLogs("apps.records.tasks", level="ERROR") as captured,
        ):
            tasks.auto_acknowledge_safety_net()

        self.assertEqual(
            [call.args[0].pk for call in mocked_queue.call_args_list],
            [blocked.pk, later.pk],
        )
        self.assertIn(
            "Safety net officer notification failed",
            "\n".join(captured.output),
        )

    def test_queued_notification_failure_does_not_starve_later_request(self):
        first_received_at = timezone.now() - dt.timedelta(minutes=1)
        first = make_request(
            statutory_response_due=timezone.localdate(),
            received_at=first_received_at,
        )
        later = make_request(
            statutory_response_due=timezone.localdate(),
            received_at=first_received_at + dt.timedelta(seconds=1),
        )

        def notify(prr, **kwargs):
            if prr.pk == first.pk:
                raise OSError("Internal mail transport unavailable.")

        with (
            mock.patch(
                "apps.correspondence.services.queue_automated_acknowledgment",
                return_value=mock.Mock(created=True),
            ) as mocked_queue,
            mock.patch("apps.records.tasks._notify", side_effect=notify),
            self.assertLogs("apps.records.tasks", level="ERROR") as captured,
        ):
            tasks.auto_acknowledge_safety_net()

        self.assertEqual(
            [call.args[0].pk for call in mocked_queue.call_args_list],
            [first.pk, later.pk],
        )
        self.assertIn(
            "Safety net officer notification failed",
            "\n".join(captured.output),
        )

    def test_existing_queued_acknowledgment_does_not_repeat_queue_alert(self):
        prr = make_request(statutory_response_due=timezone.localdate())

        with (
            self.captureOnCommitCallbacks(execute=False),
            mock.patch("apps.records.tasks._notify") as mocked_notify,
        ):
            tasks.auto_acknowledge_safety_net()
            tasks.auto_acknowledge_safety_net()

        self.assertEqual(Correspondence.objects.filter(request=prr).count(), 1)
        self.assertEqual(
            CorrespondenceDelivery.objects.filter(
                correspondence__request=prr,
            ).count(),
            1,
        )
        mocked_notify.assert_called_once_with(
            prr,
            level="AUTO_ACK_QUEUED",
            cc_supervisor=True,
        )

    @override_settings(AUTO_ACK_SAFETY_NET=False)
    def test_kill_switch(self):
        prr = make_request(statutory_response_due=timezone.localdate())
        with mock.patch(
            "apps.correspondence.services.queue_automated_acknowledgment"
        ) as queue_acknowledgment:
            tasks.auto_acknowledge_safety_net()

        queue_acknowledgment.assert_not_called()
        self.assertFalse(Correspondence.objects.filter(request=prr).exists())


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class EstimateRevisionTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="estimate-officer",
            email="estimate.officer@psd401.net",
            is_active=True,
            is_staff=True,
        )

    def test_shared_validator_is_pure_and_rejects_automated_callers(self):
        prr = make_request(estimated_completion=timezone.localdate())
        reason = "  A newly discovered archive requires additional review.  "

        validated = letters.validate_estimate_revision(
            actor=self.officer,
            reason=reason,
        )

        self.assertEqual(
            validated,
            "A newly discovered archive requires additional review.",
        )
        prr.refresh_from_db()
        self.assertEqual(prr.estimated_completion, timezone.localdate())
        self.assertFalse(EstimateRevision.objects.exists())
        self.assertFalse(Correspondence.objects.filter(request=prr).exists())

        with self.assertRaises(PermissionDenied):
            letters.validate_estimate_revision(
                actor=self.officer,
                reason=reason,
                automated=True,
            )

    def test_revision_wrapper_rejects_non_human_actor_without_mutation(self):
        old = timezone.localdate()
        prr = make_request(estimated_completion=old)
        integration = get_user_model().objects.create_user(
            username="estimate-integration",
            is_active=True,
            is_staff=False,
        )

        with self.assertRaises(PermissionDenied):
            letters.revise_estimate(
                prr,
                old + dt.timedelta(days=30),
                "A newly discovered archive requires additional review.",
                actor=integration,
            )

        prr.refresh_from_db()
        self.assertEqual(prr.estimated_completion, old)
        self.assertFalse(EstimateRevision.objects.exists())
        self.assertFalse(Correspondence.objects.filter(request=prr).exists())

    def test_reason_is_mandatory(self):
        prr = make_request(estimated_completion=timezone.localdate())
        for bad in ("", "   ", "delay", None):
            with self.assertRaises(ValueError):
                letters.revise_estimate(
                    prr,
                    timezone.localdate() + dt.timedelta(days=30),
                    bad,
                    actor=self.officer,
                )
        self.assertEqual(EstimateRevision.objects.count(), 0)

    def test_valid_revision_creates_durable_draft_without_legal_effect(self):
        prr = make_request(estimated_completion=timezone.localdate())
        old = prr.estimated_completion
        new = timezone.localdate() + dt.timedelta(days=45)

        draft = letters.revise_estimate(
            prr,
            new,
            "Vault export returned 12,400 messages requiring FERPA review.",
            actor=self.officer,
        )

        prr.refresh_from_db()
        self.assertEqual(prr.estimated_completion, old)
        self.assertEqual(draft.request, prr)
        self.assertEqual(draft.kind, Correspondence.Kind.ESTIMATE_REVISION)
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)
        self.assertEqual(draft.created_by, self.officer)
        self.assertEqual(
            draft.draft_metadata,
            {
                "previous_date": old.isoformat(),
                "new_date": new.isoformat(),
                "reason": (
                    "Vault export returned 12,400 messages requiring FERPA "
                    "review."
                ),
            },
        )
        self.assertFalse(EstimateRevision.objects.exists())
        self.assertFalse(prr.events.exists())
        self.assertFalse(
            CorrespondenceDelivery.objects.filter(correspondence=draft).exists()
        )

    def test_legacy_requester_mail_paths_are_removed(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(letters))
        calls = [
            (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        ]
        self.assertNotIn("send_mail", calls)
        self.assertNotIn("render_to_string", calls)
        self.assertFalse(hasattr(letters, "send_acknowledgment"))
        self.assertFalse(hasattr(letters, "_send_to_requester"))
        self.assertFalse(hasattr(letters, "render_letter"))
        self.assertEqual(
            list(
                (
                    Path(settings.BASE_DIR)
                    / "templates"
                    / "letters"
                ).glob("*.txt")
            ),
            [],
        )

    def test_no_automated_path_exists(self):
        """No background task may call either human-only revision entry point."""
        import ast
        import inspect

        task_paths = sorted(
            (Path(settings.BASE_DIR) / "apps").glob("**/tasks.py")
        )
        self.assertTrue(task_paths)
        for task_path in task_paths:
            source = task_path.read_text()
            tree = ast.parse(source)
            calls = [
                (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            ]
            with self.subTest(task_path=task_path):
                self.assertNotIn("revise_estimate", calls)
                self.assertNotIn("create_estimate_revision_draft", calls)
                self.assertNotIn("create_authored_draft", calls)
                self.assertNotIn("approve_draft", calls)
                self.assertNotIn("queue_approved", calls)
                self.assertNotIn("exclude_camera", calls)
                self.assertNotIn("create_no_records_closure_draft", calls)
                # Closing a case and telling a third party their records are
                # about to be disclosed are decisions with consequences no
                # schedule can weigh. Each needs a named officer.
                self.assertNotIn("create_completion_closure_draft", calls)
                self.assertNotIn("create_nonpayment_closure_draft", calls)
                self.assertNotIn("create_third_party_notice_draft", calls)
                self.assertNotIn("batch_create_closure_drafts", calls)
                self.assertNotIn("batch_approve_and_send", calls)
                # scope-ast-guard: a letter telling a requester their request
                # matched N items is read as pressure to give up, and its
                # clarification form tolls the District's own response period.
                # Both are human judgments; no task may make either.
                self.assertNotIn("create_scope_clarification_draft", calls)
                # fee-ast-guard: quoting a charge, holding a case's work for
                # a deposit, and letting go of a deposit the District already
                # asked for are all decisions a requester feels. Each needs a
                # named officer; a schedule may only surface the case.
                self.assertNotIn("create_fee_estimate_draft", calls)
                self.assertNotIn("apply_deposit_gate", calls)
                self.assertNotIn("lift_deposit_gate", calls)
                self.assertNotIn("record_deposit_payment", calls)
                self.assertNotIn("activate_fee_waiver_floor", calls)
                self.assertNotIn("assess_installment_fee", calls)
                # A first response sent outside this system is recorded only on
                # a Records Officer's statement, never inferred by a task.
                self.assertNotIn("record_external_initial_response", calls)
                # matter-kind-ast-guard: deciding that the person asking for a
                # student's education record is that student's parent is a
                # judgment with custody consequences (34 CFR 99.10). It is
                # recorded under a named officer, never derived by a schedule.
                self.assertNotIn(
                    "record_ferpa_identity_verification", calls
                )
                # Which copy of a message is the record, and which is a
                # duplicate, is a reviewer's judgement recorded in a named
                # officer's name. No task may reach the command that folds
                # them, by name or through the management-command runner.
                self.assertNotIn("fold_duplicate_record_copies", source)
                # Attaching custody evidence to historical records is a human
                # operation run against a named export, not something a
                # background task sweeps for.
                self.assertNotIn("backfill_vault_custodians", source)
                self.assertNotIn("call_command", calls)

        correspondence_tree = ast.parse(
            inspect.getsource(
                correspondence_services.queue_automated_acknowledgment
            )
        )
        correspondence_calls = [
            (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            for node in ast.walk(correspondence_tree)
            if isinstance(node, ast.Call)
        ]
        self.assertNotIn("revise_estimate", correspondence_calls)
        self.assertNotIn(
            "create_estimate_revision_draft",
            correspondence_calls,
        )
        self.assertNotIn(
            "edit_structured_draft",
            correspondence_calls,
        )

        # receipt-ast-guard: neither the automated nor the officer-triggered
        # submission receipt may acquire a statutory effect. The receipt is
        # explicitly non-statutory, so it must never record an acknowledgment,
        # transition the request, or reach a human-only entry point.
        receipt_tree = ast.parse(
            inspect.getsource(
                correspondence_services.create_submission_receipt_draft
            )
            + inspect.getsource(
                correspondence_services.create_officer_submission_receipt_draft
            )
            + inspect.getsource(
                correspondence_services.queue_submission_receipt
            )
        )
        receipt_calls = {
            (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            for node in ast.walk(receipt_tree)
            if isinstance(node, ast.Call)
        }
        self.assertTrue(
            receipt_calls.isdisjoint(
                {
                    "record_acknowledgment",
                    "transition",
                    "revise_estimate",
                    "create_estimate_revision_draft",
                    "create_authored_draft",
                    "exclude_camera",
                }
            )
        )
        self.assertNotIn(
            Correspondence.Kind.SUBMISSION_RECEIPT,
            correspondence_services.REQUEST_LIFECYCLE_EFFECT_KINDS,
        )
        effects_tree = ast.parse(
            inspect.getsource(
                correspondence_services.apply_successful_delivery_effects
            )
        )
        effect_kinds = {
            node.attr
            for node in ast.walk(effects_tree)
            if isinstance(node, ast.Attribute)
        }
        self.assertNotIn("SUBMISSION_RECEIPT", effect_kinds)

        # scope-ast-guard, second half: the scope conversation's only path to
        # a request-state change is the clarification form's delivery effect.
        # Nothing may draft, approve, or send it on the District's behalf.
        scope_tree = ast.parse(
            inspect.getsource(
                correspondence_services.create_scope_clarification_draft
            )
            + inspect.getsource(
                correspondence_services._validated_scope_clarification
            )
        )
        scope_calls = {
            (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            for node in ast.walk(scope_tree)
            if isinstance(node, ast.Call)
        }
        self.assertTrue(
            scope_calls.isdisjoint(
                {
                    "transition",
                    "approve_draft",
                    "approve_and_send",
                    "queue_approved",
                    "record_acknowledgment",
                    "revise_estimate",
                }
            )
        )
        # The advisory that proposes narrowing options must never reach the
        # drafting service: an option list is a suggestion to an officer, not
        # a letter.
        advisory_tree = ast.parse(inspect.getsource(advisory_ai))
        advisory_calls = {
            (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            for node in ast.walk(advisory_tree)
            if isinstance(node, ast.Call)
        }
        self.assertTrue(
            advisory_calls.isdisjoint(
                {
                    "create_scope_clarification_draft",
                    "create_clarification_draft",
                    "approve_and_send",
                    "transition",
                }
            )
        )

    def test_a_system_prepared_letter_is_drafted_never_sent(self):
        """estimate-letter-ast-guard (#42).

        The interpretation-and-estimate letter is the one requester letter the
        system may draft without an author. Rule 6 holds only because the
        drafting path can do nothing else: it calls no approval, queue, send,
        state-change, or deposit function directly (the shared helpers it
        reaches, _create_draft and _render_template_draft, are covered by the
        system_prepared checks and the database constraint). A Records Officer's approve_and_send is
        the only way it reaches a requester.
        """
        import ast
        import inspect

        from apps.records import estimate_letters

        tree = ast.parse(
            inspect.getsource(estimate_letters)
            + inspect.getsource(tasks.prepare_estimate_letters)
            + inspect.getsource(
                correspondence_services.create_interpretation_estimate_draft
            )
        )
        calls = {
            (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        self.assertTrue(
            calls.isdisjoint(
                {
                    "approve_draft",
                    "approve_and_send",
                    "batch_approve_and_send",
                    "queue_approved",
                    "queue_automated_acknowledgment",
                    "transition",
                    "record_acknowledgment",
                    "revise_estimate",
                    "apply_deposit_gate",
                    "create_fee_estimate_draft",
                }
            ),
            calls,
        )
        self.assertEqual(
            correspondence_services.SYSTEM_PREPARED_DRAFT_KINDS,
            frozenset({Correspondence.Kind.INTERPRETATION_ESTIMATE}),
        )
        self.assertTrue(
            correspondence_services.SYSTEM_PREPARED_DRAFT_KINDS.isdisjoint(
                correspondence_services.AUTOMATED_CORRESPONDENCE_KINDS
            )
        )

    def test_a_case_title_cannot_reach_a_requester_a_status_or_vault(self):
        """case-title-ast-guard.

        The case-title advisory is the second thing in this system that runs
        with no accountable actor. That is allowed only because of how little
        it can do: it reads the request text and returns one string for one
        column. The automatic request-intent path beside it is a different
        decision -- a completed intent proposal can authorise an unattended
        Vault count (AGENTS.md rule 11) -- and naming a case must never become
        a second way to buy one.

        The functions are read by name rather than by their module, so a
        later refactor that moves them somewhere chattier still has to keep
        them silent.
        """
        import ast
        import inspect

        from apps.records import advisory_ai as advisory_module

        source = "\n".join(
            inspect.getsource(function)
            for function in (
                advisory_module.request_case_title_advisory,
                advisory_module.request_automatic_case_title_advisory,
                advisory_module.defer_automatic_case_title_advisory,
                advisory_module._request_case_title_advisory,
                advisory_module._case_title_subject,
            )
        )
        tree = ast.parse(source)
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        forbidden = {
            # Nothing a requester ever sees.
            "create_authored_draft",
            "approve_draft",
            "approve_and_send",
            "queue_approved",
            "queue_submission_receipt",
            "queue_automated_acknowledgment",
            # No lifecycle movement, and no deadline.
            "transition",
            "transition_manually",
            "record_acknowledgment",
            "revise_estimate",
            "compute_statutory_deadline",
            # No search, no hold, no export. The whole Vault surface.
            "defer_automatic_vault_count_from_intent",
            "queue_vault_count",
            "queue_vault_collection",
            "request_automatic_request_intent_advisory",
        }
        self.assertEqual(names & forbidden, set())


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, STALL_THRESHOLD_DAYS=30)
class StallDetectorTests(TestCase):
    @mock.patch("apps.records.tasks.notifications.notify_stalled")
    def test_stalled_request_surfaces(self, notify_stalled):
        prr = make_request()
        # Backdate all activity beyond the threshold
        PublicRecordsRequest.objects.filter(pk=prr.pk).update(
            received_at=timezone.now() - timezone.timedelta(days=95)
        )
        tasks.stall_detector()

        notify_stalled.assert_called_once()
        self.assertEqual(notify_stalled.call_args.args[0].pk, prr.pk)

    @mock.patch("apps.records.tasks.notifications.notify_stalled")
    def test_active_request_not_flagged(self, notify_stalled):
        make_request()  # received_at = now
        tasks.stall_detector()
        notify_stalled.assert_not_called()

    @mock.patch("apps.records.tasks.notifications.notify_stalled")
    def test_closed_requests_ignored(self, notify_stalled):
        prr = make_request(status=RequestStatus.FULFILLED)
        PublicRecordsRequest.objects.filter(pk=prr.pk).update(
            received_at=timezone.now() - timezone.timedelta(days=200)
        )
        tasks.stall_detector()
        notify_stalled.assert_not_called()
