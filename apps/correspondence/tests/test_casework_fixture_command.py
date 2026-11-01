"""Deterministic high-volume case fixture used by local and CI browser tests."""

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.configuration import runtime
from apps.correspondence.inbound import _provider_thread_evidence
from apps.correspondence.management.commands.seed_casework_test_fixture import (
    FIXTURE_ADMIN_USERNAME,
    FIXTURE_INTAKE_USERNAME,
    FIXTURE_LIAISON_USERNAME,
    FIXTURE_MESSAGE_COUNT,
    FIXTURE_OBSERVER_USERNAME,
    FIXTURE_REQUEST_NUMBER,
    FIXTURE_REVIEWER_USERNAME,
    FIXTURE_SEARCHABLE_MESSAGE_COUNT,
    FIXTURE_USERNAME,
    _fixture_uuid,
)
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDeliveryNotification,
    CorrespondenceRecipient,
    InboundCorrespondenceReceipt,
    _validated_message_id_domain,
)
from apps.correspondence.receiving import _payload_digest
from apps.correspondence.services import (
    PERMANENT_DELIVERY_FAILURES,
    _result_fingerprint,
    claim_delivery,
    report_delivery_result,
    retry_delivery,
)
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
    StatusEvent,
)
from apps.records.service_identities import (
    is_exact_inbound_identity,
    is_exact_outbound_identity,
)
from apps.work_queue.models import WorkItem, WorkItemReceipt


@override_settings(PRR_TEST_FIXTURE_SEED_ENABLED=True)
class CaseworkFixtureCommandTests(TestCase):
    def test_seeds_one_repeatable_case_with_exactly_two_thousand_messages(self):
        stdout = StringIO()

        call_command("seed_casework_test_fixture", stdout=stdout)
        officer = get_user_model().objects.get(username=FIXTURE_USERNAME)
        observer = get_user_model().objects.get(username=FIXTURE_OBSERVER_USERNAME)
        liaison = get_user_model().objects.get(username=FIXTURE_LIAISON_USERNAME)
        reviewer = get_user_model().objects.get(username=FIXTURE_REVIEWER_USERNAME)
        intake = get_user_model().objects.get(username=FIXTURE_INTAKE_USERNAME)
        admin = get_user_model().objects.get(username=FIXTURE_ADMIN_USERNAME)
        self.assertNotEqual(observer.pk, officer.pk)
        self.assertEqual(staff_roles(liaison), {StaffRole.LIAISON})
        self.assertEqual(staff_roles(reviewer), {StaffRole.REVIEWER})
        self.assertEqual(staff_roles(intake), {StaffRole.INTAKE_CLERK})
        self.assertEqual(staff_roles(admin), {StaffRole.ADMIN})
        password_hash = officer.password
        group_ids = list(officer.groups.order_by("pk").values_list("pk", flat=True))
        call_command("seed_casework_test_fixture", stdout=stdout)

        officer.refresh_from_db()
        self.assertEqual(officer.password, password_hash)
        self.assertEqual(
            list(officer.groups.order_by("pk").values_list("pk", flat=True)),
            group_ids,
        )

        cases = PublicRecordsRequest.objects.filter(number=FIXTURE_REQUEST_NUMBER)
        self.assertEqual(cases.count(), 1)
        request = cases.get()
        self.assertEqual(request.pk, _fixture_uuid("request"))
        self.assertEqual(request.status, RequestStatus.CLARIFICATION)
        self.assertIsNone(request.acknowledgment_sent_at)
        requester = request.requester
        self.assertEqual(requester.name, "Synthetic Requester")
        self.assertEqual(requester.email, "synthetic.requester@example.org")
        self.assertEqual(requester.phone, "")
        self.assertEqual(requester.mailing_address, "")
        self.assertEqual(requester.city, "")
        self.assertEqual(requester.state, "")
        self.assertEqual(requester.postal_code, "")
        self.assertEqual(requester.company, "")
        messages = Correspondence.objects.filter(request=request)
        self.assertEqual(messages.count(), FIXTURE_MESSAGE_COUNT)
        self.assertEqual(FIXTURE_MESSAGE_COUNT, 2_000)
        inbound_work = WorkItem.objects.filter(
            request=request,
            source_type=WorkItem.SourceType.INBOUND_RECEIPT,
        )
        self.assertEqual(inbound_work.count(), 2)
        self.assertEqual(
            inbound_work.filter(assignee=officer).count(),
            1,
        )
        collection_task = CollectionTask.objects.get(
            request=request,
            instructions="Synthetic liaison collection assignment.",
        )
        self.assertEqual(collection_task.assigned_to, liaison)
        self.assertTrue(
            WorkItem.objects.filter(
                request=request,
                source_type=WorkItem.SourceType.COLLECTION_TASK,
                source_id=str(collection_task.pk),
                work_kind=WorkItem.WorkKind.COLLECTION,
                assignee=liaison,
            ).exists()
        )
        record_item = RecordItem.objects.get(pk=_fixture_uuid("review-record"))
        self.assertEqual(record_item.content_type, "application/octet-stream")
        uat_record = RecordItem.objects.get(pk=_fixture_uuid("uat-review-record"))
        self.assertEqual(uat_record.original_filename, "synthetic-uat-review-record.pdf")
        self.assertEqual(uat_record.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertTrue(
            WorkItem.objects.filter(
                request=request,
                source_type=WorkItem.SourceType.RECORD_ITEM,
                source_id=str(record_item.pk),
                work_kind=WorkItem.WorkKind.RECORD_REVIEW,
                assignee=reviewer,
            ).exists()
        )
        self.assertTrue(
            WorkItem.objects.filter(
                request=request,
                work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
            ).exists()
        )
        self.assertFalse(
            WorkItem.objects.filter(
                request=request,
                source_type=WorkItem.SourceType.CORRESPONDENCE,
            ).exists()
        )
        self.assertFalse(
            WorkItemReceipt.objects.filter(
                work_item__in=inbound_work,
                user=observer,
            ).exists()
        )
        self.assertEqual(
            set(messages.values_list("pk", flat=True)),
            {_fixture_uuid(f"correspondence-{index}") for index in range(2_000)},
        )

        self.assertTrue(messages.filter(direction="inbound").exists())
        self.assertTrue(messages.filter(direction="outbound").exists())
        self.assertTrue(messages.filter(status="draft").exists())
        self.assertEqual(messages.filter(status="draft").count(), 8)
        self.assertEqual(
            messages.exclude(status="draft").count(),
            FIXTURE_SEARCHABLE_MESSAGE_COUNT,
        )
        self.assertTrue(messages.filter(status="sent").exists())
        self.assertTrue(messages.filter(status="failed").exists())
        clarification_request = messages.get(
            pk=_fixture_uuid("correspondence-2")
        )
        clarification_response = messages.get(
            pk=_fixture_uuid("correspondence-0")
        )
        self.assertEqual(
            clarification_request.kind,
            Correspondence.Kind.CLARIFICATION_REQUEST,
        )
        self.assertEqual(
            clarification_response.kind,
            Correspondence.Kind.CLARIFICATION_RESPONSE,
        )
        self.assertTrue(messages.filter(kind="delivery_status_notification").exists())
        self.assertGreater(
            messages.exclude(provider_thread_id="")
            .values("provider_thread_id")
            .distinct()
            .count(),
            1,
        )
        self.assertTrue(
            messages.filter(received_body__contains="LONG-BODY-CONTENT").exists()
            or messages.filter(approved_body__contains="LONG-BODY-CONTENT").exists()
            or messages.filter(draft_body__contains="LONG-BODY-CONTENT").exists()
        )

        self.assertTrue(
            CorrespondenceDelivery.objects.filter(
                correspondence__request=request,
                status=CorrespondenceDelivery.Status.SENT,
            ).exists()
        )
        self.assertTrue(
            CorrespondenceDelivery.objects.filter(
                correspondence__request=request,
                status=CorrespondenceDelivery.Status.FAILED,
            ).exists()
        )
        self.assertTrue(
            InboundCorrespondenceReceipt.objects.filter(
                parsing_status=InboundCorrespondenceReceipt.ParsingStatus.DUPLICATE,
                matching_status=InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE,
            ).exists()
        )
        self.assertTrue(
            CorrespondenceAttachment.objects.filter(
                correspondence__request=request,
                original_filename__startswith="fixture-attachment-",
            ).exists()
        )
        outbound = messages.filter(direction=Correspondence.Direction.OUTBOUND)
        approved_only = outbound.filter(status=Correspondence.Status.APPROVED)
        self.assertFalse(approved_only.filter(queued_by__isnull=False).exists())
        self.assertFalse(approved_only.filter(queued_at__isnull=False).exists())
        terminal_outbound = outbound.filter(
            status__in=(Correspondence.Status.SENT, Correspondence.Status.FAILED),
        )
        self.assertFalse(terminal_outbound.filter(queued_by__isnull=True).exists())
        self.assertFalse(terminal_outbound.filter(queued_at__isnull=True).exists())
        self.assertEqual(
            CorrespondenceRecipient.objects.filter(
                correspondence__request=request,
            ).count(),
            outbound.count(),
        )
        self.assertFalse(outbound.filter(recipients__isnull=True).exists())

        deliveries = CorrespondenceDelivery.objects.filter(
            correspondence__request=request,
        ).select_related("correspondence")
        self.assertEqual(deliveries.count(), 2)
        terminal_outbound = outbound.filter(
            status__in=(Correspondence.Status.SENT, Correspondence.Status.FAILED),
        )
        self.assertEqual(terminal_outbound.count(), deliveries.count())
        self.assertFalse(terminal_outbound.filter(delivery__isnull=True).exists())
        for delivery in deliveries:
            attempts = delivery.attempts.all()
            self.assertEqual(attempts.count(), delivery.attempt_count)
            self.assertEqual(delivery.provider, "gmail")
            self.assertEqual(delivery.provider_account, "default")
            self.assertEqual(
                delivery.idempotency_key,
                f"correspondence-delivery:{delivery.correspondence_id}",
            )
            self.assertEqual(delivery.version, 3)
            self.assertEqual(delivery.correspondence.version, 4)
            self.assertLessEqual(
                delivery.correspondence.created_at,
                delivery.correspondence.approved_at,
            )
            self.assertEqual(
                delivery.recovery_not_before,
                delivery.queued_at
                + timedelta(
                    seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS")
                ),
            )
            attempt = attempts.get(attempt_number=1)
            self.assertIsNotNone(attempt.result_at)
            self.assertLessEqual(attempt.created_at, attempt.claimed_at)
            self.assertLessEqual(attempt.claimed_at, attempt.result_at)
            self.assertNotEqual(attempt.actor_id, officer.pk)
            self.assertTrue(is_exact_outbound_identity(attempt.actor))
            self.assertEqual(attempt.action, CorrespondenceDeliveryAttempt.Action.SEND)
            self.assertEqual(
                attempt.provider_response_metadata,
                {"http_status": 200, "provider_code": "fixture"}
                if delivery.status == CorrespondenceDelivery.Status.SENT
                else {"http_status": 400, "provider_code": "fixture"},
            )
            self.assertEqual(
                attempt.result_fingerprint,
                _result_fingerprint(
                    outcome=attempt.result_category,
                    provider_message_id=attempt.provider_message_id,
                    provider_thread_id=attempt.provider_thread_id,
                    provider_timestamp=attempt.provider_timestamp,
                    failure_category=attempt.error_category,
                    provider_metadata=attempt.provider_response_metadata,
                ),
            )
            replay = report_delivery_result(
                delivery,
                actor=attempt.actor,
                attempt_id=attempt.pk,
                outcome=attempt.result_category,
                provider_message_id=attempt.provider_message_id,
                provider_thread_id=attempt.provider_thread_id,
                provider_timestamp=attempt.provider_timestamp,
                failure_category=attempt.error_category,
                provider_metadata=attempt.provider_response_metadata,
                now=attempt.result_at,
            )
            self.assertTrue(replay.idempotent)
            if delivery.status == CorrespondenceDelivery.Status.SENT:
                self.assertEqual(
                    attempt.result_category,
                    CorrespondenceDeliveryAttempt.ResultCategory.SENT,
                )
                self.assertEqual(
                    delivery.provider_message_id,
                    delivery.correspondence.provider_message_id,
                )
                self.assertEqual(
                    attempt.provider_message_id,
                    delivery.provider_message_id,
                )
                self.assertEqual(
                    attempt.provider_thread_id,
                    delivery.correspondence.provider_thread_id,
                )
            else:
                self.assertEqual(
                    attempt.result_category,
                    CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
                )
                self.assertEqual(attempt.error_category, "message_rejected")
                self.assertEqual(
                    delivery.last_failure_description,
                    PERMANENT_DELIVERY_FAILURES["message_rejected"],
                )
                self.assertEqual(
                    delivery.correspondence.failure_description,
                    delivery.last_failure_description,
                )

        for receipt_label in ("primary-receipt", "dsn-receipt"):
            receipt = InboundCorrespondenceReceipt.objects.select_related(
                "matched_correspondence"
            ).get(pk=_fixture_uuid(receipt_label))
            correspondence = receipt.matched_correspondence
            self.assertEqual(
                receipt.provider_message_id,
                correspondence.provider_message_id,
            )
            self.assertEqual(
                receipt.provider_thread_id,
                correspondence.provider_thread_id,
            )
            self.assertEqual(receipt.received_at, correspondence.received_at)
            self.assertEqual(receipt.matched_at, correspondence.matched_at)
            self.assertLessEqual(receipt.created_at, receipt.completed_at)
            self.assertLessEqual(receipt.completed_at, receipt.matched_at)
            self.assertGreaterEqual(receipt.matched_at, receipt.received_at)
            self.assertTrue(is_exact_inbound_identity(receipt.submitted_by))
            self.assertEqual(correspondence.created_by_id, receipt.submitted_by_id)
            self.assertEqual(receipt.version, 3)
            self.assertEqual(correspondence.version, 2)
            self.assertEqual(
                receipt.payload_sha256,
                _payload_digest(
                    raw_sha256=receipt.raw_message_sha256,
                    provider=receipt.provider,
                    provider_account=receipt.provider_account,
                    provider_message_id=receipt.provider_message_id,
                    provider_thread_id=receipt.provider_thread_id,
                    envelope_sender=receipt.envelope_sender,
                    envelope_recipients=receipt.envelope_recipients,
                    received_at=receipt.received_at,
                ),
            )

        inbound = messages.filter(direction=Correspondence.Direction.INBOUND)
        matched_receipts = InboundCorrespondenceReceipt.objects.filter(
            pk__in=(
                _fixture_uuid("primary-receipt"),
                _fixture_uuid("dsn-receipt"),
            )
        )
        self.assertEqual(inbound.count(), matched_receipts.count())
        self.assertFalse(inbound.filter(created_by__isnull=True).exists())
        self.assertFalse(inbound.filter(matched_at__isnull=True).exists())
        self.assertFalse(messages.exclude(created_at__year=2026).exists())

        for outbound_message in outbound:
            self.assertEqual(outbound_message.provider, "gmail")
            self.assertEqual(outbound_message.provider_account, "default")
            self.assertEqual(
                outbound_message.rfc_message_id,
                (
                    f"<{outbound_message.pk}.correspondence@"
                    f"{_validated_message_id_domain()}>"
                ),
            )

        primary_receipt = matched_receipts.get(pk=_fixture_uuid("primary-receipt"))
        primary_evidence = _provider_thread_evidence(primary_receipt)
        self.assertEqual(primary_evidence.unique_request_id, request.pk)
        self.assertEqual(
            primary_evidence.exact_outbound_id,
            _fixture_uuid("correspondence-2"),
        )
        self.assertLess(
            messages.get(pk=_fixture_uuid("correspondence-2")).sent_at,
            primary_receipt.received_at,
        )

        clarification_event = request.events.get()
        self.assertEqual(clarification_event.from_status, RequestStatus.RECEIVED)
        self.assertEqual(clarification_event.to_status, RequestStatus.CLARIFICATION)
        self.assertEqual(
            clarification_event.correspondence_effect,
            StatusEvent.CorrespondenceEffect.CLARIFICATION_DELIVERY,
        )
        self.assertEqual(
            clarification_event.occurred_at,
            clarification_request.sent_at,
        )
        self.assertEqual(
            clarification_request.resulting_status_event,
            clarification_event,
        )
        cycle = ClarificationCycle.objects.get(
            pk=_fixture_uuid("clarification-cycle")
        )
        self.assertEqual(cycle.request, request)
        self.assertEqual(cycle.outbound_correspondence, clarification_request)
        self.assertEqual(cycle.inbound_response_correspondence, clarification_response)
        self.assertEqual(cycle.opened_status_event, clarification_event)
        self.assertEqual(cycle.opened_at, clarification_request.sent_at)
        self.assertEqual(cycle.responded_at, clarification_response.received_at)
        self.assertIsNone(cycle.resolved_at)
        self.assertIsNone(cycle.cancelled_at)
        status_audit = AuditLog.objects.get(
            action="request.status_changed",
            target=request.number,
        )
        self.assertEqual(status_audit.actor, officer)
        self.assertEqual(status_audit.detail["from_status"], RequestStatus.RECEIVED)
        self.assertEqual(
            status_audit.detail["to_status"],
            RequestStatus.CLARIFICATION,
        )

        outbound_count = outbound.count()
        self.assertEqual(
            AuditLog.objects.filter(
                action="correspondence.draft_created",
                detail__correspondence_id__in=[
                    str(identifier)
                    for identifier in outbound.values_list("pk", flat=True)
                ],
            ).count(),
            outbound_count,
        )
        for action in (
            "correspondence.approved",
            "correspondence.queued",
            "correspondence.delivery_claimed",
            "correspondence.delivery_result_recorded",
        ):
            self.assertEqual(
                AuditLog.objects.filter(
                    action=action,
                    detail__correspondence_id__in=(
                        str(_fixture_uuid("correspondence-2")),
                        str(_fixture_uuid("correspondence-4")),
                    ),
                ).count(),
                2,
            )
        inbound_audits = AuditLog.objects.filter(
            action="correspondence.inbound_received",
            detail__correspondence_id__in=(
                str(_fixture_uuid("correspondence-0")),
                str(_fixture_uuid("correspondence-5")),
            ),
        )
        self.assertEqual(inbound_audits.count(), 2)
        self.assertEqual(
            set(inbound_audits.values_list("actor_id", flat=True)),
            {primary_receipt.submitted_by_id},
        )

        notification = CorrespondenceDeliveryNotification.objects.get(
            pk=_fixture_uuid("dsn-notification")
        )
        self.assertEqual(
            notification.inbound_receipt.pk,
            _fixture_uuid("dsn-receipt"),
        )
        self.assertEqual(
            notification.inbound_receipt.matched_correspondence.kind,
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        )
        self.assertEqual(notification.delivery.correspondence.request, request)
        self.assertIn(
            "asserted exactly 2000 correspondence rows",
            stdout.getvalue(),
        )

    def test_refuses_to_overwrite_a_colliding_privileged_identity(self):
        User = get_user_model()
        collision = User.objects.create_superuser(
            username=FIXTURE_USERNAME,
            email="administrator@example.org",
            password="do-not-replace",
        )
        password_hash = collision.password

        with self.assertRaisesMessage(CommandError, "fixture identity"):
            call_command("seed_casework_test_fixture")

        collision.refresh_from_db()
        self.assertEqual(collision.password, password_hash)
        self.assertTrue(collision.is_superuser)
        self.assertFalse(collision.groups.exists())
        self.assertFalse(
            PublicRecordsRequest.objects.filter(number=FIXTURE_REQUEST_NUMBER).exists()
        )

    def test_refuses_to_accept_mutated_designated_fixture_evidence(self):
        call_command("seed_casework_test_fixture")
        request = PublicRecordsRequest.objects.get(number=FIXTURE_REQUEST_NUMBER)
        message = Correspondence.objects.filter(
            request=request,
            direction=Correspondence.Direction.OUTBOUND,
        ).first()
        models.QuerySet(
            model=Correspondence,
            using="default",
        ).filter(pk=message.pk).update(draft_subject="corrupted fixture")

        with self.assertRaisesMessage(CommandError, "fixture correspondence"):
            call_command("seed_casework_test_fixture")

    def test_refuses_to_accept_mutated_fixture_association_evidence(self):
        call_command("seed_casework_test_fixture")
        matched = InboundCorrespondenceReceipt.objects.get(
            pk=_fixture_uuid("primary-receipt")
        ).matched_correspondence
        models.QuerySet(
            model=Correspondence,
            using="default",
        ).filter(pk=matched.pk).update(matched_at=None)

        with self.assertRaisesMessage(CommandError, "fixture correspondence"):
            call_command("seed_casework_test_fixture")

    def test_refuses_to_accept_residual_fixture_requester_pii(self):
        call_command("seed_casework_test_fixture")
        Requester.objects.filter(pk=_fixture_uuid("requester")).update(
            phone="253-555-0100"
        )

        with self.assertRaisesMessage(CommandError, "fixture requester"):
            call_command("seed_casework_test_fixture")

    def test_refuses_to_accept_mutated_receipt_linkage_evidence(self):
        call_command("seed_casework_test_fixture")
        alternate_dsn = Correspondence.objects.get(
            pk=_fixture_uuid("correspondence-17")
        )
        models.QuerySet(
            model=InboundCorrespondenceReceipt,
            using="default",
        ).filter(pk=_fixture_uuid("dsn-receipt")).update(
            matched_correspondence_id=alternate_dsn.pk
        )

        with self.assertRaisesMessage(CommandError, "DSN receipt"):
            call_command("seed_casework_test_fixture")

    def test_refuses_to_accept_mutated_fixture_creation_timestamp(self):
        call_command("seed_casework_test_fixture")
        models.QuerySet(
            model=Correspondence,
            using="default",
        ).filter(pk=_fixture_uuid("correspondence-2")).update(
            created_at=timezone.now()
        )

        with self.assertRaisesMessage(CommandError, "fixture correspondence"):
            call_command("seed_casework_test_fixture")

    def test_refuses_foreign_result_event_on_non_lifecycle_message(self):
        call_command("seed_casework_test_fixture")
        request = PublicRecordsRequest.objects.get(number=FIXTURE_REQUEST_NUMBER)
        other_request = PublicRecordsRequest.objects.create(
            number="PRR-E2E-OTHER",
            requester=request.requester,
            description="Synthetic collision fixture.",
            channel=request.channel,
            status=RequestStatus.RECEIVED,
            received_at=request.received_at,
        )
        foreign_event = StatusEvent.objects.create(
            request=other_request,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.GATHERING,
            note="Synthetic foreign event.",
            occurred_at=request.received_at,
        )
        models.QuerySet(
            model=Correspondence,
            using="default",
        ).filter(pk=_fixture_uuid("correspondence-4")).update(
            resulting_status_event_id=foreign_event.pk
        )

        with self.assertRaisesMessage(CommandError, "fixture correspondence"):
            call_command("seed_casework_test_fixture")

    def test_refuses_provider_thread_evidence_collision(self):
        call_command("seed_casework_test_fixture")
        request = PublicRecordsRequest.objects.get(number=FIXTURE_REQUEST_NUMBER)
        officer = get_user_model().objects.get(username=FIXTURE_USERNAME)
        other_request = PublicRecordsRequest.objects.create(
            number="PRR-E2E-THREAD-X",
            requester=request.requester,
            description="Synthetic provider-thread collision.",
            channel=request.channel,
            status=RequestStatus.RECEIVED,
            received_at=request.received_at,
        )
        collision_id = _fixture_uuid("provider-thread-collision")
        collision = Correspondence(
            id=collision_id,
            request=other_request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.SENT,
            created_by=officer,
            draft_subject="Synthetic collision",
            draft_body="Synthetic collision body.",
            approved_subject="Synthetic collision",
            approved_body="Synthetic collision body.",
            approved_by=officer,
            approved_at=request.received_at,
            queued_by=officer,
            queued_at=request.received_at,
            sent_at=request.received_at,
            provider="gmail",
            provider_account="default",
            provider_message_id="fixture-thread-collision-message",
            provider_thread_id="fixture-thread-02",
            rfc_message_id=(
                f"<{collision_id}.correspondence@"
                f"{_validated_message_id_domain()}>"
            ),
            reply_token=_fixture_uuid("provider-thread-collision-reply"),
            version=4,
        )
        models.QuerySet(
            model=Correspondence,
            using="default",
        ).bulk_create([collision])

        with self.assertRaisesMessage(CommandError, "provider-thread"):
            call_command("seed_casework_test_fixture")

    def test_failed_fixture_can_retry_and_claim_through_production_contract(self):
        call_command("seed_casework_test_fixture")
        officer = get_user_model().objects.get(username=FIXTURE_USERNAME)
        failed_delivery = CorrespondenceDelivery.objects.get(
            pk=_fixture_uuid("failed-delivery")
        )
        transport = failed_delivery.attempts.get(attempt_number=1).actor

        retried = retry_delivery(
            failed_delivery,
            actor=officer,
            expected_version=failed_delivery.version,
        )
        claim = claim_delivery(retried, actor=transport)

        self.assertEqual(claim.attempt.attempt_number, 2)
        self.assertEqual(claim.action, CorrespondenceDeliveryAttempt.Action.SEND)

    @override_settings(PRR_TEST_FIXTURE_SEED_ENABLED=False)
    def test_refuses_to_seed_outside_an_explicit_test_fixture_environment(self):
        with self.assertRaisesMessage(
            CommandError,
            "test-only casework fixture seeding is disabled",
        ):
            call_command("seed_casework_test_fixture")
