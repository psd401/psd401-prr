"""The estimate engine (issue #41): a responsive range from AI labels
corrected by measured agreement, the fee from the adopted schedule, and a
completion date from district throughput over the WA business-day calendar.

Dates are real Washington dates: Monday 2026-11-09 is the estimate day and
Wednesday 2026-11-11 (Veterans Day) is a state holiday.
"""

import datetime as dt
import hashlib
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.estimates import estimate_request, wilson_interval
from apps.records.fees import activate_fee_schedule
from apps.records.models import (
    AdvisoryRun,
    ExemptionCitation,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    RecordsEstimate,
    Requester,
    RequestStatus,
    ReviewDecision,
)

PACIFIC = ZoneInfo("America/Los_Angeles")
AS_OF = dt.date(2026, 11, 9)
MB = 1_000_000


def _at(day, hour=10):
    return dt.datetime.combine(day, dt.time(hour), tzinfo=PACIFIC)


class _Fixture(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(
            username="est-officer", email="est-officer@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])
        self.prr = self._case(received=_at(dt.date(2026, 11, 2)))
        self._sequence = 0

    def _case(self, *, received, status=RequestStatus.ACKNOWLEDGED, **fields):
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="est@example.org"),
            description="Synthetic estimate request.",
            received_at=received,
            **fields,
        )
        PublicRecordsRequest.objects.filter(pk=prr.pk).update(status=status)
        prr.refresh_from_db()
        return prr

    def _item(self, prr, state=RecordItem.ReviewState.UNREVIEWED, *, size=MB, ai=None):
        self._sequence += 1
        item = RecordItem.objects.create(
            request=prr,
            original_filename=f"r{self._sequence}.pdf",
            storage_key=f"records/{prr.pk}/r{self._sequence}",
            content_length=size,
            content_type="application/pdf",
            sha256=hashlib.sha256(f"{prr.pk}{self._sequence}".encode()).hexdigest(),
            review_state=state,
            withholding_exemption=(
                ExemptionCitation.objects.get_or_create(
                    citation="RCW 42.56.230(1)", short_name="Personal information",
                )[0]
                if state == RecordItem.ReviewState.WITHHELD else None
            ),
        )
        if ai:
            self._run(item, ai)
        return item

    def _run(self, item, label):
        return AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            request=item.request,
            record_item=item,
            model_identifier="synthetic-review-model",
            input_sha256=hashlib.sha256(str(item.pk).encode()).hexdigest(),
            state=AdvisoryRun.State.COMPLETE,
            requested_by=self.officer,
            proposal={"suggested_state": label, "confidence": 0.9,
                      "matched_terms": [], "rationale": "Synthetic."},
        )

    def _history(self, label, human_state, count):
        """Past decisions on another case: what people decided after the AI
        suggested ``label``."""
        other = getattr(self, "_history_case", None) or self._case(
            received=_at(dt.date(2026, 9, 1)), status=RequestStatus.FULFILLED,
        )
        self._history_case = other
        for _ in range(count):
            item = self._item(other, human_state)
            ReviewDecision.objects.create(
                record_item=item, request=other, review_state=human_state,
                advisory_run=self._run(item, label), advisory_label=label,
                advisory_confidence=0.9, actor=self.officer,
            )

    def _schedule(self):
        return activate_fee_schedule(
            actor=self.officer,
            name="Synthetic statutory schedule",
            basis="statutory_default",
            effective_on=dt.date(2026, 1, 1),
            counsel_approval_reference="Synthetic counsel approval 2026",
            adoption_reference="Synthetic board adoption 2026",
        )

    def _reviews(self, day, count):
        with patch("django.utils.timezone.now", return_value=_at(day)):
            for _ in range(count):
                AuditLog.objects.create(action="record.reviewed", target="synthetic")


class WilsonTests(TestCase):
    def test_no_evidence_is_the_whole_range(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_the_interval_brackets_the_observed_rate(self):
        low, high = wilson_interval(9, 10)
        self.assertLess(low, 0.9)
        self.assertGreater(high, 0.9)
        self.assertLessEqual(high, 1.0)


class ResponsiveRangeTests(_Fixture):
    def test_reviewed_records_are_certain_and_unreviewed_span_their_group(self):
        state = RecordItem.ReviewState
        self._item(self.prr, state.RESPONSIVE)
        self._item(self.prr, state.WITHHELD)
        self._item(self.prr, state.NON_RESPONSIVE)
        for _ in range(3):
            self._item(self.prr, ai="responsive")
        self._item(self.prr)  # no suggestion

        result = estimate_request(self.prr, actor=self.officer, as_of=AS_OF).result

        # No agreement history: an AI label carries no evidence yet, so every
        # unreviewed record may or may not be responsive.
        self.assertEqual(result["responsive"]["low"], 2)
        self.assertEqual(result["responsive"]["high"], 6)
        self.assertEqual(result["responsive"]["reviewed_responsive"], 2)
        self.assertEqual(result["responsive"]["unreviewed"], 4)

    def test_measured_agreement_narrows_the_range(self):
        self._history("responsive", RecordItem.ReviewState.RESPONSIVE, 19)
        self._history("responsive", RecordItem.ReviewState.NON_RESPONSIVE, 1)
        self._history("non_responsive", RecordItem.ReviewState.NON_RESPONSIVE, 20)
        for _ in range(10):
            self._item(self.prr, ai="responsive")
        for _ in range(10):
            self._item(self.prr, ai="non_responsive")

        estimate = estimate_request(self.prr, actor=self.officer, as_of=AS_OF)
        responsive = estimate.result["responsive"]

        rates = estimate.inputs["agreement_rates"]
        self.assertEqual(rates["responsive"], {"decisions": 20, "responsive": 19})
        self.assertEqual(rates["non_responsive"], {"decisions": 20, "responsive": 0})
        # 10 x 0.95 + 10 x 0.0
        self.assertEqual(responsive["expected"], 10)
        self.assertGreaterEqual(responsive["low"], 7)
        self.assertLessEqual(responsive["high"], 12)
        self.assertLess(responsive["low"], responsive["high"])

    def test_superseded_and_duplicate_records_are_not_counted(self):
        self._item(self.prr, RecordItem.ReviewState.SUPERSEDED)
        self._item(self.prr, RecordItem.ReviewState.DUPLICATE)
        result = estimate_request(self.prr, actor=self.officer, as_of=AS_OF).result
        self.assertEqual(result["responsive"]["high"], 0)


class FeeTests(_Fixture):
    def test_fee_lines_use_the_adopted_rates_for_released_records_only(self):
        schedule = self._schedule()
        for _ in range(5):
            self._item(self.prr, RecordItem.ReviewState.RESPONSIVE, size=200 * MB)
        self._item(self.prr, RecordItem.ReviewState.WITHHELD, size=900 * MB)

        estimate = estimate_request(self.prr, actor=self.officer, as_of=AS_OF)
        fee = estimate.result["fee"]["expected"]

        lines = {line["item"]: line for line in fee["lines"]}
        # Five files are two bundles of four at $0.05; 1.0 GB at $0.10.
        self.assertEqual(lines["electronic_file_bundles"]["quantity"], "2")
        self.assertEqual(lines["electronic_file_bundles"]["amount"], "0.10")
        self.assertEqual(lines["electronic_gb"]["quantity"], "1.000")
        self.assertEqual(lines["electronic_gb"]["amount"], "0.10")
        self.assertEqual(lines["scanned_pages"]["amount"], "0.00")
        self.assertEqual(fee["total"], "0.20")
        self.assertEqual(fee["deposit"], "0.02")
        self.assertFalse(fee["flat_fee_available"])
        self.assertEqual(estimate.inputs["fee_schedule"]["id"], str(schedule.pk))
        self.assertEqual(estimate.inputs["fee_schedule"]["electronic_gb_rate"], "0.1000")

    def test_the_deposit_never_exceeds_ten_percent(self):
        self._schedule()
        self._item(self.prr, RecordItem.ReviewState.RESPONSIVE)
        with self.assertRaises(ValidationError):
            estimate_request(
                self.prr, actor=self.officer, as_of=AS_OF, deposit_percent=Decimal("10.5"),
            )

    def test_no_adopted_schedule_means_no_fee_rather_than_a_guess(self):
        self._item(self.prr, RecordItem.ReviewState.RESPONSIVE)
        estimate = estimate_request(self.prr, actor=self.officer, as_of=AS_OF)
        self.assertIsNone(estimate.result["fee"])
        self.assertIn("No fee schedule", estimate.result["notes"][0])


class CompletionDateTests(_Fixture):
    def test_backlog_ahead_divided_by_throughput_on_the_business_calendar(self):
        # 40 reviews inside the trailing 20-business-day window = 2 per day;
        # one review well outside it is ignored.
        self._reviews(dt.date(2026, 11, 2), 40)
        self._reviews(dt.date(2026, 8, 3), 1)
        earlier = self._case(received=_at(dt.date(2026, 10, 20)))
        for _ in range(6):
            self._item(earlier)
        later = self._case(received=_at(dt.date(2026, 11, 5)))
        self._item(later)
        closed = self._case(received=_at(dt.date(2026, 10, 1)), status=RequestStatus.FULFILLED)
        self._item(closed)
        for _ in range(4):
            self._item(self.prr)

        estimate = estimate_request(self.prr, actor=self.officer, as_of=AS_OF)

        timeline = estimate.inputs["timeline"]
        self.assertEqual(timeline["reviews_in_window"], 40)
        self.assertEqual(timeline["window_business_days"], 20)
        self.assertEqual(timeline["backlog_ahead"], 6)
        self.assertEqual(timeline["case_unreviewed"], 4)
        # 10 records / 2 per day = 5 business days after Mon 2026-11-09,
        # skipping Veterans Day (Wed 11-11): 11-10, 11-12, 11-13, 11-16, 11-17.
        self.assertEqual(estimate.result["completion_date"], "2026-11-17")

    def test_no_measured_throughput_gives_no_date(self):
        self._item(self.prr)
        estimate = estimate_request(self.prr, actor=self.officer, as_of=AS_OF)
        self.assertIsNone(estimate.result["completion_date"])
        self.assertTrue(any("throughput" in note for note in estimate.result["notes"]))


class StorageAndScopeTests(_Fixture):
    def test_every_input_is_stored_and_audited(self):
        self._item(self.prr, RecordItem.ReviewState.RESPONSIVE)
        estimate = estimate_request(self.prr, actor=self.officer, as_of=AS_OF)
        stored = RecordsEstimate.objects.get()
        self.assertEqual(stored.pk, estimate.pk)
        self.assertEqual(stored.as_of, AS_OF)
        for key in ("record_counts", "agreement_rates", "fee_schedule", "timeline"):
            self.assertIn(key, stored.inputs)
        row = AuditLog.objects.get(action="records.estimate_computed")
        self.assertEqual(row.target, self.prr.number)
        self.assertEqual(row.detail["estimate_id"], str(stored.pk))
        with self.assertRaises(ValidationError):
            stored.delete()

    def test_only_a_public_records_request_gets_an_rcw_estimate(self):
        litigation = self._case(
            received=_at(dt.date(2026, 11, 2)), matter_kind=MatterKind.LITIGATION_DISCOVERY,
        )
        with self.assertRaises(ValidationError):
            estimate_request(litigation, actor=self.officer, as_of=AS_OF)
