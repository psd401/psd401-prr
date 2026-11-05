"""Decision batches (issue #43): an agent proposes, a human approves.

``propose_decision_batch`` records what is proposed, by whom, and on what
evidence; it changes nothing else, so an attributed personal token may call
it. ``approve_decision_batch`` and ``reject_decision_batch`` are the human
decision, made in the staff application (the API hands token callers off).
Approval runs the same service the staff UI runs for that outcome:

- ``review``: ``review.bulk_review_records`` (responsive / non-responsive);
- ``supersede``: ``collection_supersession.supersede_collection``;
- ``scope_and_collect``: ``vault_collections.queue_vault_collection``, or
  ``scoping.rescope`` when the proposal carries a re-scope reason;
- ``redaction_group``: ``redaction_batches.create_redaction_batch``. That only
  opens the group's sampled-QA batch; the sampled judgments stay a reviewer's
  own decision in the Review tab (``redaction_batches.decide`` is human-only);
- ``intake_label``: ``services.merge_requests`` for ``duplicate_of`` and
  ``services.close_email_non_request`` for ``non_request``. Neither sends
  requester mail (rule 6). Labels that change no state are not proposable.

Both the proposal and the decision are append-only rows, so a decided batch
is never rewritten. A service that refuses leaves the batch proposed.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.api.identity import has_staff_role
from apps.api.models import StaffRole
from apps.audit.models import audit

from .collection_supersession import MIN_SUPERSESSION_REASON, supersede_collection
from .models import (
    CollectionTask,
    DecisionBatch,
    DecisionBatchDecision,
    PublicRecordsRequest,
    RecordItem,
    VaultCountRun,
)
from .redaction_batches import create_redaction_batch
from .review import BULK_REVIEW_STATES, bulk_review_records

#: Most items one batch may carry; a proposal is one reviewable decision.
MAX_BATCH_ITEMS = 500
MIN_REJECTION_REASON = 10

_KIND_ROLES = {
    DecisionBatch.Kind.REVIEW: (StaffRole.REVIEWER, StaffRole.RECORDS_OFFICER),
    DecisionBatch.Kind.REDACTION_GROUP: (StaffRole.REVIEWER, StaffRole.RECORDS_OFFICER),
    DecisionBatch.Kind.SUPERSEDE: (StaffRole.RECORDS_OFFICER,),
    DecisionBatch.Kind.SCOPE_AND_COLLECT: (StaffRole.RECORDS_OFFICER,),
    DecisionBatch.Kind.INTAKE_LABEL: (StaffRole.RECORDS_OFFICER,),
}
_STATE_CHANGING_INTAKE_LABELS = ("duplicate_of", "non_request")


def batch_state(batch: DecisionBatch) -> str:
    outcome = (
        DecisionBatchDecision.objects.filter(batch=batch).values_list("outcome", flat=True).first()
    )
    return outcome or "proposed"


def _require_kind_role(kind, actor):
    roles = _KIND_ROLES[kind]
    if not has_staff_role(actor, *roles):
        names = " or ".join(str(StaffRole(role).label) for role in roles)
        raise ValidationError(f"A current {names} is required for a {DecisionBatch.Kind(kind).label.lower()} batch.")


def _request_by_number(number):
    try:
        return PublicRecordsRequest.objects.get(number=str(number or ""))
    except PublicRecordsRequest.DoesNotExist as exc:
        raise ValidationError("No request has that number.") from exc


def _validate_review(payload):
    ids = [str(value) for value in payload.get("record_item_ids") or []]
    if not ids or len(ids) > MAX_BATCH_ITEMS:
        raise ValidationError(f"A review batch names 1 to {MAX_BATCH_ITEMS} records.")
    state = payload.get("state")
    if state not in BULK_REVIEW_STATES:
        raise ValidationError("A review batch's state must be responsive or non_responsive.")
    requests = set(RecordItem.objects.filter(pk__in=ids).values_list("request_id", flat=True))
    if RecordItem.objects.filter(pk__in=ids).count() != len(set(ids)):
        raise ValidationError("Every record in a review batch must exist.")
    if len(requests) != 1:
        raise ValidationError("A review batch's records must all belong to one request.")
    return PublicRecordsRequest.objects.get(pk=requests.pop()), {"record_item_ids": ids, "state": state}


def _validate_supersede(payload):
    reason = str(payload.get("reason") or "").strip()
    if len(reason) < MIN_SUPERSESSION_REASON:
        raise ValidationError(f"Say why the collection is superseded (at least {MIN_SUPERSESSION_REASON} characters).")
    task = CollectionTask.objects.filter(pk=payload.get("collection_task_id")).select_related("request").first()
    if task is None:
        raise ValidationError("No collection task has that id.")
    return task.request, {"collection_task_id": task.pk, "reason": reason}


def _validate_scope(payload):
    run = VaultCountRun.objects.filter(pk=payload.get("count_run_id")).select_related("request").first()
    if run is None:
        raise ValidationError("No Vault count has that id.")
    clean = {
        "count_run_id": str(run.pk),
        "expected_input_sha256": str(payload.get("expected_input_sha256") or ""),
    }
    rescope_reason = str(payload.get("rescope_reason") or "").strip()
    if rescope_reason:
        clean["rescope_reason"] = rescope_reason
    return run.request, clean


def _validate_redaction_group(payload):
    request = _request_by_number(payload.get("request_number"))
    try:
        exemption_id = int(payload.get("exemption_id"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("A redaction group names its exemption_id.") from exc
    return request, {
        "request_number": request.number,
        "exemption_id": exemption_id,
        "pattern": str(payload.get("pattern") or ""),
    }


def _validate_intake_label(payload):
    request = _request_by_number(payload.get("request_number"))
    label = payload.get("label")
    if label not in _STATE_CHANGING_INTAKE_LABELS:
        raise ValidationError(
            "Only the duplicate_of and non_request intake labels change a case, so only they "
            "can be proposed as a batch; record other labels as intake feedback."
        )
    reason = str(payload.get("reason") or "").strip()
    if len(reason) < MIN_REJECTION_REASON:
        raise ValidationError("Say why the label applies.")
    clean = {"request_number": request.number, "label": label, "reason": reason}
    if label == "duplicate_of":
        clean["target_case_number"] = _request_by_number(payload.get("target_case_number")).number
    return request, clean


_VALIDATORS = {
    DecisionBatch.Kind.REVIEW: _validate_review,
    DecisionBatch.Kind.SUPERSEDE: _validate_supersede,
    DecisionBatch.Kind.SCOPE_AND_COLLECT: _validate_scope,
    DecisionBatch.Kind.REDACTION_GROUP: _validate_redaction_group,
    DecisionBatch.Kind.INTAKE_LABEL: _validate_intake_label,
}


def propose_decision_batch(kind, payload, *, actor, evidence_summary) -> DecisionBatch:
    """Record a proposed batch. Changes nothing but this row and its audit."""
    if kind not in DecisionBatch.Kind.values:
        raise ValidationError("Unknown decision batch kind.")
    evidence = str(evidence_summary or "").strip()
    if not evidence:
        raise ValidationError("A decision batch needs an evidence summary.")
    if len(evidence) > 4000:
        raise ValidationError("Keep the evidence summary under 4,000 characters.")
    _require_kind_role(kind, actor)
    if not isinstance(payload, dict):
        raise ValidationError("A decision batch's payload is an object.")
    request, clean = _VALIDATORS[kind](payload)
    batch = DecisionBatch.objects.create(
        kind=kind, request=request, payload=clean, evidence_summary=evidence, proposed_by=actor,
    )
    audit(
        "decision_batch.proposed",
        target=request.number,
        actor=actor,
        batch_id=str(batch.pk),
        kind=kind,
    )
    return batch


def _execute(batch: DecisionBatch, actor) -> dict:
    payload = batch.payload
    kind = batch.kind
    if kind == DecisionBatch.Kind.REVIEW:
        items = bulk_review_records(payload["record_item_ids"], state=payload["state"], actor=actor)
        return {"reviewed": len(items)}
    if kind == DecisionBatch.Kind.SUPERSEDE:
        result = supersede_collection(payload["collection_task_id"], actor=actor, reason=payload["reason"])
        return {"collection_task_id": result.collection_task_id, "record_count": result.record_count}
    if kind == DecisionBatch.Kind.SCOPE_AND_COLLECT:
        from .scoping import rescope
        from .vault_collections import queue_vault_collection

        if payload.get("rescope_reason"):
            task = rescope(
                batch.request, count_run_id=payload["count_run_id"],
                expected_input_sha256=payload["expected_input_sha256"],
                actor=actor, reason=payload["rescope_reason"],
            )
        else:
            task = queue_vault_collection(
                payload["count_run_id"], actor=actor,
                expected_input_sha256=payload["expected_input_sha256"],
            ).task
        return {"collection_task_id": task.pk}
    if kind == DecisionBatch.Kind.REDACTION_GROUP:
        redaction_batch = create_redaction_batch(
            batch.request, exemption_id=payload["exemption_id"], pattern=payload["pattern"], actor=actor,
        )
        return {"redaction_batch_id": redaction_batch.pk}
    from .services import close_email_non_request, merge_requests

    if payload["label"] == "duplicate_of":
        target = PublicRecordsRequest.objects.get(number=payload["target_case_number"])
        merge_requests(batch.request, target, actor=actor, reason=payload["reason"])
        return {"merged_into": target.number}
    closed = close_email_non_request(batch.request, actor=actor, reason=payload["reason"])
    return {"status": closed.status}


def _decide(batch_id, *, actor, outcome, reason="", result=None) -> DecisionBatch:
    with transaction.atomic():
        batch = DecisionBatch.objects.select_for_update().get(pk=batch_id)
        if DecisionBatchDecision.objects.filter(batch=batch).exists():
            raise ValidationError("This batch was already decided.")
        DecisionBatchDecision.objects.create(
            batch=batch, outcome=outcome, decided_by=actor, reason=reason, result=result or {},
        )
        audit(
            f"decision_batch.{outcome}",
            target=batch.request.number,
            actor=actor,
            batch_id=str(batch.pk),
            kind=batch.kind,
            reason=reason,
            result=result or {},
        )
    return batch


def _undecided(batch_id, actor) -> DecisionBatch:
    batch = DecisionBatch.objects.select_related("request").get(pk=batch_id)
    if DecisionBatchDecision.objects.filter(batch=batch).exists():
        raise ValidationError("This batch was already decided.")
    _require_kind_role(batch.kind, actor)
    return batch


def approve_decision_batch(batch_id, *, actor) -> DecisionBatch:
    """Run the batch's service as ``actor`` and record the approval."""
    batch = _undecided(batch_id, actor)
    try:
        if batch.kind == DecisionBatch.Kind.SCOPE_AND_COLLECT:
            # Its services commit, then hand the export to a worker; an outer
            # transaction would dispatch the worker before the task exists.
            # Both are idempotent for a repeated approval of the same count.
            result = _execute(batch, actor)
            return _decide(batch.pk, actor=actor, outcome=DecisionBatchDecision.Outcome.APPROVED, result=result)
        with transaction.atomic():
            DecisionBatch.objects.select_for_update().get(pk=batch.pk)
            if DecisionBatchDecision.objects.filter(batch=batch).exists():
                raise ValidationError("This batch was already decided.")
            result = _execute(batch, actor)
            return _decide(batch.pk, actor=actor, outcome=DecisionBatchDecision.Outcome.APPROVED, result=result)
    except PermissionDenied as exc:
        raise ValidationError(str(exc)) from exc


def reject_decision_batch(batch_id, *, actor, reason) -> DecisionBatch:
    """Record that a person declined the batch. Nothing else changes."""
    reason = str(reason or "").strip()
    if len(reason) < MIN_REJECTION_REASON:
        raise ValidationError(f"Say why the batch is rejected (at least {MIN_REJECTION_REASON} characters).")
    batch = _undecided(batch_id, actor)
    return _decide(batch.pk, actor=actor, outcome=DecisionBatchDecision.Outcome.REJECTED, reason=reason)
