"""Stored AI-human agreement and record clusters (issue #38).

Every human responsiveness decision stores the suggestion that was on screen,
so agreement between model and people is measured instead of guessed. Records
group into clusters a reviewer can judge together: one email thread, one
sender or list, one tag, one AI label and confidence band.
"""

import hashlib
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.agreement import agreement_report, confidence_band
from apps.records.clusters import cluster_records
from apps.records.models import (
    AdvisoryRun,
    ExemptionCitation,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    ReviewDecision,
)
from apps.records.review import (
    bulk_review_confirmed_selection,
    bulk_review_records,
    bulk_review_selection,
    review_record,
)


def _user(username, role):
    user = get_user_model().objects.create(
        username=username, email=f"{username}@psd401.net", is_staff=True,
    )
    apply_staff_roles(user, [role])
    return user


class _Fixture(TestCase):
    def setUp(self):
        self.reviewer = _user("agree-reviewer", StaffRole.REVIEWER)
        self.officer = _user("agree-officer", StaffRole.RECORDS_OFFICER)
        self.prr = self._case()

    def _case(self):
        return PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="agree@example.org"),
            description="Synthetic agreement request.",
        )

    def _item(self, name, prr=None, **email):
        prr = prr or self.prr
        return RecordItem.objects.create(
            request=prr,
            original_filename=name,
            storage_key=f"records/{prr.pk}/{name}",
            content_length=10,
            content_type="message/rfc822" if email else "application/pdf",
            sha256=hashlib.sha256(f"{prr.pk}-{name}".encode()).hexdigest(),
            **email,
        )

    def _run(self, item, label, confidence, *, state=AdvisoryRun.State.COMPLETE, age=0):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            request=item.request,
            record_item=item,
            model_identifier="synthetic-review-model",
            input_sha256=hashlib.sha256(f"{item.pk}{label}{age}".encode()).hexdigest(),
            state=state,
            requested_by=self.reviewer,
            proposal={
                "suggested_state": label, "confidence": confidence,
                "matched_terms": [], "rationale": "Synthetic.",
            },
        )
        if age:
            AdvisoryRun.objects.filter(pk=run.pk).update(
                created_at=timezone.now() - timedelta(minutes=age),
            )
        return run


class ReviewDecisionTests(_Fixture):
    def test_a_single_review_stores_the_suggestion_that_was_shown(self):
        item = self._item("memo.pdf")
        self._run(item, "non_responsive", 0.4, age=10)
        shown = self._run(item, "responsive", 0.93)
        self._run(item, "non_responsive", 0.99, state=AdvisoryRun.State.FAILED)

        review_record(item.pk, state=RecordItem.ReviewState.RESPONSIVE, actor=self.reviewer)

        decision = ReviewDecision.objects.get()
        self.assertEqual(decision.advisory_run, shown)
        self.assertEqual(decision.advisory_label, "responsive")
        self.assertEqual(decision.advisory_confidence, 0.93)
        self.assertEqual(decision.review_state, RecordItem.ReviewState.RESPONSIVE)
        self.assertFalse(decision.bulk)
        self.assertEqual(decision.actor, self.reviewer)

    def test_a_review_with_no_suggestion_still_stores_the_decision(self):
        item = self._item("memo.pdf")
        review_record(item.pk, state=RecordItem.ReviewState.NON_RESPONSIVE, actor=self.reviewer)
        decision = ReviewDecision.objects.get()
        self.assertIsNone(decision.advisory_run)
        self.assertEqual(decision.advisory_label, "")
        self.assertIsNone(decision.advisory_confidence)

    def test_bulk_review_stores_each_decision_and_lists_the_runs_shown(self):
        first, second, third = (self._item(f"r{index}.pdf") for index in range(3))
        run_one = self._run(first, "non_responsive", 0.9)
        run_two = self._run(second, "non_responsive", 0.8)

        bulk_review_records(
            [first.pk, second.pk, third.pk],
            state=RecordItem.ReviewState.NON_RESPONSIVE,
            actor=self.reviewer,
        )

        self.assertEqual(ReviewDecision.objects.filter(bulk=True).count(), 3)
        row = AuditLog.objects.get(action="record.bulk_reviewed")
        self.assertEqual(
            sorted(row.detail["advisory_run_ids"]), sorted([str(run_one.pk), str(run_two.pk)]),
        )

    def test_a_filter_decision_stores_each_decision_and_lists_the_runs_shown(self):
        first, second = self._item("f1.pdf"), self._item("f2.pdf")
        shown = self._run(first, "responsive", 0.97)
        selection = bulk_review_selection(
            [first.pk, second.pk], state=RecordItem.ReviewState.RESPONSIVE,
        )

        bulk_review_confirmed_selection(
            [first.pk, second.pk],
            state=RecordItem.ReviewState.RESPONSIVE,
            actor=self.reviewer,
            expected_count=len(selection.eligible_ids),
            selection_sha256=selection.selection_sha256,
            filter_detail={"request": self.prr.number},
        )

        decisions = {d.record_item_id: d for d in ReviewDecision.objects.filter(bulk=True)}
        self.assertEqual(decisions[first.pk].advisory_run, shown)
        self.assertEqual(decisions[first.pk].advisory_label, "responsive")
        self.assertEqual(decisions[first.pk].advisory_confidence, 0.97)
        self.assertIsNone(decisions[second.pk].advisory_run)
        row = AuditLog.objects.get(action="record.bulk_reviewed")
        self.assertEqual(row.detail["advisory_run_ids"], [str(shown.pk)])

    def test_decisions_are_append_only(self):
        item = self._item("memo.pdf")
        review_record(item.pk, state=RecordItem.ReviewState.RESPONSIVE, actor=self.reviewer)
        review_record(item.pk, state=RecordItem.ReviewState.NON_RESPONSIVE, actor=self.reviewer)
        self.assertEqual(ReviewDecision.objects.count(), 2)
        decision = ReviewDecision.objects.order_by("created_at")[0]
        with self.assertRaises(ValidationError):
            decision.delete()
        with self.assertRaises(ValidationError):
            decision.save()


class AgreementReportTests(_Fixture):
    def _decide(self, name, label, confidence, state, prr=None):
        item = self._item(name, prr=prr)
        if label:
            self._run(item, label, confidence)
        exemptions = ()
        if state == RecordItem.ReviewState.WITHHELD:
            exemptions = [ExemptionCitation.objects.get_or_create(
                citation="RCW 42.56.230(1)", short_name="Personal information",
            )[0].pk]
        review_record(
            item.pk, state=state, actor=self.reviewer, withholding_exemption_ids=exemptions,
        )
        return item

    def test_bands_cover_the_whole_range(self):
        self.assertEqual(confidence_band(0.95), "90-100%")
        self.assertEqual(confidence_band(0.9), "90-100%")
        self.assertEqual(confidence_band(0.75), "70-89%")
        self.assertEqual(confidence_band(0.5), "50-69%")
        self.assertEqual(confidence_band(0.1), "below 50%")

    def test_agreement_is_counted_by_label_and_band(self):
        state = RecordItem.ReviewState
        self._decide("a.pdf", "responsive", 0.95, state.RESPONSIVE)
        self._decide("b.pdf", "responsive", 0.92, state.WITHHELD)  # withheld is responsive
        self._decide("c.pdf", "responsive", 0.91, state.NON_RESPONSIVE)
        self._decide("d.pdf", "non_responsive", 0.6, state.NON_RESPONSIVE)
        self._decide("e.pdf", "uncertain", 0.3, state.RESPONSIVE)
        self._decide("f.pdf", "responsive", 0.95, state.DUPLICATE)  # not a responsiveness call
        self._decide("g.pdf", None, None, state.RESPONSIVE)  # nothing shown

        report = agreement_report()

        rows = {(row["label"], row["band"]): row for row in report["rows"]}
        self.assertEqual(rows[("responsive", "90-100%")]["decisions"], 3)
        self.assertEqual(rows[("responsive", "90-100%")]["agreed"], 2)
        self.assertEqual(rows[("non_responsive", "50-69%")]["agreed"], 1)
        self.assertEqual(rows[("uncertain", "below 50%")]["decisions"], 1)
        self.assertIsNone(rows[("uncertain", "below 50%")]["agreement"])
        self.assertEqual(report["decisions"], 5)
        self.assertEqual(report["agreed"], 3)
        self.assertEqual(report["without_suggestion"], 1)

    def test_only_the_latest_decision_on_a_record_counts(self):
        item = self._decide("a.pdf", "responsive", 0.95, RecordItem.ReviewState.NON_RESPONSIVE)
        review_record(item.pk, state=RecordItem.ReviewState.RESPONSIVE, actor=self.reviewer)
        report = agreement_report()
        self.assertEqual(report["decisions"], 1)
        self.assertEqual(report["agreed"], 1)

    def test_a_case_report_counts_only_that_case(self):
        other = self._case()
        self._decide("a.pdf", "responsive", 0.95, RecordItem.ReviewState.RESPONSIVE)
        self._decide("b.pdf", "responsive", 0.95, RecordItem.ReviewState.RESPONSIVE, prr=other)
        self.assertEqual(agreement_report(request=self.prr)["decisions"], 1)
        self.assertEqual(agreement_report()["decisions"], 2)


class ClusterTests(_Fixture):
    def test_a_reply_chain_is_one_thread(self):
        root = self._item("root.eml", email_message_id="<a@x>", email_sender_domain="x.org")
        reply = self._item(
            "reply.eml", email_message_id="<b@x>", email_in_reply_to="<a@x>",
            email_sender_domain="psd401.net",
        )
        reply_to_reply = self._item(
            "re-re.eml", email_message_id="<c@x>", email_in_reply_to="<b@x>",
        )
        other = self._item("other.eml", email_message_id="<z@x>")
        upload = self._item("scan.pdf")

        clusters = cluster_records(RecordItem.objects.all(), "thread")

        sizes = {frozenset(c["record_item_ids"]): c["size"] for c in clusters}
        self.assertEqual(sizes[frozenset(str(i.pk) for i in (root, reply, reply_to_reply))], 3)
        self.assertIn(frozenset([str(other.pk)]), sizes)
        not_email = next(c for c in clusters if c["key"] == "")
        self.assertEqual(not_email["record_item_ids"], [str(upload.pk)])
        self.assertEqual(clusters[0]["size"], 3)

    def test_a_mailing_list_outranks_the_sender_domain(self):
        self._item("n1.eml", email_sender_domain="vendor.example", email_list_id="news.vendor.example")
        self._item("n2.eml", email_sender_domain="mail.vendor.example", email_list_id="news.vendor.example")
        self._item("p.eml", email_sender_domain="psd401.net")
        clusters = cluster_records(RecordItem.objects.all(), "sender")
        keys = {c["key"]: c["size"] for c in clusters}
        self.assertEqual(keys, {"list:news.vendor.example": 2, "domain:psd401.net": 1})

    def test_tags_and_ai_label_clusters_report_the_majority_label(self):
        items = [
            self._item(f"auto{index}.eml", email_auto_submitted="auto-replied",
                       email_message_id=f"<auto{index}@x>")
            for index in range(3)
        ]
        self._run(items[0], "non_responsive", 0.95)
        self._run(items[1], "non_responsive", 0.92)
        self._run(items[2], "responsive", 0.4)
        queryset = RecordItem.objects.all()

        tag_cluster = next(c for c in cluster_records(queryset, "tag") if c["key"] == "auto_reply")
        self.assertEqual(tag_cluster["size"], 3)
        self.assertEqual(tag_cluster["ai_majority"], {"label": "non_responsive", "count": 2})
        self.assertEqual(len(tag_cluster["samples"]), 3)

        by_ai = {c["key"]: c["size"] for c in cluster_records(queryset, "ai_label")}
        self.assertEqual(by_ai, {"non_responsive:90-100%": 2, "responsive:below 50%": 1})

    def test_samples_are_capped_and_unknown_groupings_refused(self):
        for index in range(5):
            self._item(f"s{index}.eml", email_sender_domain="psd401.net")
        cluster = cluster_records(RecordItem.objects.all(), "sender")[0]
        self.assertEqual(cluster["size"], 5)
        self.assertEqual(len(cluster["samples"]), 3)
        with self.assertRaises(ValidationError):
            cluster_records(RecordItem.objects.all(), "colour")


class ApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.item = self._item("a.eml", email_sender_domain="psd401.net")
        self._run(self.item, "responsive", 0.95)
        review_record(self.item.pk, state=RecordItem.ReviewState.RESPONSIVE, actor=self.reviewer)

    def test_a_reviewer_groups_a_case_by_cluster(self):
        self.api.force_authenticate(self.reviewer)
        response = self.api.get(
            "/api/staff/record-items/clusters/",
            {"request": self.prr.number, "group_by": "sender"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        cluster = response.json()["clusters"][0]
        self.assertEqual(cluster["key"], "domain:psd401.net")
        self.assertEqual(cluster["samples"][0]["original_filename"], "a.eml")

    def test_an_unknown_grouping_is_a_400(self):
        self.api.force_authenticate(self.reviewer)
        response = self.api.get(
            "/api/staff/record-items/clusters/",
            {"request": self.prr.number, "group_by": "colour"},
        )
        self.assertEqual(response.status_code, 400)

    def test_an_officer_reads_agreement_district_wide_and_per_case(self):
        self.api.force_authenticate(self.officer)
        response = self.api.get("/api/staff/advisory-agreement/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["agreed"], 1)
        response = self.api.get("/api/staff/advisory-agreement/", {"request": self.prr.number})
        self.assertEqual(response.json()["decisions"], 1)
        response = self.api.get("/api/staff/advisory-agreement/", {"request": "PRR-1999-9999"})
        self.assertEqual(response.status_code, 404)

    def test_a_reviewer_may_not_read_agreement(self):
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get("/api/staff/advisory-agreement/").status_code, 403)


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="us.anthropic.claude-opus-5-5",
    PRR_AI_MODEL_RESPONSIVENESS="us.anthropic.claude-haiku-5-5",
)
class AutomaticReviewModelTests(_Fixture):
    def test_automatic_responsiveness_runs_on_the_review_model(self):
        from unittest.mock import patch

        from apps.records.advisory_ai import request_automatic_record_advisory

        item = self._item("memo.pdf")
        RecordItem.objects.filter(pk=item.pk).update(ocr_text="Synthetic memo text.")
        with patch("apps.records.advisory_ai._queue", side_effect=lambda run: run):
            run = request_automatic_record_advisory(
                item.pk, kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            )
        self.assertEqual(run.model_identifier, "us.anthropic.claude-haiku-5-5")
        self.assertEqual(run.trigger, AdvisoryRun.Trigger.AUTOMATIC)
