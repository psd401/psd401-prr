"""
Statutory deadline engine — RCW 42.56.520.

Within FIVE BUSINESS DAYS of receipt an agency must (1) provide the record,
(2) acknowledge and give a reasonable estimate of time, (3) seek clarification,
or (4) deny with reasons. "Business day" excludes weekends and WA state /
district holidays. A request received after close of business is treated as
received the next business day (agency practice — confirm with counsel and
adopt in your PRA policy; PSD's policy should define close of business).

Day counting starts the day AFTER receipt (RCW 1.12.040 computation of time).

Stored datetimes are UTC-aware (USE_TZ=True), but close-of-business and
calendar-day are wall-clock concepts in America/Los_Angeles. Every function
here that reads .hour or .date() off a datetime first converts it with
django.utils.timezone.localtime() — never read those off a raw aware
datetime, since its tzinfo is UTC, not Pacific.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import holidays as pyholidays
from django.utils import timezone

from apps.configuration import runtime

RESPONSE_WINDOW_BUSINESS_DAYS = 5

#: 34 CFR 99.10(b) — a FERPA parent/eligible-student request is answered within
#: 45 CALENDAR days. This is not the RCW 42.56.520 clock and shares none of its
#: business-day, holiday, close-of-business, or tolling rules.
FERPA_ACCESS_WINDOW_CALENDAR_DAYS = 45


@lru_cache(maxsize=8)
def _state_holidays(year: int) -> frozenset:
    """WA state holidays (RCW 1.16.050) via the `holidays` package."""
    base = pyholidays.country_holidays("US", subdiv="WA", years=[year, year + 1])
    return frozenset(base.keys())


def _district_closures() -> frozenset:
    """The district's closure days, read from the settings store.

    Read per computation rather than folded into the cached state set: a
    closure an administrator adds has to change the next deadline computed,
    not the first one after a restart. The loops below read it once each.
    """
    return frozenset(
        dt.date.fromisoformat(d) for d in runtime.get("DISTRICT_HOLIDAYS")
    )


def _open(day: dt.date, closures: frozenset) -> bool:
    return (
        day.weekday() < 5
        and day not in _state_holidays(day.year)
        and day not in closures
    )


def is_business_day(day: dt.date) -> bool:
    return _open(day, _district_closures())


def add_business_days(start: dt.date, n: int) -> dt.date:
    """Return the date that is `n` business days after `start`.
    Counting begins the day after `start` per RCW 1.12.040."""
    closures = _district_closures()
    day, remaining = start, n
    while remaining > 0:
        day += dt.timedelta(days=1)
        if _open(day, closures):
            remaining -= 1
    return day


def business_days_between(start: dt.date, end: dt.date) -> int:
    """Business days elapsed from start (exclusive) to end (inclusive)."""
    if end <= start:
        return 0
    closures = _district_closures()
    return sum(
        1
        for i in range(1, (end - start).days + 1)
        if _open(start + dt.timedelta(days=i), closures)
    )


def effective_receipt_date(received_at: dt.datetime, close_of_business_hour: int = 17) -> dt.date:
    """Requests arriving after close of business or on a non-business day are
    treated as received the next business day."""
    local = timezone.localtime(received_at)
    day = local.date()
    if local.hour >= close_of_business_hour or not is_business_day(day):
        day = add_business_days(day, 1)
    return day


def statutory_response_due(received_at: dt.datetime, tolled_business_days: int = 0) -> dt.date:
    """The RCW 42.56.520 five-business-day response deadline, extended by any
    tolled days (clock paused while awaiting requester clarification).

    Only a ``public_records`` matter has one (ADR 0004)."""
    start = effective_receipt_date(received_at)
    return add_business_days(start, RESPONSE_WINDOW_BUSINESS_DAYS + tolled_business_days)


def ferpa_access_due(received_at: dt.datetime) -> dt.date:
    """The 34 CFR 99.10(b) deadline for a parent or eligible student.

    FERPA gives the agency 45 **calendar** days from receipt — not business
    days. Nothing above applies: no weekend or holiday skipping, no
    after-hours rollover to the next business day, and no clarification
    tolling. That machinery implements RCW 42.56.520 and belongs to public
    records requests alone, so this function deliberately shares none of it.

    Only a ``ferpa_parent`` matter has one (ADR 0004).
    """
    return timezone.localtime(received_at).date() + dt.timedelta(
        days=FERPA_ACCESS_WINDOW_CALENDAR_DAYS
    )
