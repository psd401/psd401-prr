import datetime as dt

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Count


_EFFECTS = {
    "acknowledgment": "acknowledgment_delivery",
    "clarification_request": "clarification_delivery",
    "estimate_revision": "estimate_revision_delivery",
    "denial": "denial_delivery",
}


def _substantive(value):
    return isinstance(value, str) and len(value.strip()) >= 10


def _delivery_evidence_is_valid(
    correspondence,
    *,
    Delivery,
    Attempt,
):
    deliveries = list(
        Delivery.objects.filter(
            correspondence_id=correspondence.pk,
        )[:2]
    )
    if len(deliveries) != 1:
        return False
    delivery = deliveries[0]
    successful_number = delivery.successful_attempt_number
    if (
        delivery.status != "sent"
        or successful_number is None
        or delivery.attempt_count != successful_number
        or delivery.completed_at is None
    ):
        return False
    attempts = list(
        Attempt.objects.filter(
            delivery_id=delivery.pk,
            attempt_number=successful_number,
        )[:2]
    )
    if len(attempts) != 1:
        return False
    attempt = attempts[0]
    return (
        attempt.result_category in {"sent", "reconciled"}
        and attempt.result_at == delivery.completed_at
        and bool(attempt.result_fingerprint)
        and attempt.actor_id is not None
        and attempt.claim_owner == str(attempt.actor_id)
        and bool(attempt.provider_message_id)
        and attempt.provider_message_id
        == delivery.provider_message_id
        == correspondence.provider_message_id
        and attempt.provider_thread_id
        == delivery.provider_thread_id
        == correspondence.provider_thread_id
        and attempt.provider_timestamp == correspondence.sent_at
    )


def _acknowledgment_evidence_is_valid(
    correspondence,
    event,
    *,
    Request,
    StatusEvent,
    AuditLog,
):
    request = Request.objects.get(pk=correspondence.request_id)
    if request.acknowledgment_sent_at != correspondence.sent_at:
        return False
    audits = list(
        AuditLog.objects.filter(
            action="request.acknowledged",
            target=request.number,
            actor_id=correspondence.approved_by_id,
        ).values_list("detail", flat=True)
    )
    communicated_at = correspondence.sent_at.isoformat()
    if not any(
        isinstance(detail, dict)
        and detail.get("communicated_at") == communicated_at
        for detail in audits
    ):
        return False
    current_transition = (
        event.to_status == "acknowledged"
        and event.visible_to_requester
        and _substantive(event.note)
    )
    late_acknowledgment = (
        event.from_status == ""
        and event.to_status == ""
        and event.visible_to_requester
        and _substantive(event.note)
    )
    legacy_transition = (
        event.from_status == "received"
        and event.to_status == "acknowledged"
        and not event.visible_to_requester
        and event.note == ""
        and any(
            _substantive(note)
            for note in StatusEvent.objects.filter(
                request_id=event.request_id,
                actor_id=event.actor_id,
                occurred_at=event.occurred_at,
                from_status="",
                to_status="",
                visible_to_requester=True,
            )
            .exclude(pk=event.pk)
            .values_list("note", flat=True)
        )
    )
    return (
        current_transition
        or late_acknowledgment
        or legacy_transition
    )


def _clarification_evidence_is_valid(
    correspondence,
    event,
    *,
    ClarificationCycle,
):
    return (
        event.to_status == "clarification"
        and event.visible_to_requester
        and _substantive(event.note)
        and ClarificationCycle.objects.filter(
            request_id=correspondence.request_id,
            outbound_correspondence_id=correspondence.pk,
            opened_status_event_id=event.pk,
            opened_at=correspondence.sent_at,
        ).count()
        == 1
    )


def _estimate_evidence_is_valid(
    correspondence,
    event,
    *,
    EstimateRevision,
):
    revisions = list(
        EstimateRevision.objects.filter(
            correspondence_id=correspondence.pk,
        )[:2]
    )
    if len(revisions) != 1:
        return False
    revision = revisions[0]
    metadata = correspondence.approved_metadata
    if not isinstance(metadata, dict):
        return False
    try:
        approved_date = dt.date.fromisoformat(metadata["new_date"])
    except (KeyError, TypeError, ValueError):
        return False
    reason = str(metadata.get("reason", "")).strip()
    expected_note = (
        f"Estimated completion revised to {approved_date}. "
        f"Reason: {reason}"
    )
    return (
        event.from_status == ""
        and event.to_status == ""
        and event.visible_to_requester
        and event.note == expected_note
        and _substantive(reason)
        and revision.request_id == correspondence.request_id
        and revision.new_date == approved_date
        and revision.reason == reason
        and revision.automated is False
        and revision.revised_by_id == correspondence.approved_by_id
        and revision.communicated_at == correspondence.sent_at
    )


def _denial_evidence_is_valid(
    correspondence,
    event,
    *,
    Request,
):
    request = Request.objects.get(pk=correspondence.request_id)
    return (
        event.to_status == "denied"
        and event.visible_to_requester
        and _substantive(event.note)
        and request.status == "denied"
        and request.closed_at == correspondence.sent_at
    )


def backfill_and_validate_resulting_events(apps, schema_editor):
    Correspondence = apps.get_model(
        "correspondence",
        "Correspondence",
    )
    Delivery = apps.get_model(
        "correspondence",
        "CorrespondenceDelivery",
    )
    Attempt = apps.get_model(
        "correspondence",
        "CorrespondenceDeliveryAttempt",
    )
    ClarificationCycle = apps.get_model(
        "correspondence",
        "ClarificationCycle",
    )
    EstimateRevision = apps.get_model("records", "EstimateRevision")
    Request = apps.get_model("records", "PublicRecordsRequest")
    StatusEvent = apps.get_model("records", "StatusEvent")
    AuditLog = apps.get_model("audit", "AuditLog")

    unsafe_ids = set()
    duplicate_event_ids = set(
        Correspondence.objects.exclude(
            resulting_status_event_id=None,
        )
        .values("resulting_status_event_id")
        .annotate(total=Count("pk"))
        .filter(total__gt=1)
        .values_list("resulting_status_event_id", flat=True)
    )
    if duplicate_event_ids:
        unsafe_ids.update(
            str(pk)
            for pk in Correspondence.objects.filter(
                resulting_status_event_id__in=duplicate_event_ids,
            ).values_list("pk", flat=True)
        )

    lifecycle_kinds = set(_EFFECTS)
    missing_bindings = Correspondence.objects.filter(
        direction="outbound",
        kind__in=lifecycle_kinds,
        status="sent",
        resulting_status_event_id=None,
    ).values_list("pk", flat=True)
    unsafe_ids.update(str(pk) for pk in missing_bindings)

    assignments = []
    candidates = Correspondence.objects.exclude(
        resulting_status_event_id=None,
    ).order_by("pk")
    for correspondence in candidates.iterator(chunk_size=250):
        correspondence_id = str(correspondence.pk)
        if correspondence_id in unsafe_ids:
            continue
        try:
            event = StatusEvent.objects.get(
                pk=correspondence.resulting_status_event_id,
            )
        except StatusEvent.DoesNotExist:
            unsafe_ids.add(correspondence_id)
            continue
        effect = _EFFECTS.get(correspondence.kind)
        common_valid = (
            effect is not None
            and correspondence.direction == "outbound"
            and correspondence.status == "sent"
            and correspondence.request_id is not None
            and correspondence.sent_at is not None
            and event.correspondence_effect == ""
            and event.request_id == correspondence.request_id
            and event.actor_id == correspondence.approved_by_id
            and event.occurred_at == correspondence.sent_at
            and _delivery_evidence_is_valid(
                correspondence,
                Delivery=Delivery,
                Attempt=Attempt,
            )
        )
        if not common_valid:
            unsafe_ids.add(correspondence_id)
            continue
        if correspondence.kind == "acknowledgment":
            valid = _acknowledgment_evidence_is_valid(
                correspondence,
                event,
                Request=Request,
                StatusEvent=StatusEvent,
                AuditLog=AuditLog,
            )
        elif correspondence.kind == "clarification_request":
            valid = _clarification_evidence_is_valid(
                correspondence,
                event,
                ClarificationCycle=ClarificationCycle,
            )
        elif correspondence.kind == "estimate_revision":
            valid = _estimate_evidence_is_valid(
                correspondence,
                event,
                EstimateRevision=EstimateRevision,
            )
        else:
            valid = _denial_evidence_is_valid(
                correspondence,
                event,
                Request=Request,
            )
        if valid:
            assignments.append((event.pk, effect))
        else:
            unsafe_ids.add(correspondence_id)

    if unsafe_ids:
        ordered = sorted(unsafe_ids)
        sample = ", ".join(ordered[:25])
        suffix = (
            f" (and {len(ordered) - 25} more)"
            if len(ordered) > 25
            else ""
        )
        raise RuntimeError(
            "Cannot enable one-shot correspondence result-event integrity "
            "while legacy lifecycle evidence is missing, ambiguous, or "
            "inconsistent. Do not synthesize or rebind evidence; review these "
            f"correspondence IDs under the prior release: {sample}{suffix}"
        )

    for event_id, effect in assignments:
        StatusEvent.objects.filter(pk=event_id).update(
            correspondence_effect=effect,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0001_initial"),
        ("correspondence", "0011_recipient_snapshot_cutover"),
        ("records", "0008_statusevent_correspondence_effect_and_more"),
    ]

    operations = [
        migrations.RunPython(
            backfill_and_validate_resulting_events,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="correspondence",
            name="resulting_status_event",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="resulting_correspondence",
                to="records.statusevent",
            ),
        ),
    ]
