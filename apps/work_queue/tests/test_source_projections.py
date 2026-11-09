# ABOUTME: Source-to-work projection tests for request, collection, draft, and review queues.
# ABOUTME: Source facts close or reopen work without changing legal lifecycle state.
import datetime as dt
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.correspondence.models import Correspondence
from apps.correspondence.services import (
    approve_draft,
    create_authored_draft,
    delete_draft,
)
from apps.records.intake import submit_intake
from apps.records.models import (
    Channel,
    CollectionTask,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
)
from apps.records.review import (
    approve_second_review,
    create_redaction,
    review_record,
    void_redaction,
)
from apps.records.services import transition
from apps.work_queue.integration import schedule_source_work_item_reconciliation
from apps.work_queue.models import WorkItem, WorkItemEvent
from apps.work_queue.services import assign_work_item
from apps.work_queue.source_reconciliation import (
    reconcile_collection_task_work,
    reconcile_correspondence_draft_work,
    reconcile_record_item_work,
    reconcile_request_work,
    reconcile_source_work_items,
)


@override_settings(
    PRR_LOCAL_IMPLICIT_STAFF_ROLES=(),
    PRR_TEST_EAGER_SOURCE_WORK_RECONCILIATION=True,
)
class SourceProjectionTests(TestCase):
    def setUp(self):
        self.officer = self.make_staff("projection-officer", StaffRole.RECORDS_OFFICER)
        self.second_officer = self.make_staff(
            "projection-second-officer",
            StaffRole.RECORDS_OFFICER,
        )
        self.liaison = self.make_staff("projection-liaison", StaffRole.LIAISON)
        self.second_liaison = self.make_staff(
            "projection-second-liaison",
            StaffRole.LIAISON,
        )
        self.reviewer = self.make_staff("projection-reviewer", StaffRole.REVIEWER)
        self.second_reviewer = self.make_staff(
            "projection-second-reviewer",
            StaffRole.REVIEWER,
        )
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="source@example.org"),
            description="Search email and review responsive records.",
            assigned_officer=self.officer,
            statutory_response_due=timezone.localdate() + dt.timedelta(days=2),
        )

    @staticmethod
    def make_staff(username, role):
        user = get_user_model().objects.create_user(
            username,
            f"{username}@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(user, {role})
        return user

    def test_request_collection_and_draft_sources_create_reopen_and_close_work(self):
        request_work = reconcile_request_work(self.request)
        self.assertEqual(request_work.work_kind, WorkItem.WorkKind.REQUEST_ACTION)
        self.assertEqual(request_work.assignee, self.officer)
        request_work = assign_work_item(
            request_work,
            assignee=self.second_officer,
            actor=self.officer,
            expected_version=request_work.version,
            idempotency_key="assign-request-next-action",
        ).work_item
        reconcile_request_work(self.request)
        request_work.refresh_from_db()
        self.assertEqual(request_work.assignee, self.second_officer)

        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            assigned_to=self.liaison,
            due_date=timezone.localdate() + dt.timedelta(days=1),
        )
        collection_work = reconcile_collection_task_work(task)
        self.assertEqual(collection_work.assignee, self.liaison)
        self.assertEqual(collection_work.disposition, WorkItem.Disposition.NEEDS_ATTENTION)
        collection_work = assign_work_item(
            collection_work,
            assignee=self.officer,
            actor=self.officer,
            expected_version=collection_work.version,
            idempotency_key="assign-collection-next-action",
        ).work_item
        reconcile_collection_task_work(task)
        collection_work.refresh_from_db()
        self.assertEqual(collection_work.assignee, self.officer)

        task.state = CollectionTask.State.COMPLETE
        task.save(update_fields=["state", "updated_at"])
        collection_work = reconcile_collection_task_work(task)
        self.assertEqual(collection_work.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

        task.state = CollectionTask.State.PENDING
        task.save(update_fields=["state", "updated_at"])
        collection_work = reconcile_collection_task_work(task)
        self.assertEqual(collection_work.disposition, WorkItem.Disposition.NEEDS_ATTENTION)

        draft = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Draft",
            draft_body="Draft body",
            created_by=self.liaison,
        )
        draft_work = reconcile_correspondence_draft_work(draft)
        self.assertEqual(draft_work.assignee, self.liaison)
        draft.status = Correspondence.Status.APPROVED
        draft.approved_subject = draft.draft_subject
        draft.approved_body = draft.draft_body
        draft.approved_metadata = {}
        draft.approved_by = self.officer
        draft.approved_at = timezone.now()
        draft.save(
            update_fields=[
                "status",
                "approved_subject",
                "approved_body",
                "approved_metadata",
                "approved_by",
                "approved_at",
            ]
        )
        draft_work = reconcile_correspondence_draft_work(draft)
        self.assertEqual(draft_work.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

        automated = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.SUBMISSION_RECEIPT,
            status=Correspondence.Status.DRAFT,
            draft_subject="Receipt",
            draft_body="Receipt body",
            automated=True,
        )
        self.assertIsNone(reconcile_correspondence_draft_work(automated))

    def test_record_source_moves_from_first_review_to_distinct_second_review(self):
        item = RecordItem.objects.create(
            request=self.request,
            source_system="gmail",
            original_filename="record.pdf",
            storage_key="records/source.pdf",
            sha256="a" * 64,
            content_length=20,
            content_type="application/pdf",
        )
        first_review = reconcile_record_item_work(item)
        self.assertEqual(first_review.work_kind, WorkItem.WorkKind.RECORD_REVIEW)
        self.assertEqual(first_review.disposition, WorkItem.Disposition.NEEDS_ATTENTION)
        assigned = assign_work_item(
            first_review,
            assignee=self.reviewer,
            actor=self.officer,
            expected_version=first_review.version,
            idempotency_key="assign-source-reviewer",
        ).work_item
        reconcile_record_item_work(item)
        assigned.refresh_from_db()
        self.assertEqual(assigned.assignee, self.reviewer)

        citation = ExemptionCitation.objects.create(
            citation="RCW 42.56.230",
            short_name="Protected record",
            active=True,
            requires_second_review=True,
        )
        item.review_state = RecordItem.ReviewState.WITHHELD
        item.reviewed_by = self.reviewer
        item.reviewed_at = timezone.now()
        item.withholding_exemption = citation
        item.save(
            update_fields=[
                "review_state",
                "reviewed_by",
                "reviewed_at",
                "withholding_exemption",
            ]
        )

        second_review = reconcile_record_item_work(item)
        first_review.refresh_from_db()
        self.assertEqual(first_review.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)
        self.assertEqual(second_review.work_kind, WorkItem.WorkKind.SECOND_REVIEW)

        item.second_reviewed_by = self.officer
        item.second_reviewed_at = timezone.now()
        item.save(update_fields=["second_reviewed_by", "second_reviewed_at"])
        second_review = reconcile_record_item_work(item)
        self.assertEqual(second_review.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

    def test_bounded_backfill_repairs_existing_sources_idempotently(self):
        task = CollectionTask.objects.create(
            request=self.request,
            kind=CollectionTask.Kind.MANUAL,
            assigned_to=self.liaison,
        )
        draft = Correspondence.objects.create(
            request=self.request,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.DRAFT,
            draft_subject="Existing draft",
            draft_body="Existing draft body",
            created_by=self.liaison,
        )
        item = RecordItem.objects.create(
            request=self.request,
            storage_key="records/existing.pdf",
            sha256="b" * 64,
        )

        for source in ("requests", "collections", "drafts", "records"):
            first = reconcile_source_work_items(source=source, limit=10)
            second = reconcile_source_work_items(source=source, limit=10)
            self.assertEqual(first.failed, 0)
            self.assertEqual(second.failed, 0)
            self.assertIsNone(first.next_cursor)

        self.assertTrue(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.REQUEST,
                source_id=str(self.request.pk),
            ).exists()
        )
        self.assertIsNone(
            WorkItem.objects.get(
                source_type=WorkItem.SourceType.REQUEST,
                source_id=str(self.request.pk),
            ).assignee
        )
        self.assertIsNone(
            WorkItem.objects.get(
                source_type=WorkItem.SourceType.COLLECTION_TASK,
                source_id=str(task.pk),
            ).assignee
        )
        self.assertIsNone(
            WorkItem.objects.get(
                source_type=WorkItem.SourceType.CORRESPONDENCE,
                source_id=str(draft.pk),
            ).assignee
        )
        self.assertTrue(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.COLLECTION_TASK,
                source_id=str(task.pk),
            ).exists()
        )
        self.assertTrue(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.CORRESPONDENCE,
                source_id=str(draft.pk),
            ).exists()
        )
        self.assertTrue(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.RECORD_ITEM,
                source_id=str(item.pk),
            ).exists()
        )

    def test_intake_draft_collection_and_review_workflows_keep_projections_current(self):
        with self.captureOnCommitCallbacks(execute=True):
            with (
                patch("apps.records.intake.compute_statutory_deadline.delay"),
                patch("apps.records.intake.triage_footage"),
                patch("apps.records.intake.route_to_systems", return_value=[]),
                patch("apps.records.intake.persist_intake_attachments"),
            ):
                submitted = submit_intake(
                    {
                        "email": "new-source@example.org",
                        "description": "A newly submitted source request.",
                    },
                    channel=Channel.MAIL,
                    actor=self.officer,
                )
        self.assertTrue(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.REQUEST,
                source_id=str(submitted.pk),
            ).exists()
        )

        with self.captureOnCommitCallbacks(execute=True):
            draft = create_authored_draft(
                self.request,
                kind=Correspondence.Kind.GENERAL,
                subject="Human draft",
                body="Human-authored correspondence.",
                actor=self.liaison,
            )
        draft_work = WorkItem.objects.get(
            source_type=WorkItem.SourceType.CORRESPONDENCE,
            source_id=str(draft.pk),
        )
        self.assertEqual(draft_work.assignee, self.liaison)
        with self.captureOnCommitCallbacks(execute=True):
            approve_draft(draft, actor=self.officer, expected_version=1)
        draft_work.refresh_from_db()
        self.assertEqual(draft_work.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

        with self.captureOnCommitCallbacks(execute=True):
            discarded = create_authored_draft(
                self.request,
                kind=Correspondence.Kind.GENERAL,
                subject="Mistaken draft",
                body="Drafted before the requester was corrected.",
                actor=self.liaison,
            )
        discarded_work = WorkItem.objects.get(
            source_type=WorkItem.SourceType.CORRESPONDENCE,
            source_id=str(discarded.pk),
        )
        with self.captureOnCommitCallbacks(execute=True):
            delete_draft(discarded, actor=self.liaison, expected_version=1)
        discarded_work.refresh_from_db()
        self.assertEqual(
            discarded_work.disposition,
            WorkItem.Disposition.SOURCE_EXCLUDED,
        )

        self.client.force_login(self.officer)
        with self.captureOnCommitCallbacks(execute=True):
            created_task = self.client.post(
                "/api/staff/collection-tasks/",
                {
                    "request": self.request.pk,
                    "kind": CollectionTask.Kind.MANUAL,
                    "instructions": "Search the assigned department system.",
                    "assigned_to_id": self.liaison.pk,
                },
                content_type="application/json",
            )
        self.assertEqual(created_task.status_code, 201, created_task.content)
        task_work = WorkItem.objects.get(
            source_type=WorkItem.SourceType.COLLECTION_TASK,
            source_id=str(created_task.json()["id"]),
        )
        self.assertEqual(task_work.assignee, self.liaison)
        with self.captureOnCommitCallbacks(execute=True):
            reassigned = self.client.patch(
                f"/api/staff/collection-tasks/{created_task.json()['id']}/",
                {"assigned_to_id": self.second_liaison.pk},
                content_type="application/json",
            )
        self.assertEqual(reassigned.status_code, 200, reassigned.content)
        task_work.refresh_from_db()
        self.assertEqual(task_work.assignee, self.second_liaison)
        self.assertTrue(
            task_work.events.filter(
                event_type=WorkItemEvent.EventType.REASSIGNED,
                actor=self.officer,
                metadata__after_assignee_id=self.second_liaison.pk,
            ).exists()
        )
        with self.captureOnCommitCallbacks(execute=True):
            completed = self.client.patch(
                f"/api/staff/collection-tasks/{created_task.json()['id']}/",
                {"state": CollectionTask.State.COMPLETE},
                content_type="application/json",
            )
        self.assertEqual(completed.status_code, 200, completed.content)
        task_work.refresh_from_db()
        self.assertEqual(task_work.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)
        with self.captureOnCommitCallbacks(execute=True):
            reassigned_while_complete = self.client.patch(
                f"/api/staff/collection-tasks/{created_task.json()['id']}/",
                {"assigned_to_id": self.liaison.pk},
                content_type="application/json",
            )
        self.assertEqual(
            reassigned_while_complete.status_code,
            200,
            reassigned_while_complete.content,
        )
        task_work.refresh_from_db()
        self.assertEqual(task_work.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)
        self.assertEqual(task_work.assignee, self.liaison)
        with self.captureOnCommitCallbacks(execute=True):
            reopened = self.client.patch(
                f"/api/staff/collection-tasks/{created_task.json()['id']}/",
                {"state": CollectionTask.State.PENDING},
                content_type="application/json",
            )
        self.assertEqual(reopened.status_code, 200, reopened.content)
        task_work.refresh_from_db()
        self.assertEqual(task_work.disposition, WorkItem.Disposition.NEEDS_ATTENTION)
        self.assertEqual(task_work.assignee, self.liaison)

        record = RecordItem.objects.create(
            request=self.request,
            storage_key="records/workflow.pdf",
            sha256="c" * 64,
        )
        record_work = reconcile_record_item_work(record)
        with self.captureOnCommitCallbacks(execute=True):
            review_record(
                record.pk,
                state=RecordItem.ReviewState.NON_RESPONSIVE,
                actor=self.reviewer,
            )
        record_work.refresh_from_db()
        self.assertEqual(record_work.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

    def test_source_hooks_enqueue_after_commit_without_masking_intake(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_source_work_item_reconciliation("requests", self.request.pk)
            self.assertFalse(
                WorkItem.objects.filter(
                    source_type=WorkItem.SourceType.REQUEST,
                    source_id=str(self.request.pk),
                ).exists()
            )
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertTrue(
            WorkItem.objects.filter(
                source_type=WorkItem.SourceType.REQUEST,
                source_id=str(self.request.pk),
            ).exists()
        )

        with (
            patch("apps.records.intake.compute_statutory_deadline.delay"),
            patch("apps.records.intake.triage_footage"),
            patch("apps.records.intake.route_to_systems", return_value=[]),
            patch("apps.records.intake.persist_intake_attachments"),
            patch("apps.correspondence.services.queue_submission_receipt"),
            patch(
                "apps.work_queue.tasks.reconcile_case_work_item_source.delay",
                side_effect=RuntimeError("queue unavailable"),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            submitted = submit_intake(
                {
                    "email": "durable-source@example.org",
                    "description": "Evidence survives projection queue failure.",
                },
                channel=Channel.EMAIL,
                actor=self.officer,
            )
        self.assertTrue(
            PublicRecordsRequest.objects.filter(
                pk=submitted.pk,
                requester__email="durable-source@example.org",
            ).exists()
        )

    def test_transition_and_redaction_hooks_project_current_work(self):
        request_work = reconcile_request_work(self.request)
        with self.captureOnCommitCallbacks(execute=True):
            transition(
                self.request,
                RequestStatus.GATHERING,
                actor=self.officer,
                note="Begin gathering records.",
            )
        request_work.refresh_from_db()
        self.assertEqual(request_work.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

        item = RecordItem.objects.create(
            request=self.request,
            storage_key="records/redaction-workflow.pdf",
            sha256="d" * 64,
        )
        first_review = reconcile_record_item_work(item)
        with self.captureOnCommitCallbacks(execute=True):
            review_record(
                item.pk,
                state=RecordItem.ReviewState.RESPONSIVE,
                actor=self.reviewer,
            )
        first_review.refresh_from_db()
        self.assertEqual(first_review.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

        citation = ExemptionCitation.objects.create(
            citation="20 U.S.C. § 1232g",
            short_name="FERPA",
            active=True,
            requires_second_review=True,
        )
        document = type(
            "DocumentStub",
            (),
            {"page_count": 1, "close": lambda self: None},
        )()
        with (
            patch("apps.records.review._open_verified_pdf", return_value=document),
            self.captureOnCommitCallbacks(execute=True),
        ):
            redaction = create_redaction(
                item.pk,
                actor=self.reviewer,
                page_number=1,
                x0=0.1,
                y0=0.1,
                x1=0.2,
                y1=0.2,
                exemption_id=citation.pk,
            )
        second_review = WorkItem.objects.get(
            source_type=WorkItem.SourceType.RECORD_ITEM,
            source_id=str(item.pk),
            work_kind=WorkItem.WorkKind.SECOND_REVIEW,
        )
        self.assertEqual(second_review.disposition, WorkItem.Disposition.NEEDS_ATTENTION)

        with self.captureOnCommitCallbacks(execute=True):
            void_redaction(
                redaction.pk,
                actor=self.reviewer,
                reason="This redaction was placed on the wrong content.",
            )
        second_review.refresh_from_db()
        self.assertEqual(second_review.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)

        with (
            patch("apps.records.review._open_verified_pdf", return_value=document),
            self.captureOnCommitCallbacks(execute=True),
        ):
            create_redaction(
                item.pk,
                actor=self.reviewer,
                page_number=1,
                x0=0.3,
                y0=0.3,
                x1=0.4,
                y1=0.4,
                exemption_id=citation.pk,
            )
        second_review.refresh_from_db()
        self.assertEqual(second_review.disposition, WorkItem.Disposition.NEEDS_ATTENTION)

        with self.captureOnCommitCallbacks(execute=True):
            approve_second_review(item.pk, actor=self.second_reviewer)
        second_review.refresh_from_db()
        self.assertEqual(second_review.disposition, WorkItem.Disposition.SOURCE_EXCLUDED)
