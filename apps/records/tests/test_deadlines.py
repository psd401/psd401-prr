"""Deadline engine tests — this math IS the legal compliance, so test it hard.
Calendar notes for the chosen dates:
  - Fri 2026-06-19 is Juneteenth (WA state holiday, RCW 1.16.050)
  - Fri 2026-07-03 is observed Independence Day
"""
import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.correspondence.models import ClarificationCycle, Correspondence
from apps.records import deadlines
from apps.records.models import StatusEvent

PT = ZoneInfo("America/Los_Angeles")


def make_records_officer(username):
    officer = get_user_model().objects.create_user(
        username,
        f"{username}@psd401.net",
        "pw",
        is_staff=True,
    )
    apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
    return officer


class BusinessDayTests(TestCase):
    def test_weekend_not_business_day(self):
        self.assertFalse(deadlines.is_business_day(dt.date(2026, 7, 4)))   # Sat
        self.assertFalse(deadlines.is_business_day(dt.date(2026, 7, 5)))   # Sun

    def test_wa_holidays_not_business_days(self):
        self.assertFalse(deadlines.is_business_day(dt.date(2026, 7, 3)))   # July 4 observed
        self.assertFalse(deadlines.is_business_day(dt.date(2026, 6, 19)))  # Juneteenth

    def test_add_business_days_skips_weekend_and_holiday(self):
        # Thu 6/18 + 2: Fri 6/19 is Juneteenth, skip weekend -> Mon 22, Tue 23
        self.assertEqual(
            deadlines.add_business_days(dt.date(2026, 6, 18), 2),
            dt.date(2026, 6, 23),
        )

    @override_settings(DISTRICT_HOLIDAYS=["2026-06-22"])
    def test_district_holiday_respected(self):
        # Thu 6/18 + 2: skip Juneteenth Fri, weekend, district-closure Mon -> Wed 24
        self.assertEqual(
            deadlines.add_business_days(dt.date(2026, 6, 18), 2),
            dt.date(2026, 6, 24),
        )


class StatutoryDueTests(TestCase):
    def test_five_business_days_from_receipt(self):
        received = dt.datetime(2026, 6, 15, 10, 0, tzinfo=PT)  # Mon morning
        # Counting from Tue 16: 16, 17, 18, (19 Juneteenth), 22, 23
        self.assertEqual(
            deadlines.statutory_response_due(received),
            dt.date(2026, 6, 23),
        )

    def test_after_hours_receipt_rolls_to_next_business_day(self):
        received = dt.datetime(2026, 6, 15, 18, 30, tzinfo=PT)  # Mon 6:30pm
        # Treated as received Tue 16 -> 17, 18, (19), 22, 23, 24
        self.assertEqual(
            deadlines.statutory_response_due(received),
            dt.date(2026, 6, 24),
        )

    def test_tolling_extends_deadline(self):
        received = dt.datetime(2026, 6, 15, 10, 0, tzinfo=PT)
        # 5 + 3 tolled = 8 business days: ...23, 24, 25, 26
        self.assertEqual(
            deadlines.statutory_response_due(received, tolled_business_days=3),
            dt.date(2026, 6, 26),
        )

    def test_weekend_receipt(self):
        received = dt.datetime(2026, 6, 13, 11, 0, tzinfo=PT)  # Saturday
        # Treated as received Mon 15 -> same as Monday-morning case
        self.assertEqual(
            deadlines.statutory_response_due(received),
            dt.date(2026, 6, 23),
        )


class EffectiveReceiptTimezoneTests(TestCase):
    """received_at is stored UTC-aware (USE_TZ=True), so every wall-clock
    decision must be made in America/Los_Angeles, not in the datetime's own
    tzinfo. These cases mirror what the DB actually hands the engine."""

    def test_utc_stored_afternoon_receipt_is_same_day(self):
        # Mon 2026-06-15 4:30pm PDT == 23:30 UTC — during business hours
        received = dt.datetime(2026, 6, 15, 23, 30, tzinfo=dt.UTC)
        self.assertEqual(
            deadlines.effective_receipt_date(received), dt.date(2026, 6, 15)
        )

    def test_utc_stored_friday_afternoon_not_pushed_over_weekend(self):
        # Fri 2026-06-12 4:00pm PDT == 23:00 UTC
        received = dt.datetime(2026, 6, 12, 23, 0, tzinfo=dt.UTC)
        self.assertEqual(
            deadlines.effective_receipt_date(received), dt.date(2026, 6, 12)
        )

    def test_utc_stored_after_hours_still_rolls(self):
        # Mon 2026-06-15 6:00pm PDT == Tue 01:00 UTC
        received = dt.datetime(2026, 6, 16, 1, 0, tzinfo=dt.UTC)
        self.assertEqual(
            deadlines.effective_receipt_date(received), dt.date(2026, 6, 16)
        )

    def test_pst_winter_receipt_crossing_utc_midnight(self):
        # Wed 2026-01-14 4:30pm PST == Thu 2026-01-15 00:30 UTC
        received = dt.datetime(2026, 1, 15, 0, 30, tzinfo=dt.UTC)
        self.assertEqual(
            deadlines.effective_receipt_date(received), dt.date(2026, 1, 14)
        )

    def test_utc_stored_statutory_due_matches_pacific_receipt(self):
        # Same instant as the Monday-morning case in StatutoryDueTests
        received = dt.datetime(2026, 6, 15, 23, 30, tzinfo=dt.UTC)
        self.assertEqual(
            deadlines.statutory_response_due(received), dt.date(2026, 6, 23)
        )


class ClarificationTollingTimezoneTests(TestCase):
    """Tolled business days must be counted between Pacific calendar dates.
    A clarification sent Monday evening Pacific lands on Tuesday in UTC;
    counting from the UTC date silently drops a tolled day."""

    def test_tolled_days_counted_in_pacific(self):
        from unittest import mock

        from apps.records import services
        from apps.records.models import PublicRecordsRequest, Requester, RequestStatus

        requester = Requester.objects.create(email="jane@example.org")
        prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="All maintenance invoices for June 2026",
            # Mon 2026-06-15 9:00am PDT == 16:00 UTC
            received_at=dt.datetime(2026, 6, 15, 16, 0, tzinfo=dt.UTC),
            status=RequestStatus.CLARIFICATION,
        )
        officer = make_records_officer("deadline-clarification-officer")
        # Clarification asked Mon 2026-06-15 6:00pm PDT (Tue 01:00 UTC).
        opened_at = dt.datetime(2026, 6, 16, 1, 0, tzinfo=dt.UTC)
        opened_event = StatusEvent.objects.create(
            request=prr,
            actor=officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Clarification request delivered.",
            visible_to_requester=True,
            occurred_at=opened_at,
        )
        outbound = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.CLARIFICATION_REQUEST,
            status=Correspondence.Status.DRAFT,
            draft_subject="Clarification requested",
            draft_body="Please clarify the requested school years.",
            created_by=officer,
        )
        inbound = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.CLARIFICATION_RESPONSE,
            status=Correspondence.Status.RECEIVED,
            received_subject="Re: clarification",
            received_body="School years 2024 through 2026.",
        )
        cycle = ClarificationCycle.objects.create(
            request=prr,
            outbound_correspondence=outbound,
            inbound_response_correspondence=inbound,
            opened_at=opened_at,
            opened_status_event=opened_event,
            responded_at=dt.datetime(
                2026,
                6,
                17,
                18,
                0,
                tzinfo=dt.UTC,
            ),
        )
        # Requester answers; clock restarts Wed 2026-06-17 noon PDT (19:00 UTC).
        with mock.patch(
            "django.utils.timezone.now",
            return_value=dt.datetime(2026, 6, 17, 19, 0, tzinfo=dt.UTC),
        ):
            services.resolve_clarification_cycle(
                cycle,
                actor=officer,
                resolution_notes=(
                    "Scope confirmed as school years 2024 through 2026."
                ),
            )
        prr.refresh_from_db()

        # Pacific dates: sent Mon 6/15, resumed Wed 6/17 -> Tue + Wed = 2 days.
        self.assertEqual(prr.tolled_business_days, 2)
        # Base due 6/23 pushed out 2 business days -> Thu 6/25.
        self.assertEqual(prr.statutory_response_due, dt.date(2026, 6, 25))


class ClosedRequestLifecycleTests(TestCase):
    def test_service_cannot_reopen_a_closed_request(self):
        from apps.records import services
        from apps.records.models import (
            PublicRecordsRequest,
            Requester,
            RequestStatus,
        )

        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="closed@example.org"),
            description="Closed request lifecycle invariant.",
        )
        officer = make_records_officer("deadline-closure-officer")
        prr = services.transition(
            prr,
            RequestStatus.FULFILLED,
            actor=officer,
            note="The Records Officer documented final fulfillment.",
        )
        original_event_count = prr.events.count()
        original_closed_at = prr.closed_at

        with self.assertRaises(services.ClosedRequestTransition):
            services.transition(prr, RequestStatus.GATHERING)

        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.FULFILLED)
        self.assertEqual(prr.closed_at, original_closed_at)
        self.assertEqual(prr.events.count(), original_event_count)
