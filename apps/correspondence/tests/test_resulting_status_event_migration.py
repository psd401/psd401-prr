import datetime as dt
import hashlib
import json

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ResultingStatusEventIntegrityMigrationTests(TransactionTestCase):
    audit_target = ("audit", "0002_auditlog_append_only_trigger")
    records_target = (
        "records",
        "0008_statusevent_correspondence_effect_and_more",
    )
    migrate_from = (
        "correspondence",
        "0011_recipient_snapshot_cutover",
    )
    migrate_to = (
        "correspondence",
        "0012_resulting_status_event_integrity",
    )

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def _migrate_before_cutover(self):
        return self._migrate(
            [
                self.migrate_from,
                self.records_target,
                self.audit_target,
            ]
        )

    def _make_delivery(self, apps, *, correspondence, actor, suffix, sent_at):
        Delivery = apps.get_model(
            "correspondence",
            "CorrespondenceDelivery",
        )
        Attempt = apps.get_model(
            "correspondence",
            "CorrespondenceDeliveryAttempt",
        )
        completed_at = sent_at + dt.timedelta(minutes=1)
        provider_message_id = f"provider-message-{suffix}"
        provider_thread_id = f"provider-thread-{suffix}"
        delivery = Delivery.objects.create(
            correspondence=correspondence,
            status="sent",
            idempotency_key=f"result-event-migration:{suffix}",
            attempt_count=1,
            successful_attempt_number=1,
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
            queued_at=sent_at - dt.timedelta(minutes=2),
            completed_at=completed_at,
        )
        Attempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            action="send",
            claim_owner=str(actor.pk),
            claimed_at=sent_at - dt.timedelta(minutes=1),
            result_at=completed_at,
            result_category="sent",
            provider_message_id=provider_message_id,
            provider_thread_id=provider_thread_id,
            provider_timestamp=sent_at,
            result_fingerprint="a" * 64,
            actor=actor,
        )

    def _attest_recipients(
        self,
        apps,
        *,
        correspondence,
        requester,
        actor,
    ):
        Recipient = apps.get_model(
            "correspondence",
            "CorrespondenceRecipient",
        )
        AuditLog = apps.get_model("audit", "AuditLog")
        recipient = Recipient.objects.create(
            correspondence=correspondence,
            recipient_type="to",
            email=requester.email,
            display_name="Migration Requester",
            requester=requester,
        )
        snapshot = [
            {
                "id": str(recipient.pk),
                "recipient_type": "to",
                "email": requester.email,
                "display_name": "Migration Requester",
                "requester_id": str(requester.pk),
            }
        ]
        serialized = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        AuditLog.objects.create(
            action="correspondence.approved",
            target=correspondence.request.number,
            actor=actor,
            detail={
                "correspondence_id": str(correspondence.pk),
                "recipient_snapshot_sha256": hashlib.sha256(
                    serialized.encode("utf-8")
                ).hexdigest(),
                "recipient_count": 1,
            },
        )

    def _make_sent_lifecycle_graph(
        self,
        apps,
        *,
        kind,
        suffix,
        acknowledgment_style="current",
    ):
        User = apps.get_model("auth", "User")
        Requester = apps.get_model("records", "Requester")
        Request = apps.get_model("records", "PublicRecordsRequest")
        StatusEvent = apps.get_model("records", "StatusEvent")
        EstimateRevision = apps.get_model("records", "EstimateRevision")
        Correspondence = apps.get_model(
            "correspondence",
            "Correspondence",
        )
        ClarificationCycle = apps.get_model(
            "correspondence",
            "ClarificationCycle",
        )
        AuditLog = apps.get_model("audit", "AuditLog")

        sent_at = timezone.now().replace(microsecond=0)
        actor = User.objects.create(
            username=f"result-event-{suffix}",
            is_staff=True,
        )
        requester = Requester.objects.create(
            name="Migration Requester",
            email=f"result-event-{suffix}@example.invalid",
        )
        request_status = {
            "acknowledgment": "acknowledged",
            "clarification_request": "clarification",
            "estimate_revision": "gathering",
            "denial": "denied",
        }[kind]
        request = Request.objects.create(
            number=f"PRR-MIG-{suffix.upper()}",
            requester=requester,
            description=f"Result event migration fixture {suffix}.",
            status=request_status,
            acknowledgment_sent_at=(
                sent_at if kind == "acknowledgment" else None
            ),
            closed_at=sent_at if kind == "denial" else None,
        )

        event_values = {
            "acknowledgment": {
                "from_status": "received",
                "to_status": "acknowledged",
                "note": "The district acknowledged this request.",
                "visible_to_requester": True,
            },
            "clarification_request": {
                "from_status": "gathering",
                "to_status": "clarification",
                "note": "Please clarify the requested date range.",
                "visible_to_requester": True,
            },
            "estimate_revision": {
                "from_status": "",
                "to_status": "",
                "note": (
                    "Estimated completion revised to 2026-09-15. "
                    "Reason: Additional custodians require more collection."
                ),
                "visible_to_requester": True,
            },
            "denial": {
                "from_status": "review",
                "to_status": "denied",
                "note": "The request was denied under the cited exemption.",
                "visible_to_requester": True,
            },
        }[kind]
        companion = None
        if (
            kind == "acknowledgment"
            and acknowledgment_style == "legacy"
        ):
            event_values = {
                "from_status": "received",
                "to_status": "acknowledged",
                "note": "",
                "visible_to_requester": False,
            }
        event = StatusEvent.objects.create(
            request=request,
            occurred_at=sent_at,
            actor=actor,
            **event_values,
        )
        if (
            kind == "acknowledgment"
            and acknowledgment_style == "legacy"
        ):
            companion = StatusEvent.objects.create(
                request=request,
                occurred_at=sent_at,
                actor=actor,
                from_status="",
                to_status="",
                note="The district acknowledged this request.",
                visible_to_requester=True,
            )

        approved_metadata = {}
        if kind == "estimate_revision":
            approved_metadata = {
                "new_date": "2026-09-15",
                "reason": (
                    "Additional custodians require more collection."
                ),
            }
        correspondence = Correspondence.objects.create(
            request=request,
            direction="outbound",
            kind=kind,
            status="sent",
            draft_subject=f"Migration {kind}",
            draft_body="Requester-facing migration evidence.",
            approved_subject=f"Migration {kind}",
            approved_body="Requester-facing migration evidence.",
            approved_metadata=approved_metadata,
            created_by=actor,
            approved_by=actor,
            queued_by=actor,
            approved_at=sent_at - dt.timedelta(minutes=3),
            queued_at=sent_at - dt.timedelta(minutes=2),
            sent_at=sent_at,
            provider_message_id=f"provider-message-{suffix}",
            provider_thread_id=f"provider-thread-{suffix}",
            resulting_status_event=event,
        )
        self._attest_recipients(
            apps,
            correspondence=correspondence,
            requester=requester,
            actor=actor,
        )
        self._make_delivery(
            apps,
            correspondence=correspondence,
            actor=actor,
            suffix=suffix,
            sent_at=sent_at,
        )

        if kind == "acknowledgment":
            AuditLog.objects.create(
                action="request.acknowledged",
                target=request.number,
                actor=actor,
                detail={"communicated_at": sent_at.isoformat()},
            )
        elif kind == "clarification_request":
            ClarificationCycle.objects.create(
                request=request,
                outbound_correspondence=correspondence,
                opened_at=sent_at,
                opened_status_event=event,
            )
        elif kind == "estimate_revision":
            EstimateRevision.objects.create(
                request=request,
                previous_date=dt.date(2026, 8, 31),
                new_date=dt.date(2026, 9, 15),
                reason=(
                    "Additional custodians require more collection."
                ),
                revised_by=actor,
                automated=False,
                communicated_at=sent_at,
                correspondence=correspondence,
            )

        return {
            "actor": actor,
            "companion": companion,
            "correspondence": correspondence,
            "event": event,
            "request": request,
            "requester": requester,
            "sent_at": sent_at,
        }

    def test_valid_current_lifecycle_graphs_are_typed(self):
        old_apps = self._migrate_before_cutover()
        fixtures = {
            kind: self._make_sent_lifecycle_graph(
                old_apps,
                kind=kind,
                suffix=suffix,
            )
            for kind, suffix in (
                ("acknowledgment", "ack"),
                ("clarification_request", "clar"),
                ("estimate_revision", "estimate"),
                ("denial", "denial"),
            )
        }

        new_apps = self._migrate([self.migrate_to])
        StatusEvent = new_apps.get_model("records", "StatusEvent")
        expected_effects = {
            "acknowledgment": "acknowledgment_delivery",
            "clarification_request": "clarification_delivery",
            "estimate_revision": "estimate_revision_delivery",
            "denial": "denial_delivery",
        }
        for kind, fixture in fixtures.items():
            with self.subTest(kind=kind):
                event = StatusEvent.objects.get(pk=fixture["event"].pk)
                self.assertEqual(
                    event.correspondence_effect,
                    expected_effects[kind],
                )

    def test_exact_legacy_acknowledgment_graph_is_accepted(self):
        old_apps = self._migrate_before_cutover()
        fixture = self._make_sent_lifecycle_graph(
            old_apps,
            kind="acknowledgment",
            suffix="legacy",
            acknowledgment_style="legacy",
        )

        new_apps = self._migrate([self.migrate_to])
        StatusEvent = new_apps.get_model("records", "StatusEvent")
        transition = StatusEvent.objects.get(pk=fixture["event"].pk)
        companion = StatusEvent.objects.get(pk=fixture["companion"].pk)
        self.assertEqual(
            transition.correspondence_effect,
            "acknowledgment_delivery",
        )
        self.assertEqual(companion.correspondence_effect, "")

    def test_corrupt_result_event_link_aborts_without_partial_backfill(self):
        old_apps = self._migrate_before_cutover()
        fixture = self._make_sent_lifecycle_graph(
            old_apps,
            kind="acknowledgment",
            suffix="corrupt",
        )
        User = old_apps.get_model("auth", "User")
        StatusEvent = old_apps.get_model("records", "StatusEvent")
        other_actor = User.objects.create(
            username="result-event-corrupt-other",
            is_staff=True,
        )
        StatusEvent.objects.filter(pk=fixture["event"].pk).update(
            actor_id=other_actor.pk,
        )

        try:
            with self.assertRaisesMessage(
                RuntimeError,
                str(fixture["correspondence"].pk),
            ):
                self._migrate([self.migrate_to])
            event = StatusEvent.objects.get(pk=fixture["event"].pk)
            self.assertEqual(event.correspondence_effect, "")
        finally:
            StatusEvent.objects.filter(pk=fixture["event"].pk).update(
                actor_id=fixture["actor"].pk,
            )
            self._migrate([self.migrate_to])

    def test_automated_estimate_revision_aborts_cutover(self):
        old_apps = self._migrate_before_cutover()
        fixture = self._make_sent_lifecycle_graph(
            old_apps,
            kind="estimate_revision",
            suffix="automated-estimate",
        )
        EstimateRevision = old_apps.get_model(
            "records",
            "EstimateRevision",
        )
        EstimateRevision.objects.filter(
            correspondence_id=fixture["correspondence"].pk,
        ).update(automated=True)

        try:
            with self.assertRaisesMessage(
                RuntimeError,
                str(fixture["correspondence"].pk),
            ):
                self._migrate([self.migrate_to])
        finally:
            EstimateRevision.objects.filter(
                correspondence_id=fixture["correspondence"].pk,
            ).update(automated=False)
            self._migrate([self.migrate_to])

    def test_non_substantive_estimate_reason_aborts_cutover(self):
        old_apps = self._migrate_before_cutover()
        fixture = self._make_sent_lifecycle_graph(
            old_apps,
            kind="estimate_revision",
            suffix="short-estimate-reason",
        )
        EstimateRevision = old_apps.get_model(
            "records",
            "EstimateRevision",
        )
        Correspondence = old_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        StatusEvent = old_apps.get_model("records", "StatusEvent")
        short_reason = "Too short"
        EstimateRevision.objects.filter(
            correspondence_id=fixture["correspondence"].pk,
        ).update(reason=short_reason)
        Correspondence.objects.filter(
            pk=fixture["correspondence"].pk,
        ).update(
            approved_metadata={
                "new_date": "2026-09-15",
                "reason": short_reason,
            }
        )
        StatusEvent.objects.filter(pk=fixture["event"].pk).update(
            note=(
                "Estimated completion revised to 2026-09-15. "
                f"Reason: {short_reason}"
            )
        )

        try:
            with self.assertRaisesMessage(
                RuntimeError,
                str(fixture["correspondence"].pk),
            ):
                self._migrate([self.migrate_to])
        finally:
            valid_reason = "Additional custodians require more collection."
            EstimateRevision.objects.filter(
                correspondence_id=fixture["correspondence"].pk,
            ).update(reason=valid_reason)
            Correspondence.objects.filter(
                pk=fixture["correspondence"].pk,
            ).update(
                approved_metadata={
                    "new_date": "2026-09-15",
                    "reason": valid_reason,
                }
            )
            StatusEvent.objects.filter(pk=fixture["event"].pk).update(
                note=(
                    "Estimated completion revised to 2026-09-15. "
                    f"Reason: {valid_reason}"
                )
            )
            self._migrate([self.migrate_to])

    def test_duplicate_use_aborts_then_database_enforces_uniqueness(self):
        old_apps = self._migrate_before_cutover()
        fixture = self._make_sent_lifecycle_graph(
            old_apps,
            kind="acknowledgment",
            suffix="duplicate-a",
        )
        Correspondence = old_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        duplicate = Correspondence.objects.create(
            request=fixture["request"],
            direction="outbound",
            kind="acknowledgment",
            status="sent",
            draft_subject="Duplicate migration acknowledgment",
            draft_body="Duplicate migration acknowledgment body.",
            approved_subject="Duplicate migration acknowledgment",
            approved_body="Duplicate migration acknowledgment body.",
            created_by=fixture["actor"],
            approved_by=fixture["actor"],
            queued_by=fixture["actor"],
            approved_at=fixture["sent_at"] - dt.timedelta(minutes=3),
            queued_at=fixture["sent_at"] - dt.timedelta(minutes=2),
            sent_at=fixture["sent_at"],
            provider_message_id="provider-message-duplicate-b",
            provider_thread_id="provider-thread-duplicate-b",
            resulting_status_event=fixture["event"],
        )
        self._attest_recipients(
            old_apps,
            correspondence=duplicate,
            requester=fixture["requester"],
            actor=fixture["actor"],
        )
        self._make_delivery(
            old_apps,
            correspondence=duplicate,
            actor=fixture["actor"],
            suffix="duplicate-b",
            sent_at=fixture["sent_at"],
        )

        try:
            with self.assertRaises(RuntimeError) as raised:
                self._migrate([self.migrate_to])
            message = str(raised.exception)
            self.assertIn(str(fixture["correspondence"].pk), message)
            self.assertIn(str(duplicate.pk), message)
        finally:
            Correspondence.objects.filter(pk=duplicate.pk).update(
                kind="general",
                resulting_status_event_id=None,
            )

        new_apps = self._migrate([self.migrate_to])
        NewCorrespondence = new_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            NewCorrespondence.objects.create(
                request_id=fixture["request"].pk,
                direction="outbound",
                kind="general",
                status="draft",
                draft_subject="Unique result-event probe",
                draft_body="Unique result-event probe body.",
                resulting_status_event_id=fixture["event"].pk,
            )


class StatusEventCorrespondenceEffectMigrationTests(TransactionTestCase):
    migrate_from = (
        "records",
        "0007_estimate_revision_delivery_evidence",
    )
    records_target = (
        "records",
        "0008_statusevent_correspondence_effect_and_more",
    )
    correspondence_before_cutover = (
        "correspondence",
        "0011_recipient_snapshot_cutover",
    )
    migrate_latest = (
        "correspondence",
        "0012_resulting_status_event_integrity",
    )

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_reverse_refuses_to_remove_typed_effect_evidence(self):
        apps = self._migrate(
            [
                self.correspondence_before_cutover,
                self.records_target,
            ]
        )
        User = apps.get_model("auth", "User")
        Requester = apps.get_model("records", "Requester")
        Request = apps.get_model("records", "PublicRecordsRequest")
        StatusEvent = apps.get_model("records", "StatusEvent")
        actor = User.objects.create(
            username="typed-effect-reverse-officer",
            is_staff=True,
        )
        requester = Requester.objects.create(
            email="typed-effect-reverse@example.invalid",
        )
        request = Request.objects.create(
            number="PRR-MIG-REVERSE",
            requester=requester,
            description="Typed effect reverse guard fixture.",
        )
        event = StatusEvent.objects.create(
            request=request,
            actor=actor,
            from_status="received",
            to_status="acknowledged",
            note="The district acknowledged this request.",
            visible_to_requester=True,
            correspondence_effect="acknowledgment_delivery",
        )

        try:
            with self.assertRaisesMessage(RuntimeError, str(event.pk)):
                self._migrate([self.migrate_from])
        finally:
            StatusEvent.objects.filter(pk=event.pk).update(
                correspondence_effect="",
            )
            self._migrate([self.migrate_from])
            self._migrate([self.migrate_latest])
