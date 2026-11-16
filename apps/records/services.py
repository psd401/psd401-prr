"""
Lifecycle service layer — ALL status changes go through transition().

Why: a status change is never just a field update. It must atomically
(1) write the append-only StatusEvent, (2) start/stop the clarification
tolling clock, (3) recompute the statutory deadline, and (4) audit-log.
Centralizing this is what keeps the compliance record coherent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.api.identity import WORKFLOW_STAFF_ROLES, has_staff_role
from apps.api.models import StaffRole
from apps.audit.models import audit

from .deadlines import business_days_between, statutory_response_due
from .models import (
    FerpaIdentityVerification,
    FerpaIdentityVerificationMethod,
    MatterKind,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle exists only at runtime
    from apps.correspondence.services import RequesterCorrectionRefresh

CLOSED_STATUSES = {
    RequestStatus.FULFILLED, RequestStatus.CLOSED_WITHDRAWN,
    RequestStatus.CLOSED_ABANDONED, RequestStatus.CLOSED_NON_REQUEST,
    RequestStatus.CLOSED_NO_RECORDS,
    RequestStatus.CLOSED_DUPLICATE,
    RequestStatus.DENIED,
}

CORRESPONDENCE_DRIVEN_STATUSES = {
    RequestStatus.ACKNOWLEDGED,
    RequestStatus.CLARIFICATION,
    RequestStatus.DENIED,
    RequestStatus.CLOSED_NO_RECORDS,
}

#: Where a reopened request may land. Deliberately excludes every
#: correspondence-driven status: ACKNOWLEDGED, CLARIFICATION and DENIED are
#: only true once a message was actually delivered to the requester, and
#: reopening a case sends nothing. Landing a reopen there would assert a
#: delivery that never happened.
REOPENABLE_TARGET_STATUSES = {
    RequestStatus.RECEIVED,
    RequestStatus.GATHERING,
    RequestStatus.REVIEW,
    RequestStatus.INSTALLMENT,
}

CLARIFICATION_CANCELLATION_CLOSURES = {
    RequestStatus.CLOSED_WITHDRAWN,
    RequestStatus.CLOSED_ABANDONED,
    RequestStatus.CLOSED_NON_REQUEST,
    RequestStatus.CLOSED_DUPLICATE,
}

#: The closures any staff member working a case may record.
#:
#: Withdrawal and abandonment are documentary facts about what the requester
#: did -- they wrote in and withdrew, or the documented clarification period
#: ran out. They are not legal determinations about the records themselves,
#: which is what the Records Officer's exclusive authority actually protects:
#: denial, no-responsive-records, fulfilment, and the "this was never a
#: records request" closure all still require that officer. Reserving
#: withdrawal and abandonment for one person only left finished cases sitting
#: open behind a queue for a signature nobody needed. What makes these
#: closures accountable is the substantive written reason on the timeline and
#: the append-only audit entry naming who recorded it, and both are still
#: required here.
STAFF_CLOSABLE_STATUSES = {
    RequestStatus.CLOSED_WITHDRAWN,
    RequestStatus.CLOSED_ABANDONED,
}


class CorrespondenceDeliveryRequired(ValueError):
    """Raised when a manual transition would bypass confirmed delivery."""


class ClarificationResolutionRequired(ValueError):
    """Raised when a manual transition would bypass clarification resolution."""


class ClosedRequestTransition(ValueError):
    """Raised when ordinary transition() is used to leave a closed status.

    Reopening a closed request is a real records-management action, but it is
    not an ordinary state change: it needs a named Records Officer and a
    written reason on the timeline. ``reopen_request`` is the only way to do
    it, so this guard stays as the wall that forces callers through it.
    """


class TerminalActionRequired(ValueError):
    """Raised when a terminal legal action lacks accountable human evidence."""


class EmailIntakeClosureRequired(ValueError):
    """Raised when non-request closure lacks canonical email-intake evidence."""


#: Every refusal this module raises when a lifecycle action is not allowed to
#: proceed. Named once so callers catch the same closed set instead of each
#: keeping its own list that drifts when a new refusal is added. Deliberately
#: not `ValueError`: a bare ValueError from deeper in the stack is a bug, not a
#: refusal, and must not be reported to a caller as a rejected request.
LIFECYCLE_REFUSALS = (
    CorrespondenceDeliveryRequired,
    ClarificationResolutionRequired,
    ClosedRequestTransition,
    TerminalActionRequired,
    EmailIntakeClosureRequired,
)


@transaction.atomic
def assign_request_owner(
    prr: PublicRecordsRequest,
    *,
    owner,
    actor,
) -> PublicRecordsRequest:
    """Assign accountable ownership from fresh, locked authority state."""
    actor_id = getattr(actor, "pk", None)
    owner_id = getattr(owner, "pk", None)
    if actor_id is None:
        raise PermissionDenied(
            "Current Records Officer authority is required to assign a case owner."
        )
    if owner is not None and owner_id is None:
        raise ValidationError("The case owner must be a saved user.")

    locked_request = PublicRecordsRequest.objects.select_for_update(
        of=("self",)
    ).get(pk=prr.pk)
    previous_owner_id = locked_request.assigned_officer_id
    user_ids = sorted({
        user_id
        for user_id in (actor_id, owner_id, previous_owner_id)
        if user_id is not None
    })
    user_model = get_user_model()
    locked_users = user_model.objects.select_for_update(
        of=("self",)
    ).filter(pk__in=user_ids).in_bulk()
    membership_model = user_model.groups.through
    list(
        membership_model.objects.select_for_update()
        .filter(user_id__in=user_ids)
        .order_by("user_id", "pk")
        .values_list("pk", flat=True)
    )

    current_actor = locked_users.get(actor_id)
    if (
        current_actor is None
        or not current_actor.is_active
        or not current_actor.is_staff
        or not has_staff_role(current_actor, StaffRole.RECORDS_OFFICER)
    ):
        raise PermissionDenied(
            "Current Records Officer authority is required to assign a case owner."
        )

    current_owner = locked_users.get(owner_id) if owner_id is not None else None
    if current_owner is not None and (
        not current_owner.is_active
        or not current_owner.is_staff
        or not has_staff_role(current_owner, StaffRole.RECORDS_OFFICER)
    ):
        raise ValidationError("The case owner must be an active Records Officer.")
    if owner_id is not None and current_owner is None:
        raise ValidationError("The case owner no longer exists.")

    if previous_owner_id == owner_id:
        return locked_request

    previous_owner = locked_users.get(previous_owner_id)
    occurred_at = timezone.now()
    locked_request.assigned_officer = current_owner
    locked_request.save(update_fields=["assigned_officer"])
    audit(
        "request.owner_changed",
        target=locked_request.number,
        actor=current_actor,
        previous_owner_id=previous_owner_id,
        previous_owner_username=(
            previous_owner.username if previous_owner is not None else None
        ),
        new_owner_id=owner_id,
        new_owner_username=(
            current_owner.username if current_owner is not None else None
        ),
        occurred_at=occurred_at.isoformat(),
    )
    return locked_request


_CLARIFICATION_EXIT_MARKER = object()


class _ClarificationExitEvidence:
    """Unexported proof created only by the dedicated cycle workflows."""

    __slots__ = ("_marker", "cycle_id", "mode")

    def __init__(self, *, cycle_id, mode: str):
        self._marker = _CLARIFICATION_EXIT_MARKER
        self.cycle_id = cycle_id
        self.mode = mode


def _require_current_staff(*, actor, message: str, roles):
    """Reload and lock a human actor before authorizing a new legal action."""
    actor_id = getattr(actor, "pk", None)
    if actor_id is None:
        raise PermissionDenied(message)
    user_model = get_user_model()
    try:
        current_actor = user_model.objects.select_for_update(
            of=("self",)
        ).get(pk=actor_id)
    except user_model.DoesNotExist as exc:
        raise PermissionDenied(message) from exc

    # Serialize managed-role revocation with the entire legal-write
    # transaction. Locking only auth_user is insufficient because Django
    # stores group authority in separate M2M rows that can otherwise be
    # deleted immediately after this check.
    membership_model = current_actor.groups.through
    list(
        membership_model.objects.select_for_update()
        .filter(user_id=current_actor.pk)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if (
        not getattr(current_actor, "is_authenticated", False)
        or not current_actor.is_active
        or not current_actor.is_staff
        or not has_staff_role(current_actor, *roles)
    ):
        raise PermissionDenied(message)
    return current_actor


def _require_current_records_officer(*, actor, message: str):
    """Authorize a legal action that only the Records Officer may take."""
    return _require_current_staff(
        actor=actor,
        message=message,
        roles=(StaffRole.RECORDS_OFFICER,),
    )


def _closure_authority(to_status: str) -> tuple[tuple[str, ...], str]:
    """Return the roles and refusal message that govern one closure."""
    if to_status in STAFF_CLOSABLE_STATUSES:
        return (
            tuple(sorted(WORKFLOW_STAFF_ROLES)),
            "Staff authority is required for a terminal request action.",
        )
    return (
        (StaffRole.RECORDS_OFFICER,),
        "Records Officer authority is required for a terminal request action.",
    )


def _require_terminal_action_actor(*, actor, reason: str, to_status: str):
    roles, message = _closure_authority(to_status)
    current_actor = _require_current_staff(
        actor=actor,
        message=message,
        roles=roles,
    )
    normalized = (reason or "").strip()
    if len(normalized) < 10:
        raise TerminalActionRequired(
            "A substantive reason is required for a terminal request action."
        )
    return current_actor, normalized


def _require_confirmed_correspondence_delivery(
    prr: PublicRecordsRequest,
    *,
    to_status: str,
    correspondence_id,
    actor,
    note: str,
    visible_to_requester: bool,
    occurred_at,
) -> object:
    """Validate provider evidence authorizing a delivery-driven state."""
    from apps.correspondence.models import (
        Correspondence,
        CorrespondenceDelivery,
        CorrespondenceDeliveryAttempt,
    )
    from apps.correspondence.services import (
        TOLLING_SCOPE_CLARIFICATION_FORM,
    )

    # Two letters can ask a requester to clarify: the plain clarification
    # request, and the clarification form of the scope conversation. Both are
    # RCW 42.56.520(3) requests, so both may open the tolled status; nothing
    # else may.
    expected_kinds = {
        RequestStatus.ACKNOWLEDGED: frozenset(
            {Correspondence.Kind.ACKNOWLEDGMENT}
        ),
        RequestStatus.CLARIFICATION: frozenset(
            {
                Correspondence.Kind.CLARIFICATION_REQUEST,
                Correspondence.Kind.SCOPE_CLARIFICATION,
            }
        ),
        RequestStatus.DENIED: frozenset({Correspondence.Kind.DENIAL}),
        RequestStatus.CLOSED_NO_RECORDS: frozenset(
            {Correspondence.Kind.NO_RECORDS_CLOSURE}
        ),
    }
    permitted_kinds = expected_kinds[to_status]
    label = {
        RequestStatus.ACKNOWLEDGED: "acknowledgment",
        RequestStatus.CLARIFICATION: "clarification request",
        RequestStatus.DENIED: "denial correspondence",
        RequestStatus.CLOSED_NO_RECORDS: "no-records closure correspondence",
    }[to_status]
    if correspondence_id is None:
        raise CorrespondenceDeliveryRequired(
            f"A confirmed {label} delivery is required before this request "
            "status may change."
        )
    try:
        correspondence = Correspondence.objects.select_for_update(
            of=("self",)
        ).get(
            pk=correspondence_id,
            request_id=prr.pk,
            direction=Correspondence.Direction.OUTBOUND,
            kind__in=permitted_kinds,
            status=Correspondence.Status.SENT,
            resulting_status_event__isnull=True,
        )
        delivery = CorrespondenceDelivery.objects.select_for_update(
            of=("self",)
        ).get(
            correspondence=correspondence,
            status=CorrespondenceDelivery.Status.SENT,
            successful_attempt_number__isnull=False,
        )
        attempt = CorrespondenceDeliveryAttempt.objects.select_for_update(
            of=("self",)
        ).get(
            delivery=delivery,
            attempt_number=delivery.successful_attempt_number,
        )
    except (
        Correspondence.DoesNotExist,
        CorrespondenceDelivery.DoesNotExist,
        CorrespondenceDeliveryAttempt.DoesNotExist,
        ValidationError,
    ) as exc:
        raise CorrespondenceDeliveryRequired(
            f"The {label} transition does not have matching confirmed "
            "delivery evidence."
        ) from exc

    expected_kind = correspondence.kind
    if (
        expected_kind == Correspondence.Kind.SCOPE_CLARIFICATION
        and correspondence.approved_metadata.get("form")
        != TOLLING_SCOPE_CLARIFICATION_FORM
    ):
        # An informational scope letter says in its own approved text that it
        # does not stop the clock. Letting it open CLARIFICATION would toll
        # the response period the requester was told stayed running.
        raise CorrespondenceDeliveryRequired(
            "An informational scope letter does not request clarification "
            "and cannot open the tolled clarification status."
        )
    actor_id = getattr(actor, "pk", None)
    approval_actor_matches = (
        correspondence.automated
        and expected_kind == Correspondence.Kind.ACKNOWLEDGMENT
        and correspondence.approved_by_id is None
        and actor is None
    ) or (
        not correspondence.automated
        and correspondence.approved_by_id is not None
        and actor_id == correspondence.approved_by_id
    )
    if (
        not approval_actor_matches
        or correspondence.approved_at is None
        or correspondence.queued_at is None
        or correspondence.sent_at is None
        or occurred_at != correspondence.sent_at
        or not visible_to_requester
        or len((note or "").strip()) < 10
        or delivery.attempt_count != delivery.successful_attempt_number
        or delivery.completed_at is None
        or attempt.result_category
        not in {
            CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED,
        }
        or attempt.result_at != delivery.completed_at
        or not attempt.result_fingerprint
        or attempt.actor_id is None
        or attempt.claim_owner != str(attempt.actor_id)
        or not attempt.provider_message_id
        or attempt.provider_message_id != delivery.provider_message_id
        or attempt.provider_message_id != correspondence.provider_message_id
        or attempt.provider_thread_id != delivery.provider_thread_id
        or attempt.provider_thread_id != correspondence.provider_thread_id
        or attempt.provider_timestamp != correspondence.sent_at
    ):
        raise CorrespondenceDeliveryRequired(
            f"The {label} transition does not have matching confirmed "
            "delivery evidence."
        )
    if expected_kind == Correspondence.Kind.DENIAL:
        from apps.correspondence.denial_basis import (
            require_approved_denial_basis_snapshot,
        )

        try:
            require_approved_denial_basis_snapshot(correspondence, lock=True)
        except ValidationError as exc:
            raise CorrespondenceDeliveryRequired(
                "The denial transition does not have a matching approved "
                "exemption-basis snapshot."
            ) from exc
    if expected_kind == Correspondence.Kind.NO_RECORDS_CLOSURE:
        from apps.correspondence.services import require_adequate_search_snapshot

        try:
            require_adequate_search_snapshot(correspondence, lock=True)
        except ValidationError as exc:
            raise CorrespondenceDeliveryRequired(
                "The no-records closure does not have matching adequate-search evidence."
            ) from exc
    return correspondence


def _require_clarification_exit_evidence(
    prr: PublicRecordsRequest,
    *,
    to_status: str,
    actor,
    note: str,
    occurred_at,
    evidence,
) -> None:
    """Reject every clarification exit except the two dedicated workflows."""
    from apps.correspondence.models import ClarificationCycle, Correspondence

    if (
        not isinstance(evidence, _ClarificationExitEvidence)
        or evidence._marker is not _CLARIFICATION_EXIT_MARKER
    ):
        raise ClarificationResolutionRequired(
            "An open clarification may be left only through the dedicated "
            "clarification resolution or cancellation workflow."
        )
    try:
        cycle = (
            ClarificationCycle.objects.select_for_update(of=("self",))
            .select_related(
                "opened_status_event",
                "inbound_response_correspondence",
            )
            .get(pk=evidence.cycle_id, request_id=prr.pk)
        )
    except ClarificationCycle.DoesNotExist as exc:
        raise ClarificationResolutionRequired(
            "The dedicated clarification exit evidence is missing."
        ) from exc

    opened_event = cycle.opened_status_event
    common_valid = (
        opened_event is not None
        and opened_event.request_id == prr.pk
        and opened_event.to_status == RequestStatus.CLARIFICATION
        and cycle.opened_at == opened_event.occurred_at
    )
    if evidence.mode == "cancelled":
        valid = (
            common_valid
            and to_status in CLARIFICATION_CANCELLATION_CLOSURES
            and cycle.cancelled_at == occurred_at
            and cycle.resolved_at is None
            and cycle.resolved_status_event_id is None
        )
    elif evidence.mode == "resolved":
        inbound = cycle.inbound_response_correspondence
        valid = (
            common_valid
            and to_status == RequestStatus.GATHERING
            and cycle.cancelled_at is None
            and cycle.resolved_at == occurred_at
            and cycle.resolved_by_id == getattr(actor, "pk", None)
            and cycle.resolved_status_event_id is None
            and cycle.responded_at is not None
            and inbound is not None
            and inbound.request_id == prr.pk
            and inbound.direction == Correspondence.Direction.INBOUND
            and inbound.kind == Correspondence.Kind.CLARIFICATION_RESPONSE
            and inbound.status == Correspondence.Status.RECEIVED
            and len(cycle.resolution_notes.strip()) >= 10
            and note == f"Clarification resolved: {cycle.resolution_notes}"
        )
    else:
        valid = False
    if not valid:
        raise ClarificationResolutionRequired(
            "The dedicated clarification exit evidence is incomplete or "
            "does not match this request transition."
        )


def _retire_inbox_rows_on_closure(
    prr: PublicRecordsRequest,
    *,
    actor,
    to_status: str,
) -> None:
    """Closing a case ends the need to triage its requester emails.

    Inbox rows are projections of inbound receipts, and receipt matching does
    not change when a case closes, so nothing else would ever retire them: a
    fulfilled case kept sitting in Needs attention until someone found the
    row and clicked Mark handled. The officer who closed the case is the one
    who handled them, and each row records that attribution.

    A closure without an accountable officer (only the automatic intake
    closure, ``auto_close_non_request``) leaves the rows alone rather than
    inventing a handler.
    """
    if actor is None or getattr(actor, "pk", None) is None:
        return
    # Whoever was allowed to record this closure handled its rows: an officer
    # for most closures, any workflow staff for a withdrawal or abandonment.
    closure_roles, _ = _closure_authority(to_status)
    if not has_staff_role(actor, *closure_roles):
        return
    from apps.work_queue.services import handle_case_inbox_items

    handle_case_inbox_items(
        prr,
        actor=actor,
        reason=f"request_closed:{to_status}",
        roles=frozenset(closure_roles),
    )


def _require_no_unresolved_delivery_before_closure(
    prr: PublicRecordsRequest,
) -> None:
    """Require every frozen outbound delivery to be terminal before closure."""
    # Local import avoids making the records lifecycle module depend on
    # correspondence at application import time.
    from apps.correspondence.models import CorrespondenceDelivery

    unresolved = CorrespondenceDelivery.objects.filter(
        correspondence__request_id=prr.pk,
        status__in=[
            CorrespondenceDelivery.Status.PENDING,
            CorrespondenceDelivery.Status.CLAIMED,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
            CorrespondenceDelivery.Status.RETRYABLE,
            CorrespondenceDelivery.Status.FAILED,
        ],
    ).exists()
    if unresolved:
        raise CorrespondenceDeliveryRequired(
            "Resolve or cancel every unsent correspondence delivery before "
            "closing this request."
        )


def _require_no_unresolved_lifecycle_delivery(
    prr: PublicRecordsRequest,
) -> None:
    """Prevent later actions from overtaking delivery-dependent legal effects."""
    # Local import keeps the records lifecycle independent at import time.
    from apps.correspondence.models import CorrespondenceDelivery
    from apps.correspondence.services import REQUEST_LIFECYCLE_EFFECT_KINDS

    # One list, owned by correspondence: a kind whose delivery moves the case
    # must also hold the case still until that delivery resolves.
    unresolved = CorrespondenceDelivery.objects.filter(
        correspondence__request_id=prr.pk,
        correspondence__kind__in=REQUEST_LIFECYCLE_EFFECT_KINDS,
        status__in=[
            CorrespondenceDelivery.Status.PENDING,
            CorrespondenceDelivery.Status.CLAIMED,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
            CorrespondenceDelivery.Status.RETRYABLE,
            CorrespondenceDelivery.Status.FAILED,
        ],
    ).exists()
    if unresolved:
        raise CorrespondenceDeliveryRequired(
            "Resolve or cancel the outstanding requester-lifecycle delivery "
            "before changing request state or estimates."
        )


@transaction.atomic
def transition(
    prr: PublicRecordsRequest,
    to_status: str,
    *,
    actor=None,
    note: str = "",
    visible_to_requester: bool = False,
    occurred_at=None,
    confirmed_correspondence_id=None,
    _clarification_exit_evidence=None,
    _system_closure_note=None,
) -> PublicRecordsRequest:
    if to_status not in RequestStatus.values:
        raise ValidationError("The destination must be a valid request status.")
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    from_status = prr.status
    if from_status == to_status:
        return prr
    if from_status in CLOSED_STATUSES:
        raise ClosedRequestTransition(
            f"This request is already closed ({from_status}) and cannot be "
            "transitioned further."
        )
    now = occurred_at or timezone.now()
    if timezone.is_naive(now):
        raise ValueError("Status-event timestamps must include a timezone.")
    if from_status == RequestStatus.CLARIFICATION:
        _require_clarification_exit_evidence(
            prr,
            to_status=to_status,
            actor=actor,
            note=note,
            occurred_at=now,
            evidence=_clarification_exit_evidence,
        )

    confirmed_correspondence = None
    if to_status in CORRESPONDENCE_DRIVEN_STATUSES:
        confirmed_correspondence = _require_confirmed_correspondence_delivery(
            prr,
            to_status=to_status,
            correspondence_id=confirmed_correspondence_id,
            actor=actor,
            note=note,
            visible_to_requester=visible_to_requester,
            occurred_at=now,
        )
        if (
            to_status == RequestStatus.ACKNOWLEDGED
            and prr.acknowledgment_sent_at != now
        ):
            raise CorrespondenceDeliveryRequired(
                "The acknowledgment transition does not match the persisted "
                "delivery timestamp."
            )
    if to_status in CLOSED_STATUSES:
        if _system_closure_note is not None:
            # auto_close_non_request: the one closure with no human actor.
            if to_status != RequestStatus.CLOSED_NON_REQUEST:
                raise ValidationError("The system may only close a case as non-request.")
            actor, note = None, _system_closure_note
        elif to_status != RequestStatus.DENIED:
            actor, note = _require_terminal_action_actor(
                actor=actor,
                reason=note,
                to_status=to_status,
            )
        _require_no_unresolved_delivery_before_closure(prr)
    else:
        _require_no_unresolved_lifecycle_delivery(prr)

    # --- Tolling: leaving CLARIFICATION adds the paused business days and
    # pushes the statutory deadline out (RCW 42.56.520(2) — clarification
    # tolls the response period).
    # Only a public records request has an RCW 42.56.520 clock to toll, so
    # only a public records request may ever have one written (ADR 0004).
    if (
        from_status == RequestStatus.CLARIFICATION
        and prr.matter_kind == MatterKind.PUBLIC_RECORDS
    ):
        last_clar = (
            prr.events.filter(to_status=RequestStatus.CLARIFICATION)
            .order_by("-occurred_at").first()
        )
        if last_clar:
            paused = business_days_between(
                timezone.localtime(last_clar.occurred_at).date(),
                timezone.localtime(now).date(),
            )
            prr.tolled_business_days += paused
            prr.statutory_response_due = statutory_response_due(
                prr.received_at, prr.tolled_business_days
            )

    prr.status = to_status
    if to_status in CLOSED_STATUSES:
        prr.closed_at = now
    prr.save()

    status_event = StatusEvent.objects.create(
        request=prr, actor=actor, from_status=from_status,
        to_status=to_status, note=note,
        visible_to_requester=visible_to_requester, occurred_at=now,
        correspondence_effect=(
            {
                RequestStatus.ACKNOWLEDGED: (
                    StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
                ),
                RequestStatus.CLARIFICATION: (
                    StatusEvent.CorrespondenceEffect.CLARIFICATION_DELIVERY
                ),
                RequestStatus.DENIED: (
                    StatusEvent.CorrespondenceEffect.DENIAL_DELIVERY
                ),
            }.get(to_status, "")
            if confirmed_correspondence is not None
            else ""
        ),
    )
    if confirmed_correspondence is not None:
        from apps.correspondence.models import Correspondence

        confirmed_correspondence = Correspondence.objects.filter(
            pk=confirmed_correspondence.pk,
        )._bind_locked_resulting_status_event(
            status_event=status_event,
        )
    audit(
        "request.status_changed",
        target=prr.number,
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        occurred_at=now.isoformat(),
    )
    if to_status in CLOSED_STATUSES:
        _retire_inbox_rows_on_closure(prr, actor=actor, to_status=to_status)
        from celery import Task

        from .tasks import release_due_footage_preservations

        # Footage due at closing is released now rather than at the next
        # beat; the task decides what is due.
        transaction.on_commit(
            lambda: cast(Task, release_due_footage_preservations).delay(),
            robust=True,
        )

    if to_status == RequestStatus.CLARIFICATION:
        from apps.correspondence.models import ClarificationCycle

        ClarificationCycle.objects.create(
            request=prr,
            outbound_correspondence=confirmed_correspondence,
            opened_at=now,
            opened_status_event=status_event,
        )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("requests", prr.pk)
    return prr


@transaction.atomic
def reopen_request(
    prr: PublicRecordsRequest,
    to_status: str,
    *,
    actor,
    reason: str,
) -> PublicRecordsRequest:
    """Reopen a closed request, on the record.

    Reopening is ordinary records practice: a requester follows up, a closure
    turns out to have been premature, or records surface after the fact. What
    it is not is ordinary *state change* -- it reverses a decision that was
    made in writing, so it demands the same accountability the closure did: a
    named Records Officer and a substantive written reason, both appended to
    the timeline and the audit log.

    Deliberately NOT done here:

    * No new five-day clock. ``statutory_response_due`` is derived from
      ``received_at`` and accumulated tolling, and reopening changes neither.
      A request that was already overdue when it closed is still overdue when
      it reopens; inventing a fresh deadline would erase a real exposure.
    * No closure record erased. ``closed_at`` is cleared on the live row
      because the case is no longer closed, but the StatusEvent that closed
      it stays exactly where it is -- the timeline is append-only, and the
      history has to show that this case was closed and then reopened, not
      that it was never closed.
    * No requester correspondence. Reopening sends nothing; whether the
      requester should hear about it is a separate, human, drafted decision.
    """
    current_actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required to reopen a request.",
    )
    normalized = (reason or "").strip()
    if len(normalized) < 10:
        raise TerminalActionRequired(
            "A substantive reason is required to reopen a closed request."
        )
    if to_status not in REOPENABLE_TARGET_STATUSES:
        raise ValidationError(
            "A reopened request must land in an active working status; "
            "acknowledged, clarification and denied require a delivered "
            "message and cannot be reached by reopening."
        )

    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    from_status = prr.status
    if from_status not in CLOSED_STATUSES:
        raise ClosedRequestTransition(
            f"This request is not closed ({from_status}); use transition() "
            "for an ordinary state change."
        )
    if prr.merged_into_id is not None:
        raise ValidationError(
            f"{prr.number} was merged into {prr.merged_into.number}; the "
            "requester's correspondence lives there. Reopen that case instead."
        )

    now = timezone.now()
    prr.status = to_status
    prr.closed_at = None
    prr.save()

    StatusEvent.objects.create(
        request=prr,
        actor=current_actor,
        from_status=from_status,
        to_status=to_status,
        note=normalized,
        visible_to_requester=False,
        occurred_at=now,
    )
    _note_released_footage_on_reopen(prr, occurred_at=now)
    audit(
        "request.reopened",
        target=prr.number,
        actor=current_actor,
        from_status=from_status,
        to_status=to_status,
        occurred_at=now.isoformat(),
    )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("requests", prr.pk)
    return prr


def _note_released_footage_on_reopen(prr, *, occurred_at) -> None:
    """Reopening restores the request but not footage whose hold was already
    released at closing (rule 7), so say so on the timeline. Covers the
    duplicates merged into this case too: their holds released with it."""
    from django.db.models import Q

    from .models import FootagePreservation

    released = list(
        FootagePreservation.objects.filter(
            Q(request=prr) | Q(request__merged_into=prr),
            state=FootagePreservation.State.RELEASED,
        )
        .select_related("request")
        .order_by("released_at", "pk")
    )
    if not released:
        return
    held = "; ".join(
        f"{fp.request.number} on "
        f"{timezone.localdate(fp.released_at).isoformat() if fp.released_at else 'an unknown date'}"
        for fp in released
    )
    StatusEvent.objects.create(
        request=prr,
        note=(
            f"The footage hold was released before this reopen ({held}). "
            "Footage still within camera retention is not held: re-preserve "
            "any footage the reopened request covers by hand."
        ),
        visible_to_requester=False,
        occurred_at=occurred_at,
    )


@transaction.atomic
def close_open_clarification(
    prr: PublicRecordsRequest,
    to_status: str,
    *,
    actor,
    reason: str,
) -> PublicRecordsRequest:
    """Close a request without inventing a clarification response."""
    if to_status not in CLARIFICATION_CANCELLATION_CLOSURES:
        raise ClarificationResolutionRequired(
            "Only an authorized terminal closure may cancel an open "
            "clarification without a response."
        )
    if (
        actor is None
        or getattr(actor, "pk", None) is None
    ):
        raise ClarificationResolutionRequired(
            "A current human staff identity is required to close an open "
            "clarification."
        )
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ClarificationResolutionRequired(
            "A substantive closure reason is required to close an open "
            "clarification."
        )

    # Lock in canonical order with clarification resolution: request first,
    # then cycle. This prevents a response resolution racing a terminal close.
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.status != RequestStatus.CLARIFICATION:
        raise ClarificationResolutionRequired(
            "This request is not awaiting clarification."
        )
    closure_roles, _ = _closure_authority(to_status)
    actor = _require_current_staff(
        actor=actor,
        message=(
            "Records Officer authority is required to close an open "
            "clarification."
            if closure_roles == (StaffRole.RECORDS_OFFICER,)
            else "Staff authority is required to close an open clarification."
        ),
        roles=closure_roles,
    )

    from apps.correspondence.models import ClarificationCycle

    try:
        cycle = (
            ClarificationCycle.objects.select_for_update(of=("self",))
            .select_related("outbound_correspondence")
            .get(
                request=prr,
                resolved_at__isnull=True,
                cancelled_at__isnull=True,
            )
        )
    except ClarificationCycle.DoesNotExist as exc:
        raise ClarificationResolutionRequired(
            "The request has no open clarification cycle to cancel."
        ) from exc

    occurred_at = timezone.now()
    cycle.cancelled_at = occurred_at
    cycle.save(update_fields=["cancelled_at"])
    prr = transition(
        prr,
        to_status,
        actor=actor,
        note=reason,
        occurred_at=occurred_at,
        _clarification_exit_evidence=_ClarificationExitEvidence(
            cycle_id=cycle.pk,
            mode="cancelled",
        ),
    )
    event = (
        StatusEvent.objects.filter(
            request=prr,
            from_status=RequestStatus.CLARIFICATION,
            to_status=to_status,
            occurred_at=occurred_at,
        )
        .order_by("-pk")
        .first()
    )
    audit(
        "correspondence.clarification_cancelled_for_closure",
        target=prr.number,
        actor=actor,
        clarification_cycle_id=str(cycle.id),
        outbound_correspondence_id=str(cycle.outbound_correspondence_id),
        status_event_id=str(event.pk),
        closure_status=to_status,
        reason=reason,
    )
    return prr


@transaction.atomic
def resolve_clarification_cycle(
    cycle,
    *,
    actor,
    resolution_notes: str,
):
    """Resolve a responded clarification and atomically restart the clock."""
    notes = (resolution_notes or "").strip()
    if len(notes) < 10:
        raise ValidationError("Clarification resolution notes must be substantive.")

    from apps.correspondence.models import ClarificationCycle

    request_id = ClarificationCycle.objects.only("request_id").get(
        pk=cycle.pk
    ).request_id
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=request_id)
    actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer approval is required.",
    )
    cycle = (
        ClarificationCycle.objects.select_for_update(of=("self",))
        .select_related(
            "inbound_response_correspondence",
            "opened_status_event",
        )
        .get(pk=cycle.pk)
    )
    if cycle.request_id != prr.pk:
        raise ValidationError(
            "Clarification request identity changed during resolution."
        )
    if prr.status != RequestStatus.CLARIFICATION:
        raise ClarificationResolutionRequired(
            "This request is not awaiting clarification."
        )
    if cycle.resolved_at is not None or cycle.cancelled_at is not None:
        raise ValidationError("This clarification cycle is already closed.")
    if (
        cycle.inbound_response_correspondence_id is None
        or cycle.responded_at is None
    ):
        raise ValidationError(
            "A received clarification response is required before resolution."
        )

    occurred_at = timezone.now()
    cycle.resolved_at = occurred_at
    cycle.resolved_by = actor
    cycle.resolution_notes = notes
    cycle.save(
        update_fields=[
            "resolved_at",
            "resolved_by",
            "resolution_notes",
        ]
    )
    prr = transition(
        prr,
        RequestStatus.GATHERING,
        actor=actor,
        note=f"Clarification resolved: {notes}",
        visible_to_requester=True,
        occurred_at=occurred_at,
        _clarification_exit_evidence=_ClarificationExitEvidence(
            cycle_id=cycle.pk,
            mode="resolved",
        ),
    )
    event = (
        StatusEvent.objects.filter(
            request=prr,
            from_status=RequestStatus.CLARIFICATION,
            to_status=RequestStatus.GATHERING,
            occurred_at=occurred_at,
        )
        .order_by("-pk")
        .first()
    )
    if event is None:
        raise ClarificationResolutionRequired(
            "The clarification resolution status event was not persisted."
        )
    cycle.resolved_status_event = event
    cycle.save(update_fields=["resolved_status_event"])
    audit(
        "correspondence.clarification_resolved",
        target=prr.number,
        actor=actor,
        clarification_cycle_id=str(cycle.id),
        inbound_correspondence_id=str(cycle.inbound_response_correspondence_id),
    )
    return cycle


#: The shortest statement worth recording: what went out and where a copy is.
MIN_ATTESTATION_EVIDENCE = 20

#: What the officer relied on to be satisfied this is the right parent.
MIN_FERPA_IDENTITY_NOTE = 20


@transaction.atomic
def record_ferpa_identity_verification(
    prr: PublicRecordsRequest,
    *,
    actor,
    method: str,
    note: str,
):
    """Record that a named officer verified a FERPA requester's identity.

    34 CFR 99.10 gives the right of access to a parent or an eligible student
    and to nobody else, so before an education record is disclosed somebody
    accountable has to have satisfied themselves that the person asking is
    that parent or that student. Custody disputes make this the part of a
    FERPA request most likely to end badly, and the question is not one a
    schedule, a heuristic, or a model can answer.

    This records the judgment — who, when, by what method, and what they
    relied on — and does nothing else. It sends nothing, changes no status,
    and starts no clock. Never callable from a task.
    """
    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to verify a FERPA "
            "requester's identity."
        ),
    )
    if method not in FerpaIdentityVerificationMethod.values:
        raise ValidationError("Choose how the requester's identity was verified.")
    note = (note or "").strip()
    if len(note) < MIN_FERPA_IDENTITY_NOTE:
        raise ValidationError(
            "Say what you relied on to verify this requester (at least "
            f"{MIN_FERPA_IDENTITY_NOTE} characters)."
        )

    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.matter_kind != MatterKind.FERPA_PARENT:
        raise ValidationError(
            "Identity verification under 34 CFR 99.10 applies only to a FERPA "
            "parent or eligible-student matter."
        )
    if FerpaIdentityVerification.objects.filter(request=prr).exists():
        raise ValidationError(
            f"{prr.number} already has a recorded identity verification."
        )

    now = timezone.now()
    verification = FerpaIdentityVerification.objects.create(
        request=prr,
        method=method,
        note=note,
        verified_by=current_actor,
        verified_at=now,
    )
    StatusEvent.objects.create(
        request=prr,
        actor=current_actor,
        note=(
            "FERPA requester identity verified under 34 CFR 99.10 by "
            f"{verification.get_method_display().lower()}: {note}"
        ),
        visible_to_requester=False,
        occurred_at=now,
    )
    audit(
        "request.ferpa_identity_verified",
        target=prr.number,
        actor=current_actor,
        method=method,
        note_length=len(note),
        occurred_at=now.isoformat(),
    )
    return prr


@transaction.atomic
@transaction.atomic
def add_case_note(prr: PublicRecordsRequest, *, actor, body: str):
    """Append one staff member's internal note to a case, in their name.

    Internal, and only internal. Nothing here drafts, queues, approves, or
    sends correspondence; nothing moves the case's status or touches a
    deadline; and no requester-facing letter, production manifest, or export
    reads ``CaseNote``. It is the running record staff keep for each other,
    which today lives in email and therefore does not reach whoever opens the
    case next.

    Append-only. A note is evidence of what somebody knew and when, so it is
    never edited or deleted -- a correction is another note saying so, the
    way a paper case file works.

    Every workflow role may write one, scoped by the same rules that decide
    which cases they can see at all: a liaison who may open a case may write
    on it, and one who may not, cannot. It applies to every matter kind: a
    litigation pull and a FERPA request are worked by the same people.

    Note for whoever writes one: a note about a public records request is
    very likely itself a public record and may have to be produced on a
    request for it. "Internal" means it is not part of *this* case's response
    to *this* requester, not that it is confidential.
    """
    from .models import CASE_NOTE_MAX_LENGTH, CaseNote

    current_actor = _require_current_staff(
        actor=actor,
        message="Staff authority is required to write on a case.",
        roles=tuple(sorted(WORKFLOW_STAFF_ROLES)),
    )
    body = (body or "").strip()
    if not body:
        raise ValidationError("A note needs something in it.")
    if len(body) > CASE_NOTE_MAX_LENGTH:
        raise ValidationError(
            f"A note is at most {CASE_NOTE_MAX_LENGTH} characters. Attach a "
            "record or write several notes instead."
        )
    note = CaseNote.objects.create(request=prr, author=current_actor, body=body)
    audit(
        "case_note.added",
        target=prr.number,
        actor=current_actor,
        note_id=str(note.pk),
        # The length, never the text: the audit log is read by people who are
        # not necessarily authorized to read the case.
        body_length=len(body),
    )
    return note


def record_external_initial_response(
    prr: PublicRecordsRequest,
    *,
    actor,
    sent_via: str,
    evidence: str,
    responded_on=None,
):
    """Record, on a Records Officer's statement, a first response this system
    never delivered.

    Cases migrated from NextRequest were acknowledged there, and the export
    carried no date for it; a response sent from a district mailbox or on
    paper leaves no trace here either. Until this existed the case showed
    "five-day response not sent" forever, sat in the statutory backlog, and
    would have drawn an automatic second acknowledgment the day the safety
    net was switched on.

    The statement is the officer's, made in writing under their name: the
    channel, the date when one is on record, and what they relied on. It is
    appended as an ``InitialResponseAttestation`` with a timeline note and an
    audit entry. It sends nothing, changes no status, and starts no clock.
    Never callable from a task: this is a human legal judgment, not a
    projection.
    """
    from .models import InitialResponseAttestation

    current_actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Records Officer authority is required to record a response sent "
            "outside this system."
        ),
    )
    evidence = (evidence or "").strip()
    if len(evidence) < MIN_ATTESTATION_EVIDENCE:
        raise ValidationError(
            "Say what was sent and where a copy can be found (at least "
            f"{MIN_ATTESTATION_EVIDENCE} characters)."
        )
    if sent_via not in InitialResponseAttestation.SentVia.values:
        raise ValidationError("Choose the channel the response went through.")

    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.initial_response_delivered:
        raise ValidationError(
            f"{prr.number} already has a first response on record."
        )
    # An acknowledgment already approved or queued here would overtake the
    # statement; cancel it first rather than record two responses.
    _require_no_unresolved_lifecycle_delivery(prr)
    received_on = timezone.localtime(prr.received_at).date()
    if responded_on is not None:
        if responded_on < received_on:
            raise ValidationError("The response cannot predate receipt.")
        if responded_on > timezone.localdate():
            raise ValidationError("The response date cannot be in the future.")

    attestation = InitialResponseAttestation.objects.create(
        request=prr,
        sent_via=sent_via,
        responded_on=responded_on,
        evidence=evidence,
        attested_by=current_actor,
    )
    when = (
        responded_on.isoformat()
        if responded_on is not None
        else "a date not on record"
    )
    now = timezone.now()
    StatusEvent.objects.create(
        request=prr,
        actor=current_actor,
        note=(
            "Initial response recorded as sent outside this system through "
            f"{attestation.get_sent_via_display()} on {when}, on the Records "
            f"Officer's statement: {evidence}"
        ),
        visible_to_requester=False,
        occurred_at=now,
    )
    audit(
        "request.initial_response_attested",
        target=prr.number,
        actor=current_actor,
        attestation_id=str(attestation.pk),
        sent_via=sent_via,
        responded_on=responded_on.isoformat() if responded_on else None,
        evidence_length=len(evidence),
        occurred_at=now.isoformat(),
    )
    from apps.work_queue.integration import schedule_source_work_item_reconciliation

    schedule_source_work_item_reconciliation("requests", prr.pk)
    return attestation


@transaction.atomic
def merge_requests(
    source: PublicRecordsRequest,
    target: PublicRecordsRequest,
    *,
    actor,
    reason: str,
) -> PublicRecordsRequest:
    """Fold a duplicate case into the case of record, on the record.

    A requester who emails after using the web form, or emails twice, ends up
    with two case numbers for one request. The law knows one request, and the
    five-day clock runs from the first receipt, so the officer names the case
    of record (``target``) and everything the requester sent on the duplicate
    (``source``) moves there:

    * Every matched inbound email on the source is rematched to the target
      through the audited ``rematch_inbound_receipt`` primitive, so the
      message, its attachments, and its Inbox row now belong to the target,
      and a later reply threads to the target as well.
    * The source closes as ``CLOSED_DUPLICATE`` through ``transition()`` with
      ``merged_into`` pointing at the target, so its own timeline, sent
      correspondence, and audit trail stay exactly where they were written.
      The target's Messages view shows that sent correspondence alongside its
      own.
    * The target's timeline records the merge with the officer's reason.

    Deliberately NOT done: the target keeps its own ``received_at`` and
    deadline. Merging never invents or erases a statutory clock; if the
    officer picks the later case as the case of record, that is their call
    and the audit trail says so.
    """
    # Closing a duplicate is an officer's call: it is not one of the
    # staff-closable endings, and it moves a requester's correspondence.
    actor, reason = _require_terminal_action_actor(
        actor=actor,
        reason=reason,
        to_status=RequestStatus.CLOSED_DUPLICATE,
    )
    if source.pk == target.pk:
        raise ValidationError("A case cannot be merged into itself.")
    if source.matter_kind != target.matter_kind:
        # Two numbers for one matter is a duplicate. A public records request
        # and a FERPA request for the same records are two different legal
        # obligations that happen to share a search (ADR 0004).
        raise ValidationError(
            "Cases governed by different law cannot be merged; "
            f"{source.number} is a {source.get_matter_kind_display()} and "
            f"{target.number} is a {target.get_matter_kind_display()}."
        )
    locked = {
        prr.pk: prr
        for prr in PublicRecordsRequest.objects.select_for_update()
        .filter(pk__in=[source.pk, target.pk])
        .order_by("pk")
    }
    source = locked[source.pk]
    target = locked[target.pk]
    if source.status in CLOSED_STATUSES:
        raise ClosedRequestTransition(
            f"{source.number} is already closed ({source.status}) and cannot "
            "be merged."
        )
    if target.status in CLOSED_STATUSES:
        raise ClosedRequestTransition(
            f"{target.number} is closed; reopen it before merging another "
            "case into it."
        )
    if target.merged_into_id is not None:
        raise ValidationError(
            f"{target.number} was itself merged into another case."
        )

    from apps.correspondence import inbound
    from apps.correspondence.models import InboundCorrespondenceReceipt
    from apps.work_queue.reconciliation import reconcile_inbound_receipt

    receipts = list(
        InboundCorrespondenceReceipt.objects.filter(
            matched_request=source,
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.MATCHED
            ),
        ).order_by("received_at", "pk")
    )
    moved = []
    unmoved = []
    for receipt in receipts:
        try:
            inbound.rematch_inbound_receipt(
                receipt,
                request=target,
                actor=actor,
                reason=reason,
                expected_version=receipt.version,
            )
        except ValidationError as exc:
            # Delivery-status evidence and resolved clarification responses
            # are bound to the source's own outbound messages and stay there.
            unmoved.append(
                {
                    "receipt_id": str(receipt.pk),
                    "detail": "; ".join(exc.messages),
                }
            )
            continue
        # Move the Inbox row now rather than after commit, so the target is
        # flagged the moment the merge lands and the source has no open rows
        # left for its own closure to retire.
        reconcile_inbound_receipt(receipt.pk)
        moved.append(str(receipt.pk))

    source.merged_into = target
    source.save(update_fields=["merged_into"])
    if source.status == RequestStatus.CLARIFICATION:
        source = close_open_clarification(
            source,
            RequestStatus.CLOSED_DUPLICATE,
            actor=actor,
            reason=reason,
        )
    else:
        source = transition(
            source,
            RequestStatus.CLOSED_DUPLICATE,
            actor=actor,
            note=reason,
        )

    now = timezone.now()
    StatusEvent.objects.create(
        request=target,
        actor=actor,
        note=f"Merged {source.number} into this case: {reason}",
        visible_to_requester=False,
        occurred_at=now,
    )
    audit(
        "request.merged",
        target=target.number,
        actor=actor,
        source=source.number,
        moved_receipts=moved,
        unmoved_receipts=unmoved,
        reason=reason,
        occurred_at=now.isoformat(),
    )
    return target


@transaction.atomic
def transition_manually(
    prr: PublicRecordsRequest,
    to_status: str,
    *,
    actor,
    note: str = "",
) -> PublicRecordsRequest:
    """Apply an administrative transition that is not delivery-dependent."""
    if to_status == RequestStatus.CLOSED_NON_REQUEST:
        raise TerminalActionRequired(
            "Use the dedicated email non-request closure so retained intake "
            "evidence is verified."
        )
    if to_status == RequestStatus.CLOSED_DUPLICATE:
        raise TerminalActionRequired(
            "Use the merge workflow to close a duplicate case; it moves the "
            "requester's correspondence to the case of record first."
        )
    current_status = (
        PublicRecordsRequest.objects.select_for_update()
        .values_list("status", flat=True)
        .get(pk=prr.pk)
    )
    if (
        current_status == RequestStatus.CLARIFICATION
        and to_status != RequestStatus.CLARIFICATION
    ):
        if to_status in CLARIFICATION_CANCELLATION_CLOSURES:
            return close_open_clarification(
                prr,
                to_status,
                actor=actor,
                reason=note,
            )
        raise ClarificationResolutionRequired(
            "An open clarification may be left only through the clarification "
            "resolution workflow after a response and substantive review, or "
            "through an attributed withdrawn/abandoned closure."
        )
    if to_status in CORRESPONDENCE_DRIVEN_STATUSES:
        raise CorrespondenceDeliveryRequired(
            "Acknowledgment, clarification, and denial correspondence status "
            "changes are created only after the corresponding requester "
            "message has confirmed delivery."
        )
    actor = _require_current_records_officer(
        actor=actor,
        message="Records Officer authority is required for status changes.",
    )
    return transition(prr, to_status, actor=actor, note=note)


@dataclass(frozen=True)
class RequesterIdentityCorrection:
    """The audited outcome of correcting who a request's requester is."""

    request: PublicRecordsRequest
    previous_requester: Requester
    requester: Requester
    refresh: RequesterCorrectionRefresh


@transaction.atomic
def correct_requester_identity(
    prr: PublicRecordsRequest,
    *,
    actor,
    name: str,
    email: str,
    reason: str,
) -> RequesterIdentityCorrection:
    """Record that the request was submitted on behalf of someone else.

    A request forwarded into intake -- a front-desk mailbox relaying an email,
    a staff member typing up a phone call -- lands with the forwarder's
    contact details on the legal record, and every statutory notice then
    addresses the wrong person. The day-5 response is strict liability, so the
    record has to be correctable.

    It is corrected the way every other consequential act here is: a named
    current Records Officer, a substantive written reason, and an audit entry.
    The submitted ``Requester`` row is never edited -- a new row is created and
    the request repointed at it -- so what intake actually received stays
    readable, exactly as ``request.description`` does.

    Nothing statutory is reset. An acknowledgment already delivered to the
    wrong address was still delivered on a date, and pretending otherwise
    would falsify the compliance record; the officer follows up on the merits.
    """
    from apps.correspondence.services import (
        refresh_drafts_for_requester_correction,
    )

    actor = _require_current_records_officer(
        actor=actor,
        message=(
            "Current Records Officer authority is required to correct the "
            "requester identity on a request."
        ),
    )
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValidationError(
            "Correcting the requester identity requires a substantive reason "
            "(e.g. 'submitted by the district info mailbox on behalf of the "
            "actual requester, who is named in the forwarded email')."
        )
    name = (name or "").strip()
    email = (email or "").strip()
    if not name:
        raise ValidationError(
            "A corrected requester identity requires the requester's name; it "
            "is what the statutory correspondence addresses."
        )
    validate_email(email)

    prr = (
        PublicRecordsRequest.objects.select_for_update(of=("self",))
        .select_related("requester")
        .get(pk=prr.pk)
    )
    previous = prr.requester
    if (
        previous.email.casefold() == email.casefold()
        and previous.name.strip() == name
    ):
        raise ValidationError(
            "The corrected identity matches the requester already on this "
            "request; nothing would change."
        )

    corrected = Requester(
        name=name,
        email=email,
        # Carry the rest of the submitted contact record forward. Intake
        # minimizes PII already, and dropping a mailing address here would
        # silently remove the only way to fulfil a paper-copy request.
        phone=previous.phone,
        mailing_address=previous.mailing_address,
        city=previous.city,
        state=previous.state,
        postal_code=previous.postal_code,
        company=previous.company,
    )
    corrected.full_clean()
    corrected.save()

    prr.requester = corrected
    prr.save(update_fields=["requester"])

    refresh = refresh_drafts_for_requester_correction(
        prr,
        previous_requester=previous,
        actor=actor,
        reason=reason,
    )
    audit(
        "request.requester_corrected",
        target=prr.number,
        actor=actor,
        reason=reason,
        previous_requester_id=str(previous.pk),
        previous_name=previous.name,
        previous_email=previous.email,
        requester_id=str(corrected.pk),
        corrected_name=corrected.name,
        corrected_email=corrected.email,
        refreshed_correspondence=[str(item.id) for item in refresh.refreshed],
        correspondence_needing_review=[
            str(item.id) for item, _ in refresh.needs_review
        ],
        cancelled_correspondence=[str(item.id) for item in refresh.cancelled],
        uncancelled_correspondence=[
            str(item.id) for item, _ in refresh.uncancelled
        ],
    )
    return RequesterIdentityCorrection(
        request=prr,
        previous_requester=previous,
        requester=corrected,
        refresh=refresh,
    )


@transaction.atomic
def close_case(
    prr: PublicRecordsRequest,
    to_status: str,
    *,
    actor,
    reason: str,
) -> PublicRecordsRequest:
    """Close a case as withdrawn or abandoned, on the record.

    This is the one closure every staff member working a case may record, so
    it is deliberately a separate entry point from ``transition_manually``
    rather than another destination in it: the generic administrative
    transition moves a case between working states and stays with the Records
    Officer, while this records a documented ending. An open clarification is
    cancelled atomically here for the same reason it is in
    ``close_open_clarification`` -- a requester who withdrew never answered,
    and no response may be fabricated to tidy the timeline.
    """
    if to_status not in STAFF_CLOSABLE_STATUSES:
        raise ValidationError(
            "Only a withdrawn or abandoned closure may be recorded here; "
            "fulfilment, denial, no responsive records, and the "
            "not-a-records-request closure each have their own workflow."
        )
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.status == RequestStatus.CLARIFICATION:
        return close_open_clarification(
            prr,
            to_status,
            actor=actor,
            reason=reason,
        )
    return transition(prr, to_status, actor=actor, note=reason)


def _require_email_intake_case(prr: PublicRecordsRequest) -> None:
    """Refuse a case that was not auto-created from an unmatched email."""
    from apps.correspondence.models import InboundCorrespondenceReceipt

    receipt = (
        InboundCorrespondenceReceipt.objects.select_for_update()
        .filter(
            matched_request_id=prr.pk,
            matched_correspondence__request_id=prr.pk,
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.MATCHED
            ),
            match_method=(
                InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
            ),
        )
        .first()
    )
    if receipt is None:
        raise EmailIntakeClosureRequired(
            "Only a request auto-created from unmatched email may use this closure."
        )


@transaction.atomic
def auto_close_non_request(
    prr: PublicRecordsRequest,
    *,
    note: str,
) -> StatusEvent:
    """The system closes a new email case as non-request, with no human (#34).

    The only caller is ``intake_trust.auto_apply_intake_label``, under a spam
    or non-request label's ``auto`` trust mode (``test_compliance`` pins it).
    It is narrower than the officer's closure: the case must still be
    ``received`` -- nobody has worked it -- and must have been auto-created
    from email, so a web-form request is never closed this way. Nothing is
    sent to the requester, and ``reopen_request`` undoes it.
    """
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.status != RequestStatus.RECEIVED:
        raise ValidationError(
            "Only a case nobody has worked yet can be closed automatically."
        )
    _require_email_intake_case(prr)
    transition(
        prr,
        RequestStatus.CLOSED_NON_REQUEST,
        _system_closure_note=note,
    )
    return StatusEvent.objects.filter(
        request=prr, to_status=RequestStatus.CLOSED_NON_REQUEST,
    ).latest("occurred_at")


@transaction.atomic
def close_email_non_request(
    prr: PublicRecordsRequest,
    *,
    actor,
    reason: str,
) -> PublicRecordsRequest:
    """Close only a canonical auto-created email case, retaining all evidence."""
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    _require_email_intake_case(prr)
    if prr.status == RequestStatus.CLARIFICATION:
        return close_open_clarification(
            prr,
            RequestStatus.CLOSED_NON_REQUEST,
            actor=actor,
            reason=reason,
        )
    return transition(
        prr,
        RequestStatus.CLOSED_NON_REQUEST,
        actor=actor,
        note=reason,
    )


@transaction.atomic
def record_acknowledgment(
    prr: PublicRecordsRequest,
    *,
    actor=None,
    note: str = "",
    communicated_at=None,
    confirmed_correspondence_id=None,
) -> PublicRecordsRequest:
    """Mark the RCW 42.56.520 response as sent — stops the watchdog."""
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    effective_at = communicated_at or timezone.now()
    if timezone.is_naive(effective_at):
        raise ValueError("Acknowledgment timestamps must include a timezone.")
    event_note = note or "Statutory acknowledgment sent."
    _require_confirmed_correspondence_delivery(
        prr,
        to_status=RequestStatus.ACKNOWLEDGED,
        correspondence_id=confirmed_correspondence_id,
        actor=actor,
        note=event_note,
        visible_to_requester=True,
        occurred_at=effective_at,
    )
    _require_no_unresolved_lifecycle_delivery(prr)
    prr.acknowledgment_sent_at = effective_at
    prr.save(update_fields=["acknowledgment_sent_at"])
    acknowledgment_event = StatusEvent.objects.create(
        request=prr, actor=actor,
        note=event_note,
        visible_to_requester=True,
        occurred_at=effective_at,
        correspondence_effect=(
            ""
            if prr.status == RequestStatus.RECEIVED
            else (
                StatusEvent.CorrespondenceEffect.ACKNOWLEDGMENT_DELIVERY
            )
        ),
    )
    audit(
        "request.acknowledged",
        target=prr.number,
        actor=actor,
        communicated_at=effective_at.isoformat(),
    )
    if prr.status == RequestStatus.RECEIVED:
        prr = transition(
            prr,
            RequestStatus.ACKNOWLEDGED,
            actor=actor,
            note=event_note,
            visible_to_requester=True,
            occurred_at=effective_at,
            confirmed_correspondence_id=confirmed_correspondence_id,
        )
    else:
        from apps.correspondence.models import Correspondence

        Correspondence.objects.filter(
            pk=confirmed_correspondence_id,
        )._bind_locked_resulting_status_event(
            status_event=acknowledgment_event,
        )
    return prr
