"""Decision batches (issue #43): an agent proposes, a human approves.

A batch carries a kind, its items, who proposed it, and the evidence. Only a
person in the staff application decides it, and approving runs the same
service the staff UI runs. Both the proposal and the decision are
append-only, so a decided batch can never be rewritten.
"""

import hashlib
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.decision_batches import (
    approve_decision_batch,
    batch_state,
    propose_decision_batch,
    reject_decision_batch,
)
from apps.records.models import (
    CollectionTask,
    DecisionBatch,
    DecisionBatchDecision,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
    VaultCountRun,
)

VAULT = {
    "PRR_VAULT_CREDENTIALS_FILE": "",
    "PRR_VAULT_ADMIN_USER": "vault-admin@example.org",
    "PRR_VAULT_IMPERSONATION_TARGET": "prr-vault@psd401-prr-identity.iam.gserviceaccount.com",
    "PRR_GOOGLE_HOSTED_DOMAIN": "psd401.net",
}


class _Fixture(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.officer = users.create(username="batch-officer", email="batch-officer@psd401.net", is_staff=True)
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.reviewer = users.create(username="batch-reviewer", email="batch-reviewer@psd401.net", is_staff=True)
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="batch@example.org"),
            description="Board minutes.",
        )
        self.items = [self._item(f"minutes-{n}.pdf") for n in range(2)]

    def _item(self, name, request=None):
        return RecordItem.objects.create(
            request=request or self.request, original_filename=name,
            storage_key=f"records/{name}", content_length=1, content_type="application/pdf",
            sha256=hashlib.sha256(name.encode()).hexdigest(),
        )

    def _review_batch(self, actor=None):
        return propose_decision_batch(
            DecisionBatch.Kind.REVIEW,
            {"record_item_ids": [str(i.pk) for i in self.items], "state": "non_responsive"},
            actor=actor or self.reviewer,
            evidence_summary="Both are drafts of the newsletter, not board minutes.",
        )


class ProposeTests(_Fixture):
    def test_a_proposal_records_kind_items_proposer_and_evidence(self):
        batch = self._review_batch()

        self.assertEqual(batch.request, self.request)
        self.assertEqual(batch.proposed_by, self.reviewer)
        self.assertEqual(batch_state(batch), "proposed")
        self.assertTrue(AuditLog.objects.filter(action="decision_batch.proposed", target=self.request.number).exists())
        # Proposing changes nothing.
        self.assertFalse(RecordItem.objects.exclude(review_state=RecordItem.ReviewState.UNREVIEWED).exists())

    def test_a_proposal_needs_evidence_and_items_from_one_request(self):
        with self.assertRaisesMessage(ValidationError, "evidence"):
            propose_decision_batch(
                DecisionBatch.Kind.REVIEW,
                {"record_item_ids": [str(self.items[0].pk)], "state": "responsive"},
                actor=self.reviewer, evidence_summary=" ",
            )
        other = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"), description="Other.",
        )
        stray = self._item("stray.pdf", request=other)
        with self.assertRaisesMessage(ValidationError, "one request"):
            propose_decision_batch(
                DecisionBatch.Kind.REVIEW,
                {"record_item_ids": [str(self.items[0].pk), str(stray.pk)], "state": "responsive"},
                actor=self.reviewer, evidence_summary="Mixed.",
            )
        with self.assertRaisesMessage(ValidationError, "state"):
            propose_decision_batch(
                DecisionBatch.Kind.REVIEW,
                {"record_item_ids": [str(self.items[0].pk)], "state": "withheld"},
                actor=self.reviewer, evidence_summary="Withholding needs citations.",
            )

    def test_the_proposer_must_hold_the_kind_role(self):
        with self.assertRaises(ValidationError):
            propose_decision_batch(
                DecisionBatch.Kind.SUPERSEDE, {"collection_task_id": 1, "reason": "x" * 30},
                actor=self.reviewer, evidence_summary="Reviewers do not supersede.",
            )

    def test_proposals_and_decisions_are_append_only(self):
        batch = self._review_batch()
        batch.evidence_summary = "rewritten"
        with self.assertRaises(ValidationError):
            batch.save()
        with self.assertRaises(ValidationError):
            DecisionBatch.objects.filter(pk=batch.pk).update(evidence_summary="rewritten")
        decided = reject_decision_batch(batch.pk, actor=self.reviewer, reason="Not drafts; keep reviewing.")
        decision = DecisionBatchDecision.objects.get(batch=decided)
        with self.assertRaises(ValidationError):
            decision.delete()


class ApproveTests(_Fixture):
    def test_approving_a_review_batch_runs_the_review_service(self):
        batch = self._review_batch()

        approve_decision_batch(batch.pk, actor=self.reviewer)

        self.assertEqual(
            set(RecordItem.objects.values_list("review_state", flat=True)),
            {RecordItem.ReviewState.NON_RESPONSIVE},
        )
        self.assertEqual(RecordItem.objects.get(pk=self.items[0].pk).reviewed_by, self.reviewer)
        decision = DecisionBatchDecision.objects.get(batch=batch)
        self.assertEqual(decision.outcome, DecisionBatchDecision.Outcome.APPROVED)
        self.assertEqual(decision.decided_by, self.reviewer)
        self.assertEqual(batch_state(batch), "approved")

    def test_a_batch_is_decided_once(self):
        batch = self._review_batch()
        approve_decision_batch(batch.pk, actor=self.reviewer)
        with self.assertRaisesMessage(ValidationError, "already"):
            reject_decision_batch(batch.pk, actor=self.officer, reason="Changed my mind about this.")

    def test_a_refused_service_records_no_decision(self):
        batch = self._review_batch()
        RecordItem.objects.filter(pk=self.items[0].pk).update(review_state=RecordItem.ReviewState.SUPERSEDED)
        with self.assertRaises(ValidationError):
            approve_decision_batch(batch.pk, actor=self.reviewer)
        self.assertFalse(DecisionBatchDecision.objects.exists())
        self.assertEqual(RecordItem.objects.get(pk=self.items[1].pk).review_state, RecordItem.ReviewState.UNREVIEWED)

    def test_the_approver_must_hold_the_kind_role(self):
        outsider = get_user_model().objects.create(username="liaison", email="liaison@psd401.net", is_staff=True)
        apply_staff_roles(outsider, [StaffRole.LIAISON])
        batch = self._review_batch()
        with self.assertRaises(ValidationError):
            approve_decision_batch(batch.pk, actor=outsider)

    def test_rejecting_needs_a_reason_and_changes_nothing(self):
        batch = self._review_batch()
        with self.assertRaises(ValidationError):
            reject_decision_batch(batch.pk, actor=self.reviewer, reason="no")
        reject_decision_batch(batch.pk, actor=self.reviewer, reason="These are the real minutes.")
        self.assertEqual(batch_state(batch), "rejected")
        self.assertFalse(RecordItem.objects.exclude(review_state=RecordItem.ReviewState.UNREVIEWED).exists())

    def test_approving_a_supersede_batch_supersedes_the_collection(self):
        task = CollectionTask.objects.create(
            request=self.request, kind=CollectionTask.Kind.VAULT_MAIL, state=CollectionTask.State.COMPLETE,
        )
        RecordItem.objects.filter(pk__in=[i.pk for i in self.items]).update(collection_task=task)
        batch = propose_decision_batch(
            DecisionBatch.Kind.SUPERSEDE,
            {"collection_task_id": task.pk, "reason": "The export swept in the whole newsletter archive."},
            actor=self.officer, evidence_summary="2,300 of 2,340 messages are the newsletter.",
        )
        approve_decision_batch(batch.pk, actor=self.officer)
        self.assertEqual(
            RecordItem.objects.filter(review_state=RecordItem.ReviewState.SUPERSEDED).count(), 2,
        )

    @override_settings(**VAULT)
    @patch("apps.records.tasks.execute_vault_collection.delay")
    def test_approving_a_scope_and_collect_batch_starts_the_collection(self, delay):
        run = VaultCountRun.objects.create(
            request=self.request, trigger=VaultCountRun.Trigger.AUTOMATIC,
            state=VaultCountRun.State.COMPLETE, search_scope=VaultCountRun.SearchScope.ACCOUNTS,
            terms='"board"', custodian_emails=["board@psd401.net"],
            input_sha256=hashlib.sha256(b"run").hexdigest(), vault_matter_id="matter-1",
            total_count=40, completed_at=timezone.now(),
        )
        batch = propose_decision_batch(
            DecisionBatch.Kind.SCOPE_AND_COLLECT,
            {"count_run_id": str(run.pk), "expected_input_sha256": run.input_sha256},
            actor=self.officer, evidence_summary="40 messages in the board mailbox.",
        )
        approve_decision_batch(batch.pk, actor=self.officer)
        task = CollectionTask.objects.get(source_vault_count=run)
        self.assertEqual(DecisionBatchDecision.objects.get(batch=batch).result["collection_task_id"], task.pk)
        delay.assert_called_once_with(task.pk)

    def test_approving_a_duplicate_label_merges_the_cases(self):
        target = PublicRecordsRequest.objects.create(
            requester=self.request.requester, description="Board minutes, first copy.",
        )
        batch = propose_decision_batch(
            DecisionBatch.Kind.INTAKE_LABEL,
            {"request_number": self.request.number, "label": "duplicate_of",
             "target_case_number": target.number, "reason": "Same requester asked twice the same day."},
            actor=self.officer, evidence_summary="Identical wording, eleven minutes apart.",
        )
        approve_decision_batch(batch.pk, actor=self.officer)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, RequestStatus.CLOSED_DUPLICATE)

    def test_an_intake_label_without_a_state_change_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "label"):
            propose_decision_batch(
                DecisionBatch.Kind.INTAKE_LABEL,
                {"request_number": self.request.number, "label": "new_request", "reason": "A new request."},
                actor=self.officer, evidence_summary="Looks new.",
            )

    def test_a_redaction_group_needs_an_active_exemption(self):
        with self.assertRaisesMessage(ValidationError, "exemption"):
            propose_decision_batch(
                DecisionBatch.Kind.REDACTION_GROUP,
                {"request_number": self.request.number, "exemption_id": 999999, "pattern": "phone"},
                actor=self.reviewer, evidence_summary="No such exemption.",
            )

    @patch("apps.records.decision_batches.create_redaction_batch")
    def test_approving_a_redaction_group_opens_its_sampled_qa_batch(self, create):
        create.return_value.pk = 41
        exemption = ExemptionCitation.objects.create(citation="RCW 42.56.250(4)", short_name="Employee contact")
        batch = propose_decision_batch(
            DecisionBatch.Kind.REDACTION_GROUP,
            {"request_number": self.request.number, "exemption_id": exemption.pk, "pattern": "phone"},
            actor=self.reviewer, evidence_summary="Twelve staff cell numbers.",
        )
        approve_decision_batch(batch.pk, actor=self.reviewer)
        create.assert_called_once_with(self.request, exemption_id=exemption.pk, pattern="phone", actor=self.reviewer)
        self.assertEqual(DecisionBatchDecision.objects.get(batch=batch).result, {"redaction_batch_id": 41})


class DecisionBatchApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.session = APIClient()
        self.session.force_login(self.reviewer)
        self.token = APIClient()
        self.token.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.reviewer).key}")
        self.body = {
            "kind": "review",
            "payload": {"record_item_ids": [str(i.pk) for i in self.items], "state": "non_responsive"},
            "evidence_summary": "Both are newsletter drafts.",
        }

    def test_a_token_caller_proposes_and_reads_but_cannot_decide(self):
        created = self.token.post("/api/staff/decision-batches/", self.body, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        batch_id = created.json()["id"]
        self.assertEqual(created.json()["state"], "proposed")
        self.assertEqual(self.token.get(f"/api/staff/decision-batches/{batch_id}/").status_code, 200)

        refused = self.token.post(f"/api/staff/decision-batches/{batch_id}/approve/", {}, format="json")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["code"], "human_approval_required")
        self.assertFalse(DecisionBatchDecision.objects.exists())

    def test_an_unknown_state_filter_is_refused(self):
        self.assertEqual(self.session.get("/api/staff/decision-batches/?state=pending").status_code, 400)

    def test_a_person_approves_in_the_staff_application(self):
        batch_id = self.token.post("/api/staff/decision-batches/", self.body, format="json").json()["id"]
        queue = self.session.get("/api/staff/decision-batches/?state=proposed").json()
        self.assertEqual([row["id"] for row in queue["results"]], [batch_id])

        approved = self.session.post(f"/api/staff/decision-batches/{batch_id}/approve/", {}, format="json")
        self.assertEqual(approved.status_code, 200, approved.content)
        self.assertEqual(approved.json()["state"], "approved")
        self.assertEqual(self.session.get("/api/staff/decision-batches/?state=proposed").json()["results"], [])

    def test_a_token_caller_is_handed_off_on_the_endpoints_batches_replace(self):
        response = self.token.post(
            f"/api/staff/record-items/{self.items[0].pk}/review/", {"state": "responsive"}, format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "human_approval_required")
        bulk = self.token.post(
            "/api/staff/record-items/bulk-review/",
            {"record_item_ids": [str(self.items[0].pk)], "state": "responsive"}, format="json",
        )
        self.assertEqual(bulk.status_code, 403)
        # The browser session is unaffected.
        ok = self.session.post(
            f"/api/staff/record-items/{self.items[0].pk}/review/", {"state": "responsive"}, format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)

    def test_an_officer_token_is_handed_off_on_every_endpoint_a_batch_replaces(self):
        officer = APIClient()
        officer.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.officer).key}")
        number = self.request.number
        task = CollectionTask.objects.create(
            request=self.request, kind=CollectionTask.Kind.VAULT_MAIL, state=CollectionTask.State.COMPLETE,
        )
        endpoints = (
            f"/api/staff/collection-tasks/{task.pk}/supersede/",
            f"/api/staff/requests/{number}/vault-collection/",
            f"/api/staff/requests/{number}/rescope/",
            f"/api/staff/requests/{number}/merge/",
            f"/api/staff/requests/{number}/close-non-request/",
        )
        for path in endpoints:
            with self.subTest(path=path):
                response = officer.post(path, {}, format="json")
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertFalse(AuditLog.objects.filter(action__startswith="collection.").exists())
