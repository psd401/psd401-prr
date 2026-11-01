"""Human-authorized RCW 42.56.120 fee schedules and receipt evidence."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.models import audit

from .models import (
    FeeAssessment,
    FeeDepositGate,
    FeePayment,
    FeeSchedule,
    FeeWaiverFloor,
    Installment,
    PublicRecordsRequest,
)
from .services import CLOSED_STATUSES, _require_current_records_officer

CENT = Decimal("0.01")
STATUTORY_CAPS = {
    "photocopy_page_rate": Decimal("0.15"),
    "scanned_page_rate": Decimal("0.10"),
    "electronic_file_bundle_rate": Decimal("0.05"),
    "electronic_gb_rate": Decimal("0.10"),
    "flat_fee_amount": Decimal("2.00"),
}


def _money(value, *, field):
    try:
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a valid monetary amount.") from exc
    if not result.is_finite() or result < 0:
        raise ValidationError(f"{field} cannot be negative or non-finite.")
    return result


def _decimal(value, *, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a valid number.") from exc
    if not result.is_finite() or result < 0:
        raise ValidationError(f"{field} cannot be negative or non-finite.")
    return result


def _count(value, *, field):
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a whole number.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a whole number.") from exc
    if result < 0 or str(value).strip() not in {str(result), f"{result}.0"}:
        raise ValidationError(f"{field} must be a nonnegative whole number.")
    return result


def _substantive(value, *, field, minimum=8):
    text = str(value or "").strip()
    if len(text) < minimum:
        raise ValidationError(f"A substantive {field} is required.")
    return text


def current_fee_schedule(*, on_date=None):
    """Return the latest effective adopted snapshot, or ``None``."""
    when = on_date or timezone.localdate()
    return (
        FeeSchedule.objects.filter(effective_on__lte=when)
        .order_by("-effective_on", "-activated_at", "-pk")
        .first()
    )


@transaction.atomic
def activate_fee_schedule(
    *,
    actor,
    name,
    basis,
    effective_on,
    counsel_approval_reference,
    adoption_reference,
    actual_cost_statement_reference="",
    photocopy_page_rate=Decimal("0.15"),
    scanned_page_rate=Decimal("0.10"),
    electronic_file_bundle_rate=Decimal("0.05"),
    electronic_gb_rate=Decimal("0.10"),
    flat_fee_amount=Decimal("2.00"),
):
    """Record an immutable schedule only after counsel and agency adoption."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to activate a fee schedule.",
    )
    list(FeeSchedule.objects.select_for_update().order_by("pk").values_list("pk", flat=True))
    schedule_name = _substantive(name, field="schedule name", minimum=4)
    counsel_reference = _substantive(
        counsel_approval_reference,
        field="counsel approval reference",
    )
    adopted_reference = _substantive(
        adoption_reference,
        field="formal adoption reference",
    )
    if basis not in FeeSchedule.Basis.values:
        raise ValidationError("The fee schedule basis is invalid.")
    if not isinstance(effective_on, date):
        raise ValidationError("The fee schedule effective date is invalid.")

    rates = {
        "photocopy_page_rate": _decimal(
            photocopy_page_rate, field="photocopy rate"
        ),
        "scanned_page_rate": _decimal(scanned_page_rate, field="scan rate"),
        "electronic_file_bundle_rate": _decimal(
            electronic_file_bundle_rate, field="electronic file rate"
        ),
        "electronic_gb_rate": _decimal(
            electronic_gb_rate, field="electronic transmission rate"
        ),
    }
    flat_fee = None if flat_fee_amount in (None, "") else _money(
        flat_fee_amount,
        field="flat fee",
    )
    if flat_fee is not None and not Decimal(0) < flat_fee <= Decimal("2.00"):
        raise ValidationError("The flat fee must be greater than zero and no more than $2.00.")
    if basis == FeeSchedule.Basis.STATUTORY_DEFAULT:
        labels = {
            "photocopy_page_rate": "photocopy",
            "scanned_page_rate": "scan",
            "electronic_file_bundle_rate": "electronic file",
            "electronic_gb_rate": "electronic transmission",
        }
        for field, value in rates.items():
            if value > STATUTORY_CAPS[field]:
                raise ValidationError(
                    f"The {labels[field]} rate exceeds the RCW 42.56.120 statutory cap."
                )
    actual_reference = str(actual_cost_statement_reference or "").strip()
    if basis == FeeSchedule.Basis.ACTUAL_COST and len(actual_reference) < 8:
        raise ValidationError(
            "A published actual-cost statement reference is required for an actual-cost schedule."
        )

    previous = FeeSchedule.objects.order_by(
        "-effective_on", "-activated_at", "-pk"
    ).first()
    if previous is not None and effective_on < previous.effective_on:
        raise ValidationError(
            "A replacement schedule cannot take effect before the schedule it supersedes."
        )
    schedule = FeeSchedule.objects.create(
        name=schedule_name,
        basis=basis,
        effective_on=effective_on,
        counsel_approval_reference=counsel_reference,
        adoption_reference=adopted_reference,
        actual_cost_statement_reference=actual_reference,
        flat_fee_amount=flat_fee,
        supersedes=previous,
        activated_by=current_actor,
        **rates,
    )
    audit(
        "fee_schedule.activated",
        target=str(schedule.pk),
        actor=current_actor,
        schedule_id=str(schedule.pk),
        basis=schedule.basis,
        effective_on=schedule.effective_on.isoformat(),
        counsel_approval_reference=schedule.counsel_approval_reference,
        adoption_reference=schedule.adoption_reference,
        supersedes_id=str(previous.pk) if previous else None,
    )
    return schedule


def _current_assessment(installment):
    return (
        FeeAssessment.objects.filter(
            installment=installment,
            superseded_by__isnull=True,
        )
        .select_related("schedule")
        .order_by("-revision")
        .first()
    )


@transaction.atomic
def assess_installment_fee(
    installment_id,
    *,
    actor,
    schedule_id,
    photocopy_pages=0,
    scanned_pages=0,
    electronic_files=0,
    electronic_gb=Decimal(0),
    media_cost=Decimal(0),
    postage_cost=Decimal(0),
    customized_service_cost=Decimal(0),
    customized_service_explanation="",
    customized_service_expertise="",
    requester_notice_reference="",
    requester_approval_reference="",
    use_flat_fee=False,
    waiver_amount=Decimal(0),
    waiver_reason="",
    deposit_percent=Decimal(0),
    reason="",
):
    """Create a new immutable assessment revision and update its projection."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to assess a fee.",
    )
    installment = (
        Installment.objects.select_for_update()
        .select_related("request")
        .get(pk=installment_id)
    )
    if installment.released_at is not None:
        raise ValidationError("A released installment fee cannot be changed.")
    list(
        FeeAssessment.objects.select_for_update()
        .filter(installment=installment)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if FeePayment.objects.select_for_update().filter(installment=installment).exists():
        raise ValidationError(
            "A fee assessment cannot be revised after payment evidence exists."
        )
    try:
        schedule = FeeSchedule.objects.get(pk=schedule_id)
    except (FeeSchedule.DoesNotExist, ValueError) as exc:
        raise ValidationError("The adopted fee schedule was not found.") from exc
    active_schedule = current_fee_schedule()
    if active_schedule is None or schedule.pk != active_schedule.pk:
        raise ValidationError("Only the currently effective adopted fee schedule may be used.")

    counts = {
        "photocopy_pages": _count(photocopy_pages, field="photocopy pages"),
        "scanned_pages": _count(scanned_pages, field="scanned pages"),
        "electronic_files": _count(electronic_files, field="electronic files"),
    }
    gb = _decimal(electronic_gb, field="electronic gigabytes")
    media = _money(media_cost, field="media cost")
    postage = _money(postage_cost, field="postage cost")
    custom = _money(customized_service_cost, field="customized service cost")
    deposit = _decimal(deposit_percent, field="deposit percent")
    if deposit > Decimal(10):
        raise ValidationError("A deposit may not exceed ten percent of the estimated charge.")
    substantive_reason = _substantive(reason, field="assessment reason", minimum=10)

    custom_explanation = str(customized_service_explanation or "").strip()
    custom_expertise = str(customized_service_expertise or "").strip()
    notice_reference = str(requester_notice_reference or "").strip()
    approval_reference = str(requester_approval_reference or "").strip()
    if custom > 0:
        custom_requirements = (
            (custom_explanation, "customized service explanation"),
            (custom_expertise, "specific expertise"),
            (notice_reference, "requester notice reference"),
            (approval_reference, "requester approval reference"),
        )
        for value, label in custom_requirements:
            if len(value) < 8:
                raise ValidationError(f"A substantive {label} is required.")
    elif any((custom_explanation, custom_expertise, notice_reference, approval_reference)):
        raise ValidationError(
            "Customized-service evidence may be recorded only with a customized service charge."
        )

    itemized = (
        Decimal(counts["photocopy_pages"]) * schedule.photocopy_page_rate
        + Decimal(counts["scanned_pages"]) * schedule.scanned_page_rate
        + Decimal((counts["electronic_files"] + 3) // 4)
        * schedule.electronic_file_bundle_rate
        + gb * schedule.electronic_gb_rate
        + media
        + postage
        + custom
    ).quantize(CENT, rounding=ROUND_HALF_UP)

    current = _current_assessment(installment)
    active_flat = (
        FeeAssessment.objects.select_related("installment")
        .filter(
            installment__request=installment.request,
            installment__sequence=1,
            use_flat_fee=True,
            superseded_by__isnull=True,
        )
        .exclude(pk=getattr(current, "pk", None))
        .first()
    )
    if use_flat_fee:
        if installment.sequence != 1:
            raise ValidationError("The flat-fee alternative may begin only on the first installment.")
        if schedule.flat_fee_amount is None:
            raise ValidationError("The adopted schedule does not authorize the flat-fee alternative.")
        if itemized < schedule.flat_fee_amount:
            raise ValidationError(
                "The documented itemized estimate must equal or exceed the flat fee."
            )
        gross_amount = schedule.flat_fee_amount
        covered = False
    elif active_flat is not None and installment.sequence > 1:
        gross_amount = Decimal("0.00")
        covered = True
    else:
        gross_amount = itemized
        covered = False

    waived = _money(waiver_amount, field="waiver amount")
    if waived > gross_amount:
        raise ValidationError("The waiver amount cannot exceed the calculated charge.")
    waiver_text = str(waiver_reason or "").strip()
    if waived > 0 and len(waiver_text) < 10:
        raise ValidationError("A substantive fee-waiver reason is required.")
    if waived == 0 and waiver_text:
        raise ValidationError("A waiver reason may be recorded only with a waiver amount.")
    amount = (gross_amount - waived).quantize(CENT, rounding=ROUND_HALF_UP)
    deposit_amount = (amount * deposit / Decimal(100)).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )
    revision = current.revision + 1 if current else 1
    assessment = FeeAssessment.objects.create(
        installment=installment,
        schedule=schedule,
        revision=revision,
        supersedes=current,
        electronic_gb=gb,
        media_cost=media,
        postage_cost=postage,
        customized_service_cost=custom,
        customized_service_explanation=custom_explanation,
        customized_service_expertise=custom_expertise,
        requester_notice_reference=notice_reference,
        requester_approval_reference=approval_reference,
        itemized_amount=itemized,
        use_flat_fee=bool(use_flat_fee),
        covered_by_request_flat_fee=covered,
        waiver_amount=waived,
        waiver_reason=waiver_text,
        amount=amount,
        deposit_percent=deposit,
        deposit_amount=deposit_amount,
        reason=substantive_reason,
        created_by=current_actor,
        **counts,
    )
    installment.fee_amount = amount
    installment.fee_paid_at = None
    installment.save(update_fields=["fee_amount", "fee_paid_at"])
    audit(
        "installment.fee_assessed",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        assessment_id=str(assessment.pk),
        revision=assessment.revision,
        schedule_id=str(schedule.pk),
        itemized_amount=f"{assessment.itemized_amount:.2f}",
        amount=f"{assessment.amount:.2f}",
        deposit_amount=f"{assessment.deposit_amount:.2f}",
        use_flat_fee=assessment.use_flat_fee,
        covered_by_request_flat_fee=assessment.covered_by_request_flat_fee,
        waiver_amount=f"{assessment.waiver_amount:.2f}",
        reason=assessment.reason,
    )
    return assessment


@transaction.atomic
def record_fee_payment(
    installment_id,
    *,
    actor,
    amount,
    method,
    reference,
    received_on,
):
    """Append receipt evidence and mark the projection paid only at full total."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to record a fee payment.",
    )
    installment = (
        Installment.objects.select_for_update()
        .select_related("request")
        .get(pk=installment_id)
    )
    if installment.released_at is not None:
        raise ValidationError("Payment evidence cannot be added after release.")
    assessment = _current_assessment(installment)
    if assessment is None or assessment.amount <= 0:
        raise ValidationError("A positive current fee assessment is required before payment.")
    if method not in FeePayment.Method.values:
        raise ValidationError("The payment method is invalid.")
    receipt_reference = _substantive(reference, field="payment reference", minimum=5)
    if not isinstance(received_on, date) or received_on > timezone.localdate():
        raise ValidationError("The payment received date is invalid or in the future.")
    payment_amount = _money(amount, field="payment amount")
    if payment_amount <= 0:
        raise ValidationError("The payment amount must be greater than zero.")
    existing_total = (
        FeePayment.objects.select_for_update()
        .filter(installment=installment)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal(0)
    )
    new_total = existing_total + payment_amount
    if new_total > assessment.amount:
        raise ValidationError("Recorded payments cannot exceed the current assessed fee.")
    payment = FeePayment.objects.create(
        installment=installment,
        assessment=assessment,
        amount=payment_amount,
        method=method,
        reference=receipt_reference,
        received_on=received_on,
        recorded_by=current_actor,
    )
    if new_total == assessment.amount:
        installment.fee_paid_at = timezone.now()
        installment.save(update_fields=["fee_paid_at"])
    audit(
        "installment.fee_payment_recorded",
        target=installment.request.number,
        actor=current_actor,
        installment_id=installment.pk,
        assessment_id=str(assessment.pk),
        payment_id=str(payment.pk),
        amount=f"{payment.amount:.2f}",
        cumulative_amount=f"{new_total:.2f}",
        method=payment.method,
        reference=payment.reference,
        received_on=payment.received_on.isoformat(),
        paid_in_full=new_total == assessment.amount,
    )
    return payment


#: RCW 42.56.120(2) caps a deposit at ten percent of the estimated charge.
DEPOSIT_PERCENT_CAP = Decimal(10)

#: How long an unpaid fee or deposit waits before the case surfaces in the
#: officer's inbox. Nothing closes at day 30; RCW 42.56.120(2) permits the
#: District to stop fulfilling an unpaid request, and a person decides that.
UNPAID_FEE_ATTENTION_DAYS = 30


def current_fee_waiver_floor(*, on_date=None):
    """Return the adopted waiver floor in effect, or ``None``."""
    when = on_date or timezone.localdate()
    return (
        FeeWaiverFloor.objects.filter(effective_on__lte=when)
        .order_by("-effective_on", "-activated_at", "-pk")
        .first()
    )


@transaction.atomic
def activate_fee_waiver_floor(
    *,
    actor,
    amount,
    counsel_reference,
    effective_on,
):
    """Adopt the amount below which an officer is prompted to waive a fee.

    Adopting a floor changes no existing assessment and waives nothing. It
    only supplies the number the workspace compares an assessment against.
    """
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to adopt a fee waiver "
            "floor."
        ),
    )
    list(
        FeeWaiverFloor.objects.select_for_update()
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    floor_amount = _money(amount, field="waiver floor amount")
    if floor_amount <= 0:
        raise ValidationError("The waiver floor must be greater than zero.")
    reference = _substantive(counsel_reference, field="counsel reference")
    if not isinstance(effective_on, date):
        raise ValidationError("The waiver floor effective date is invalid.")
    previous = FeeWaiverFloor.objects.order_by(
        "-effective_on", "-activated_at", "-pk"
    ).first()
    if previous is not None and effective_on < previous.effective_on:
        raise ValidationError(
            "A replacement waiver floor cannot take effect before the floor "
            "it supersedes."
        )
    floor = FeeWaiverFloor.objects.create(
        amount=floor_amount,
        counsel_reference=reference,
        effective_on=effective_on,
        supersedes=previous,
        activated_by=current_actor,
    )
    audit(
        "fee_waiver_floor.activated",
        target=str(floor.pk),
        actor=current_actor,
        floor_id=str(floor.pk),
        amount=f"{floor.amount:.2f}",
        counsel_reference=floor.counsel_reference,
        effective_on=floor.effective_on.isoformat(),
        supersedes_id=str(previous.pk) if previous else None,
    )
    return floor


def assessment_is_below_waiver_floor(assessment, *, floor=None):
    """Does this assessment fall under the adopted floor without being free?

    An assessment already reduced to zero needs no prompt: there is nothing
    left to waive.
    """
    if assessment is None or assessment.amount <= 0:
        return False
    adopted = floor if floor is not None else current_fee_waiver_floor()
    if adopted is None:
        return False
    return assessment.amount < adopted.amount


def open_deposit_gate(request, *, scope):
    """The open deposit gate on this case for that work, or ``None``."""
    if scope not in FeeDepositGate.Scope.values:
        raise ValidationError("The deposit gate scope is invalid.")
    return (
        FeeDepositGate.objects.filter(
            request=request,
            scope=scope,
            satisfied_at__isnull=True,
            lifted_at__isnull=True,
        )
        .order_by("-applied_at")
        .first()
    )


def assert_deposit_gate_clear(request, *, scope):
    """Refuse gated work while its deposit is unpaid.

    Raised as a ``ValidationError`` rather than a permission error: the work
    is not forbidden, it is waiting on money the District asked for.
    """
    gate = open_deposit_gate(request, scope=scope)
    if gate is None:
        return
    label = FeeDepositGate.Scope(gate.scope).label.lower()
    raise ValidationError(
        f"A ${gate.amount:.2f} deposit requested on "
        f"{gate.requested_on.isoformat()} must be recorded before {label} "
        "work continues, or the gate must be lifted with a reason."
    )


@transaction.atomic
def apply_deposit_gate(
    request_number,
    *,
    actor,
    scope,
    amount,
    estimated_total,
    reason,
    requested_on=None,
):
    """Hold collection or production on this case until a deposit is paid."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to require a deposit.",
    )
    prr = (
        PublicRecordsRequest.objects.select_for_update()
        .get(number=request_number)
    )
    if prr.status in CLOSED_STATUSES:
        raise ValidationError("A closed request cannot take a new deposit gate.")
    if scope not in FeeDepositGate.Scope.values:
        raise ValidationError("The deposit gate scope is invalid.")
    schedule = current_fee_schedule()
    if schedule is None:
        raise ValidationError(
            "An adopted fee schedule is required before the District may "
            "require a deposit."
        )
    gate_amount = _money(amount, field="deposit amount")
    total = _money(estimated_total, field="estimated charge")
    if gate_amount <= 0:
        raise ValidationError("The deposit must be greater than zero.")
    if total <= 0:
        raise ValidationError("The estimated charge must be greater than zero.")
    # Round the cap DOWN. Rounding a ten-percent ceiling up to the nearest
    # cent would let a deposit sit above ten percent of the estimate, which
    # is exactly what RCW 42.56.120(2) forbids.
    cap = (total * DEPOSIT_PERCENT_CAP / Decimal(100)).quantize(
        CENT,
        rounding=ROUND_DOWN,
    )
    if gate_amount > cap:
        raise ValidationError(
            "A deposit may not exceed ten percent of the estimated charge."
        )
    substantive_reason = _substantive(reason, field="deposit reason", minimum=10)
    asked_on = requested_on or timezone.localdate()
    if not isinstance(asked_on, date):
        raise ValidationError("The deposit request date is invalid.")
    if asked_on > timezone.localdate():
        raise ValidationError("The deposit request date cannot be in the future.")
    if asked_on < timezone.localtime(prr.received_at).date():
        raise ValidationError(
            "The deposit request cannot predate the public records request."
        )
    if open_deposit_gate(prr, scope=scope) is not None:
        raise ValidationError(
            "That work is already held by an open deposit on this request."
        )
    gate = FeeDepositGate.objects.create(
        request=prr,
        scope=scope,
        schedule=schedule,
        amount=gate_amount,
        estimated_total=total,
        reason=substantive_reason,
        requested_on=asked_on,
        applied_by=current_actor,
    )
    audit(
        "fee_deposit_gate.applied",
        target=prr.number,
        actor=current_actor,
        gate_id=str(gate.pk),
        scope=gate.scope,
        amount=f"{gate.amount:.2f}",
        estimated_total=f"{gate.estimated_total:.2f}",
        schedule_id=str(schedule.pk),
        requested_on=gate.requested_on.isoformat(),
        reason=gate.reason,
    )
    return gate


@transaction.atomic
def record_deposit_payment(
    gate_id,
    *,
    actor,
    amount,
    method,
    reference,
    received_on,
):
    """Record the deposit evidence that releases the gated work."""
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to record a deposit "
            "payment."
        ),
    )
    gate = (
        FeeDepositGate.objects.select_for_update()
        .select_related("request")
        .get(pk=gate_id)
    )
    if gate.lifted_at is not None:
        raise ValidationError("A lifted deposit gate takes no payment.")
    if gate.satisfied_at is not None:
        raise ValidationError("This deposit is already recorded as paid.")
    if method not in FeePayment.Method.values:
        raise ValidationError("The payment method is invalid.")
    receipt_reference = _substantive(
        reference,
        field="payment reference",
        minimum=5,
    )
    if not isinstance(received_on, date) or received_on > timezone.localdate():
        raise ValidationError(
            "The payment received date is invalid or in the future."
        )
    paid = _money(amount, field="payment amount")
    if paid < gate.amount:
        raise ValidationError(
            "A partial deposit does not release the held work; record the "
            "full deposit or lift the gate with a reason."
        )
    gate.satisfied_at = timezone.now()
    gate.satisfied_by = current_actor
    gate.payment_amount = paid
    gate.payment_method = method
    gate.payment_reference = receipt_reference
    gate.payment_received_on = received_on
    gate.save(
        update_fields=[
            "satisfied_at",
            "satisfied_by",
            "payment_amount",
            "payment_method",
            "payment_reference",
            "payment_received_on",
        ]
    )
    audit(
        "fee_deposit_gate.satisfied",
        target=gate.request.number,
        actor=current_actor,
        gate_id=str(gate.pk),
        scope=gate.scope,
        amount=f"{gate.payment_amount:.2f}",
        method=gate.payment_method,
        reference=gate.payment_reference,
        received_on=gate.payment_received_on.isoformat(),
    )
    return gate


@transaction.atomic
def lift_deposit_gate(gate_id, *, actor, reason):
    """Release held work without payment, on a named officer's written reason.

    There is no automatic path here and no schedule that lifts a gate: a
    deposit the District asked for and then stopped waiting on is a decision
    somebody has to own.
    """
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to lift a deposit gate.",
    )
    gate = (
        FeeDepositGate.objects.select_for_update()
        .select_related("request")
        .get(pk=gate_id)
    )
    if gate.satisfied_at is not None:
        raise ValidationError("A paid deposit gate is already closed.")
    if gate.lifted_at is not None:
        raise ValidationError("This deposit gate is already lifted.")
    substantive_reason = _substantive(reason, field="lift reason", minimum=10)
    gate.lifted_at = timezone.now()
    gate.lifted_by = current_actor
    gate.lift_reason = substantive_reason
    gate.save(update_fields=["lifted_at", "lifted_by", "lift_reason"])
    audit(
        "fee_deposit_gate.lifted",
        target=gate.request.number,
        actor=current_actor,
        gate_id=str(gate.pk),
        scope=gate.scope,
        amount=f"{gate.amount:.2f}",
        reason=gate.lift_reason,
    )
    return gate


def unpaid_fee_attention_requests(*, as_of=None):
    """Cases whose fee or deposit has gone unanswered long enough to surface.

    This is a read. It closes nothing, sends nothing, and drafts nothing: the
    non-payment closure it feeds is a Records Officer's letter.
    """
    today = as_of or timezone.localdate()
    cutoff_date = today - timedelta(days=UNPAID_FEE_ATTENTION_DAYS)
    cutoff_moment = timezone.make_aware(
        datetime.combine(cutoff_date, datetime_time.max),
        timezone.get_current_timezone(),
    )
    gated = set(
        FeeDepositGate.objects.filter(
            satisfied_at__isnull=True,
            lifted_at__isnull=True,
            requested_on__lte=cutoff_date,
        )
        .exclude(request__status__in=CLOSED_STATUSES)
        .values_list("request_id", flat=True)
    )
    assessed = set(
        FeeAssessment.objects.filter(
            superseded_by__isnull=True,
            amount__gt=0,
            created_at__lte=cutoff_moment,
            installment__fee_paid_at__isnull=True,
            installment__released_at__isnull=True,
        )
        .exclude(installment__request__status__in=CLOSED_STATUSES)
        .values_list("installment__request_id", flat=True)
    )
    return PublicRecordsRequest.objects.filter(
        pk__in=gated | assessed
    ).order_by("received_at")
