"""Batch approval with sampled QA, and the elusion test (issue #39).

A reviewer approves a cluster only after judging a random sample; one
disagreement blocks the batch and sends the rest back to per-record review.
In auto mode, records the AI calls non-responsive above the floor are staged
as Ready to discard, and nothing is applied until a reviewer finds no
responsive record in a random elusion sample. Responsive is never automatic.
"""

import hashlib

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    AdvisoryRun,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    ReviewBatch,
    ReviewDecision,
)
from apps.records.review_batches import (
    create_cluster_batch,
    create_discard_batch,
    decide_batch,
    discard_candidates,
)

R = RecordItem.ReviewState
BATCH = {"PRR_TRUST_RESPONSIVE_MODE": "batch", "PRR_TRUST_NON_RESPONSIVE_MODE": "batch",
         "PRR_TRUST_BATCH_SAMPLE_SIZE": 3}
AUTO = {"PRR_TRUST_NON_RESPONSIVE_MODE": "auto",
        "PRR_TRUST_NON_RESPONSIVE_AUTO_MIN_CONFIDENCE": 90,
        "PRR_TRUST_ELUSION_SAMPLE_SIZE": 4}


def _user(username, role):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [role])
    return user


class _Fixture(TestCase):
    def setUp(self):
        self.reviewer = _user("batch-reviewer", StaffRole.REVIEWER)
        self.clerk = _user("batch-clerk", StaffRole.INTAKE_CLERK)
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="batch@example.org"),
            description="Synthetic batch request.",
        )
        self._n = 0

    def _item(self, state=R.UNREVIEWED, *, ai=None, confidence=0.95, prr=None):
        prr = prr or self.prr
        self._n += 1
        item = RecordItem.objects.create(
            request=prr,
            original_filename=f"r{self._n}.eml",
            storage_key=f"records/{prr.pk}/r{self._n}",
            content_length=10,
            content_type="message/rfc822",
            sha256=hashlib.sha256(f"{prr.pk}-{self._n}".encode()).hexdigest(),
            review_state=state,
        )
        if ai:
            AdvisoryRun.objects.create(
                kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
                request=prr,
                record_item=item,
                model_identifier="synthetic-review-model",
                input_sha256=hashlib.sha256(str(item.pk).encode()).hexdigest(),
                state=AdvisoryRun.State.COMPLETE,
                requested_by=self.reviewer,
                proposal={"suggested_state": ai, "confidence": confidence,
                          "matched_terms": [], "rationale": "Synthetic."},
            )
        return item

    def _items(self, count, **kwargs):
        return [self._item(**kwargs) for _ in range(count)]


@override_settings(**BATCH)
class ClusterBatchTests(_Fixture):
    def test_a_sample_is_drawn_from_the_unreviewed_records(self):
        items = self._items(8)
        reviewed = self._item(R.RESPONSIVE)

        batch = create_cluster_batch(
            [i.pk for i in items] + [reviewed.pk],
            state=R.NON_RESPONSIVE, actor=self.reviewer, label="psd401.net",
        )

        self.assertEqual(batch.outcome, ReviewBatch.Outcome.OPEN)
        self.assertEqual(len(batch.record_item_ids), 8)
        self.assertNotIn(str(reviewed.pk), batch.record_item_ids)
        self.assertEqual(len(batch.sample_ids), 3)
        self.assertTrue(set(batch.sample_ids) <= set(batch.record_item_ids))
        self.assertTrue(AuditLog.objects.filter(action="review.batch_created").exists())
        # Opening a batch decides nothing.
        self.assertFalse(ReviewDecision.objects.exists())

    def test_full_agreement_applies_the_outcome_to_every_record_in_the_reviewers_name(self):
        items = self._items(8)
        batch = create_cluster_batch([i.pk for i in items], state=R.NON_RESPONSIVE, actor=self.reviewer)

        decided = decide_batch(
            batch.pk, actor=self.reviewer,
            judgments=dict.fromkeys(batch.sample_ids, R.NON_RESPONSIVE),
        )

        self.assertEqual(decided.outcome, ReviewBatch.Outcome.APPLIED)
        self.assertEqual(decided.applied_count, 8)
        self.assertEqual(RecordItem.objects.filter(review_state=R.NON_RESPONSIVE).count(), 8)
        self.assertEqual(set(RecordItem.objects.values_list("reviewed_by", flat=True)), {self.reviewer.pk})
        self.assertEqual(ReviewDecision.objects.filter(bulk=True).count(), 5)
        self.assertEqual(ReviewDecision.objects.filter(bulk=False).count(), 3)
        self.assertEqual(decided.judgments, dict.fromkeys(batch.sample_ids, R.NON_RESPONSIVE))
        row = AuditLog.objects.get(action="review.batch_decided")
        self.assertEqual(row.detail["outcome"], "applied")

    def test_one_disagreement_blocks_the_batch_and_records_only_the_sample(self):
        items = self._items(8)
        batch = create_cluster_batch([i.pk for i in items], state=R.NON_RESPONSIVE, actor=self.reviewer)
        judgments = dict.fromkeys(batch.sample_ids, R.NON_RESPONSIVE)
        judgments[batch.sample_ids[0]] = R.RESPONSIVE

        decided = decide_batch(batch.pk, actor=self.reviewer, judgments=judgments)

        self.assertEqual(decided.outcome, ReviewBatch.Outcome.BLOCKED)
        # The three sampled judgments were recorded, and are counted.
        self.assertEqual(decided.applied_count, 3)
        self.assertEqual(RecordItem.objects.get(pk=batch.sample_ids[0]).review_state, R.RESPONSIVE)
        self.assertEqual(RecordItem.objects.filter(review_state=R.NON_RESPONSIVE).count(), 2)
        self.assertEqual(RecordItem.objects.filter(review_state=R.UNREVIEWED).count(), 5)
        # Blocked means per-record review: the rest cannot be re-batched.
        with self.assertRaises(ValidationError):
            create_cluster_batch([i.pk for i in items], state=R.NON_RESPONSIVE, actor=self.reviewer)

    def test_every_sampled_record_must_be_judged_and_nothing_else(self):
        batch = create_cluster_batch([i.pk for i in self._items(5)], state=R.RESPONSIVE, actor=self.reviewer)
        for judgments in (
            dict.fromkeys(batch.sample_ids[:2], R.RESPONSIVE),
            {**dict.fromkeys(batch.sample_ids, R.RESPONSIVE), "not-sampled": R.RESPONSIVE},
            dict.fromkeys(batch.sample_ids, "withheld"),
        ):
            with self.subTest(judgments=judgments), self.assertRaises(ValidationError):
                decide_batch(batch.pk, actor=self.reviewer, judgments=judgments)

    def test_reopening_the_same_cluster_reuses_its_open_batch(self):
        ids = [i.pk for i in self._items(5)]
        first = create_cluster_batch(ids, state=R.NON_RESPONSIVE, actor=self.reviewer)
        again = create_cluster_batch(list(reversed(ids)), state=R.NON_RESPONSIVE, actor=self.reviewer)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.sample_ids, first.sample_ids)
        self.assertEqual(
            AuditLog.objects.get(action="review.batch_reopened").detail["batch_id"], first.pk,
        )
        other_outcome = create_cluster_batch(ids, state=R.RESPONSIVE, actor=self.reviewer)
        self.assertNotEqual(other_outcome.pk, first.pk)
        self.assertEqual(ReviewBatch.objects.filter(outcome=ReviewBatch.Outcome.OPEN).count(), 2)

    def test_a_batch_is_decided_once(self):
        batch = create_cluster_batch([i.pk for i in self._items(3)], state=R.RESPONSIVE, actor=self.reviewer)
        judgments = dict.fromkeys(batch.sample_ids, R.RESPONSIVE)
        decide_batch(batch.pk, actor=self.reviewer, judgments=judgments)
        with self.assertRaises(ValidationError):
            decide_batch(batch.pk, actor=self.reviewer, judgments=judgments)

    def test_a_record_reviewed_meanwhile_is_skipped_not_overturned(self):
        items = self._items(6)
        batch = create_cluster_batch([i.pk for i in items], state=R.NON_RESPONSIVE, actor=self.reviewer)
        other = next(pk for pk in batch.record_item_ids if pk not in batch.sample_ids)
        RecordItem.objects.filter(pk=other).update(review_state=R.RESPONSIVE)

        decided = decide_batch(
            batch.pk, actor=self.reviewer,
            judgments=dict.fromkeys(batch.sample_ids, R.NON_RESPONSIVE),
        )

        self.assertEqual(decided.skipped_count, 1)
        self.assertEqual(RecordItem.objects.get(pk=other).review_state, R.RESPONSIVE)

    def test_only_reviewers_and_one_case_and_unreviewed_records(self):
        items = self._items(2)
        other_case = PublicRecordsRequest.objects.create(
            requester=self.prr.requester, description="Another synthetic request.",
        )
        stranger = self._item(prr=other_case)
        with self.assertRaises(ValidationError):
            create_cluster_batch([i.pk for i in items], state=R.RESPONSIVE, actor=self.clerk)
        with self.assertRaises(ValidationError):
            create_cluster_batch([items[0].pk, stranger.pk], state=R.RESPONSIVE, actor=self.reviewer)
        with self.assertRaises(ValidationError):
            create_cluster_batch([self._item(R.RESPONSIVE).pk], state=R.RESPONSIVE, actor=self.reviewer)
        with self.assertRaises(ValidationError):
            create_cluster_batch([i.pk for i in items], state="withheld", actor=self.reviewer)

    @override_settings(PRR_TRUST_RESPONSIVE_MODE="auto")
    def test_responsive_is_never_automatic_whatever_is_stored(self):
        from apps.records.review_batches import trust_mode

        self.assertNotEqual(trust_mode(R.RESPONSIVE), "auto")

    @override_settings(PRR_TRUST_RESPONSIVE_MODE="suggest", PRR_TRUST_NON_RESPONSIVE_MODE="suggest")
    def test_suggest_mode_allows_no_batch(self):
        with self.assertRaises(ValidationError):
            create_cluster_batch([i.pk for i in self._items(3)], state=R.NON_RESPONSIVE, actor=self.reviewer)


@override_settings(**AUTO)
class DiscardBatchTests(_Fixture):
    def _pile(self):
        staged = self._items(10, ai="non_responsive", confidence=0.95)
        self._items(2, ai="non_responsive", confidence=0.6)  # below the floor
        self._items(2, ai="responsive", confidence=0.99)  # never discarded
        self._item(R.NON_RESPONSIVE, ai="non_responsive")  # already decided
        return staged

    def test_only_high_confidence_non_responsive_unreviewed_records_are_staged(self):
        staged = self._pile()
        self.assertEqual(
            {str(i.pk) for i in discard_candidates(self.prr)}, {str(i.pk) for i in staged},
        )

    def test_a_clean_elusion_sample_applies_the_discard(self):
        self._pile()
        batch = create_discard_batch(self.prr, actor=self.reviewer)
        self.assertEqual(batch.kind, ReviewBatch.Kind.DISCARD)
        self.assertEqual(len(batch.sample_ids), 4)
        self.assertEqual(batch.min_confidence, 0.9)
        self.assertFalse(ReviewDecision.objects.exists())

        decided = decide_batch(
            batch.pk, actor=self.reviewer,
            judgments=dict.fromkeys(batch.sample_ids, R.NON_RESPONSIVE),
        )

        self.assertEqual(decided.outcome, ReviewBatch.Outcome.APPLIED)
        self.assertEqual(decided.applied_count, 10)
        self.assertEqual(
            AuditLog.objects.get(action="review.batch_decided").detail["elusion_responsive_found"], 0,
        )

    def test_reopening_the_same_pile_reuses_its_open_batch(self):
        self._pile()
        first = create_discard_batch(self.prr, actor=self.reviewer)
        self.assertEqual(create_discard_batch(self.prr, actor=self.reviewer).pk, first.pk)
        # A pile that changed is a different batch.
        self._item(ai="non_responsive", confidence=0.99)
        self.assertNotEqual(create_discard_batch(self.prr, actor=self.reviewer).pk, first.pk)

    def test_one_responsive_find_in_the_elusion_sample_blocks_the_discard(self):
        self._pile()
        batch = create_discard_batch(self.prr, actor=self.reviewer)
        judgments = dict.fromkeys(batch.sample_ids, R.NON_RESPONSIVE)
        judgments[batch.sample_ids[-1]] = R.RESPONSIVE

        decided = decide_batch(batch.pk, actor=self.reviewer, judgments=judgments)

        self.assertEqual(decided.outcome, ReviewBatch.Outcome.BLOCKED)
        self.assertEqual(
            AuditLog.objects.get(action="review.batch_decided").detail["elusion_responsive_found"], 1,
        )
        self.assertEqual(RecordItem.objects.filter(review_state=R.UNREVIEWED).count(), 10)
        # A failed elusion check cannot be retried on the same pile.
        with self.assertRaises(ValidationError):
            create_discard_batch(self.prr, actor=self.reviewer)

    @override_settings(PRR_TRUST_NON_RESPONSIVE_MODE="batch")
    def test_nothing_is_staged_unless_non_responsive_is_auto(self):
        self._pile()
        self.assertEqual(list(discard_candidates(self.prr)), [])
        with self.assertRaises(ValidationError):
            create_discard_batch(self.prr, actor=self.reviewer)

    def test_the_database_refuses_a_responsive_discard(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReviewBatch.objects.create(
                request=self.prr, kind=ReviewBatch.Kind.DISCARD,
                proposed_state=R.RESPONSIVE, record_item_ids=[], sample_ids=[],
                created_by=self.reviewer,
            )


@override_settings(**BATCH, **{k: v for k, v in AUTO.items() if k != "PRR_TRUST_NON_RESPONSIVE_MODE"})
class ApiTests(_Fixture):
    def test_a_reviewer_opens_and_decides_a_cluster_batch(self):
        api = APIClient()
        api.force_authenticate(self.reviewer)
        items = self._items(4)

        policy = api.get("/api/staff/review-batches/policy/", {"request": self.prr.number})
        self.assertEqual(policy.status_code, 200, policy.content)
        self.assertEqual(policy.json()["non_responsive"], "batch")
        self.assertEqual(policy.json()["discard_candidates"], 0)

        created = api.post(
            "/api/staff/review-batches/",
            {"record_item_ids": [str(i.pk) for i in items], "state": "responsive", "cluster_label": "Thread"},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual(len(body["samples"]), 3)
        self.assertIn("original_filename", body["samples"][0])

        decided = api.post(
            f"/api/staff/review-batches/{body['id']}/decide/",
            {"judgments": {s["id"]: "responsive" for s in body["samples"]}},
            format="json",
        )
        self.assertEqual(decided.status_code, 200, decided.content)
        self.assertEqual(decided.json()["outcome"], "applied")

    @override_settings(PRR_TRUST_NON_RESPONSIVE_MODE="auto")
    def test_a_reviewer_opens_and_signs_off_the_discard_pile(self):
        api = APIClient()
        api.force_authenticate(self.reviewer)
        self._items(5, ai="non_responsive", confidence=0.97)

        opened = api.post(
            "/api/staff/review-batches/discard/", {"request": self.prr.number}, format="json",
        )
        self.assertEqual(opened.status_code, 201, opened.content)
        body = opened.json()
        self.assertEqual(body["kind"], "discard")
        self.assertEqual(len(body["samples"]), 4)

        decided = api.post(
            f"/api/staff/review-batches/{body['id']}/decide/",
            {"judgments": {s["id"]: "non_responsive" for s in body["samples"]}},
            format="json",
        )
        self.assertEqual(decided.status_code, 200, decided.content)
        self.assertEqual(decided.json()["applied_count"], 5)

    def test_a_token_caller_is_handed_off_and_decides_nothing(self):
        from rest_framework.authtoken.models import Token

        batch = create_cluster_batch(
            [i.pk for i in self._items(3)], state=R.RESPONSIVE, actor=self.reviewer,
        )
        token = Token.objects.create(user=self.reviewer)
        response = APIClient().post(
            f"/api/staff/review-batches/{batch.pk}/decide/",
            {"judgments": dict.fromkeys(batch.sample_ids, "responsive")},
            format="json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertEqual(response.json()["handoff"]["capability_id"], "review_batches.decide")
        batch.refresh_from_db()
        self.assertEqual(batch.outcome, ReviewBatch.Outcome.OPEN)
        self.assertFalse(ReviewDecision.objects.exists())

    def test_an_intake_clerk_may_not(self):
        api = APIClient()
        api.force_authenticate(self.clerk)
        self.assertEqual(
            api.get("/api/staff/review-batches/policy/", {"request": self.prr.number}).status_code, 403,
        )
