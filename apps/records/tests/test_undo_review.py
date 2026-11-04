"""Undo the last review decision (issue #46, the `u` key).

A reviewer may undo only their own most recent decision on a record. The
record goes back to the state it had before that decision; a record withheld
in full, or one with active redactions, is never silently undone.
"""

import hashlib

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.agreement import agreement_report
from apps.records.models import (
    PublicRecordsRequest,
    RecordItem,
    Requester,
    ReviewDecision,
)
from apps.records.review import review_record, undo_review

R = RecordItem.ReviewState


def _user(username):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [StaffRole.REVIEWER])
    return user


class UndoReviewTests(TestCase):
    def setUp(self):
        self.reviewer = _user("undo-reviewer")
        self.other = _user("undo-other")
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="undo@example.org"),
            description="Synthetic undo request.",
        )
        self.item = RecordItem.objects.create(
            request=self.prr,
            original_filename="memo.pdf",
            storage_key=f"records/{self.prr.pk}/memo.pdf",
            content_length=10,
            content_type="application/pdf",
            sha256=hashlib.sha256(b"undo").hexdigest(),
        )

    def test_undo_returns_a_first_decision_to_unreviewed(self):
        review_record(self.item.pk, state=R.NON_RESPONSIVE, actor=self.reviewer)

        item = undo_review(self.item.pk, actor=self.reviewer)

        self.assertEqual(item.review_state, R.UNREVIEWED)
        self.assertIsNone(item.reviewed_by)
        self.assertIsNone(item.reviewed_at)
        row = AuditLog.objects.get(action="record.review_undone")
        self.assertEqual(row.detail["undone_state"], R.NON_RESPONSIVE)
        self.assertEqual(row.detail["restored_state"], R.UNREVIEWED)
        # The undone decision no longer counts toward agreement.
        self.assertEqual(ReviewDecision.objects.order_by("-created_at")[0].review_state, R.UNREVIEWED)
        self.assertEqual(agreement_report()["decisions"], 0)

    def test_undo_restores_the_previous_human_decision(self):
        review_record(self.item.pk, state=R.RESPONSIVE, actor=self.other)
        review_record(self.item.pk, state=R.NON_RESPONSIVE, actor=self.reviewer)

        item = undo_review(self.item.pk, actor=self.reviewer)

        self.assertEqual(item.review_state, R.RESPONSIVE)
        self.assertEqual(item.reviewed_by, self.reviewer)

    def test_only_your_own_latest_decision_can_be_undone(self):
        review_record(self.item.pk, state=R.RESPONSIVE, actor=self.other)
        with self.assertRaises(ValidationError):
            undo_review(self.item.pk, actor=self.reviewer)

    def test_a_record_never_reviewed_has_nothing_to_undo(self):
        with self.assertRaises(ValidationError):
            undo_review(self.item.pk, actor=self.reviewer)

    def test_a_withheld_record_is_never_restored_by_undo(self):
        from apps.records.models import ExemptionCitation

        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)", short_name="Personal information",
        )
        review_record(
            self.item.pk, state=R.WITHHELD, actor=self.other,
            withholding_exemption_ids=[exemption.pk],
        )
        review_record(self.item.pk, state=R.RESPONSIVE, actor=self.reviewer)
        with self.assertRaises(ValidationError):
            undo_review(self.item.pk, actor=self.reviewer)

    def test_the_api_undoes_and_names_the_restored_state(self):
        review_record(self.item.pk, state=R.DUPLICATE, actor=self.reviewer)
        api = APIClient()
        api.force_authenticate(self.reviewer)
        response = api.post(f"/api/staff/record-items/{self.item.pk}/undo-review/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["review_state"], "unreviewed")

    def test_a_record_with_active_redactions_is_not_undone(self):
        from apps.records.models import ExemptionCitation, Redaction

        review_record(self.item.pk, state=R.RESPONSIVE, actor=self.reviewer)
        Redaction.objects.create(
            record_item=self.item, page_number=1, x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=ExemptionCitation.objects.create(citation="RCW 42.56.250(4)", short_name="Phone"),
            created_by=self.reviewer,
        )
        with self.assertRaises(ValidationError):
            undo_review(self.item.pk, actor=self.reviewer)

    def test_a_superseded_prior_state_is_not_restored(self):
        review_record(self.item.pk, state=R.RESPONSIVE, actor=self.reviewer)
        row = AuditLog.objects.get(action="record.reviewed")
        AuditLog.objects.create(
            action="record.reviewed", target=row.target, actor=self.reviewer,
            detail={**row.detail, "before": {"state": "superseded"}},
        )
        with self.assertRaises(ValidationError):
            undo_review(self.item.pk, actor=self.reviewer)

    def test_the_api_reports_a_refused_undo_as_a_400(self):
        api = APIClient()
        api.force_authenticate(self.reviewer)
        response = api.post(f"/api/staff/record-items/{self.item.pk}/undo-review/")
        self.assertEqual(response.status_code, 400, response.content)

