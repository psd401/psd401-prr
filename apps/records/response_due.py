"""The date a case is answerable by *next*, and what makes it that date.

RCW 42.56.520 asks one question and answers it once: within five business days
of receipt, did the District respond. ``statutory_response_due`` is the date
that question is measured against, and the moment the first response is
delivered it is spent. A case list that keeps showing it afterwards asserts a
statutory deadline the District no longer owes, on a screen officers use to
decide what to work on next.

What this module computes instead is *the next thing owed*, which is not always
a statutory obligation and never pretends to be:

===================== ============================ ==================
Matter kind           Situation                    Basis
===================== ============================ ==================
``public_records``    no first response yet        ``statutory``
``public_records``    responded, estimate recorded ``estimate``
``public_records``    responded, no estimate       ``district_target``
``ferpa_parent``      always                       ``ferpa``
``litigation_discovery`` always                    none
any kind              closed or terminal           none
===================== ============================ ==================

``district_target`` is 60 **calendar** days from the day the first response was
delivered. It is a district working target, not law: the business-day engine in
:mod:`apps.records.deadlines`, its after-hours rollover, its holiday calendar,
and its clarification tolling are deliberately not reused, and every surface
that shows this basis labels it as a target. Nothing here changes what the
statutory clock computes, when the day-5 safety net fires, what the watchdogs
escalate, or what JLARC counts.

Two implementations live here on purpose. :func:`next_response_due` answers for
one case, from data the list already loads. :func:`annotate_next_response_due`
answers for a whole page in SQL, because a column has to be sortable by the
value it displays. ``apps/records/tests/test_next_response_due.py`` asserts
they agree case by case; a column that shows one date and sorts by another is
worse than either alone.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db.models import (
    BooleanField,
    Case,
    DateField,
    DateTimeField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from .models import (
    INITIAL_RESPONSE_STATUSES,
    InitialResponseAttestation,
    LegacyRequestImport,
    MatterKind,
    PublicRecordsRequest,
    StatusEvent,
)

#: The District's internal working target for finishing a request whose first
#: response went out without a completion estimate. Calendar days, counted from
#: the day the first response was delivered. Not a statutory period.
DISTRICT_TARGET_CALENDAR_DAYS = 60


class DueBasis:
    """Why the date is the date. Shown beside it, never inferred by the reader."""

    STATUTORY = "statutory"
    ESTIMATE = "estimate"
    DISTRICT_TARGET = "district_target"
    FERPA = "ferpa"

    #: Every value the API may emit, for schema and for the front end's union.
    CHOICES = (STATUTORY, ESTIMATE, DISTRICT_TARGET, FERPA)


@dataclass(frozen=True)
class NextResponseDue:
    """A date and the reason it is that date. Both null when nothing is owed."""

    date: dt.date | None
    basis: str | None


NOTHING_DUE = NextResponseDue(date=None, basis=None)


def _closed_statuses() -> set[str]:
    # Imported lazily: services imports models, and models is imported here.
    from . import services

    return set(services.CLOSED_STATUSES)


def initial_response_date(prr: PublicRecordsRequest) -> dt.date | None:
    """The day the RCW 42.56.520 first response was delivered, if it is known.

    The sources are read in the same order, and with the same meanings, as
    :attr:`PublicRecordsRequest.initial_response_delivered`, so the two can
    never disagree about which record spoke. ``None`` means either that no
    first response was delivered or -- the attestation case -- that one was and
    nobody can say on what day. Neither is a licence to invent one.
    """
    if prr.acknowledgment_sent_at is not None:
        return timezone.localtime(prr.acknowledgment_sent_at).date()
    try:
        legacy_at = prr.legacy_import.legacy_initial_response_at
    except LegacyRequestImport.DoesNotExist:
        legacy_at = None
    if legacy_at is not None:
        return timezone.localtime(legacy_at).date()
    try:
        attestation = prr.initial_response_attestation
    except InitialResponseAttestation.DoesNotExist:
        attestation = None
    if attestation is not None and attestation.responded_on is not None:
        return attestation.responded_on
    event = _earliest_initial_response_event(prr)
    if event is not None:
        return timezone.localtime(event.occurred_at).date()
    return None


def _earliest_initial_response_event(prr: PublicRecordsRequest):
    """The first status event that *was* the initial response, prefetch-aware."""
    prefetched = getattr(prr, "_prefetched_objects_cache", {}).get("events")
    if prefetched is not None:
        qualifying = [
            event
            for event in prefetched
            if event.to_status in INITIAL_RESPONSE_STATUSES
        ]
        # The primary key is only a tiebreak, and only so this and the SQL
        # below pick deterministically. It does not have to pick the *same*
        # row they would: two events can tie here only by sharing
        # ``occurred_at``, and the caller reads nothing off the row but that
        # timestamp's date, which is then identical either way.
        return min(qualifying, key=lambda e: (e.occurred_at, str(e.pk)), default=None)
    return (
        prr.events.filter(to_status__in=INITIAL_RESPONSE_STATUSES)
        .order_by("occurred_at", "pk")
        .first()
    )


def next_response_due(prr: PublicRecordsRequest) -> NextResponseDue:
    """The next date this case owes an answer by, and why."""
    if prr.status in _closed_statuses():
        return NOTHING_DUE
    if prr.matter_kind == MatterKind.LITIGATION_DISCOVERY:
        # No requester, no statutory clock. The date counsel set lives in
        # ``estimated_completion`` and is shown in its own column.
        return NOTHING_DUE
    if prr.matter_kind == MatterKind.FERPA_PARENT:
        if prr.ferpa_access_due is None:
            return NOTHING_DUE
        return NextResponseDue(date=prr.ferpa_access_due, basis=DueBasis.FERPA)
    if not prr.initial_response_delivered:
        if prr.statutory_response_due is None:
            return NOTHING_DUE
        return NextResponseDue(
            date=prr.statutory_response_due, basis=DueBasis.STATUTORY
        )
    if prr.estimated_completion is not None:
        return NextResponseDue(
            date=prr.estimated_completion, basis=DueBasis.ESTIMATE
        )
    delivered_on = initial_response_date(prr)
    if delivered_on is None:
        return NOTHING_DUE
    return NextResponseDue(
        date=delivered_on + dt.timedelta(days=DISTRICT_TARGET_CALENDAR_DAYS),
        basis=DueBasis.DISTRICT_TARGET,
    )


def _initial_response_events():
    return StatusEvent.objects.filter(
        request=OuterRef("pk"), to_status__in=INITIAL_RESPONSE_STATUSES
    )


def annotate_next_response_due(queryset):
    """Add ``_next_due`` and ``_next_due_basis`` for ordering the case list.

    The SQL mirrors :func:`next_response_due` clause for clause. It is used to
    *sort* by the column; the value the API serves comes from the service, so
    the two are proved equal by test rather than assumed equal by construction.
    """
    events = _initial_response_events()
    delivered_on = Coalesce(
        TruncDate("acknowledgment_sent_at"),
        TruncDate("legacy_import__legacy_initial_response_at"),
        F("initial_response_attestation__responded_on"),
        Subquery(
            events.order_by("occurred_at", "pk")
            .annotate(_day=TruncDate("occurred_at"))
            .values("_day")[:1],
            output_field=DateField(),
        ),
        output_field=DateField(),
    )
    queryset = queryset.annotate(
        _initial_response_on=delivered_on,
        _initial_response_delivered=Case(
            When(acknowledgment_sent_at__isnull=False, then=Value(True)),
            When(
                legacy_import__legacy_initial_response_at__isnull=False,
                then=Value(True),
            ),
            When(initial_response_attestation__isnull=False, then=Value(True)),
            When(Exists(events), then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
    )
    district_target = ExpressionWrapper(
        F("_initial_response_on")
        + dt.timedelta(days=DISTRICT_TARGET_CALENDAR_DAYS),
        output_field=DateTimeField(),
    )
    nothing = Q(status__in=_closed_statuses()) | Q(
        matter_kind=MatterKind.LITIGATION_DISCOVERY
    )
    return queryset.annotate(
        _next_due=Case(
            When(nothing, then=Value(None, output_field=DateField())),
            When(
                matter_kind=MatterKind.FERPA_PARENT, then=F("ferpa_access_due")
            ),
            When(
                _initial_response_delivered=False,
                then=F("statutory_response_due"),
            ),
            When(
                estimated_completion__isnull=False,
                then=F("estimated_completion"),
            ),
            default=district_target,
            output_field=DateField(),
        ),
        # A basis without a date would label a deadline that is not there, so
        # every branch that yields no date yields no basis either.
        _next_due_basis=Case(
            When(nothing, then=Value("")),
            When(
                Q(matter_kind=MatterKind.FERPA_PARENT)
                & Q(ferpa_access_due__isnull=False),
                then=Value(DueBasis.FERPA),
            ),
            When(matter_kind=MatterKind.FERPA_PARENT, then=Value("")),
            When(
                Q(_initial_response_delivered=False)
                & Q(statutory_response_due__isnull=False),
                then=Value(DueBasis.STATUTORY),
            ),
            When(_initial_response_delivered=False, then=Value("")),
            When(
                estimated_completion__isnull=False,
                then=Value(DueBasis.ESTIMATE),
            ),
            When(_initial_response_on__isnull=True, then=Value("")),
            default=Value(DueBasis.DISTRICT_TARGET),
        ),
    )


#: Soonest first, with the cases that owe nothing at the end. Nulls are pinned
#: explicitly in both directions because SQLite and Postgres disagree about
#: where they sort by default, and a deadline column whose undated rows move
#: between test and production is a compliance surface, not a cosmetic one.
NEXT_DUE_ASCENDING = (F("_next_due").asc(nulls_last=True), "pk")
NEXT_DUE_DESCENDING = (F("_next_due").desc(nulls_last=True), "-pk")
