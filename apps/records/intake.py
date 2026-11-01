"""The one compliance intake pipeline shared by every submission channel."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit

from .advisory_ai import (
    defer_automatic_case_title_advisory,
    defer_automatic_request_intent_advisory,
)
from .attachments import persist_intake_attachments
from .deadlines import ferpa_access_due
from .models import (
    REQUESTER_ORIGINATED_CHANNELS,
    Channel,
    MatterKind,
    PublicRecordsRequest,
    Requester,
)
from .tasks import compute_statutory_deadline
from .triage import route_to_systems, triage_footage


def submit_intake(
    validated_data,
    *,
    channel=Channel.PORTAL,
    matter_kind=MatterKind.PUBLIC_RECORDS,
    actor=None,
    integration_source="",
    idempotency_key="",
    ip_address=None,
    on_created=None,
    attachments=(),
    write_identity_guard=None,
    received_at=None,
    prepare_intent=True,
):
    """Create a legal request, then run every required intake side effect.

    ``prepare_intent=False`` is for callers that attach the request's files
    after this returns (email intake); they start the proposal themselves
    once those files exist.
    """
    if received_at is not None and timezone.is_naive(received_at):
        raise ValidationError("Intake receipt timestamps require an explicit offset.")
    if matter_kind not in MatterKind.values:
        raise ValidationError("A matter must be opened as a valid kind.")
    # A litigation pull and a FERPA request are opened by a named staff member
    # who already knows which law applies. Nothing a requester reaches on
    # their own can declare itself anything but a public records request
    # (ADR 0004): the anonymous form and the intake mailbox are PRA-only.
    if (
        channel in REQUESTER_ORIGINATED_CHANNELS
        and matter_kind != MatterKind.PUBLIC_RECORDS
    ):
        raise ValidationError(
            "Only a public records request may arrive through the public form "
            "or the intake mailbox."
        )
    with transaction.atomic():
        # The integration guard reloads and locks the exact current principal
        # in this transaction. Its lock therefore spans every durable legal
        # acceptance row and the idempotency receipt link.
        if write_identity_guard is not None:
            actor = write_identity_guard(actor)

        # Each request retains the contact details actually submitted with
        # that legal request. Reusing and mutating a requester row would
        # retroactively change older requests when the same email submits
        # different details.
        requester = Requester.objects.create(
            email=validated_data.get("email", ""),
            name=validated_data.get("name", ""),
            phone=validated_data.get("phone", ""),
            mailing_address=validated_data.get("street_address", ""),
            city=validated_data.get("city", ""),
            state=validated_data.get("state", ""),
            postal_code=validated_data.get("postal_code", ""),
            company=validated_data.get("company", ""),
        )
        request_values = {
            "requester": requester,
            "description": validated_data["description"],
            "channel": channel,
            "matter_kind": matter_kind,
        }
        if received_at is not None:
            request_values["received_at"] = received_at
        prr = PublicRecordsRequest.objects.create(
            **request_values,
        )
        if matter_kind == MatterKind.FERPA_PARENT:
            # 34 CFR 99.10(b) is plain calendar arithmetic with no tolling, so
            # it commits with the matter rather than going through the Celery
            # task that owns the RCW 42.56.520 business-day computation.
            prr.ferpa_access_due = ferpa_access_due(prr.received_at)
            prr.save(update_fields=["ferpa_access_due"])

        # Link the integration receipt before any asynchronous or external
        # side effect. Requester, request, and receipt linkage commit together,
        # so a retry cannot create a second legal request.
        if on_created:
            on_created(prr)

        # Creation evidence commits with the legal request and idempotency
        # link. A later preservation, routing, or storage failure therefore
        # leaves one attributable request to reconcile instead of an unaudited
        # legal record.
        detail = {}
        if integration_source:
            detail["integration_source"] = integration_source
        if idempotency_key:
            detail["idempotency_key"] = idempotency_key
        audit(
            "request.created",
            target=prr.number,
            actor=actor,
            ip_address=ip_address,
            **detail,
        )

        from apps.work_queue.integration import (
            schedule_source_work_item_reconciliation,
        )

        schedule_source_work_item_reconciliation("requests", prr.pk)

    if matter_kind == MatterKind.PUBLIC_RECORDS:
        # The RCW 42.56.520 clock belongs to public records requests alone.
        compute_statutory_deadline.delay(str(prr.id))
    triage_footage(prr)  # auto-hold footage BEFORE anything else happens
    route_to_systems(prr)
    persist_intake_attachments(prr, attachments)
    # Every channel, including the ones that attach their files later: the
    # title proposal reads the request text alone, so there is nothing for it
    # to wait on. It is a label for a staff column and reaches no requester.
    defer_automatic_case_title_advisory(prr.pk)
    if prepare_intent:
        # Prepares the officer's starting point only. Nothing here drafts,
        # sends, or defers anything the requester sees. It runs after the
        # files are stored because the proposal reads them.
        defer_automatic_request_intent_advisory(prr.pk)

    if channel in {Channel.PORTAL, Channel.EMAIL}:
        from apps.correspondence.services import (
            OutboundTransportDisabled,
            queue_submission_receipt,
        )

        try:
            queue_submission_receipt(prr)
        except OutboundTransportDisabled:
            # The receipt is non-statutory. The transport kill switch must not
            # turn a successfully accepted legal request into a failed intake.
            pass
    return prr
