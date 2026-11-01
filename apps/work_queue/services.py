import hashlib
import json
import re
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.api.identity import has_staff_role
from apps.api.models import StaffRole
from apps.audit.models import audit

from .authorization import lock_authorized_staff
from .models import WorkItem, WorkItemEvent, WorkItemReceipt

_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


class VersionConflict(Exception):
    def __init__(self, *, expected, current):
        self.expected = expected
        self.current = current
        super().__init__(
            f"Version conflict: expected {expected}, current {current}."
        )


class IdempotencyConflict(Exception):
    pass


@dataclass(frozen=True)
class MaterializationResult:
    work_item: WorkItem
    event: WorkItemEvent
    created: bool


@dataclass(frozen=True)
class WorkMutationResult:
    work_item: WorkItem
    event: WorkItemEvent
    idempotent: bool
    receipt: WorkItemReceipt | None = None


_INBOUND_KINDS = frozenset(
    {
        WorkItem.WorkKind.INBOUND_REPLY,
        WorkItem.WorkKind.EMAIL_INTAKE,
        WorkItem.WorkKind.INBOUND_UNMATCHED,
        WorkItem.WorkKind.INBOUND_CANDIDATE,
        WorkItem.WorkKind.INBOUND_AMBIGUOUS,
        WorkItem.WorkKind.INBOUND_QUARANTINED,
    }
)

_ASSIGNMENT_SCOPED_LIAISON_KINDS = frozenset(
    {
        WorkItem.WorkKind.COLLECTION,
        WorkItem.WorkKind.CORRESPONDENCE_DRAFT,
    }
)

_RECORDS_OFFICER_ONLY = frozenset({StaffRole.RECORDS_OFFICER})
#: The two kinds the Inbox shows as requester messages on a case.
_INBOX_MESSAGE_KINDS = frozenset(
    {
        WorkItem.WorkKind.INBOUND_REPLY,
        WorkItem.WorkKind.EMAIL_INTAKE,
    }
)
_ROLES_BY_WORK_KIND = {
    **dict.fromkeys(_INBOUND_KINDS, _RECORDS_OFFICER_ONLY),
    WorkItem.WorkKind.REQUEST_ACTION: _RECORDS_OFFICER_ONLY,
    WorkItem.WorkKind.CORRESPONDENCE_DRAFT: frozenset(
        {StaffRole.RECORDS_OFFICER, StaffRole.LIAISON}
    ),
    WorkItem.WorkKind.CORRESPONDENCE_APPROVAL: _RECORDS_OFFICER_ONLY,
    WorkItem.WorkKind.DELIVERY_EXCEPTION: frozenset(
        {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN}
    ),
    WorkItem.WorkKind.FEE_UNPAID: _RECORDS_OFFICER_ONLY,
    WorkItem.WorkKind.COLLECTION: frozenset(
        {StaffRole.RECORDS_OFFICER, StaffRole.LIAISON}
    ),
    WorkItem.WorkKind.RECORD_REVIEW: frozenset(
        {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER}
    ),
    WorkItem.WorkKind.SECOND_REVIEW: frozenset(
        {StaffRole.RECORDS_OFFICER, StaffRole.REVIEWER}
    ),
}


def roles_for_work_kind(work_kind):
    return _ROLES_BY_WORK_KIND[work_kind]


def _mutation_key(value) -> str:
    normalized = str(value or "").strip()
    if not _IDEMPOTENCY_KEY.fullmatch(normalized):
        raise ValidationError(
            "A valid mutation idempotency key is required."
        )
    return normalized


def _fingerprint(action, work_item, payload) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "work_item_id": str(work_item.pk),
            "payload": payload,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _existing_mutation(*, actor, key, fingerprint, work_item):
    # Every caller holds this actor's user-row lock through
    # lock_authorized_staff(). That serializes the actor-scoped idempotency
    # namespace even when the same key races across different work items.
    event = WorkItemEvent.objects.filter(
        actor=actor,
        idempotency_key=key,
    ).first()
    if event is None:
        return None
    if (
        event.work_item_id != work_item.pk
        or event.mutation_fingerprint != fingerprint
    ):
        raise IdempotencyConflict(
            "The mutation idempotency key was already used for another input."
        )
    return event


def _check_version(work_item, expected_version):
    expected = _normalize_expected_version(expected_version)
    if work_item.version != expected:
        raise VersionConflict(expected=expected, current=work_item.version)
    return expected


def _normalize_expected_version(expected_version):
    try:
        expected = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValidationError("A positive expected version is required.") from exc
    if expected < 1:
        raise ValidationError("A positive expected version is required.")
    return expected


def _authorize_actor(work_item, actor):
    roles = roles_for_work_kind(work_item.work_kind)
    current = lock_authorized_staff(
        [(actor, roles)],
        message="An active staff member authorized for this work kind is required.",
    )[actor.pk]
    _require_current_assignment_scope(work_item, current)
    return current


def _require_current_assignment_scope(work_item, actor):
    if (
        work_item.work_kind in _ASSIGNMENT_SCOPED_LIAISON_KINDS
        and work_item.assignee_id != actor.pk
        and not has_staff_role(actor, StaffRole.RECORDS_OFFICER)
    ):
        raise PermissionDenied(
            "Liaison access requires a current assignment to this work item."
        )


def _event_and_audit(
    *,
    work_item,
    event_type,
    actor,
    expected_version,
    idempotency_key,
    fingerprint,
    metadata,
    audit_action,
    audit_metadata=None,
):
    event = WorkItemEvent.objects.create(
        work_item=work_item,
        event_type=event_type,
        actor=actor,
        expected_version=expected_version,
        resulting_version=work_item.version,
        idempotency_key=idempotency_key,
        mutation_fingerprint=fingerprint,
        metadata=metadata,
    )
    audit(
        audit_action,
        target=str(work_item.pk),
        actor=actor,
        work_kind=work_item.work_kind,
        version=work_item.version,
        event_id=str(event.pk),
        **(metadata if audit_metadata is None else audit_metadata),
    )
    return event


def _save_projection(work_item):
    work_item.save(
        update_fields=[
            "assignee",
            "disposition",
            "version",
            "disposed_at",
            "disposed_by",
            "no_action_category",
            "no_action_reason",
            "no_action_by",
            "no_action_at",
            "updated_at",
        ]
    )


def _clear_disposition_evidence(work_item):
    work_item.disposed_at = None
    work_item.disposed_by = None
    work_item.no_action_category = ""
    work_item.no_action_reason = ""
    work_item.no_action_by = None
    work_item.no_action_at = None


def _source_id(value) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 64:
        raise ValidationError("A bounded source identifier is required.")
    return normalized


@transaction.atomic
def materialize_work_item(
    *,
    source_type,
    source_id,
    work_kind,
    request=None,
    assignee=None,
) -> MaterializationResult:
    """Create exactly one current projection and one creation event."""
    if source_type not in WorkItem.SourceType.values:
        raise ValidationError("Unknown work-item source type.")
    if work_kind not in WorkItem.WorkKind.values:
        raise ValidationError("Unknown work kind.")
    if assignee is not None and not has_staff_role(
        assignee,
        *roles_for_work_kind(work_kind),
    ):
        raise ValidationError(
            "The initial assignee is not authorized for this work kind."
        )
    source_id = _source_id(source_id)
    work_item, created = WorkItem.objects.get_or_create(
        source_type=source_type,
        source_id=source_id,
        work_kind=work_kind,
        defaults={"request": request, "assignee": assignee},
    )
    if not created and work_item.request_id != getattr(request, "pk", None):
        raise ValidationError(
            "Existing work-item scope does not match the supplied request."
        )
    if created:
        event = WorkItemEvent.objects.create(
            work_item=work_item,
            event_type=WorkItemEvent.EventType.CREATED,
            resulting_version=work_item.version,
            metadata={
                "source_type": work_item.source_type,
                "source_id": work_item.source_id,
                "work_kind": work_item.work_kind,
                "request_id": (
                    str(work_item.request_id) if work_item.request_id else None
                ),
                "assignee_id": work_item.assignee_id,
            },
        )
        audit(
            "work_queue.item_created",
            target=str(work_item.pk),
            source_type=work_item.source_type,
            source_id=work_item.source_id,
            work_kind=work_item.work_kind,
            request_id=(
                str(work_item.request_id) if work_item.request_id else None
            ),
        )
    else:
        event = WorkItemEvent.objects.get(
            work_item=work_item,
            event_type=WorkItemEvent.EventType.CREATED,
        )
    return MaterializationResult(work_item, event, created)


@transaction.atomic
def _set_read_state(
    work_item,
    *,
    actor,
    idempotency_key,
    is_read,
    expected_version=None,
):
    locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
    actor = _authorize_actor(locked, actor)
    key = _mutation_key(idempotency_key)
    action = "read" if is_read else "unread"
    expected = (
        None
        if expected_version is None
        else _normalize_expected_version(expected_version)
    )
    fingerprint = _fingerprint(
        action,
        locked,
        {"expected_version": expected, "is_read": is_read},
    )
    existing = _existing_mutation(
        actor=actor,
        key=key,
        fingerprint=fingerprint,
        work_item=locked,
    )
    if existing is not None:
        receipt, _created = WorkItemReceipt.objects.get_or_create(
            work_item=locked,
            user=actor,
        )
        return WorkMutationResult(
            locked,
            existing,
            True,
            receipt=receipt,
        )

    if expected is not None:
        _check_version(locked, expected)

    receipt, _created = WorkItemReceipt.objects.select_for_update().get_or_create(
        work_item=locked,
        user=actor,
    )
    receipt.read_at = timezone.now() if is_read else None
    receipt.version += 1
    receipt.save(update_fields=["read_at", "version", "updated_at"])
    event_type = (
        WorkItemEvent.EventType.READ
        if is_read
        else WorkItemEvent.EventType.UNREAD
    )
    metadata = {
        "read_at": receipt.read_at.isoformat() if receipt.read_at else None,
        "receipt_version": receipt.version,
    }
    event = _event_and_audit(
        work_item=locked,
        event_type=event_type,
        actor=actor,
        expected_version=expected,
        idempotency_key=key,
        fingerprint=fingerprint,
        metadata=metadata,
        audit_action=f"work_queue.item_{action}",
    )
    return WorkMutationResult(locked, event, False, receipt=receipt)


def mark_work_item_read(
    work_item,
    *,
    actor,
    idempotency_key,
    expected_version=None,
):
    return _set_read_state(
        work_item,
        actor=actor,
        idempotency_key=idempotency_key,
        is_read=True,
        expected_version=expected_version,
    )


def mark_work_item_unread(
    work_item,
    *,
    actor,
    idempotency_key,
    expected_version=None,
):
    return _set_read_state(
        work_item,
        actor=actor,
        idempotency_key=idempotency_key,
        is_read=False,
        expected_version=expected_version,
    )


@transaction.atomic
def assign_work_item(
    work_item,
    *,
    assignee,
    actor,
    expected_version,
    idempotency_key,
    allow_terminal=False,
):
    locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
    roles = roles_for_work_kind(locked.work_kind)
    current = lock_authorized_staff(
        [(actor, roles), (assignee, roles)],
        message="An active staff member authorized for this work kind is required.",
    )
    actor = current[actor.pk]
    assignee = current[assignee.pk]
    _require_current_assignment_scope(locked, actor)
    key = _mutation_key(idempotency_key)
    expected = _normalize_expected_version(expected_version)
    payload = {
        "assignee_id": assignee.pk,
        "expected_version": expected,
        "allow_terminal": allow_terminal,
    }
    fingerprint = _fingerprint("assign", locked, payload)
    existing = _existing_mutation(
        actor=actor,
        key=key,
        fingerprint=fingerprint,
        work_item=locked,
    )
    if existing is not None:
        return WorkMutationResult(locked, existing, True)
    _check_version(locked, expected)
    if locked.is_terminal and not allow_terminal:
        raise ValidationError("Reopen a disposed work item before assigning it.")
    before_assignee_id = locked.assignee_id
    locked.assignee = assignee
    locked.version += 1
    _save_projection(locked)
    event_type = (
        WorkItemEvent.EventType.ASSIGNED
        if before_assignee_id is None
        else WorkItemEvent.EventType.REASSIGNED
    )
    metadata = {
        "before_assignee_id": before_assignee_id,
        "after_assignee_id": assignee.pk,
    }
    event = _event_and_audit(
        work_item=locked,
        event_type=event_type,
        actor=actor,
        expected_version=expected,
        idempotency_key=key,
        fingerprint=fingerprint,
        metadata=metadata,
        audit_action="work_queue.item_assigned",
    )
    return WorkMutationResult(locked, event, False)


@transaction.atomic
def unassign_work_item(
    work_item,
    *,
    actor,
    expected_version,
    idempotency_key,
    allow_terminal=False,
):
    locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
    actor = _authorize_actor(locked, actor)
    key = _mutation_key(idempotency_key)
    expected = _normalize_expected_version(expected_version)
    fingerprint = _fingerprint(
        "unassign",
        locked,
        {
            "expected_version": expected,
            "allow_terminal": allow_terminal,
        },
    )
    existing = _existing_mutation(
        actor=actor,
        key=key,
        fingerprint=fingerprint,
        work_item=locked,
    )
    if existing is not None:
        return WorkMutationResult(locked, existing, True)
    _check_version(locked, expected)
    if locked.is_terminal and not allow_terminal:
        raise ValidationError("Reopen a disposed work item before unassigning it.")
    if locked.assignee_id is None:
        raise ValidationError("The work item is already unassigned.")
    before_assignee_id = locked.assignee_id
    locked.assignee = None
    locked.version += 1
    _save_projection(locked)
    metadata = {
        "before_assignee_id": before_assignee_id,
        "after_assignee_id": None,
    }
    event = _event_and_audit(
        work_item=locked,
        event_type=WorkItemEvent.EventType.UNASSIGNED,
        actor=actor,
        expected_version=expected,
        idempotency_key=key,
        fingerprint=fingerprint,
        metadata=metadata,
        audit_action="work_queue.item_unassigned",
    )
    return WorkMutationResult(locked, event, False)


@transaction.atomic
def set_work_item_disposition(
    work_item,
    *,
    disposition,
    actor,
    expected_version,
    idempotency_key,
):
    if disposition not in {
        WorkItem.Disposition.WAITING,
        WorkItem.Disposition.HANDLED,
    }:
        raise ValidationError(
            "Shared disposition must be Waiting or Handled on this action."
        )
    locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
    actor = _authorize_actor(locked, actor)
    key = _mutation_key(idempotency_key)
    expected = _normalize_expected_version(expected_version)
    payload = {
        "disposition": disposition,
        "expected_version": expected,
    }
    fingerprint = _fingerprint("disposition", locked, payload)
    existing = _existing_mutation(
        actor=actor,
        key=key,
        fingerprint=fingerprint,
        work_item=locked,
    )
    if existing is not None:
        return WorkMutationResult(locked, existing, True)
    _check_version(locked, expected)
    if locked.is_terminal:
        raise ValidationError("Reopen a disposed work item before redispositioning it.")
    before = locked.disposition
    _clear_disposition_evidence(locked)
    locked.disposition = disposition
    if disposition == WorkItem.Disposition.HANDLED:
        locked.disposed_at = timezone.now()
        locked.disposed_by = actor
    locked.version += 1
    _save_projection(locked)
    metadata = {
        "before_disposition": before,
        "after_disposition": disposition,
    }
    event = _event_and_audit(
        work_item=locked,
        event_type=WorkItemEvent.EventType.DISPOSITIONED,
        actor=actor,
        expected_version=expected,
        idempotency_key=key,
        fingerprint=fingerprint,
        metadata=metadata,
        audit_action="work_queue.item_dispositioned",
    )
    return WorkMutationResult(locked, event, False)


@transaction.atomic
def handle_case_inbox_items(
    request,
    *,
    actor,
    reason,
    roles=_RECORDS_OFFICER_ONLY,
):
    """Mark every open requester-email row on one case Handled by one officer.

    Two callers: ``records.services.transition()`` when the case closes (its
    emails no longer need triage), and the Inbox's per-case "Mark caught up"
    action. Each row keeps its own dispositioned event and audit entry, with
    ``reason`` saying which of the two it was; nothing about any email or the
    case changes. Rows already disposed are left exactly as they are.

    ``roles`` is who may do this: the Records Officer from the Inbox, or the
    roles that were allowed to record the closure when a closure calls it.
    """
    if actor is None or getattr(actor, "pk", None) is None:
        raise PermissionDenied("A current staff identity is required.")
    current = lock_authorized_staff(
        [(actor, frozenset(roles))],
        message=(
            "Records Officer authority is required to handle a case's "
            "Inbox rows."
        ),
    )[actor.pk]
    items = list(
        WorkItem.objects.select_for_update()
        .filter(
            request_id=request.pk,
            work_kind__in=_INBOX_MESSAGE_KINDS,
            disposition__in=[
                WorkItem.Disposition.NEEDS_ATTENTION,
                WorkItem.Disposition.WAITING,
            ],
        )
        .order_by("created_at", "id")
    )
    now = timezone.now()
    handled = []
    for item in items:
        before = item.disposition
        _clear_disposition_evidence(item)
        item.disposition = WorkItem.Disposition.HANDLED
        item.disposed_at = now
        item.disposed_by = current
        item.version += 1
        _save_projection(item)
        _event_and_audit(
            work_item=item,
            event_type=WorkItemEvent.EventType.DISPOSITIONED,
            actor=current,
            expected_version=None,
            idempotency_key="",
            fingerprint="",
            metadata={
                "before_disposition": before,
                "after_disposition": WorkItem.Disposition.HANDLED,
                "reason": reason,
            },
            audit_action="work_queue.item_dispositioned",
        )
        handled.append(item)
    return handled


@transaction.atomic
def set_no_action_required(
    work_item,
    *,
    category,
    reason,
    actor,
    expected_version,
    idempotency_key,
):
    if category not in WorkItem.NoActionCategory.values:
        raise ValidationError("Select an allowlisted no-action category.")
    reason = str(reason or "").strip()
    if len(reason) < 10 or len(reason) > 2000:
        raise ValidationError(
            "No action required needs a 10-2000 character substantive reason."
        )
    locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
    actor = _authorize_actor(locked, actor)
    if StaffRole.RECORDS_OFFICER not in roles_for_work_kind(locked.work_kind):
        raise ValidationError("No action required is a Records Officer decision.")
    # The actor must hold Records Officer even for work kinds that also permit
    # another role such as delivery operations or record review.
    actor = lock_authorized_staff(
        [(actor, {StaffRole.RECORDS_OFFICER})],
        message="Records Officer approval is required.",
    )[actor.pk]
    key = _mutation_key(idempotency_key)
    expected = _normalize_expected_version(expected_version)
    payload = {
        "category": category,
        "expected_version": expected,
        "reason": reason,
    }
    fingerprint = _fingerprint("no_action_required", locked, payload)
    existing = _existing_mutation(
        actor=actor,
        key=key,
        fingerprint=fingerprint,
        work_item=locked,
    )
    if existing is not None:
        return WorkMutationResult(locked, existing, True)
    _check_version(locked, expected)
    if locked.is_terminal:
        raise ValidationError("Reopen a disposed work item before redispositioning it.")
    before = locked.disposition
    now = timezone.now()
    locked.disposition = WorkItem.Disposition.NO_ACTION_REQUIRED
    locked.disposed_at = now
    locked.disposed_by = actor
    locked.no_action_category = category
    locked.no_action_reason = reason
    locked.no_action_by = actor
    locked.no_action_at = now
    locked.version += 1
    _save_projection(locked)
    metadata = {
        "before_disposition": before,
        "after_disposition": WorkItem.Disposition.NO_ACTION_REQUIRED,
        "category": category,
        "reason": reason,
    }
    event = _event_and_audit(
        work_item=locked,
        event_type=WorkItemEvent.EventType.DISPOSITIONED,
        actor=actor,
        expected_version=expected,
        idempotency_key=key,
        fingerprint=fingerprint,
        metadata=metadata,
        audit_action="work_queue.item_no_action_required",
        audit_metadata={
            "before_disposition": before,
            "after_disposition": WorkItem.Disposition.NO_ACTION_REQUIRED,
            "category": category,
            "reason_length": len(reason),
            "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
        },
    )
    return WorkMutationResult(locked, event, False)


@transaction.atomic
def reopen_work_item(
    work_item,
    *,
    actor,
    expected_version,
    idempotency_key,
):
    locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
    actor = _authorize_actor(locked, actor)
    key = _mutation_key(idempotency_key)
    expected = _normalize_expected_version(expected_version)
    fingerprint = _fingerprint(
        "reopen",
        locked,
        {"expected_version": expected},
    )
    existing = _existing_mutation(
        actor=actor,
        key=key,
        fingerprint=fingerprint,
        work_item=locked,
    )
    if existing is not None:
        return WorkMutationResult(locked, existing, True)
    _check_version(locked, expected)
    if not locked.is_terminal:
        raise ValidationError("Only a disposed work item can be reopened.")
    if locked.disposition == WorkItem.Disposition.SOURCE_EXCLUDED:
        raise ValidationError(
            "Source-excluded work can be reopened only by reconciliation."
        )
    before = locked.disposition
    locked.disposition = WorkItem.Disposition.NEEDS_ATTENTION
    _clear_disposition_evidence(locked)
    locked.version += 1
    _save_projection(locked)
    metadata = {
        "before_disposition": before,
        "after_disposition": WorkItem.Disposition.NEEDS_ATTENTION,
    }
    event = _event_and_audit(
        work_item=locked,
        event_type=WorkItemEvent.EventType.REOPENED,
        actor=actor,
        expected_version=expected,
        idempotency_key=key,
        fingerprint=fingerprint,
        metadata=metadata,
        audit_action="work_queue.item_reopened",
    )
    return WorkMutationResult(locked, event, False)
