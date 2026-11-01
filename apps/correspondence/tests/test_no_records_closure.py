"""No-records/out-of-retention closure requires a reasonable-search record."""

import datetime as dt

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.correspondence import services
from apps.correspondence.models import Correspondence
from apps.correspondence.tests.test_outbound_delivery import (
    OutboundDeliveryServiceTestCase,
)
from apps.records import services as records_services
from apps.records.models import (
    AdequateSearchAttestation,
    CollectionTask,
    FootagePreservation,
    RecordItem,
    RecordSystem,
    RequestStatus,
)

SEARCH_STEPS = [
    {
        "location": "Google Workspace",
        "custodian": "Facilities Director",
        "method": "Vault search for transportation plan terms and date range",
        "date_range": "2025-01-01 through 2025-12-31",
        "result": "No responsive records located",
    }
]


class NoRecordsClosureTests(OutboundDeliveryServiceTestCase):
    def setUp(self):
        super().setUp()
        system = RecordSystem.objects.create(
            slug="google-workspace-test",
            name="Google Workspace",
            active=True,
        )
        self.task = CollectionTask.objects.create(
            request=self.prr,
            system=system,
            kind=CollectionTask.Kind.MANUAL,
            state=CollectionTask.State.COMPLETE,
            completed_at=timezone.now(),
        )

    def create_draft(self, **overrides):
        values = {
            "determination": AdequateSearchAttestation.Determination.NO_RESPONSIVE_RECORDS,
            "search_completed_at": timezone.now(),
            "search_steps": SEARCH_STEPS,
            "adequacy_reason": (
                "All locations reasonably likely to contain the requested records "
                "were searched and obvious leads were followed."
            ),
            "retention_schedule_reference": "",
            "incident_hold_checked": False,
            "prior_exports_checked": False,
            "email_drive_copies_checked": False,
            "other_copies_checked": False,
            "counsel_approval_reference": "Counsel review memo PRA-2026-18",
            "actor": self.officer,
        }
        values.update(overrides)
        return services.create_no_records_closure_draft(self.prr, **values)

    def test_creation_requires_officer_completed_tasks_counsel_and_detailed_steps(self):
        with self.assertRaises(PermissionDenied):
            self.create_draft(actor=self.integration)
        with self.assertRaisesMessage(ValidationError, "counsel approval"):
            self.create_draft(counsel_approval_reference="")
        with self.assertRaisesMessage(ValidationError, "search step"):
            self.create_draft(search_steps=[])

        self.task.state = CollectionTask.State.RUNNING
        self.task.completed_at = None
        self.task.save(update_fields=["state", "completed_at"])
        with self.assertRaisesMessage(ValidationError, "collection tasks"):
            self.create_draft()

    def test_task_lock_query_does_not_join_nullable_record_system(self):
        """PostgreSQL rejects FOR UPDATE on the nullable side of a join."""
        with CaptureQueriesContext(connection) as queries:
            self.create_draft()

        lock_query = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "records_collectiontask"' in query["sql"]
        )
        self.assertNotIn(
            'LEFT OUTER JOIN "records_recordsystem"',
            lock_query,
        )

    def test_out_of_retention_requires_expired_preservation_and_all_obvious_leads(self):
        with self.assertRaisesMessage(ValidationError, "expired preservation"):
            self.create_draft(
                determination=AdequateSearchAttestation.Determination.OUTSIDE_RETENTION,
                retention_schedule_reference="CORE GS 50-02-05, verified 2026-07-31",
                incident_hold_checked=True,
                prior_exports_checked=True,
                email_drive_copies_checked=True,
                other_copies_checked=True,
            )

        FootagePreservation.objects.create(
            request=self.prr,
            state=FootagePreservation.State.EXPIRED,
            window_start=timezone.now() - dt.timedelta(days=50),
            window_end=timezone.now() - dt.timedelta(days=40),
        )
        with self.assertRaisesMessage(ValidationError, "obvious-lead checks"):
            self.create_draft(
                determination=AdequateSearchAttestation.Determination.OUTSIDE_RETENTION,
                retention_schedule_reference="CORE GS 50-02-05, verified 2026-07-31",
                incident_hold_checked=False,
                prior_exports_checked=True,
                email_drive_copies_checked=True,
                other_copies_checked=True,
            )

        draft = self.create_draft(
            determination=AdequateSearchAttestation.Determination.OUTSIDE_RETENTION,
            retention_schedule_reference="CORE GS 50-02-05, verified 2026-07-31",
            incident_hold_checked=True,
            prior_exports_checked=True,
            email_drive_copies_checked=True,
            other_copies_checked=True,
        )
        attestation = draft.adequate_search_attestation
        self.assertEqual(
            attestation.determination,
            AdequateSearchAttestation.Determination.OUTSIDE_RETENTION,
        )
        self.assertEqual(attestation.attested_by, self.officer)
        self.assertEqual(len(attestation.task_snapshot_sha256), 64)

    def test_confirmed_delivery_closes_as_no_records_not_denied_and_is_idempotent(self):
        draft = self.create_draft()
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        claim = self.claim(delivery)
        callback = {
            "provider_message_id": f"gmail-no-records-{delivery.id}",
            "provider_thread_id": "no-records-thread",
            "provider_timestamp": self.now + dt.timedelta(seconds=5),
            "provider_metadata": {"http_status": 200},
        }

        first = self.report(delivery, claim, **callback)
        second = self.report(delivery, claim, **callback)

        self.prr.refresh_from_db()
        approved.refresh_from_db()
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(self.prr.status, RequestStatus.CLOSED_NO_RECORDS)
        self.assertNotEqual(self.prr.status, RequestStatus.DENIED)
        self.assertEqual(self.prr.closed_at, callback["provider_timestamp"])
        self.assertEqual(
            approved.resulting_status_event.to_status,
            RequestStatus.CLOSED_NO_RECORDS,
        )
        self.assertTrue(AuditLog.objects.filter(
            action="request.closed_no_records",
            target=self.prr.number,
            actor=self.officer,
            detail__attestation_id=str(draft.adequate_search_attestation.pk),
        ).exists())

    def test_a_closure_approved_under_the_prior_template_still_delivers(self):
        # A notice approved before the v2 template shipped is frozen content a
        # Records Officer already signed off on. The version bump must not
        # strand it in the delivery queue.
        draft = self.create_draft()
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        delivery = services.queue_approved(
            approved,
            actor=self.officer,
            expected_version=approved.version,
        )
        models.QuerySet(model=Correspondence, using="default").filter(
            pk=approved.pk
        ).update(template_version="1")

        claim = self.claim(delivery)
        self.report(
            delivery,
            claim,
            provider_message_id=f"gmail-no-records-v1-{delivery.id}",
            provider_thread_id="no-records-v1-thread",
            provider_timestamp=self.now + dt.timedelta(seconds=5),
            provider_metadata={"http_status": 200},
        )

        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.CLOSED_NO_RECORDS)

    def test_a_draft_under_the_prior_template_must_be_redrafted(self):
        draft = self.create_draft()
        models.QuerySet(model=Correspondence, using="default").filter(
            pk=draft.pk
        ).update(template_version="1")
        draft.refresh_from_db()

        with self.assertRaisesMessage(ValidationError, "template identity"):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

    def test_approval_revalidates_task_snapshot_and_attestation_is_append_only(self):
        draft = self.create_draft()
        extra = CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            state=CollectionTask.State.COMPLETE,
            completed_at=timezone.now(),
        )
        with self.assertRaisesMessage(ValidationError, "search snapshot"):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

        attestation = draft.adequate_search_attestation
        attestation.adequacy_reason = "Rewritten evidence is forbidden."
        with self.assertRaises(ValidationError):
            attestation.save()
        extra.delete()

    def test_unreviewed_or_responsive_items_block_no_records_closure(self):
        item = RecordItem.objects.create(
            request=self.prr,
            storage_key=f"records/{self.prr.pk}/candidate.pdf",
            sha256="a" * 64,
            content_length=1,
            content_type="application/pdf",
        )
        with self.assertRaisesMessage(ValidationError, "candidate records"):
            self.create_draft()

        item.review_state = RecordItem.ReviewState.RESPONSIVE
        item.reviewed_by = self.officer
        item.reviewed_at = timezone.now()
        item.save(update_fields=["review_state", "reviewed_by", "reviewed_at"])
        with self.assertRaisesMessage(ValidationError, "responsive records"):
            self.create_draft()

    def test_approval_revalidates_candidate_record_snapshot(self):
        draft = self.create_draft()
        RecordItem.objects.create(
            request=self.prr,
            storage_key=f"records/{self.prr.pk}/nonresponsive.pdf",
            sha256="b" * 64,
            content_length=1,
            content_type="application/pdf",
            review_state=RecordItem.ReviewState.NON_RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )

        with self.assertRaisesMessage(ValidationError, "search snapshot"):
            services.approve_draft(
                draft,
                actor=self.officer,
                expected_version=draft.version,
            )

    def test_changed_search_can_be_re_attested_without_rewriting_old_evidence(self):
        first = self.create_draft()
        CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            state=CollectionTask.State.COMPLETE,
            completed_at=timezone.now(),
            instructions="Followed a newly identified obvious lead.",
        )

        replacement = self.create_draft(
            search_steps=[*SEARCH_STEPS, {"location": "Facilities shared drive", "custodian": "Transportation Manager", "method": "Folder and filename review for the identified lead", "date_range": "2025-01-01 through 2025-12-31", "result": "Lead followed; no responsive records located"}],
            adequacy_reason=(
                "The original likely locations and the newly identified shared "
                "drive lead were searched to completion."
            ),
        )

        self.assertEqual(self.prr.adequate_search_attestations.count(), 2)
        self.assertNotEqual(
            first.adequate_search_attestation.task_snapshot_sha256,
            replacement.adequate_search_attestation.task_snapshot_sha256,
        )
        approved = services.approve_draft(
            replacement,
            actor=self.officer,
            expected_version=replacement.version,
        )
        self.assertEqual(approved.status, approved.Status.APPROVED)

    def test_database_rejects_invalid_or_incomplete_search_evidence(self):
        def create_invalid(**overrides):
            correspondence = services.create_authored_draft(
                self.prr,
                kind=Correspondence.Kind.GENERAL,
                subject="Evidence constraint fixture",
                body="This draft only supplies a protected relation.",
                actor=self.officer,
            )
            values = {
                "request": self.prr,
                "correspondence": correspondence,
                "determination": "no_responsive_records",
                "search_completed_at": timezone.now(),
                "search_steps": SEARCH_STEPS,
                "task_snapshot": [],
                "task_snapshot_sha256": "a" * 64,
                "adequacy_reason": "Substantive database constraint evidence.",
                "retention_schedule_reference": "",
                "incident_hold_checked": False,
                "prior_exports_checked": False,
                "email_drive_copies_checked": False,
                "other_copies_checked": False,
                "counsel_approval_reference": "Counsel memo PRA-2026-18",
                "attested_by": self.officer,
            }
            values.update(overrides)
            AdequateSearchAttestation.objects.create(**values)

        with self.assertRaises(IntegrityError), transaction.atomic():
            create_invalid(determination="invented_outcome")
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_invalid(
                determination="outside_retention",
                retention_schedule_reference="CORE GS 50-02-05",
            )

    def test_generic_status_transition_cannot_bypass_delivered_search_closure(self):
        with self.assertRaisesMessage(ValueError, "confirmed no-records closure"):
            records_services.transition(
                self.prr,
                RequestStatus.CLOSED_NO_RECORDS,
                actor=self.officer,
                note="Attempted manual no-records closure without delivery.",
            )
        self.prr.refresh_from_db()
        self.assertEqual(self.prr.status, RequestStatus.RECEIVED)

    def test_not_maintained_determination_carries_the_wac_creation_language(self):
        """WAC 44-14-04003(6): no duty to create a record that is not kept."""
        draft = self.create_draft(
            determination=AdequateSearchAttestation.Determination.NOT_MAINTAINED,
            adequacy_reason=(
                "The District keeps no record answering this question; it was "
                "a request for information rather than for an existing record."
            ),
        )

        self.assertEqual(
            draft.adequate_search_attestation.determination,
            AdequateSearchAttestation.Determination.NOT_MAINTAINED,
        )
        self.assertIn("WAC 44-14-04003(6)", draft.draft_body)
        self.assertIn(
            "not required to create a record",
            draft.draft_body,
        )
        self.assertNotIn("outside the verified retention period", draft.draft_body)
        # It is a no-record determination, not an exemption determination.
        self.assertIn(
            "not a determination that responsive records are exempt",
            draft.draft_body,
        )
        approved = services.approve_draft(
            draft,
            actor=self.officer,
            expected_version=draft.version,
        )
        self.assertEqual(approved.status, approved.Status.APPROVED)

    def test_not_maintained_needs_no_retention_evidence_but_still_needs_a_reason(self):
        with self.assertRaisesMessage(ValidationError, "adequate-search reason"):
            self.create_draft(
                determination=AdequateSearchAttestation.Determination.NOT_MAINTAINED,
                adequacy_reason="Too short.",
            )
