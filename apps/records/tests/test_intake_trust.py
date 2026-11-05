"""Trust ladder for intake labels (#34): auto-apply spam and non-request.

With a label's trust mode set to ``auto``, an automatic intake classification
of ``spam`` or ``non_request`` at or above the confidence floor closes an
email-created case as non-request with no human actor. Nothing is sent to the
requester, Records Officers are notified, and one click reopens the case.
No other label can be auto-applied (Hagel, 2026-11-05).
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.configuration.registry import SETTINGS_BY_KEY
from apps.correspondence.models import Correspondence, InboundCorrespondenceReceipt
from apps.records import services
from apps.records.advisory_ai import execute_advisory_run
from apps.records.intake_trust import AUTO_LABEL_SETTINGS, auto_apply_intake_label
from apps.records.models import (
    AdvisoryFeedback,
    AdvisoryRun,
    EvaluationRun,
    EvaluationSet,
    IntakeAutoApplication,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)

KIND = AdvisoryRun.Kind.INTAKE_CLASSIFICATION
AUTO_SPAM = {
    "PRR_TRUST_INTAKE_SPAM_MODE": "auto",
    "PRR_TRUST_INTAKE_NON_REQUEST_MODE": "auto",
    "PRR_TRUST_INTAKE_AUTO_MIN_CONFIDENCE": 95,
    "PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS": ["officer@psd401.net"],
}


def _officer(username="trust-officer"):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [StaffRole.RECORDS_OFFICER])
    return user


@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class _Fixture(TestCase):
    def setUp(self):
        notify = patch("apps.records.intake_trust.notify_intake_auto_applied")
        self.notify = notify.start()
        self.addCleanup(notify.stop)
        self.officer = _officer()

    def _case(self, *, email=True, status=RequestStatus.RECEIVED):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="sender@example.org"),
            description="Win a free cruise! Click here.",
            channel="email" if email else "portal",
            status=status,
        )
        if email:
            self._receipt(prr)
        return prr

    def _receipt(self, prr):
        now = timezone.now()
        message_id = f"msg-{prr.pk}"
        correspondence = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="A message",
            received_body=prr.description,
            received_sender="sender@example.org",
            received_recipients=["publicrecords@psd401.net"],
            received_at=prr.received_at,
            matched_at=now,
            provider="gmail",
            provider_account="publicrecords@psd401.net",
            provider_message_id=message_id,
        )
        InboundCorrespondenceReceipt.objects.create(
            provider="gmail",
            provider_account="publicrecords@psd401.net",
            provider_message_id=message_id,
            provider_thread_id=f"{message_id}-thread",
            received_at=prr.received_at,
            envelope_recipients=["publicrecords@psd401.net"],
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            match_method=InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
            matched_request=prr,
            matched_correspondence=correspondence,
            matched_at=now,
            quarantined=False,
            raw_message_size=128,
            raw_message_sha256="a" * 64,
            payload_sha256="b" * 64,
            idempotency_key=f"gmail:poll:{message_id}",
            submitted_by=self.officer,
            completed_at=now,
        )

    def _run(self, prr, *, label="spam", confidence=0.98, trigger=AdvisoryRun.Trigger.AUTOMATIC):
        return AdvisoryRun.objects.create(
            kind=KIND,
            request=prr,
            trigger=trigger,
            requested_by=self.officer if trigger == AdvisoryRun.Trigger.MANUAL else None,
            model_identifier="model-a",
            input_sha256="c" * 64,
            state=AdvisoryRun.State.COMPLETE,
            completed_at=timezone.now(),
            proposal={
                "label": label,
                "target_case_number": None,
                "confidence": confidence,
                "evidence": "Unsolicited advertising.",
            },
        )


@override_settings(**AUTO_SPAM)
class AutoApplyTests(_Fixture):
    def test_spam_above_the_floor_closes_the_case_with_no_human_actor(self):
        prr = self._case()
        run = self._run(prr)

        applied = auto_apply_intake_label(run.pk)
        assert applied is not None

        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLOSED_NON_REQUEST)
        self.assertIsNotNone(prr.closed_at)
        event = StatusEvent.objects.get(request=prr, to_status=RequestStatus.CLOSED_NON_REQUEST)
        self.assertIsNone(event.actor)
        self.assertFalse(event.visible_to_requester)
        self.assertIn("spam", event.note)
        self.assertIn("98%", event.note)
        self.assertEqual(applied.status_event, event)
        self.assertEqual(applied.advisory_run, run)
        self.assertEqual(applied.label, "spam")
        self.assertTrue(AuditLog.objects.filter(action="intake.auto_applied", target=prr.number).exists())
        self.notify.assert_called_once_with(applied)

    def test_nothing_is_sent_to_the_requester(self):
        prr = self._case()
        before = Correspondence.objects.filter(direction=Correspondence.Direction.OUTBOUND).count()

        auto_apply_intake_label(self._run(prr, label="non_request").pk)

        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLOSED_NON_REQUEST)
        self.assertEqual(
            Correspondence.objects.filter(direction=Correspondence.Direction.OUTBOUND).count(), before,
        )

    def test_below_the_floor_changes_nothing(self):
        prr = self._case()
        self.assertIsNone(auto_apply_intake_label(self._run(prr, confidence=0.94).pk))
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.notify.assert_not_called()

    def test_other_labels_are_never_applied(self):
        for label in ("new_request", "related_to", "follow_up", "duplicate_of"):
            with self.subTest(label=label):
                prr = self._case()
                self.assertIsNone(auto_apply_intake_label(self._run(prr, label=label).pk))
                prr.refresh_from_db()
                self.assertEqual(prr.status, RequestStatus.RECEIVED)

    @override_settings(PRR_TRUST_INTAKE_SPAM_MODE="suggest")
    def test_each_label_has_its_own_mode(self):
        spam_case, non_request_case = self._case(), self._case()
        self.assertIsNone(auto_apply_intake_label(self._run(spam_case).pk))
        self.assertIsNotNone(auto_apply_intake_label(self._run(non_request_case, label="non_request").pk))

    def test_a_web_form_case_is_never_closed(self):
        prr = self._case(email=False)
        self.assertIsNone(auto_apply_intake_label(self._run(prr).pk))
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)

    def test_a_case_a_person_already_moved_is_left_alone(self):
        prr = self._case(status=RequestStatus.GATHERING)
        self.assertIsNone(auto_apply_intake_label(self._run(prr).pk))

    def test_a_case_a_person_already_answered_is_left_alone(self):
        prr = self._case()
        run = self._run(prr)
        AdvisoryFeedback.objects.create(
            advisory_run=run, actor=self.officer,
            outcome=AdvisoryFeedback.Outcome.DISMISSED, final_label="new_request",
        )
        self.assertIsNone(auto_apply_intake_label(run.pk))

    def test_a_run_an_officer_asked_for_is_not_applied(self):
        prr = self._case()
        run = self._run(prr, trigger=AdvisoryRun.Trigger.MANUAL)
        self.assertIsNone(auto_apply_intake_label(run.pk))

    def test_an_older_run_is_not_applied(self):
        prr = self._case()
        older = self._run(prr)
        self._run(prr, label="new_request")
        self.assertIsNone(auto_apply_intake_label(older.pk))

    def test_a_run_is_applied_once(self):
        prr = self._case()
        run = self._run(prr)
        auto_apply_intake_label(run.pk)
        self.assertIsNone(auto_apply_intake_label(run.pk))
        self.assertEqual(IntakeAutoApplication.objects.filter(request=prr).count(), 1)

    def test_the_record_is_append_only(self):
        prr = self._case()
        applied = auto_apply_intake_label(self._run(prr).pk)
        assert applied is not None
        with self.assertRaises(ValidationError):
            applied.delete()

    @override_settings(
        PRR_AI_ADVISORY_ENABLED=True,
        PRR_AI_AWS_REGION="us-west-2",
        PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
        PRR_AI_MAX_INPUT_CHARACTERS=50_000,
        PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED=True,
    )
    def test_a_finished_automatic_classification_closes_the_case(self):
        from apps.records.advisory_ai import request_automatic_intake_classification

        prr = self._case()
        with patch("apps.records.tasks.execute_advisory_analysis.delay"):
            run = request_automatic_intake_classification(prr.pk)
        reply = {"label": "spam", "target_case_number": None,
                 "confidence": 0.99, "evidence": "Advertising."}
        with patch("apps.records.advisory_ai.invoke_advisory_model", return_value=reply):
            execute_advisory_run(run.pk)

        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.CLOSED_NON_REQUEST)
        self.assertTrue(IntakeAutoApplication.objects.filter(advisory_run=run).exists())

    def test_a_refused_closure_leaves_the_case_with_a_person(self):
        # Every closure refusal (an unresolved delivery, a closed case, ...)
        # is a ValueError; none may escape into the advisory worker.
        prr = self._case()
        with patch(
            "apps.records.intake_trust.services.auto_close_non_request",
            side_effect=services.CorrespondenceDeliveryRequired("Unresolved delivery."),
        ):
            self.assertIsNone(auto_apply_intake_label(self._run(prr).pk))
        self.assertFalse(IntakeAutoApplication.objects.filter(request=prr).exists())
        self.assertFalse(AuditLog.objects.filter(action="intake.auto_applied").exists())
        self.notify.assert_not_called()


    @override_settings(AUTO_ACK_SAFETY_NET=True)
    def test_the_day_five_safety_net_does_not_reach_an_automatic_closure(self):
        # Accepted by Hagel 2026-11-05: a case the system closed has no
        # safety-net acknowledgment; the officer email, the open inbox item,
        # and Undo are the safeguard against a misclassified real request.
        from apps.records.tasks import auto_acknowledge_safety_net

        closed, still_open = self._case(), self._case()
        auto_apply_intake_label(self._run(closed).pk)
        PublicRecordsRequest.objects.filter(pk__in=[closed.pk, still_open.pk]).update(
            statutory_response_due=timezone.localdate(),
        )

        with patch(
            "apps.correspondence.services.queue_automated_acknowledgment",
        ) as queue:
            auto_acknowledge_safety_net()

        # The open case on its last day is queued; the auto-closed one is not.
        self.assertEqual([call.args[0].pk for call in queue.call_args_list], [still_open.pk])


class SystemClosureGuardTests(_Fixture):
    def test_the_system_closure_only_closes_as_non_request_from_received(self):
        prr = self._case(status=RequestStatus.GATHERING)
        with self.assertRaises(ValidationError):
            services.auto_close_non_request(prr, note="Closed automatically as spam.")

    def test_the_system_closure_needs_an_email_created_case(self):
        prr = self._case(email=False)
        with self.assertRaises(services.EmailIntakeClosureRequired):
            services.auto_close_non_request(prr, note="Closed automatically as spam.")


class TrustSettingTests(TestCase):
    def test_auto_is_offered_only_for_spam_and_non_request(self):
        self.assertEqual(
            set(AUTO_LABEL_SETTINGS),
            {"spam", "non_request"},
        )
        for key in AUTO_LABEL_SETTINGS.values():
            spec = SETTINGS_BY_KEY[key]
            self.assertEqual(spec.default, "suggest")
            self.assertEqual(tuple(spec.choices), ("suggest", "auto"))
        floor = SETTINGS_BY_KEY["PRR_TRUST_INTAKE_AUTO_MIN_CONFIDENCE"]
        self.assertEqual((floor.default, floor.minimum, floor.maximum), (95, 50, 100))


@override_settings(**AUTO_SPAM)
class UndoAndApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.officer)

    def test_the_intake_read_says_the_case_was_closed_automatically(self):
        prr = self._case()
        auto_apply_intake_label(self._run(prr).pk)

        body = self.api.get(f"/api/staff/requests/{prr.number}/advisory-intake/").json()

        self.assertEqual(body["auto_applied"]["label"], "spam")
        self.assertEqual(body["auto_applied"]["confidence"], 0.98)
        self.assertTrue(body["auto_applied"]["in_effect"])

    def test_one_reopen_undoes_it_and_the_read_says_so(self):
        prr = self._case()
        auto_apply_intake_label(self._run(prr).pk)

        reopened = self.api.post(
            f"/api/staff/requests/{prr.number}/reopen/",
            {"to_status": "received", "reason": "Undid the automatic closure as spam."},
            format="json",
        )

        self.assertEqual(reopened.status_code, 200, reopened.content)
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        body = self.api.get(f"/api/staff/requests/{prr.number}/advisory-intake/").json()
        self.assertFalse(body["auto_applied"]["in_effect"])

    def test_no_auto_applied_entry_without_one(self):
        prr = self._case()
        body = self.api.get(f"/api/staff/requests/{prr.number}/advisory-intake/").json()
        self.assertIsNone(body["auto_applied"])


@override_settings(**AUTO_SPAM)
class EvaluationTests(_Fixture):
    def test_an_unconfirmed_automatic_closure_is_not_a_gold_label(self):
        from apps.records.evaluation import build_evaluation_set

        auto_closed = self._case()
        auto_apply_intake_label(self._run(auto_closed).pk)
        confirmed = self._case()
        run = self._run(confirmed)
        auto_apply_intake_label(run.pk)
        AdvisoryFeedback.objects.create(
            advisory_run=run, actor=self.officer,
            outcome=AdvisoryFeedback.Outcome.CONFIRMED, final_label="spam",
        )

        evaluation_set = build_evaluation_set(EvaluationSet.Kind.INTAKE, actor=self.officer)

        refs = {example["request_id"] for example in evaluation_set.examples}
        self.assertNotIn(str(auto_closed.pk), refs)
        self.assertIn(str(confirmed.pk), refs)


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_AI_MODEL_INTAKE="model-a",
)
class SettingEvaluationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create(
            username="trust-admin", email="trust-admin@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.admin, [StaffRole.ADMIN])
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def _rows(self):
        body = self.api.get("/api/staff/settings/").json()
        return {row["key"]: row for row in body["results"]}

    def test_intake_trust_rows_show_the_latest_not_a_request_precision_and_recall(self):
        evaluation_set = EvaluationSet.objects.create(kind=EvaluationSet.Kind.INTAKE, examples=[])
        EvaluationRun.objects.create(
            evaluation_set=evaluation_set,
            model_identifiers=["model-a"],
            results={"model-a": {"items": 10, "confusion": {
                # 4 real non-requests: 3 caught, 1 missed. 1 real request called a non-request.
                "not_request": {"not_request": 3, "new": 1},
                "new": {"new": 5, "not_request": 1},
            }}},
        )

        rows = self._rows()

        for key in ("PRR_TRUST_INTAKE_SPAM_MODE", "PRR_TRUST_INTAKE_NON_REQUEST_MODE"):
            evaluation = rows[key]["evaluation"]
            self.assertEqual(evaluation["model"], "model-a")
            self.assertEqual(evaluation["items"], 10)
            self.assertEqual(evaluation["precision"], 0.75)
            self.assertEqual(evaluation["recall"], 0.75)
        self.assertIsNone(rows["PRR_TRUST_SCOPE_MODE"]["evaluation"])

    def test_no_evaluation_of_the_live_model_says_so(self):
        evaluation_set = EvaluationSet.objects.create(kind=EvaluationSet.Kind.INTAKE, examples=[])
        EvaluationRun.objects.create(
            evaluation_set=evaluation_set, model_identifiers=["model-b"],
            results={"model-b": {"items": 1, "confusion": {}}},
        )
        self.assertIsNone(self._rows()["PRR_TRUST_INTAKE_SPAM_MODE"]["evaluation"])
