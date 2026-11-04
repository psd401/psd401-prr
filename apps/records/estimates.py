# ABOUTME: The estimate engine (#41): responsive range from AI labels corrected
# ABOUTME: by measured agreement, fee by line item, and a backlog-based date.
"""Compute an estimate for a public records request, and keep its evidence.

A count is not an estimate. ``estimate_request`` answers three questions and
stores a ``RecordsEstimate`` holding the answer and every input behind it:

* **How many responsive records?** Reviewed records are certain (withheld in
  full counts as responsive). Each unreviewed record is grouped by its
  standing AI suggestion, and each group is scaled by how often people found
  a record responsive after the AI suggested that label (stored
  ``ReviewDecision`` rows, latest per record, district-wide). The range is
  the 95% Wilson interval of each group's rate; a group with no measured
  decisions spans 0 to all of its records. Records with no suggestion use the
  overall measured rate for the point estimate and span 0 to all.
* **What fee?** The adopted ``FeeSchedule`` in effect on ``as_of``, applied to
  the records that would be released (responsive, not withheld). Records here
  are already electronic, so the per-file bundle and per-GB rates apply and
  the scanned-page line is zero. The deposit is ``deposit_percent`` of the
  expected fee, never more than RCW 42.56.120(4)'s ten percent. No adopted
  schedule means no fee, said plainly, rather than a guessed one.
* **When?** District-wide review throughput -- ``record.reviewed`` audit rows
  per business day over a trailing window -- applied to every unreviewed
  record in open cases received before this one, plus this case's own, on the
  WA business-day calendar. No measured throughput means no date.

Nothing here changes the request, its statutory estimate, or reaches the
requester. A person revises the estimate (``letters.revise_estimate``) and
approves any letter. RCW 42.56 estimates belong to ``public_records`` cases
only (rule 13); the backlog ahead counts every kind, because every kind
competes for the same reviewers.
"""

import datetime as dt
import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit.models import AuditLog, audit

from .agreement import annotate_latest_suggestion, latest_decisions
from .deadlines import add_business_days, business_days_between
from .fees import CENT, current_fee_schedule, current_fee_waiver_floor
from .models import MatterKind, PublicRecordsRequest, RecordItem, RecordsEstimate
from .services import CLOSED_STATUSES

#: Trailing business days of review activity that set the throughput.
THROUGHPUT_WINDOW_BUSINESS_DAYS = 20
MAX_DEPOSIT_PERCENT = Decimal(10)
_Z = 1.96
_GROUPS = ("responsive", "non_responsive", "uncertain", "none")
_EXCLUDED = {RecordItem.ReviewState.SUPERSEDED, RecordItem.ReviewState.DUPLICATE}
_GB = Decimal(1_000_000_000)


def wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    """95% Wilson score interval for a proportion; (0, 1) with no trials."""
    if trials == 0:
        return 0.0, 1.0
    p = successes / trials
    denominator = 1 + _Z**2 / trials
    centre = (p + _Z**2 / (2 * trials)) / denominator
    margin = _Z * math.sqrt(p * (1 - p) / trials + _Z**2 / (4 * trials**2)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _agreement_rates() -> dict[str, dict[str, int]]:
    rates = {label: {"decisions": 0, "responsive": 0} for label in _GROUPS}
    for row in latest_decisions():
        if row["human_label"] is None or row["advisory_run_id"] is None:
            continue
        label = row["advisory_label"] if row["advisory_label"] in rates else "none"
        rates[label]["decisions"] += 1
        rates[label]["responsive"] += row["human_label"] == "responsive"
    return rates


def _record_counts(request) -> dict[str, Any]:
    items = annotate_latest_suggestion(
        RecordItem.objects.filter(request=request).exclude(review_state__in=_EXCLUDED)
    ).values("review_state", "content_length", "advisory_suggested_state")
    counts = {
        "reviewed_responsive": 0,
        "withheld": 0,
        "reviewed_non_responsive": 0,
        "released_bytes": 0,
        "unreviewed": {label: {"records": 0, "bytes": 0} for label in _GROUPS},
    }
    for row in items:
        state = row["review_state"]
        if state == RecordItem.ReviewState.RESPONSIVE:
            counts["reviewed_responsive"] += 1
            counts["released_bytes"] += row["content_length"]
        elif state == RecordItem.ReviewState.WITHHELD:
            counts["withheld"] += 1
        elif state == RecordItem.ReviewState.NON_RESPONSIVE:
            counts["reviewed_non_responsive"] += 1
        else:
            label = row["advisory_suggested_state"]
            group = counts["unreviewed"][label if label in _GROUPS else "none"]
            group["records"] += 1
            group["bytes"] += row["content_length"]
    return counts


def _responsive_range(counts, rates) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    """Low / expected / high responsive records, and per-group expectations
    (records and bytes) for the fee."""
    total = rates["responsive"]["decisions"] + rates["non_responsive"]["decisions"] + (
        rates["uncertain"]["decisions"] + rates["none"]["decisions"]
    )
    overall = (
        sum(rate["responsive"] for rate in rates.values()) / total if total else 0.5
    )
    certain = counts["reviewed_responsive"] + counts["withheld"]
    scenarios = {"low": 0.0, "expected": 0.0, "high": 0.0}
    released = {name: {"records": 0.0, "bytes": 0.0} for name in scenarios}
    for label in _GROUPS:
        group = counts["unreviewed"][label]
        if not group["records"]:
            continue
        rate = rates[label]
        if label == "none":
            point, (low, high) = overall, (0.0, 1.0)
        else:
            low, high = wilson_interval(rate["responsive"], rate["decisions"])
            point = rate["responsive"] / rate["decisions"] if rate["decisions"] else 0.5
        mean_bytes = group["bytes"] / group["records"]
        for name, fraction in (("low", low), ("expected", point), ("high", high)):
            scenarios[name] += group["records"] * fraction
            released[name]["records"] += group["records"] * fraction
            released[name]["bytes"] += group["records"] * fraction * mean_bytes
    unreviewed = sum(group["records"] for group in counts["unreviewed"].values())
    responsive = {
        "low": certain + math.floor(scenarios["low"] + 1e-9),
        "expected": certain + int(Decimal(scenarios["expected"]).quantize(0, ROUND_HALF_UP)),
        "high": certain + math.ceil(scenarios["high"] - 1e-9),
        "reviewed_responsive": certain,
        "unreviewed": unreviewed,
    }
    return responsive, released


def _fee(schedule, files: int, released_bytes: float, deposit_percent: Decimal, floor):
    bundles = (files + 3) // 4
    gb = (Decimal(str(released_bytes)) / _GB).quantize(Decimal("0.001"), ROUND_HALF_UP)
    lines = [
        {"item": "electronic_file_bundles", "quantity": str(bundles),
         "rate": str(schedule.electronic_file_bundle_rate),
         "amount": str((bundles * schedule.electronic_file_bundle_rate).quantize(CENT, ROUND_HALF_UP))},
        {"item": "electronic_gb", "quantity": str(gb),
         "rate": str(schedule.electronic_gb_rate),
         "amount": str((gb * schedule.electronic_gb_rate).quantize(CENT, ROUND_HALF_UP))},
        # The records are already electronic: nothing is scanned or copied.
        {"item": "scanned_pages", "quantity": "0",
         "rate": str(schedule.scanned_page_rate), "amount": "0.00"},
    ]
    total = sum((Decimal(line["amount"]) for line in lines), Decimal("0.00"))
    flat = schedule.flat_fee_amount
    return {
        "files": files,
        "lines": lines,
        "total": str(total),
        "deposit": str((total * deposit_percent / 100).quantize(CENT, ROUND_HALF_UP)),
        "flat_fee_available": flat is not None and total >= flat,
        "below_waiver_floor": floor is not None and Decimal(0) < total < floor.amount,
    }


def _window_start(as_of: dt.date) -> dt.date:
    """The day before the first business day of the trailing window."""
    start = as_of
    while business_days_between(start, as_of) < THROUGHPUT_WINDOW_BUSINESS_DAYS:
        start -= dt.timedelta(days=1)
    return start


def _timeline(request, as_of: dt.date, case_unreviewed: int) -> dict[str, Any]:
    start = _window_start(as_of)
    zone = timezone.get_current_timezone()
    reviews = AuditLog.objects.filter(
        action="record.reviewed",
        at__gte=dt.datetime.combine(start + dt.timedelta(days=1), dt.time.min, tzinfo=zone),
        at__lt=dt.datetime.combine(as_of + dt.timedelta(days=1), dt.time.min, tzinfo=zone),
    ).count()
    ahead = (
        RecordItem.objects.filter(
            review_state=RecordItem.ReviewState.UNREVIEWED,
            request__received_at__lt=request.received_at,
        )
        .exclude(request__status__in=CLOSED_STATUSES)
        .count()
    )
    return {
        "window_start_exclusive": start.isoformat(),
        "window_business_days": THROUGHPUT_WINDOW_BUSINESS_DAYS,
        "reviews_in_window": reviews,
        "per_business_day": round(reviews / THROUGHPUT_WINDOW_BUSINESS_DAYS, 4),
        "backlog_ahead": ahead,
        "case_unreviewed": case_unreviewed,
    }


def estimate_request(
    request: PublicRecordsRequest,
    *,
    actor=None,
    as_of: dt.date | None = None,
    deposit_percent: Decimal = MAX_DEPOSIT_PERCENT,
) -> RecordsEstimate:
    """Compute and store an estimate for ``request``. See the module doc."""
    if request.matter_kind != MatterKind.PUBLIC_RECORDS:
        raise ValidationError("Only a public records request gets an RCW 42.56 estimate.")
    deposit_percent = Decimal(str(deposit_percent))
    if not Decimal(0) <= deposit_percent <= MAX_DEPOSIT_PERCENT:
        raise ValidationError("A deposit may not exceed ten percent of the estimated charge.")
    as_of = as_of or timezone.localdate()
    notes = []

    counts = _record_counts(request)
    rates = _agreement_rates()
    responsive, released = _responsive_range(counts, rates)

    schedule = current_fee_schedule(on_date=as_of)
    floor = current_fee_waiver_floor(on_date=as_of)
    if schedule is None:
        fee = None
        notes.append("No fee schedule is adopted, so no fee is estimated.")
    else:
        fee = {
            name: _fee(
                schedule,
                counts["reviewed_responsive"] + round(released[name]["records"]),
                counts["released_bytes"] + released[name]["bytes"],
                deposit_percent,
                floor,
            )
            for name in ("low", "expected", "high")
        }

    case_unreviewed = responsive["unreviewed"]
    timeline = _timeline(request, as_of, case_unreviewed)
    completion = None
    if timeline["reviews_in_window"] == 0:
        notes.append(
            "No records were reviewed in the throughput window, so no completion date is estimated."
        )
    else:
        days = math.ceil(
            (timeline["backlog_ahead"] + case_unreviewed) / timeline["per_business_day"]
        )
        completion = add_business_days(as_of, days).isoformat()
    notes.append("The completion date covers review only, not redaction or release.")

    inputs = {
        "record_counts": counts,
        "agreement_rates": {label: rate for label, rate in rates.items() if label != "none"},
        "fee_schedule": None if schedule is None else {
            "id": str(schedule.pk),
            "name": schedule.name,
            "effective_on": schedule.effective_on.isoformat(),
            "electronic_file_bundle_rate": str(schedule.electronic_file_bundle_rate),
            "electronic_gb_rate": str(schedule.electronic_gb_rate),
            "scanned_page_rate": str(schedule.scanned_page_rate),
            "flat_fee_amount": None if schedule.flat_fee_amount is None else str(schedule.flat_fee_amount),
        },
        "waiver_floor": None if floor is None else str(floor.amount),
        "deposit_percent": str(deposit_percent),
        "timeline": timeline,
    }
    result = {
        "responsive": responsive,
        "fee": fee,
        "completion_date": completion,
        "notes": notes,
    }
    estimate = RecordsEstimate.objects.create(
        request=request, as_of=as_of, inputs=inputs, result=result, created_by=actor,
    )
    audit(
        "records.estimate_computed",
        target=request.number,
        actor=actor,
        estimate_id=str(estimate.pk),
        as_of=as_of.isoformat(),
        responsive=responsive,
        completion_date=completion,
        fee_expected=None if fee is None else fee["expected"]["total"],
    )
    return estimate

