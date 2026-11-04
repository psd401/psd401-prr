"""Bulk review by filter: one human decision over every record a filter shows.

The reviewer sees the exact count first; the decision is refused if the set
changed in between; records whose state needs its own judgement are skipped
and named, never silently overturned.
"""

import hashlib

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
)

URL = "/api/staff/record-items/bulk-review/"
SELECTION_URL = "/api/staff/record-items/bulk-selection/"


def _user(username, role):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [role])
    return user


class BulkReviewByFilterTests(TestCase):
    def setUp(self):
        self.reviewer = _user("filter-reviewer", StaffRole.REVIEWER)
        self.clerk = _user("filter-clerk", StaffRole.INTAKE_CLERK)
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Bulk review by filter.",
        )
        self.other = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="Another case.",
        )
        self.api = APIClient()
        self.api.force_authenticate(self.reviewer)

    def _item(self, index, *, domain="news.example.org", request=None, **fields):
        request = request or self.prr
        return RecordItem.objects.create(
            request=request,
            original_filename=f"Newsletter {index}.pdf",
            storage_key=f"records/{request.pk}/filter-{index}.pdf",
            content_length=10,
            content_type="application/pdf",
            sha256=hashlib.sha256(f"{request.pk}-filter-{index}".encode()).hexdigest(),
            email_sender_domain=domain,
            **fields,
        )

    def _filter(self, **extra):
        return {"request": self.prr.number, "sender_domain": "news.example.org", **extra}

    def _preview(self, state="non_responsive", **extra):
        return self.api.get(SELECTION_URL, {**self._filter(**extra), "state": state})

    def _confirm(self, preview, state="non_responsive", **extra):
        body = preview.json()
        return self.api.post(URL, {
            "filter": self._filter(**extra),
            "state": state,
            "expected_count": body["eligible_count"],
            "selection_sha256": body["selection_sha256"],
        }, format="json")

    def test_every_matching_record_is_decided_beyond_the_explicit_cap(self):
        items = [self._item(index) for index in range(205)]
        untouched = self._item(999, domain="psd401.net")

        preview = self._preview()
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.json()["matching"], 205)
        self.assertEqual(preview.json()["eligible_count"], 205)

        response = self._confirm(preview)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reviewed_count"], 205)
        states = set(
            RecordItem.objects.filter(pk__in=[item.pk for item in items])
            .values_list("review_state", flat=True)
        )
        self.assertEqual(states, {RecordItem.ReviewState.NON_RESPONSIVE})
        untouched.refresh_from_db()
        self.assertEqual(untouched.review_state, RecordItem.ReviewState.UNREVIEWED)

    def test_each_record_gets_its_own_audit_row_and_one_bulk_row(self):
        items = [self._item(index) for index in range(3)]

        self._confirm(self._preview())

        rows = AuditLog.objects.filter(action="record.reviewed")
        self.assertEqual({row.target for row in rows}, {str(item.pk) for item in items})
        for row in rows:
            self.assertEqual(row.actor, self.reviewer)
            self.assertEqual(row.detail["before"]["state"], "unreviewed")
            self.assertEqual(row.detail["review_state"], "non_responsive")
        bulk = AuditLog.objects.get(action="record.bulk_reviewed")
        self.assertEqual(bulk.detail["record_item_count"], 3)
        self.assertEqual(bulk.detail["selection"], "filter")
        self.assertEqual(bulk.detail["filter"]["sender_domain"], "news.example.org")
        for item in items:
            item.refresh_from_db()
            self.assertEqual(item.reviewed_by, self.reviewer)
            self.assertIsNotNone(item.reviewed_at)

    def test_records_needing_their_own_judgement_are_skipped_and_counted(self):
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.999", short_name="Test exemption",
        )
        eligible = self._item(1)
        withheld = self._item(
            2,
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=exemption,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
        )
        duplicate = self._item(3, review_state=RecordItem.ReviewState.DUPLICATE)
        superseded = self._item(4, review_state=RecordItem.ReviewState.SUPERSEDED)
        self._item(5, review_state=RecordItem.ReviewState.NON_RESPONSIVE)  # already decided
        redacted = self._item(
            6,
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
        )
        Redaction.objects.create(
            record_item=redacted, page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=exemption, created_by=self.reviewer,
        )

        preview = self._preview(review_state="")
        body = preview.json()
        self.assertEqual(body["matching"], 5)  # superseded is hidden by default
        self.assertEqual(body["eligible_count"], 1)
        self.assertEqual(body["excluded"], {
            "withheld": 1, "duplicate": 1, "superseded": 0,
            "already": 1, "redacted": 1,
        })

        response = self._confirm(preview, review_state="")

        self.assertEqual(response.json()["reviewed_count"], 1)
        eligible.refresh_from_db()
        self.assertEqual(eligible.review_state, RecordItem.ReviewState.NON_RESPONSIVE)
        for record, state in (
            (withheld, RecordItem.ReviewState.WITHHELD),
            (duplicate, RecordItem.ReviewState.DUPLICATE),
            (superseded, RecordItem.ReviewState.SUPERSEDED),
            (redacted, RecordItem.ReviewState.RESPONSIVE),
        ):
            record.refresh_from_db()
            self.assertEqual(record.review_state, state)

    def test_a_changed_set_is_refused_and_nothing_is_written(self):
        first = self._item(1)
        preview = self._preview()
        self._item(2)  # arrives between the preview and the confirmation

        response = self._confirm(preview)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "selection_changed")
        first.refresh_from_db()
        self.assertEqual(first.review_state, RecordItem.ReviewState.UNREVIEWED)
        self.assertFalse(AuditLog.objects.filter(action="record.reviewed").exists())

    def test_a_filter_must_name_one_request(self):
        self._item(1)
        response = self.api.post(URL, {
            "filter": {"sender_domain": "news.example.org"},
            "state": "non_responsive",
            "expected_count": 1,
            "selection_sha256": "0" * 64,
        }, format="json")
        self.assertEqual(response.status_code, 400)
        preview = self.api.get(SELECTION_URL, {
            "sender_domain": "news.example.org", "state": "non_responsive",
        })
        self.assertEqual(preview.status_code, 400)

    def test_another_requests_records_never_join_the_selection(self):
        self._item(1)
        stranger = self._item(2, request=self.other)

        self._confirm(self._preview())

        stranger.refresh_from_db()
        self.assertEqual(stranger.review_state, RecordItem.ReviewState.UNREVIEWED)

    def test_only_review_roles_may_preview_or_decide(self):
        self._item(1)
        preview = self._preview()
        self.api.force_authenticate(self.clerk)
        self.assertEqual(self._preview().status_code, 403)
        self.assertEqual(self._confirm(preview).status_code, 403)

    def test_a_responsive_decision_is_allowed_on_redacted_records(self):
        record = self._item(1, review_state=RecordItem.ReviewState.NON_RESPONSIVE)

        preview = self._preview(state="responsive")
        self.assertEqual(preview.json()["eligible_count"], 1)
        self._confirm(preview, state="responsive")

        record.refresh_from_db()
        self.assertEqual(record.review_state, RecordItem.ReviewState.RESPONSIVE)

    def test_explicit_ids_still_work_unchanged(self):
        item = self._item(1)
        response = self.api.post(URL, {
            "record_item_ids": [str(item.pk)], "state": "non_responsive",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reviewed_count"], 1)
