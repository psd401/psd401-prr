"""Property-based tests for the RCW 42.56.520 business-day engine.

`test_deadlines.py` pins the engine to real Washington calendar dates — the
examples a records officer or counsel can check by eye. This module states the
same compliance as invariants and lets Hypothesis hunt for the date that breaks
one (psd-dev-standards `standards/05-testing.md` rule 8: property-based tests
for date math and permission logic).

Every test patches `deadlines._district_closures` instead of writing the
settings store. The engine's only other inputs are the `holidays` package and
`TIME_ZONE`, so these run on `SimpleTestCase` with no database and no cache —
which is also what keeps Hypothesis's per-example reruns deterministic.
"""
import datetime as dt
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase
from django.utils import timezone
from hypothesis import HealthCheck, assume, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from apps.records import deadlines

PT = ZoneInfo("America/Los_Angeles")

# Six years keeps every example inside `_state_holidays`'s lru_cache, so the
# WA holiday table is built once per year rather than per example.
HORIZON_START = dt.date(2025, 1, 1)
HORIZON_END = dt.date(2030, 12, 31)

calendar_days = st.dates(min_value=HORIZON_START, max_value=HORIZON_END)
closure_sets = st.frozensets(calendar_days, max_size=4)
tolled_days = st.integers(min_value=0, max_value=20)
utc_instants = st.datetimes(
    min_value=dt.datetime(HORIZON_START.year, 1, 1),
    max_value=dt.datetime(HORIZON_END.year, 12, 31),
    timezones=st.just(dt.UTC),
)

# Generating dates and rebuilding holiday tables is slower than Hypothesis's
# default per-example deadline, and the backend suite runs on shared CI
# runners, so time out the example budget rather than individual examples.
#
# `derandomize` is the important one. A random search that finds a bad date on
# one run and misses it on the next is indistinguishable, from CI's side, from
# a flaky test -- and FLAKY.md's whole triage-versus-quarantine decision turns
# on "does it fail every time". CI keeps no `.hypothesis` example database
# between runs, so without this a real deadline defect could be found once,
# vanish on a re-run, and be quarantined as flake. Fixed inputs make a failure
# reproducible on the same commit; the breadth across commits is covered by
# the monthly mutation run instead.
engine_properties = hypothesis_settings(
    max_examples=75,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)


def _with_closures(closures):
    return patch.object(deadlines, "_district_closures", return_value=closures)


class BusinessDayPropertyTests(SimpleTestCase):
    @engine_properties
    @given(calendar_days, st.integers(min_value=1, max_value=30), closure_sets)
    def test_add_business_days_always_lands_on_an_open_day(self, start, n, closures):
        with _with_closures(closures):
            landed = deadlines.add_business_days(start, n)

            self.assertGreater(landed, start)
            self.assertTrue(deadlines._open(landed, closures))

    @engine_properties
    @given(calendar_days, st.integers(min_value=0, max_value=20), closure_sets)
    def test_add_business_days_is_strictly_increasing_in_n(self, start, n, closures):
        with _with_closures(closures):
            self.assertLess(
                deadlines.add_business_days(start, n),
                deadlines.add_business_days(start, n + 1),
            )

    @engine_properties
    @given(calendar_days, st.integers(min_value=0, max_value=30), closure_sets)
    def test_business_days_between_inverts_add_business_days(self, start, n, closures):
        """Counting back the open days to a deadline must return the window."""
        with _with_closures(closures):
            due = deadlines.add_business_days(start, n)

            self.assertEqual(deadlines.business_days_between(start, due), n)

    @engine_properties
    @given(calendar_days, calendar_days, closure_sets)
    def test_business_days_between_is_bounded_by_the_calendar(self, start, end, closures):
        with _with_closures(closures):
            elapsed = deadlines.business_days_between(start, end)

            if end <= start:
                self.assertEqual(elapsed, 0)
            else:
                self.assertLessEqual(elapsed, (end - start).days)
            self.assertGreaterEqual(elapsed, 0)

    @engine_properties
    @given(calendar_days, closure_sets, closure_sets)
    def test_closing_the_district_never_pulls_a_date_earlier(self, start, base, extra):
        """Adding closure days may only push a date out, never bring it in."""
        with _with_closures(base):
            fewer = deadlines.add_business_days(start, 5)
        with _with_closures(base | extra):
            more = deadlines.add_business_days(start, 5)

        self.assertGreaterEqual(more, fewer)


class ReceiptPropertyTests(SimpleTestCase):
    @engine_properties
    @given(utc_instants, closure_sets)
    def test_effective_receipt_is_always_an_open_day(self, received_at, closures):
        with _with_closures(closures):
            effective = deadlines.effective_receipt_date(received_at)

            self.assertTrue(deadlines._open(effective, closures))

    @engine_properties
    @given(utc_instants, closure_sets)
    def test_effective_receipt_reads_the_pacific_wall_clock(self, received_at, closures):
        """A UTC-stored instant is interpreted in Pacific time, not UTC.

        Late-evening Pacific is already the next UTC calendar day, so an
        engine that read `.date()` off the raw aware datetime would answer a
        day late for a third of every day. The deadline may only ever be the
        Pacific receipt day or later.
        """
        local_day = timezone.localtime(received_at).date()
        local_hour = timezone.localtime(received_at).hour

        with _with_closures(closures):
            effective = deadlines.effective_receipt_date(received_at)

            self.assertGreaterEqual(effective, local_day)
            if local_hour < 17 and deadlines._open(local_day, closures):
                self.assertEqual(effective, local_day)
            else:
                self.assertGreater(effective, local_day)

    @engine_properties
    @given(calendar_days, st.integers(min_value=0, max_value=16), closure_sets)
    def test_after_hours_receipt_never_responds_sooner(self, day, open_hour, closures):
        """Arriving later in the day can only move the deadline out."""
        during = dt.datetime.combine(day, dt.time(open_hour), tzinfo=PT)
        after = dt.datetime.combine(day, dt.time(17), tzinfo=PT)
        # Pacific spring-forward skips 02:00–02:59; those wall clocks are not
        # instants and `ZoneInfo` would silently reinterpret them.
        assume(during.astimezone(dt.UTC).astimezone(PT).hour == open_hour)

        with _with_closures(closures):
            self.assertGreaterEqual(
                deadlines.effective_receipt_date(after),
                deadlines.effective_receipt_date(during),
            )


class StatutoryDuePropertyTests(SimpleTestCase):
    @engine_properties
    @given(utc_instants, tolled_days, closure_sets)
    def test_due_date_is_the_window_plus_tolling_in_open_days(
        self, received_at, tolled, closures
    ):
        with _with_closures(closures):
            effective = deadlines.effective_receipt_date(received_at)
            due = deadlines.statutory_response_due(
                received_at, tolled_business_days=tolled
            )

            self.assertTrue(deadlines._open(due, closures))
            self.assertEqual(
                deadlines.business_days_between(effective, due),
                deadlines.RESPONSE_WINDOW_BUSINESS_DAYS + tolled,
            )

    @engine_properties
    @given(utc_instants, tolled_days, closure_sets)
    def test_tolling_only_ever_extends_the_deadline(self, received_at, tolled, closures):
        with _with_closures(closures):
            self.assertLess(
                deadlines.statutory_response_due(received_at, tolled_business_days=tolled),
                deadlines.statutory_response_due(
                    received_at, tolled_business_days=tolled + 1
                ),
            )

    @engine_properties
    @given(utc_instants, closure_sets)
    def test_due_date_leaves_at_least_five_calendar_days(self, received_at, closures):
        """Five open days can never be fewer than five calendar days."""
        with _with_closures(closures):
            due = deadlines.statutory_response_due(received_at)

            self.assertGreaterEqual(
                (due - timezone.localtime(received_at).date()).days,
                deadlines.RESPONSE_WINDOW_BUSINESS_DAYS,
            )


class FerpaWindowPropertyTests(SimpleTestCase):
    """34 CFR 99.10(b) shares none of the RCW 42.56.520 machinery (ADR 0004)."""

    @engine_properties
    @given(utc_instants, closure_sets)
    def test_ferpa_window_is_exactly_forty_five_calendar_days(self, received_at, closures):
        with _with_closures(closures):
            due = deadlines.ferpa_access_due(received_at)

            self.assertEqual(
                (due - timezone.localtime(received_at).date()).days,
                deadlines.FERPA_ACCESS_WINDOW_CALENDAR_DAYS,
            )

    @engine_properties
    @given(utc_instants, closure_sets)
    def test_ferpa_window_ignores_holidays_and_closures(self, received_at, closures):
        """Closing the district moves no FERPA deadline."""
        with _with_closures(frozenset()):
            without = deadlines.ferpa_access_due(received_at)
        with _with_closures(closures):
            self.assertEqual(deadlines.ferpa_access_due(received_at), without)
