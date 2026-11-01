"""Reproducible RCW 40.14.026 annual metrics and public-request log data."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.audit.models import audit

from .deadlines import business_days_between, effective_receipt_date
from .models import (
    EstimateRevision,
    FeePayment,
    JlarcAnnualSupplement,
    JlarcRequestMetrics,
    MatterKind,
    PublicRecordsRequest,
    RecordItem,
    RequestStatus,
    StatusEvent,
    TimeEntry,
)
from .services import _require_current_records_officer

CENT = Decimal("0.01")
TERMINAL_STATUSES = {
    RequestStatus.FULFILLED,
    RequestStatus.CLOSED_WITHDRAWN,
    RequestStatus.CLOSED_ABANDONED,
    RequestStatus.CLOSED_NON_REQUEST,
    RequestStatus.CLOSED_NO_RECORDS,
    RequestStatus.CLOSED_DUPLICATE,
    RequestStatus.DENIED,
}
ABANDONED_STATUSES = {
    RequestStatus.CLOSED_WITHDRAWN,
    RequestStatus.CLOSED_ABANDONED,
}
NO_FULFILLMENT_STATUSES = ABANDONED_STATUSES | {
    RequestStatus.CLOSED_NON_REQUEST,
    RequestStatus.CLOSED_DUPLICATE,
    RequestStatus.CLOSED_NO_RECORDS,
    RequestStatus.DENIED,
}
SUPPLEMENT_MONEY_FIELDS = (
    "staff_compensation_cost",
    "legal_review_cost",
    "supplies_services_cost",
    "litigation_agency_cost",
    "litigation_settlements",
    "litigation_penalties",
    "litigation_requester_attorney_fees",
    "litigation_requester_costs",
    "records_management_staff_cost",
    "records_management_systems_cost",
    "records_management_other_cost",
    "other_expenses_recovered",
    "customized_service_expenses_recovered_external",
)


def _money(value, *, field):
    try:
        amount = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a valid monetary amount.") from exc
    if not amount.is_finite() or amount < 0:
        raise ValidationError(f"{field} must be nonnegative and finite.")
    return amount


def _count(value, *, field):
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a nonnegative whole number.")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a nonnegative whole number.") from exc
    if count < 0 or str(value).strip() not in {str(count), f"{count}.0"}:
        raise ValidationError(f"{field} must be a nonnegative whole number.")
    return count


def _reason(value, *, label="reason"):
    text = str(value or "").strip()
    if len(text) < 10:
        raise ValidationError(f"A substantive {label} is required.")
    return text


def _current_request_metrics(request):
    return (
        JlarcRequestMetrics.objects.filter(
            request=request,
            superseded_by__isnull=True,
        )
        .order_by("-revision")
        .first()
    )


@transaction.atomic
def classify_request_for_jlarc(
    request_id,
    *,
    actor,
    requester_type,
    fulfillment_format,
    scanned_physical,
    records_produced_description,
    records_withheld_description,
    reason,
):
    """Append an optional classification without collecting new requester PII."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to classify JLARC data.",
    )
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=request_id)
    if prr.matter_kind != MatterKind.PUBLIC_RECORDS:
        # Refuse outright rather than store a classification that a later
        # report would have to remember to leave out.
        raise ValidationError(
            f"{prr.number} is a {prr.get_matter_kind_display()} and is not "
            "reported under RCW 40.14.026."
        )
    list(
        JlarcRequestMetrics.objects.select_for_update()
        .filter(request=prr)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if requester_type not in JlarcRequestMetrics.RequesterType.values:
        raise ValidationError("The JLARC requester type is invalid.")
    if fulfillment_format not in JlarcRequestMetrics.FulfillmentFormat.values:
        raise ValidationError("The JLARC fulfillment format is invalid.")
    if not isinstance(scanned_physical, bool):
        raise ValidationError("The physical-scanning flag must be a boolean.")
    produced = str(records_produced_description or "").strip()
    withheld = str(records_withheld_description or "").strip()
    if len(produced) < 4 or len(withheld) < 4:
        raise ValidationError(
            "Produced and withheld-record descriptions are required; use 'None' when applicable."
        )
    substantive_reason = _reason(reason, label="classification reason")
    current = _current_request_metrics(prr)
    snapshot = JlarcRequestMetrics.objects.create(
        request=prr,
        revision=current.revision + 1 if current else 1,
        supersedes=current,
        requester_type=requester_type,
        fulfillment_format=fulfillment_format,
        scanned_physical=scanned_physical,
        records_produced_description=produced,
        records_withheld_description=withheld,
        reason=substantive_reason,
        created_by=current_actor,
    )
    audit(
        "jlarc.request_classified",
        target=prr.number,
        actor=current_actor,
        snapshot_id=str(snapshot.pk),
        revision=snapshot.revision,
        requester_type=snapshot.requester_type,
        fulfillment_format=snapshot.fulfillment_format,
        scanned_physical=snapshot.scanned_physical,
        reason=snapshot.reason,
    )
    return snapshot


def _validated_breakdown(value, *, label, claims_filed, exact_total):
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object of labels and counts.")
    normalized = {}
    for raw_label, raw_count in value.items():
        item_label = str(raw_label).strip()
        if not item_label or len(item_label) > 200:
            raise ValidationError(f"{label} contains an invalid label.")
        normalized[item_label] = _count(raw_count, field=f"{label} count")
    if exact_total and normalized and sum(normalized.values()) != claims_filed:
        raise ValidationError(f"{label} counts must total the number of claims filed.")
    return normalized


@transaction.atomic
def record_jlarc_annual_supplement(
    reporting_year,
    *,
    actor,
    unallocated_staff_minutes=0,
    staff_compensation_cost=0,
    legal_review_cost=0,
    supplies_services_cost=0,
    claims_filed=0,
    claims_by_type=None,
    claims_by_exemption=None,
    litigation_agency_cost=0,
    litigation_settlements=0,
    litigation_penalties=0,
    litigation_requester_attorney_fees=0,
    litigation_requester_costs=0,
    records_management_staff_cost=0,
    records_management_systems_cost=0,
    records_management_other_cost=0,
    other_expenses_recovered=0,
    customized_service_expenses_recovered_external=0,
    methodology_notes="",
):
    """Append annual Finance/Risk/HR inputs that cannot be derived in-app."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to record JLARC inputs.",
    )
    year = _count(reporting_year, field="reporting year")
    if year < 2017 or year > timezone.localdate().year:
        raise ValidationError("The JLARC reporting year must be from 2017 through the current year.")
    list(
        JlarcAnnualSupplement.objects.select_for_update()
        .filter(reporting_year=year)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    current = (
        JlarcAnnualSupplement.objects.filter(
            reporting_year=year,
            superseded_by__isnull=True,
        )
        .order_by("-revision")
        .first()
    )
    claims = _count(claims_filed, field="claims filed")
    breakdown_type = _validated_breakdown(
        claims_by_type or {},
        label="Claims-by-type",
        claims_filed=claims,
        exact_total=True,
    )
    breakdown_exemption = _validated_breakdown(
        claims_by_exemption or {},
        label="Claims-by-exemption",
        claims_filed=claims,
        exact_total=False,
    )
    if sum(breakdown_exemption.values()) > claims:
        raise ValidationError("Claims-by-exemption counts cannot exceed claims filed.")
    values = {
        field: _money(locals()[field], field=field.replace("_", " "))
        for field in SUPPLEMENT_MONEY_FIELDS
    }
    supplement = JlarcAnnualSupplement.objects.create(
        reporting_year=year,
        revision=current.revision + 1 if current else 1,
        supersedes=current,
        unallocated_staff_minutes=_count(
            unallocated_staff_minutes,
            field="unallocated staff minutes",
        ),
        claims_filed=claims,
        claims_by_type=breakdown_type,
        claims_by_exemption=breakdown_exemption,
        methodology_notes=_reason(methodology_notes, label="methodology note"),
        created_by=current_actor,
        **values,
    )
    audit(
        "jlarc.supplement_recorded",
        target=str(year),
        actor=current_actor,
        supplement_id=str(supplement.pk),
        revision=supplement.revision,
        fulfillment_cost=f"{sum((values[field] for field in ('staff_compensation_cost', 'legal_review_cost', 'supplies_services_cost')), Decimal(0)):.2f}",
        claims_filed=claims,
        reason=supplement.methodology_notes,
    )
    return supplement


def current_jlarc_supplement(reporting_year):
    return (
        JlarcAnnualSupplement.objects.filter(
            reporting_year=reporting_year,
            superseded_by__isnull=True,
        )
        .select_related("created_by")
        .order_by("-revision")
        .first()
    )


def _period(reporting_year):
    zone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime(reporting_year, 1, 1), zone)
    end = timezone.make_aware(datetime(reporting_year + 1, 1, 1), zone)
    return start, end


def _money_string(value):
    return f"{Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def _percent(part, whole):
    if not whole:
        return "0.00"
    return f"{Decimal(part * 100) / Decimal(whole):.2f}"


def _request_citations(prr):
    citations = set()
    for item in prr.record_items.all():
        if item.withholding_exemption is not None:
            citations.add(item.withholding_exemption.citation)
        citations.update(ex.citation for ex in item.withholding_exemptions.all())
        citations.update(
            redaction.exemption.citation
            for redaction in item.redactions.all()
            if redaction.voided_at is None
        )
    return sorted(citations)


def _request_has_partial_denial(prr):
    if prr.status == RequestStatus.DENIED:
        return False
    return any(
        item.review_state == RecordItem.ReviewState.WITHHELD
        or item.withholding_exemption_id is not None
        or bool(item.withholding_exemptions.all())
        or any(redaction.voided_at is None for redaction in item.redactions.all())
        for item in prr.record_items.all()
    )


def build_jlarc_report(reporting_year):
    """Build a deterministic transfer worksheet; never submit it externally."""
    year = _count(reporting_year, field="reporting year")
    supplement = current_jlarc_supplement(year)
    if supplement is None:
        raise ValidationError(
            "A Records Officer must record the annual supplement before cost metrics can be generated."
        )
    start, end = _period(year)
    all_requests = list(
        # RCW 40.14.026 reports public records requests. A litigation pull and
        # a FERPA request are not requests under RCW 42.56 and the District
        # must not report them as if they were (ADR 0004).
        PublicRecordsRequest.objects.public_records()
        .filter(received_at__lt=end)
        # A merged duplicate is the same request as its case of record, and
        # that case is already counted once.
        .exclude(status=RequestStatus.CLOSED_DUPLICATE)
        .filter(Q(closed_at__isnull=True) | Q(closed_at__gte=start))
        .select_related("requester")
        .prefetch_related(
            "installments",
            "record_items__withholding_exemption",
            "record_items__withholding_exemptions",
            "record_items__redactions__exemption",
        )
        .order_by("received_at", "number")
    )
    received = [prr for prr in all_requests if start <= prr.received_at < end]
    open_at_start = [
        prr
        for prr in all_requests
        if prr.received_at < start
        and (prr.closed_at is None or prr.closed_at >= start)
    ]
    closed = [
        prr
        for prr in all_requests
        if prr.closed_at is not None and start <= prr.closed_at < end
    ]
    received_closed = [prr for prr in received if prr in closed]
    carryover_closed = [prr for prr in open_at_start if prr in closed]
    current_metrics = {
        snapshot.request_id: snapshot
        for snapshot in JlarcRequestMetrics.objects.filter(
            request__in=all_requests,
            superseded_by__isnull=True,
        )
    }

    baseline = {
        "open_at_start": len(open_at_start),
        "open_at_start_closed_during_period": len(carryover_closed),
        "received_during_period": len(received),
        "received_during_period_closed_during_period": len(received_closed),
        "total_closed_during_period": len(closed),
    }
    metric1_eligible = [
        prr
        for prr in received_closed
        if prr.status not in ABANDONED_STATUSES | {
            RequestStatus.CLOSED_NON_REQUEST,
            RequestStatus.CLOSED_DUPLICATE,
        }
    ]
    within_five = sum(
        business_days_between(
            effective_receipt_date(prr.received_at),
            timezone.localtime(prr.closed_at).date(),
        )
        <= 5
        for prr in metric1_eligible
    )
    estimate_request_ids = {
        prr.pk
        for prr in received
        if prr.statutory_response_due is not None
        and EstimateRevision.objects.filter(
            request=prr,
            new_date__gt=prr.statutory_response_due,
        ).exists()
    }
    disposition_days = [
        max(
            1,
            (
                timezone.localtime(prr.closed_at).date()
                - timezone.localtime(prr.received_at).date()
            ).days,
        )
        for prr in closed
    ]
    clarifications = StatusEvent.objects.filter(
        occurred_at__gte=start,
        occurred_at__lt=end,
        to_status=RequestStatus.CLARIFICATION,
    ).values("request_id").distinct().count()
    denied_full = sum(prr.status == RequestStatus.DENIED for prr in closed)
    partial_denials = [prr for prr in closed if _request_has_partial_denial(prr)]
    denial_reasons = Counter()
    for prr in closed:
        if prr.status == RequestStatus.DENIED or prr in partial_denials:
            denial_reasons.update(_request_citations(prr))
    requester_types = Counter(
        current_metrics[prr.pk].requester_type
        if prr.pk in current_metrics
        else JlarcRequestMetrics.RequesterType.UNKNOWN
        for prr in received
    )
    by_requester_type = {
        value: requester_types.get(value, 0)
        for value in JlarcRequestMetrics.RequesterType.values
    }
    formats = Counter()
    unclassified = []
    for prr in closed:
        snapshot = current_metrics.get(prr.pk)
        if snapshot is not None:
            formats[snapshot.fulfillment_format] += 1
        elif prr.status in NO_FULFILLMENT_STATUSES:
            formats[JlarcRequestMetrics.FulfillmentFormat.NO_RECORDS_OR_OTHER] += 1
        elif any(inst.released_at is not None for inst in prr.installments.all()):
            formats[JlarcRequestMetrics.FulfillmentFormat.ELECTRONIC] += 1
        else:
            unclassified.append(prr.number)
    scanned = sum(
        bool(current_metrics.get(prr.pk) and current_metrics[prr.pk].scanned_physical)
        for prr in closed
    )
    time_minutes = (
        TimeEntry.objects.filter(date__gte=start.date(), date__lt=end.date())
        .aggregate(total=Sum("minutes"))["total"]
        or 0
    ) + supplement.unallocated_staff_minutes
    request_workload = len(open_at_start) + len(received)
    fulfillment_cost = (
        supplement.staff_compensation_cost
        + supplement.legal_review_cost
        + supplement.supplies_services_cost
    )
    litigation_cost = (
        supplement.litigation_agency_cost
        + supplement.litigation_settlements
        + supplement.litigation_penalties
        + supplement.litigation_requester_attorney_fees
        + supplement.litigation_requester_costs
    )
    management_cost = (
        supplement.records_management_staff_cost
        + supplement.records_management_systems_cost
        + supplement.records_management_other_cost
    )
    period_payments = list(
        FeePayment.objects.filter(received_on__gte=start.date(), received_on__lt=end.date())
        .select_related("assessment")
    )
    recovered_in_app = sum((payment.amount for payment in period_payments), Decimal(0))
    payments_by_assessment = Counter()
    for payment in period_payments:
        payments_by_assessment[payment.assessment_id] += payment.amount
    custom_recovered_in_app = Decimal(0)
    partial_custom_assessments = []
    for payment in period_payments:
        assessment = payment.assessment
        if assessment.customized_service_cost <= 0:
            continue
        if payments_by_assessment[assessment.pk] >= assessment.amount:
            custom_recovered_in_app += min(
                assessment.customized_service_cost,
                assessment.amount,
            )
        elif str(assessment.pk) not in partial_custom_assessments:
            partial_custom_assessments.append(str(assessment.pk))
    total_recovered = recovered_in_app + supplement.other_expenses_recovered
    custom_recovered = (
        custom_recovered_in_app
        + supplement.customized_service_expenses_recovered_external
    )

    metrics = {
        "1": {
            "label": "Requests closed within five business days",
            "requests_closed_within_five_business_days": within_five,
        },
        "2": {
            "label": "Requests given an estimate beyond five business days",
            "requests_with_estimate_beyond_five_days": len(estimate_request_ids),
        },
        "3": {
            "label": "Days to final disposition",
            "requests_with_final_disposition": len(closed),
            "total_calendar_days_to_final_disposition": sum(disposition_days),
            "median_calendar_days": (
                f"{Decimal(str(statistics.median(disposition_days))):.2f}"
                if disposition_days
                else "0.00"
            ),
            "average_calendar_days": (
                f"{Decimal(sum(disposition_days)) / Decimal(len(disposition_days)):.2f}"
                if disposition_days
                else "0.00"
            ),
        },
        "4": {
            "label": "Requests where clarification was sought",
            "requests_with_clarification": clarifications,
        },
        "5": {
            "label": "Requests denied in full or part",
            "denied_in_full": denied_full,
            "partially_denied_or_redacted": len(partial_denials),
            "common_reasons": [
                {"citation": citation, "request_count": count}
                for citation, count in denial_reasons.most_common(10)
            ],
        },
        "6": {
            "label": "Requests abandoned by requesters",
            "abandoned": sum(prr.status in ABANDONED_STATUSES for prr in closed),
        },
        "7": {
            "label": "Requests by voluntarily known requester type",
            "by_requester_type": by_requester_type,
        },
        "8": {
            "label": "Electronic and physical fulfillment",
            "electronic": formats[JlarcRequestMetrics.FulfillmentFormat.ELECTRONIC],
            "physical": formats[JlarcRequestMetrics.FulfillmentFormat.PHYSICAL],
            "both": formats[JlarcRequestMetrics.FulfillmentFormat.BOTH],
            "no_records_or_other": formats[
                JlarcRequestMetrics.FulfillmentFormat.NO_RECORDS_OR_OTHER
            ],
            "unclassified": len(unclassified),
            "percent_electronic": _percent(
                formats[JlarcRequestMetrics.FulfillmentFormat.ELECTRONIC],
                len(closed),
            ),
            "percent_physical": _percent(
                formats[JlarcRequestMetrics.FulfillmentFormat.PHYSICAL],
                len(closed),
            ),
            "percent_both": _percent(
                formats[JlarcRequestMetrics.FulfillmentFormat.BOTH],
                len(closed),
            ),
            "percent_no_records_or_other": _percent(
                formats[JlarcRequestMetrics.FulfillmentFormat.NO_RECORDS_OR_OTHER],
                len(closed),
            ),
        },
        "9": {
            "label": "Requests involving physical-record scanning",
            "requests_involving_physical_scanning": scanned,
        },
        "10": {
            "label": "Estimated staff time",
            "total_staff_minutes": time_minutes,
            "total_staff_hours": f"{Decimal(time_minutes) / Decimal(60):.2f}",
            "average_staff_minutes_per_request": (
                f"{Decimal(time_minutes) / Decimal(request_workload):.2f}"
                if request_workload
                else "0.00"
            ),
        },
        "11": {
            "label": "Estimated fulfillment costs",
            "staff_compensation_cost": _money_string(supplement.staff_compensation_cost),
            "legal_review_cost": _money_string(supplement.legal_review_cost),
            "supplies_services_cost": _money_string(supplement.supplies_services_cost),
            "total_fulfillment_cost": _money_string(fulfillment_cost),
            "average_cost_per_request": (
                _money_string(fulfillment_cost / Decimal(request_workload))
                if request_workload
                else "0.00"
            ),
        },
        "12": {
            "label": "Claims filed",
            "claims_filed": supplement.claims_filed,
            "by_type": supplement.claims_by_type,
            "by_exemption": supplement.claims_by_exemption,
        },
        "13": {
            "label": "Public-records litigation costs",
            "agency_cost": _money_string(supplement.litigation_agency_cost),
            "settlements": _money_string(supplement.litigation_settlements),
            "penalties": _money_string(supplement.litigation_penalties),
            "requester_attorney_fees": _money_string(
                supplement.litigation_requester_attorney_fees
            ),
            "requester_costs": _money_string(supplement.litigation_requester_costs),
            "total_litigation_cost": _money_string(litigation_cost),
        },
        "14": {
            "label": "Records management and retention costs",
            "staff_cost": _money_string(supplement.records_management_staff_cost),
            "systems_cost": _money_string(supplement.records_management_systems_cost),
            "other_cost": _money_string(supplement.records_management_other_cost),
            "total_records_management_cost": _money_string(management_cost),
        },
        "15": {
            "label": "Expenses recovered from requesters",
            "in_application_receipts": _money_string(recovered_in_app),
            "other_expenses_recovered": _money_string(
                supplement.other_expenses_recovered
            ),
            "total_expenses_recovered": _money_string(total_recovered),
            "customized_service_charges_recovered": _money_string(custom_recovered),
        },
    }
    quality_warnings = []
    if unclassified:
        quality_warnings.append(
            "Fulfillment format is unclassified for closed requests: "
            + ", ".join(unclassified)
        )
    if partial_custom_assessments:
        quality_warnings.append(
            "Customized-service recovery needs manual allocation for partial "
            "payments on assessments: " + ", ".join(partial_custom_assessments)
        )
    request_log = []
    missing_log_descriptions = []
    for prr in all_requests:
        snapshot = current_metrics.get(prr.pk)
        citations = _request_citations(prr)
        produced = snapshot.records_produced_description if snapshot else ""
        withheld = snapshot.records_withheld_description if snapshot else ""
        if prr.closed_at and (not produced or not withheld):
            missing_log_descriptions.append(prr.number)
        identity = prr.requester.name.strip()
        if prr.requester.email:
            identity = (
                f"{identity} <{prr.requester.email}>"
                if identity
                else prr.requester.email
            )
        request_log.append({
            "request_number": prr.number,
            "requester_identity": identity,
            "received_date": timezone.localtime(prr.received_at).date().isoformat(),
            "original_request": prr.description,
            "records_produced_description": produced,
            "records_withheld_description": withheld,
            "withholding_reasons": citations,
            "final_disposition_date": (
                timezone.localtime(prr.closed_at).date().isoformat()
                if prr.closed_at
                else None
            ),
        })
    if missing_log_descriptions:
        quality_warnings.append(
            "RCW 40.14.026(4) request-log production/withholding descriptions "
            "need classification for: " + ", ".join(missing_log_descriptions)
        )
    core = {
        "reporting_year": year,
        "period": {
            "start": start.date().isoformat(),
            "end_exclusive": end.date().isoformat(),
        },
        "guidance": {
            "statute": "RCW 40.14.026(4)-(6)",
            "metric_guidance_version": "January 2025",
            "submission": "Human transfer to the JLARC online reporting system; no automatic submission.",
        },
        "supplement": {
            "id": str(supplement.pk),
            "revision": supplement.revision,
            "methodology_notes": supplement.methodology_notes,
            "created_by": supplement.created_by.username,
            "created_at": supplement.created_at.isoformat(),
        },
        "baseline": baseline,
        "metrics": metrics,
        "quality_warnings": quality_warnings,
        "request_log": request_log,
    }
    digest = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **core,
        "data_sha256": digest,
        "generated_at": timezone.now().isoformat(),
    }
