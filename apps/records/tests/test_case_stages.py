"""The case stage rail (#45): which stage a case is in, and why.

Stages run Intake, Scope, Collect, Review, Redact, Release, Close. The
current stage is the earliest one with open work; earlier stages read done
and later ones upcoming. A current stage that waits on the requester reads
blocked, with the reason. A closed case is done throughout.
"""

import hashlib
from collections import Counter
from unittest.mock import patch

from django.test import TestCase

from apps.records.models import (
    CollectionTask,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
)
from apps.records.stages import STAGE_IDS, case_stages, current_stages


class CaseStagesTests(TestCase):
    def setUp(self):
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="stages@example.org"),
            description="All board emails about the levy.",
            channel="portal",
            status=RequestStatus.ACKNOWLEDGED,
        )
        self.records = 0

    def _states(self):
        return {stage["id"]: stage["state"] for stage in case_stages(self.prr)}

    def _stage(self, stage_id):
        return next(s for s in case_stages(self.prr) if s["id"] == stage_id)

    def _task(self, state=CollectionTask.State.PENDING):
        return CollectionTask.objects.create(
            request=self.prr, kind=CollectionTask.Kind.VAULT_MAIL, state=state,
        )

    def _record(self, task=None, state=RecordItem.ReviewState.UNREVIEWED):
        self.records += 1
        return RecordItem.objects.create(
            request=self.prr,
            collection_task=task,
            original_filename=f"Message {self.records}.pdf",
            storage_key=f"records/{self.prr.pk}/stage-{self.records}.pdf",
            content_length=10,
            content_type="application/pdf",
            sha256=hashlib.sha256(f"stage-{self.records}".encode()).hexdigest(),
            review_state=state,
        )

    def test_stages_run_in_order(self):
        self.assertEqual(
            [stage["id"] for stage in case_stages(self.prr)],
            ["intake", "scope", "collect", "review", "redact", "release", "close"],
        )
        self.assertEqual(STAGE_IDS, tuple(s["id"] for s in case_stages(self.prr)))

    def test_a_received_case_is_at_intake(self):
        self.prr.status = RequestStatus.RECEIVED
        self.prr.save(update_fields=["status"])
        states = self._states()
        self.assertEqual(states["intake"], "current")
        self.assertEqual(states["scope"], "upcoming")
        self.assertEqual(states["close"], "upcoming")

    def test_only_a_public_records_case_is_read_as_owing_an_acknowledgment(self):
        # Rule 13 / ADR 0004: the acknowledgment is the RCW 42.56.520
        # response, which belongs to public_records alone. The rail itself
        # serves every kind, since collection through closure are shared.
        self.prr.status = RequestStatus.RECEIVED
        self.prr.save(update_fields=["status"])
        self.assertEqual(self._stage("intake")["summary"], "Received and not yet acknowledged")
        for kind in (MatterKind.LITIGATION_DISCOVERY, MatterKind.FERPA_PARENT):
            with self.subTest(kind=kind):
                other = PublicRecordsRequest.objects.create(
                    requester=Requester.objects.create(email=f"{kind}@example.org"),
                    description="A matter governed by other law.",
                    channel="portal",
                    status=RequestStatus.RECEIVED,
                    matter_kind=kind,
                )
                intake = case_stages(other)[0]
                self.assertEqual(intake["state"], "current")
                self.assertEqual(intake["summary"], "Received and not yet triaged")
                self.assertNotIn("acknowledg", " ".join(
                    f"{stage['summary']} {stage['reason']}" for stage in case_stages(other)
                ))

    def test_an_acknowledged_case_with_no_collection_is_at_scope(self):
        states = self._states()
        self.assertEqual(states["intake"], "done")
        self.assertEqual(states["scope"], "current")
        self.assertEqual(states["collect"], "upcoming")

    def test_awaiting_clarification_blocks_scope_with_the_reason(self):
        self.prr.status = RequestStatus.CLARIFICATION
        self.prr.save(update_fields=["status"])
        scope = self._stage("scope")
        self.assertEqual(scope["state"], "blocked")
        self.assertIn("clarification", scope["reason"])

    def test_an_open_collection_makes_collect_current(self):
        self._task()
        self._task(CollectionTask.State.COMPLETE)
        states = self._states()
        self.assertEqual(states["scope"], "done")
        self.assertEqual(states["collect"], "current")
        collect = self._stage("collect")
        self.assertEqual(collect["open_count"], 1)
        self.assertEqual(collect["summary"], "1 open collection")

    def test_unreviewed_records_make_review_current(self):
        task = self._task(CollectionTask.State.COMPLETE)
        self._record(task)
        self._record(task)
        self._record(task, RecordItem.ReviewState.RESPONSIVE)
        states = self._states()
        self.assertEqual(states["collect"], "done")
        self.assertEqual(states["review"], "current")
        self.assertEqual(self._stage("review")["summary"], "2 records to review")

    def test_later_stages_still_report_their_open_work(self):
        # Records arrive while a collection is still running: Collect is the
        # current stage, and Review says how much is already waiting.
        task = self._task(CollectionTask.State.INGESTING)
        self._record(task)
        states = self._states()
        self.assertEqual(states["collect"], "current")
        self.assertEqual(states["review"], "upcoming")
        self.assertEqual(self._stage("review")["open_count"], 1)

    def test_superseded_records_are_not_review_work(self):
        task = self._task(CollectionTask.State.COMPLETE)
        self._record(task, RecordItem.ReviewState.SUPERSEDED)
        self.assertEqual(self._stage("review")["open_count"], 0)

    def test_pending_redaction_proposals_make_redact_current(self):
        task = self._task(CollectionTask.State.COMPLETE)
        self._record(task, RecordItem.ReviewState.RESPONSIVE)
        with patch("apps.records.stages._pending_proposals", return_value=Counter({self.prr.pk: 3})):
            states = self._states()
            redact = self._stage("redact")
        self.assertEqual(states["redact"], "current")
        self.assertEqual(redact["summary"], "3 redaction proposals to decide")

    def test_unreleased_responsive_records_make_release_current(self):
        task = self._task(CollectionTask.State.COMPLETE)
        self._record(task, RecordItem.ReviewState.RESPONSIVE)
        states = self._states()
        self.assertEqual(states["redact"], "done")
        self.assertEqual(states["release"], "current")
        self.assertEqual(self._stage("release")["summary"], "1 responsive record not yet released")

    def test_an_open_deposit_blocks_release(self):
        task = self._task(CollectionTask.State.COMPLETE)
        self._record(task, RecordItem.ReviewState.RESPONSIVE)
        with patch("apps.records.stages._open_deposit_gates", return_value=Counter({self.prr.pk: 1})):
            release = self._stage("release")
        self.assertEqual(release["state"], "blocked")
        self.assertIn("deposit", release["reason"])

    def test_nothing_left_makes_close_current(self):
        task = self._task(CollectionTask.State.COMPLETE)
        self._record(task, RecordItem.ReviewState.NON_RESPONSIVE)
        states = self._states()
        self.assertEqual(states["release"], "done")
        self.assertEqual(states["close"], "current")

    def test_a_closed_case_is_done_throughout(self):
        self._task()
        self.prr.status = RequestStatus.CLOSED_WITHDRAWN
        self.prr.save(update_fields=["status"])
        self.assertEqual(set(self._states().values()), {"done"})

    def test_current_stages_match_each_case_rail(self):
        # The Today list reads one stage per case for a whole page; it must
        # be the same stage the case's own rail calls current.
        received = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="received@example.org"),
            description="A new request.",
            channel="portal",
            status=RequestStatus.RECEIVED,
        )
        reviewing = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="review@example.org"),
            description="A request in review.",
            channel="portal",
            status=RequestStatus.ACKNOWLEDGED,
        )
        task = CollectionTask.objects.create(
            request=reviewing, kind=CollectionTask.Kind.VAULT_MAIL,
            state=CollectionTask.State.COMPLETE,
        )
        RecordItem.objects.create(
            request=reviewing, collection_task=task,
            original_filename="One.pdf", storage_key="records/x/one.pdf",
            content_length=10, content_type="application/pdf",
            sha256=hashlib.sha256(b"one").hexdigest(),
        )
        closed = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="closed@example.org"),
            description="A finished request.",
            channel="portal",
            status=RequestStatus.CLOSED_WITHDRAWN,
        )
        cases = [self.prr, received, reviewing, closed]

        current = current_stages(cases)

        for case in cases:
            with self.subTest(case=case.number):
                rail = case_stages(case)
                expected = next(
                    (s for s in rail if s["state"] in {"current", "blocked"}),
                    rail[-1],
                )
                self.assertEqual(current[case.pk], expected)
        self.assertEqual(current[reviewing.pk]["summary"], "1 record to review")
        self.assertEqual(current[closed.pk]["state"], "done")

    def test_current_stages_cost_the_same_for_one_case_or_many(self):
        others = [
            PublicRecordsRequest.objects.create(
                requester=Requester.objects.create(email=f"many{i}@example.org"),
                description="Another request.",
                channel="portal",
                status=RequestStatus.ACKNOWLEDGED,
            )
            for i in range(4)
        ]
        with self.assertNumQueries(6):
            current_stages([self.prr])
        with self.assertNumQueries(6):
            current_stages([self.prr, *others])

    def test_every_stage_has_a_label_and_reason_key(self):
        for stage in case_stages(self.prr):
            self.assertTrue(stage["label"])
            self.assertIn("reason", stage)
            self.assertIn("summary", stage)
