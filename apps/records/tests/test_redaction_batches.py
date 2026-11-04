"""Group acceptance of redaction proposals with a sampled check (issue #40).

Pending proposals on a case are grouped by exemption and pattern. Accepting
a group follows the same sampled-QA rule as record batches: every sampled
proposal must be accepted before the rest are, and each acceptance is its own
create_redaction with its own exemption citation (rule 3). Second review stays
required where the exemption requires it, and one action builds the redacted
PDFs for every record that is ready.
"""

import hashlib
import shutil
import tempfile

import pymupdf as fitz
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.advisory_ai import advisory_input_sha256
from apps.records.models import (
    AdvisoryRun,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    RedactionBatch,
    RedactionProposal,
    RedactionProposalDecision,
    Requester,
)
from apps.records.redaction_batches import (
    build_ready_redacted_records,
    create_redaction_batch,
    decide_redaction_batch,
    proposal_groups,
)
from apps.secure_files import _FilesystemObjectStore

STORE = tempfile.mkdtemp(prefix="prr-redaction-batches-")


def _pdf(text):
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 96), text, fontsize=12)
    payload = document.tobytes()
    document.close()
    return payload


@override_settings(
    PRR_E2E_FILESYSTEM_OBJECT_STORE=STORE,
    PRR_TEST_FIXTURE_SEED_ENABLED=True,
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
    PRR_TRUST_REDACTION_MODE="batch",
    PRR_TRUST_BATCH_SAMPLE_SIZE=2,
)
class RedactionBatchTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(STORE, ignore_errors=True)

    def setUp(self):
        self.reviewer = get_user_model().objects.create(
            username="redact-batch-reviewer", email="rbr@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="redact@example.org"),
            description="Synthetic redaction request.",
        )
        self.phone = ExemptionCitation.objects.create(
            citation="RCW 42.56.250(4)", short_name="Employee personal phone",
        )
        self.ferpa = ExemptionCitation.objects.create(
            citation="FERPA 20 U.S.C. 1232g", short_name="Student PII",
            requires_second_review=True,
        )
        self._n = 0

    def _proposal(self, exemption, pattern):
        self._n += 1
        pdf = _pdf(f"Synthetic record {self._n}: call 555-0100 for details.")
        key = f"records/{self.prr.pk}/batch-{self._n}.pdf"
        _FilesystemObjectStore(STORE).put_object(
            Bucket=settings.OBJECT_STORE["bucket"], Key=key, Body=_bytes(pdf),
        )
        item = RecordItem.objects.create(
            request=self.prr,
            original_filename=f"batch-{self._n}.pdf",
            storage_key=key,
            content_length=len(pdf),
            content_type="application/pdf",
            sha256=hashlib.sha256(pdf).hexdigest(),
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.reviewer,
        )
        run = AdvisoryRun(
            kind=AdvisoryRun.Kind.RECORD_REDACTIONS, request=self.prr, record_item=item,
            model_identifier="synthetic", input_sha256="", state=AdvisoryRun.State.COMPLETE,
            requested_by=self.reviewer,
        )
        run.input_sha256 = advisory_input_sha256(run)
        run.save()
        return RedactionProposal.objects.create(
            advisory_run=run, record_item=item, page_number=1,
            x0=0.1, y0=0.1, x1=0.4, y1=0.16, exemption=exemption, pattern=pattern,
            confidence=0.9, rationale="A personal phone number.",
            source_span_ids=["p1s1"], source_text_sha256="0" * 64,
        )

    def test_pending_proposals_are_grouped_by_exemption_and_pattern(self):
        for _ in range(3):
            self._proposal(self.phone, "phone")
        self._proposal(self.ferpa, "student_identifier")

        groups = {(g["exemption_id"], g["pattern"]): g for g in proposal_groups(self.prr)}

        self.assertEqual(groups[(self.phone.pk, "phone")]["pending"], 3)
        self.assertEqual(groups[(self.phone.pk, "phone")]["records"], 3)
        self.assertEqual(groups[(self.ferpa.pk, "student_identifier")]["pending"], 1)
        self.assertEqual(groups[(self.phone.pk, "phone")]["pattern_label"], "Phone number")

    def test_accepting_the_sample_accepts_the_group_one_redaction_each(self):
        proposals = [self._proposal(self.phone, "phone") for _ in range(4)]
        batch = create_redaction_batch(
            self.prr, exemption_id=self.phone.pk, pattern="phone", actor=self.reviewer,
        )
        self.assertEqual(len(batch.sample_ids), 2)
        self.assertFalse(RedactionProposalDecision.objects.exists())

        decided = decide_redaction_batch(
            batch.pk, actor=self.reviewer,
            judgments={pk: {"accept": True} for pk in batch.sample_ids},
        )

        self.assertEqual(decided.outcome, RedactionBatch.Outcome.APPLIED)
        self.assertEqual(decided.accepted_count, 4)
        for proposal in proposals:
            decision = RedactionProposalDecision.objects.get(proposal=proposal)
            self.assertEqual(decision.outcome, RedactionProposalDecision.Outcome.ACCEPTED)
            self.assertEqual(decision.redaction.exemption, self.phone)
            self.assertEqual(decision.decided_by, self.reviewer)
        self.assertEqual(proposal_groups(self.prr), [])
        self.assertTrue(AuditLog.objects.filter(action="redaction.batch_decided").exists())

    def test_one_rejection_blocks_the_group_and_keeps_the_sample_decisions(self):
        for _ in range(4):
            self._proposal(self.phone, "phone")
        batch = create_redaction_batch(
            self.prr, exemption_id=self.phone.pk, pattern="phone", actor=self.reviewer,
        )
        rejected, accepted = batch.sample_ids
        decided = decide_redaction_batch(
            batch.pk, actor=self.reviewer,
            judgments={
                accepted: {"accept": True},
                rejected: {"accept": False, "reason": "This is the district main line."},
            },
        )

        self.assertEqual(decided.outcome, RedactionBatch.Outcome.BLOCKED)
        self.assertEqual(RedactionProposalDecision.objects.count(), 2)
        self.assertEqual(
            RedactionProposalDecision.objects.get(proposal_id=rejected).outcome,
            RedactionProposalDecision.Outcome.REJECTED,
        )
        self.assertEqual(proposal_groups(self.prr)[0]["pending"], 2)

    def test_a_rejection_needs_a_reason_and_every_sample_needs_a_judgment(self):
        for _ in range(3):
            self._proposal(self.phone, "phone")
        batch = create_redaction_batch(
            self.prr, exemption_id=self.phone.pk, pattern="phone", actor=self.reviewer,
        )
        for judgments in (
            {batch.sample_ids[0]: {"accept": True}},
            {pk: {"accept": False, "reason": "no"} for pk in batch.sample_ids},
        ):
            with self.subTest(judgments=judgments), self.assertRaises(ValidationError):
                decide_redaction_batch(batch.pk, actor=self.reviewer, judgments=judgments)
        self.assertFalse(RedactionProposalDecision.objects.exists())

    def test_second_review_stays_required_and_only_ready_records_are_built(self):
        phone = [self._proposal(self.phone, "phone") for _ in range(2)]
        ferpa = [self._proposal(self.ferpa, "student_identifier") for _ in range(2)]
        for exemption, pattern in ((self.phone, "phone"), (self.ferpa, "student_identifier")):
            batch = create_redaction_batch(
                self.prr, exemption_id=exemption.pk, pattern=pattern, actor=self.reviewer,
            )
            decide_redaction_batch(
                batch.pk, actor=self.reviewer,
                judgments={pk: {"accept": True} for pk in batch.sample_ids},
            )
        for proposal in ferpa:
            proposal.record_item.refresh_from_db()
            self.assertTrue(proposal.record_item.second_review_required)
            self.assertIsNone(proposal.record_item.second_reviewed_at)

        result = build_ready_redacted_records(self.prr, actor=self.reviewer)

        self.assertEqual(result["built"], 2)
        self.assertEqual(result["awaiting_second_review"], 2)
        for proposal in phone:
            proposal.record_item.refresh_from_db()
            self.assertTrue(proposal.record_item.redacted_storage_key)
        for proposal in ferpa:
            proposal.record_item.refresh_from_db()
            self.assertEqual(proposal.record_item.redacted_storage_key, "")
        # A second run builds nothing new.
        self.assertEqual(build_ready_redacted_records(self.prr, actor=self.reviewer)["built"], 0)

    @override_settings(PRR_TRUST_REDACTION_MODE="suggest")
    def test_suggest_mode_allows_no_group_acceptance(self):
        self._proposal(self.phone, "phone")
        with self.assertRaises(ValidationError):
            create_redaction_batch(
                self.prr, exemption_id=self.phone.pk, pattern="phone", actor=self.reviewer,
            )


class RedactionBatchApiTests(RedactionBatchTests):
    def test_a_reviewer_groups_accepts_and_builds_through_the_api(self):
        from rest_framework.test import APIClient

        api = APIClient()
        api.force_authenticate(self.reviewer)
        for _ in range(3):
            self._proposal(self.phone, "phone")

        groups = api.get("/api/staff/redaction-batches/groups/", {"request": self.prr.number})
        self.assertEqual(groups.status_code, 200, groups.content)
        self.assertEqual(groups.json()["mode"], "batch")
        self.assertEqual(groups.json()["groups"][0]["pending"], 3)

        opened = api.post(
            "/api/staff/redaction-batches/",
            {"request": self.prr.number, "exemption_id": self.phone.pk, "pattern": "phone"},
            format="json",
        )
        self.assertEqual(opened.status_code, 201, opened.content)
        body = opened.json()
        self.assertEqual(len(body["samples"]), 2)
        self.assertEqual(body["samples"][0]["page_number"], 1)

        decided = api.post(
            f"/api/staff/redaction-batches/{body['id']}/decide/",
            {"judgments": {s["id"]: {"accept": True} for s in body["samples"]}},
            format="json",
        )
        self.assertEqual(decided.status_code, 200, decided.content)
        self.assertEqual(decided.json()["accepted_count"], 3)

        built = api.post(
            "/api/staff/redaction-batches/build/", {"request": self.prr.number}, format="json",
        )
        self.assertEqual(built.status_code, 200, built.content)
        self.assertEqual(built.json()["built"], 3)

    def test_token_callers_are_handed_off_for_decide_and_build(self):
        from rest_framework.authtoken.models import Token
        from rest_framework.test import APIClient

        self._proposal(self.phone, "phone")
        batch = create_redaction_batch(
            self.prr, exemption_id=self.phone.pk, pattern="phone", actor=self.reviewer,
        )
        token = Token.objects.create(user=self.reviewer).key
        for url, body in (
            (f"/api/staff/redaction-batches/{batch.pk}/decide/",
             {"judgments": {pk: {"accept": True} for pk in batch.sample_ids}}),
            ("/api/staff/redaction-batches/build/", {"request": self.prr.number}),
        ):
            with self.subTest(url=url):
                response = APIClient().post(
                    url, body, format="json", HTTP_AUTHORIZATION=f"Token {token}",
                )
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertFalse(RedactionProposalDecision.objects.exists())


def _bytes(payload):
    import io

    return io.BytesIO(payload)
