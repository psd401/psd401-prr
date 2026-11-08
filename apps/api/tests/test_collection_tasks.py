# ABOUTME: Staff API tests for the collection-task resource — search
# ABOUTME: assignments (manual or Vault-backed) attached to a PRA request.
import hashlib

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.api.identity import StaffRole, apply_staff_roles
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    RecordItem,
    RecordSystem,
    Requester,
    VaultIngestRun,
)


class CollectionTaskApiTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.client.login(username="officer", password="pw")

        self.requester = Requester.objects.create(email="req@example.com")
        self.request_obj = PublicRecordsRequest.objects.create(
            requester=self.requester, description="board meeting minutes")

        self.system = RecordSystem.objects.create(
            slug="bus-video", name="Bus Video (DVR)", urgent_hold=True)

        self.task = CollectionTask.objects.create(
            request=self.request_obj,
            system=self.system,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Pull DVR footage for the requested dates.",
        )
        self.no_system_task = CollectionTask.objects.create(
            request=self.request_obj,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Check the shared drive.",
        )

    # -- staff gate --------------------------------------------------
    def test_anonymous_refused(self):
        self.client.logout()
        r = self.client.get("/api/staff/collection-tasks/")
        self.assertEqual(r.status_code, 403)

    def test_non_staff_refused(self):
        self.client.logout()
        get_user_model().objects.create_user(
            "civilian", "c@example.com", "pw", is_staff=False)
        self.client.login(username="civilian", password="pw")
        r = self.client.get("/api/staff/collection-tasks/")
        self.assertEqual(r.status_code, 403)

    # -- list / null-safe system fields --------------------------------
    def test_list_includes_system_fields(self):
        r = self.client.get("/api/staff/collection-tasks/")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        by_id = {row["id"]: row for row in rows}

        with_system = by_id[self.task.id]
        self.assertEqual(with_system["system_name"], "Bus Video (DVR)")
        self.assertEqual(with_system["system_slug"], "bus-video")
        self.assertTrue(with_system["system_urgent_hold"])
        self.assertEqual(with_system["request_number"], self.request_obj.number)

        without_system = by_id[self.no_system_task.id]
        self.assertIsNone(without_system["system_name"])
        self.assertIsNone(without_system["system_slug"])
        self.assertIsNone(without_system["system_urgent_hold"])

    def _record(self, task, index, state):
        return RecordItem.objects.create(
            request=self.request_obj,
            collection_task=task,
            original_filename=f"Message {index}.pdf",
            storage_key=f"records/{self.request_obj.pk}/card-{index}.pdf",
            content_length=10,
            content_type="application/pdf",
            sha256=hashlib.sha256(f"card-{index}".encode()).hexdigest(),
            review_state=state,
        )

    def test_each_collection_names_its_source_and_counts_its_records(self):
        # The Collect stage card (#45): a Vault task has no record system, so
        # its source is named from its kind, never shown as a dash.
        vault = CollectionTask.objects.create(
            request=self.request_obj, kind=CollectionTask.Kind.VAULT_MAIL,
        )
        self._record(vault, 1, RecordItem.ReviewState.UNREVIEWED)
        self._record(vault, 2, RecordItem.ReviewState.UNREVIEWED)
        self._record(vault, 3, RecordItem.ReviewState.RESPONSIVE)
        self._record(vault, 4, RecordItem.ReviewState.SUPERSEDED)
        r = self.client.get(
            f"/api/staff/collection-tasks/?request={self.request_obj.number}"
        )
        rows = r.json()["results"] if isinstance(r.json(), dict) else r.json()
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[vault.pk]["source_label"], "Google Workspace (Gmail)")
        self.assertEqual(by_id[vault.pk]["records_created"], 4)
        self.assertEqual(by_id[vault.pk]["records_unreviewed"], 2)
        self.assertEqual(by_id[self.task.pk]["source_label"], "Bus Video (DVR)")
        self.assertEqual(by_id[self.task.pk]["records_created"], 0)
        self.assertEqual(by_id[self.no_system_task.pk]["source_label"], "Manual / department")

    def test_records_list_filters_to_one_collection(self):
        vault = CollectionTask.objects.create(
            request=self.request_obj, kind=CollectionTask.Kind.VAULT_MAIL,
        )
        mine = self._record(vault, 1, RecordItem.ReviewState.UNREVIEWED)
        self._record(self.task, 2, RecordItem.ReviewState.UNREVIEWED)
        r = self.client.get(f"/api/staff/record-items/?collection_task={vault.pk}")
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.json()["results"] if isinstance(r.json(), dict) else r.json()
        self.assertEqual([row["id"] for row in rows], [str(mine.pk)])
        bad = self.client.get("/api/staff/record-items/?collection_task=abc")
        self.assertEqual(bad.status_code, 400)

    def test_assigned_to_null_safe(self):
        r = self.client.get(f"/api/staff/collection-tasks/{self.task.id}/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["assigned_to_username"])
        self.assertIsNone(body["assigned_to_id"])

    def test_vault_ingest_progress_is_visible_without_storage_or_gcs_details(self):
        vault_task = CollectionTask.objects.create(
            request=self.request_obj,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.INGESTING,
            instructions="Ingest the completed export.",
        )
        VaultIngestRun.objects.create(
            collection_task=vault_task,
            state=VaultIngestRun.State.FAILED,
            attempt_count=2,
            files_total=3,
            files_processed=2,
            messages_seen=27,
            items_created=20,
            duplicates_skipped=7,
            last_error_code="message_conversion_failed",
            last_error_detail="sensitive external object detail",
        )

        response = self.client.get(
            f"/api/staff/collection-tasks/{vault_task.pk}/"
        )

        self.assertEqual(response.status_code, 200)
        progress = response.json()["vault_ingest"]
        self.assertEqual(progress["state"], "failed")
        self.assertEqual(progress["attempt_count"], 2)
        self.assertEqual(progress["files_total"], 3)
        self.assertEqual(progress["files_processed"], 2)
        self.assertEqual(progress["messages_seen"], 27)
        self.assertEqual(progress["items_created"], 20)
        self.assertEqual(progress["duplicates_skipped"], 7)
        self.assertEqual(progress["last_error_code"], "message_conversion_failed")
        self.assertNotIn("last_error_detail", progress)
        self.assertNotIn("storage", str(progress).lower())
        self.assertNotIn("bucket", str(progress).lower())

    def test_manual_task_has_no_vault_ingest_progress(self):
        response = self.client.get(
            f"/api/staff/collection-tasks/{self.task.pk}/"
        )

        self.assertIsNone(response.json()["vault_ingest"])

    # -- create --------------------------------------------------------
    def test_create_manual_task_defaults_kind(self):
        r = self.client.post("/api/staff/collection-tasks/", {
            "request": str(self.request_obj.id),
            "instructions": "Search the district file server.",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["kind"], CollectionTask.Kind.MANUAL)
        self.assertEqual(body["state"], CollectionTask.State.PENDING)
        created = CollectionTask.objects.get(id=body["id"])
        self.assertEqual(created.kind, CollectionTask.Kind.MANUAL)

    def test_create_requires_instructions(self):
        r = self.client.post("/api/staff/collection-tasks/", {
            "request": str(self.request_obj.id),
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_create_requires_request(self):
        r = self.client.post("/api/staff/collection-tasks/", {
            "instructions": "Search the district file server.",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_generic_create_cannot_bypass_reviewed_vault_collection_workflow(self):
        for kind in CollectionTask.VAULT_KINDS:
            with self.subTest(kind=kind):
                response = self.client.post(
                    "/api/staff/collection-tasks/",
                    {
                        "request": str(self.request_obj.id),
                        "kind": kind,
                        "instructions": "Attempt to bypass the reviewed Vault count.",
                    },
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 400)
                self.assertIn("kind", response.json())
                self.assertFalse(
                    CollectionTask.objects.filter(
                        request=self.request_obj,
                        kind=kind,
                    ).exists()
                )

    def test_a_chat_collection_names_its_source(self):
        task = CollectionTask.objects.create(
            request=self.request_obj,
            kind=CollectionTask.Kind.VAULT_CHAT,
            instructions="Vault Chat task.",
        )
        response = self.client.get(f"/api/staff/collection-tasks/{task.pk}/")
        self.assertEqual(response.json()["source_label"], "Google Workspace (Chat)")

    def test_no_destroy(self):
        r = self.client.delete(f"/api/staff/collection-tasks/{self.task.id}/")
        self.assertEqual(r.status_code, 405)

    # -- patch / state transitions --------------------------------------
    def test_patch_state_to_complete_sets_completed_at(self):
        self.assertIsNone(self.task.completed_at)
        r = self.client.patch(f"/api/staff/collection-tasks/{self.task.id}/",
                              {"state": CollectionTask.State.COMPLETE},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, CollectionTask.State.COMPLETE)
        self.assertIsNotNone(self.task.completed_at)

    def test_a_held_task_tells_a_liaison_only_that_it_waits_on_payment(self):
        from datetime import date
        from decimal import Decimal

        from apps.records.fees import activate_fee_schedule, apply_deposit_gate
        from apps.records.models import FeeDepositGate, FeeSchedule

        officer = get_user_model().objects.get(username="officer")
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        activate_fee_schedule(
            actor=officer,
            name="PSD adopted PRA copy schedule",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=date(2026, 8, 1),
            counsel_approval_reference="Legal memo PRA-fees-2026-07-28",
            adoption_reference="Board policy 4040 resolution 2026-08-01",
        )
        apply_deposit_gate(
            self.request_obj.number,
            actor=officer,
            scope=FeeDepositGate.Scope.COLLECTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the estimated review time.",
        )
        liaison = get_user_model().objects.create_user(
            "liaison", "liaison@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(liaison, {StaffRole.LIAISON})
        self.task.assigned_to = liaison
        self.task.save(update_fields=["assigned_to"])

        self.client.force_login(liaison)
        liaison_refusal = self.client.patch(
            f"/api/staff/collection-tasks/{self.task.id}/",
            {"state": CollectionTask.State.COMPLETE},
            content_type="application/json",
        )
        self.client.force_login(officer)
        officer_refusal = self.client.patch(
            f"/api/staff/collection-tasks/{self.task.id}/",
            {"state": CollectionTask.State.COMPLETE},
            content_type="application/json",
        )

        self.assertEqual(liaison_refusal.status_code, 400)
        self.assertIn(b"waiting on payment", liaison_refusal.content)
        self.assertNotIn(b"36.72", liaison_refusal.content)
        self.assertEqual(officer_refusal.status_code, 400)
        self.assertIn(b"36.72", officer_refusal.content)

    def test_patch_state_not_complete_leaves_completed_at_null(self):
        r = self.client.patch(f"/api/staff/collection-tasks/{self.task.id}/",
                              {"state": CollectionTask.State.RUNNING},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, CollectionTask.State.RUNNING)
        self.assertIsNone(self.task.completed_at)

    def test_patch_rejects_invalid_state(self):
        r = self.client.patch(f"/api/staff/collection-tasks/{self.task.id}/",
                              {"state": "bogus"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_patch_rejects_backend_owned_ingesting_state(self):
        r = self.client.patch(
            f"/api/staff/collection-tasks/{self.task.id}/",
            {"state": CollectionTask.State.INGESTING},
            content_type="application/json",
        )

        self.assertEqual(r.status_code, 400)
        self.assertIn("state", r.json())
        self.task.refresh_from_db()
        self.assertEqual(self.task.state, CollectionTask.State.PENDING)

    def test_patch_cannot_mark_vault_collection_complete_without_ingest_evidence(self):
        vault_task = CollectionTask.objects.create(
            request=self.request_obj,
            kind=CollectionTask.Kind.VAULT_CHAT,
            state=CollectionTask.State.RUNNING,
            instructions="Vault task.",
        )

        response = self.client.patch(
            f"/api/staff/collection-tasks/{vault_task.pk}/",
            {"state": CollectionTask.State.COMPLETE},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        vault_task.refresh_from_db()
        self.assertEqual(vault_task.state, CollectionTask.State.RUNNING)

    def test_patch_assigned_to_by_id(self):
        officer2 = get_user_model().objects.create_user(
            "officer2", "officer2@psd401.net", "pw", is_staff=True)
        r = self.client.patch(f"/api/staff/collection-tasks/{self.task.id}/",
                              {"assigned_to_id": officer2.id},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, officer2.id)
        self.assertEqual(r.json()["assigned_to_username"], "officer2")

    def test_patch_rejects_inactive_or_nonstaff_assignee(self):
        invalid_assignees = (
            get_user_model().objects.create_user(
                "inactive-officer",
                "inactive-officer@psd401.net",
                "pw",
                is_staff=True,
                is_active=False,
            ),
            get_user_model().objects.create_user(
                "nonstaff-assignee",
                "nonstaff-assignee@example.org",
                "pw",
                is_staff=False,
            ),
        )

        for assignee in invalid_assignees:
            with self.subTest(assignee=assignee.username):
                response = self.client.patch(
                    f"/api/staff/collection-tasks/{self.task.id}/",
                    {"assigned_to_id": assignee.id},
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("assigned_to_id", response.json())
                self.task.refresh_from_db()
                self.assertIsNone(self.task.assigned_to_id)

    def test_patch_rejects_staff_without_collection_role(self):
        reviewer = get_user_model().objects.create_user(
            "reviewer-assignee",
            "reviewer-assignee@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})

        response = self.client.patch(
            f"/api/staff/collection-tasks/{self.task.id}/",
            {"assigned_to_id": reviewer.id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("assigned_to_id", response.json())
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_to_id)

    def test_create_rejects_inactive_or_nonstaff_assignee(self):
        invalid_assignees = (
            get_user_model().objects.create_user(
                "inactive-create-assignee",
                "inactive-create-assignee@psd401.net",
                "pw",
                is_staff=True,
                is_active=False,
            ),
            get_user_model().objects.create_user(
                "nonstaff-create-assignee",
                "nonstaff-create-assignee@example.org",
                "pw",
                is_staff=False,
            ),
        )

        for assignee in invalid_assignees:
            with self.subTest(assignee=assignee.username):
                response = self.client.post(
                    "/api/staff/collection-tasks/",
                    {
                        "request": str(self.request_obj.id),
                        "instructions": "Search the district file server.",
                        "assigned_to_id": assignee.id,
                    },
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("assigned_to_id", response.json())

    def test_patch_instructions_and_custodian_emails(self):
        r = self.client.patch(f"/api/staff/collection-tasks/{self.task.id}/", {
            "instructions": "Updated instructions.",
            "custodian_emails": ["custodian@psd401.net"],
        }, content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.instructions, "Updated instructions.")
        self.assertEqual(self.task.custodian_emails, ["custodian@psd401.net"])

    def test_patch_due_date(self):
        r = self.client.patch(f"/api/staff/collection-tasks/{self.task.id}/",
                              {"due_date": "2026-08-01"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.task.refresh_from_db()
        self.assertEqual(str(self.task.due_date), "2026-08-01")

    # -- vault ids read-only ---------------------------------------------
    def test_vault_ids_read_only(self):
        r = self.client.patch(f"/api/staff/collection-tasks/{self.task.id}/",
                              {"vault_matter_id": "matters/should-not-stick"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.vault_matter_id, "")

    # -- filters ---------------------------------------------------------
    def test_filter_by_state(self):
        self.task.state = CollectionTask.State.RUNNING
        self.task.save()
        r = self.client.get("/api/staff/collection-tasks/?state=running")
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        ids = {row["id"] for row in rows}
        self.assertEqual(ids, {self.task.id})

    def test_filter_by_request_number(self):
        other_request = PublicRecordsRequest.objects.create(
            requester=self.requester, description="other request")
        other_task = CollectionTask.objects.create(
            request=other_request, kind=CollectionTask.Kind.MANUAL,
            instructions="Unrelated task.")

        r = self.client.get(
            f"/api/staff/collection-tasks/?request={self.request_obj.number}")
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        ids = {row["id"] for row in rows}
        self.assertEqual(ids, {self.task.id, self.no_system_task.id})
        self.assertNotIn(other_task.id, ids)

    def test_filter_urgent(self):
        r = self.client.get("/api/staff/collection-tasks/?urgent=true")
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        ids = {row["id"] for row in rows}
        self.assertEqual(ids, {self.task.id})

    # -- ordering: due_date asc, nulls last -------------------------------
    def test_default_ordering_due_date_nulls_last(self):
        self.task.due_date = timezone.localdate()
        self.task.save()
        r = self.client.get("/api/staff/collection-tasks/")
        payload = r.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        ids_in_order = [row["id"] for row in rows]
        self.assertLess(
            ids_in_order.index(self.task.id),
            ids_in_order.index(self.no_system_task.id),
        )
