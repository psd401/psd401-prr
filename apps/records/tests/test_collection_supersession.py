"""Superseding a collection: an over-broad export leaves the review queue
without deleting a byte of evidence, and a later collection that finds the
same message brings it back."""

import hashlib

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.collection_supersession import supersede_collection
from apps.records.models import (
    CollectionSupersession,
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    StatusEvent,
)
from apps.records.production import _production_plan
from apps.records.tests.test_review_throughput import (
    _ObjectClient,
    _VaultIngestFixture,
    build_message,
    mbox,
)

REASON = "The first scope swept in district mass mail; re-scoping."


def _user(username, role):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [role])
    return user


class _SupersessionFixture:
    def setUp(self):
        self.officer = _user("supersede-officer", StaffRole.RECORDS_OFFICER)
        self.reviewer = _user("supersede-reviewer", StaffRole.REVIEWER)
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Supersede an over-broad export.",
        )
        self.task = self._vault_task("export-broad")
        self.other_task = self._vault_task("export-other")

    def _vault_task(self, export_id):
        return CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.COMPLETE,
            instructions="Ingest the export.",
            vault_matter_id="matter-1",
            vault_export_id=export_id,
        )

    def _item(self, index, *, task=None, state=RecordItem.ReviewState.UNREVIEWED):
        return RecordItem.objects.create(
            request=self.prr,
            collection_task=task or self.task,
            original_filename=f"Message {index}.pdf",
            storage_key=f"records/{self.prr.pk}/supersede-{index}.pdf",
            content_length=10,
            content_type="application/pdf",
            sha256=hashlib.sha256(f"supersede-{index}".encode()).hexdigest(),
            review_state=state,
        )


class SupersedeCollectionServiceTests(_SupersessionFixture, TestCase):
    def test_only_unreviewed_records_of_that_task_are_superseded(self):
        unreviewed = [self._item(index) for index in range(3)]
        responsive = self._item(10, state=RecordItem.ReviewState.RESPONSIVE)
        elsewhere = self._item(20, task=self.other_task)

        result = supersede_collection(
            self.task.pk, actor=self.officer, reason=REASON,
        )

        self.assertEqual(result.record_count, 3)
        for item in unreviewed:
            item.refresh_from_db()
            self.assertEqual(item.review_state, RecordItem.ReviewState.SUPERSEDED)
            self.assertEqual(item.reviewed_by, self.officer)
            self.assertIsNotNone(item.reviewed_at)
        responsive.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertEqual(responsive.review_state, RecordItem.ReviewState.RESPONSIVE)
        self.assertEqual(elsewhere.review_state, RecordItem.ReviewState.UNREVIEWED)

    def test_the_supersession_is_recorded_as_append_only_evidence(self):
        items = [self._item(index) for index in range(2)]

        supersede_collection(self.task.pk, actor=self.officer, reason=REASON)

        row = CollectionSupersession.objects.get()
        self.assertEqual(row.collection_task, self.task)
        self.assertEqual(row.actor, self.officer)
        self.assertEqual(row.reason, REASON)
        self.assertEqual(row.record_count, 2)
        with self.assertRaises(ValidationError):
            CollectionSupersession.objects.filter(pk=row.pk).update(record_count=9)

        reviewed = AuditLog.objects.filter(action="record.reviewed")
        self.assertEqual(
            {entry.target for entry in reviewed},
            {str(item.pk) for item in items},
        )
        for entry in reviewed:
            self.assertEqual(entry.detail["before"]["state"], "unreviewed")
            self.assertEqual(entry.detail["review_state"], "superseded")
        summary = AuditLog.objects.get(action="collection.superseded")
        self.assertEqual(summary.actor, self.officer)
        self.assertEqual(summary.detail["record_count"], 2)
        self.assertEqual(summary.detail["collection_task_id"], self.task.pk)
        event = StatusEvent.objects.get(request=self.prr)
        self.assertIn("superseded", event.note)
        self.assertFalse(event.visible_to_requester)

    def test_a_second_supersession_changes_nothing(self):
        self._item(1)
        supersede_collection(self.task.pk, actor=self.officer, reason=REASON)

        result = supersede_collection(
            self.task.pk, actor=self.officer, reason=REASON,
        )

        self.assertEqual(result.record_count, 0)
        self.assertEqual(CollectionSupersession.objects.count(), 1)
        self.assertEqual(
            AuditLog.objects.filter(action="collection.superseded").count(), 1,
        )

    def test_a_substantive_reason_is_required(self):
        self._item(1)
        with self.assertRaises(ValidationError):
            supersede_collection(self.task.pk, actor=self.officer, reason="too broad")
        self.assertFalse(CollectionSupersession.objects.exists())

    def test_only_a_completed_vault_collection_can_be_superseded(self):
        ingesting = self._vault_task("export-ingesting")
        CollectionTask.objects.filter(pk=ingesting.pk).update(
            state=CollectionTask.State.INGESTING,
        )
        manual = CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            state=CollectionTask.State.COMPLETE,
            instructions="Search the department share.",
        )
        for index, task in enumerate((ingesting, manual)):
            item = self._item(100 + index, task=task)
            with self.assertRaises(ValidationError):
                supersede_collection(task.pk, actor=self.officer, reason=REASON)
            item.refresh_from_db()
            self.assertEqual(item.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertFalse(CollectionSupersession.objects.exists())

    def test_only_a_records_officer_may_supersede(self):
        item = self._item(1)
        with self.assertRaises(ValidationError):
            supersede_collection(self.task.pk, actor=self.reviewer, reason=REASON)
        item.refresh_from_db()
        self.assertEqual(item.review_state, RecordItem.ReviewState.UNREVIEWED)

    def test_a_superseded_record_cannot_be_released(self):
        from apps.records.models import Installment

        item = self._item(1)
        supersede_collection(self.task.pk, actor=self.officer, reason=REASON)
        installment = Installment.objects.create(request=self.prr, sequence=1)
        installment.items.add(item)

        with self.assertRaises(ValidationError):
            _production_plan(installment)

    def test_bulk_review_refuses_a_superseded_record(self):
        from apps.records.review import bulk_review_records

        item = self._item(1)
        supersede_collection(self.task.pk, actor=self.officer, reason=REASON)

        with self.assertRaisesMessage(ValidationError, "superseded"):
            bulk_review_records(
                [item.pk],
                state=RecordItem.ReviewState.NON_RESPONSIVE,
                actor=self.reviewer,
            )

    def test_a_reviewer_can_still_decide_a_superseded_record(self):
        from apps.records.review import review_record

        item = self._item(1)
        supersede_collection(self.task.pk, actor=self.officer, reason=REASON)

        review_record(
            item.pk,
            state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.reviewer,
        )

        item.refresh_from_db()
        self.assertEqual(item.review_state, RecordItem.ReviewState.RESPONSIVE)


class SupersedeCollectionApiTests(_SupersessionFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()

    def _post(self, user, reason=REASON):
        self.api.force_authenticate(user)
        return self.api.post(
            f"/api/staff/collection-tasks/{self.task.pk}/supersede/",
            {"reason": reason},
            format="json",
        )

    def test_an_officer_supersedes_and_sees_the_count(self):
        self._item(1)
        self._item(2)

        response = self._post(self.officer)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["record_count"], 2)

    def test_a_reviewer_is_refused(self):
        self._item(1)
        response = self._post(self.reviewer)
        self.assertEqual(response.status_code, 403)

    def test_an_unfinished_collection_is_a_validation_error(self):
        CollectionTask.objects.filter(pk=self.task.pk).update(
            state=CollectionTask.State.INGESTING,
        )
        self._item(1)
        response = self._post(self.officer)
        self.assertEqual(response.status_code, 400, response.content)

    def test_a_short_reason_is_a_validation_error(self):
        self._item(1)
        response = self._post(self.officer, reason="broad")
        self.assertEqual(response.status_code, 400)

    def test_the_record_list_hides_superseded_records_unless_asked(self):
        kept = self._item(1, task=self.other_task)
        self._item(2)
        self._post(self.officer)
        self.api.force_authenticate(self.officer)

        default = self.api.get(
            "/api/staff/record-items/", {"request": self.prr.number},
        ).json()
        superseded = self.api.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "review_state": "superseded"},
        ).json()
        facets = self.api.get(
            "/api/staff/record-items/facets/", {"request": self.prr.number},
        ).json()

        self.assertEqual([row["id"] for row in default["results"]], [str(kept.pk)])
        self.assertEqual(superseded["count"], 1)
        self.assertEqual(facets["total"], 1)

    def test_a_superseded_record_is_still_retrievable(self):
        item = self._item(1)
        self._post(self.officer)
        self.api.force_authenticate(self.officer)

        response = self.api.get(f"/api/staff/record-items/{item.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review_state"], "superseded")


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PRR_VAULT_EXPORT_MAX_BYTES=10 * 1024 * 1024,
    PRR_VAULT_EXPORT_TOTAL_MAX_BYTES=20 * 1024 * 1024,
    PRR_VAULT_ARCHIVE_MAX_MEMBERS=100,
    PRR_VAULT_ARCHIVE_EXPANDED_MAX_BYTES=20 * 1024 * 1024,
)
class RecollectionRestoresSupersededRecordsTests(_VaultIngestFixture, TestCase):
    """A narrower re-collection that finds a superseded message must put it
    back in front of a reviewer rather than folding it, unseen, into a record
    nobody will look at again."""

    def setUp(self):
        self.officer = _user("recollect-officer", StaffRole.RECORDS_OFFICER)
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Re-scope a Vault collection.",
        )
        self.client_stub = _ObjectClient()

    def test_a_recollected_message_returns_to_review(self):
        message = build_message(message_id="<kept@psd401.net>")
        broad = self._task(["alice@psd401.net"], "export-broad")
        self._ingest(broad, mbox(message), "export-broad")
        supersede_collection(broad.pk, actor=self.officer, reason=REASON)

        narrow = self._task(["alice@psd401.net"], "export-narrow")
        self._ingest(narrow, mbox(message), "export-narrow")

        item = RecordItem.objects.get(request=self.prr)
        self.assertEqual(item.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertIsNone(item.reviewed_by)
        self.assertEqual(item.collection_task, narrow)
        restored = AuditLog.objects.get(action="record.restored_by_collection")
        self.assertEqual(restored.target, str(item.pk))
        self.assertEqual(restored.detail["collection_task_id"], narrow.pk)
