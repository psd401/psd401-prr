"""PRA-safe conversion of plain unmatched Gmail into canonical intake."""

from __future__ import annotations

import datetime as dt
from email.utils import getaddresses, parseaddr

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from apps.api.models import StaffRole
from apps.audit.models import audit
from apps.configuration import runtime
from apps.correspondence import inbound
from apps.correspondence.authorization import require_current_human_staff
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceAttachment,
    InboundCorrespondenceReceipt,
)

from . import email_forwards, services
from .advisory_ai import defer_automatic_request_intent_advisory
from .intake import submit_intake
from .models import Channel, IntakeAttachment, PublicRecordsRequest
from .service_identities import require_exact_inbound_identity

_AUTOMATED_PRECEDENCE = frozenset({"bulk", "list", "junk"})
_RECONCILIATION_MAX_RECEIPTS = 10_000
_RECONCILIATION_MAX_SCANNED_RECEIPTS = 100_000


class _EmailIntakeClaimConflict(Exception):
    """A concurrent worker claimed the durable receipt first."""


def _normalized_headers(correspondence: Correspondence) -> dict[str, str]:
    return {
        str(name).casefold(): str(value)
        for name, value in correspondence.received_headers.items()
    }


def _bare_mailbox_recipient(receipt: InboundCorrespondenceReceipt) -> bool:
    recipients = []
    for supplied in receipt.envelope_recipients or ():
        address = parseaddr(str(supplied))[1].strip().casefold()
        if address:
            recipients.append(address)
    return recipients == [runtime.get("PRR_GMAIL_MAILBOX").casefold()]


def _requester_identity(correspondence: Correspondence) -> tuple[str, str] | None:
    parsed = [
        (display_name.strip(), address.strip())
        for display_name, address in getaddresses(
            [correspondence.received_sender]
        )
        if address.strip()
    ]
    if len(parsed) != 1:
        return None
    display_name, address = parsed[0]
    try:
        validate_email(address)
    except ValidationError:
        return None
    return display_name, address


def _eligible_unmatched_email(
    receipt: InboundCorrespondenceReceipt,
) -> tuple[str, str, str, dt.datetime, str] | None:
    """Return (name, email, body, received_at, forwarded_by) for intake.

    A forward opens under the original sender and the date they sent it
    (never later than the mailbox's own arrival time); ``forwarded_by`` names
    the forwarder, and is empty for mail the requester sent directly.
    """
    correspondence = receipt.matched_correspondence
    if (
        receipt.parsing_status
        != InboundCorrespondenceReceipt.ParsingStatus.PARSED
        or receipt.quarantined
        or receipt.matching_status
        != InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
        or correspondence is None
        or inbound.automatic_processing_blocked(receipt)
        or correspondence.kind
        == Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
        or not _bare_mailbox_recipient(receipt)
        or not correspondence.received_body.strip()
    ):
        return None

    headers = _normalized_headers(correspondence)
    auto_submitted = headers.get("auto-submitted", "").strip().casefold()
    if auto_submitted and auto_submitted != "no":
        return None
    precedence_values = {
        value.strip().casefold()
        for value in headers.get("precedence", "").split(",")
    }
    if precedence_values.intersection(_AUTOMATED_PRECEDENCE):
        return None
    if "list-id" in headers:
        return None

    identity = _requester_identity(correspondence)
    if identity is None:
        return None
    name, email = identity
    body = correspondence.received_body
    origin = email_forwards.original_sender(
        correspondence.received_subject, body
    )
    if origin is None:
        return name, email, body, receipt.received_at, ""
    received_at = receipt.received_at
    if origin.sent_at is not None and origin.sent_at <= receipt.received_at:
        received_at = origin.sent_at
    return origin.name, origin.email, body, received_at, email


def _stored_unmatched_email_candidates(*, actor):
    return (
        InboundCorrespondenceReceipt.objects.filter(
            provider="gmail",
            provider_account=runtime.get("PRR_GMAIL_MAILBOX"),
            submitted_by=actor,
            parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
            quarantined=False,
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
            ),
            matched_correspondence__isnull=False,
        )
        .select_related("matched_correspondence", "matched_request")
        .order_by("received_at", "id")
    )


def sync_intake_attachment_verdict(
    source: CorrespondenceAttachment,
) -> IntakeAttachment | None:
    """Mirror one existing scan verdict without reading or rescanning bytes."""
    source = CorrespondenceAttachment.objects.get(pk=source.pk)
    try:
        intake_attachment = IntakeAttachment.objects.get(
            source_correspondence_attachment=source,
        )
    except IntakeAttachment.DoesNotExist:
        return None
    IntakeAttachment.objects.filter(pk=intake_attachment.pk).update(
        storage_key=source.storage_key,
        scan_status=source.scan_status,
        scan_detail=source.quarantine_reason[:500],
        scanned_at=source.scanned_at,
    )
    # A final verdict on the last email file lets the intake proposal start.
    defer_automatic_request_intent_advisory(intake_attachment.request_id)
    return IntakeAttachment.objects.get(pk=intake_attachment.pk)


@transaction.atomic
def ensure_email_intake_attachments(
    receipt: InboundCorrespondenceReceipt,
    request: PublicRecordsRequest,
) -> list[IntakeAttachment]:
    """Represent every retained email attachment on the legal intake case."""
    receipt = InboundCorrespondenceReceipt.objects.select_for_update().get(
        pk=receipt.pk
    )
    if (
        receipt.matching_status
        != InboundCorrespondenceReceipt.MatchingStatus.MATCHED
        or receipt.match_method
        != InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
        or receipt.matched_request_id != request.pk
    ):
        raise ValidationError(
            "Email intake attachments require the receipt's canonical request."
        )

    represented = []
    for source in receipt.attachments.select_for_update().order_by(
        "created_at", "id"
    ):
        attachment, created = IntakeAttachment.objects.get_or_create(
            source_correspondence_attachment=source,
            defaults={
                "request": request,
                "original_filename": source.original_filename,
                "content_type": source.content_type,
                "size": source.size,
                "sha256": source.sha256,
                "storage_key": source.storage_key,
                "scan_status": source.scan_status,
                "scan_detail": source.quarantine_reason[:500],
                "scanned_at": source.scanned_at,
            },
        )
        if not created and attachment.request_id != request.pk:
            raise ValidationError(
                "An email attachment cannot be reassigned to another request."
            )
        represented.append(sync_intake_attachment_verdict(source))
    return [attachment for attachment in represented if attachment is not None]


def create_request_from_unmatched_email(
    receipt: InboundCorrespondenceReceipt,
    *,
    actor,
) -> PublicRecordsRequest | None:
    """Create or recover exactly one request for one eligible Gmail receipt."""
    receipt = InboundCorrespondenceReceipt.objects.select_related(
        "matched_correspondence",
        "matched_request",
    ).get(pk=receipt.pk)
    if (
        receipt.matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.MATCHED
        and receipt.match_method
        == InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
        and receipt.matched_request is not None
    ):
        ensure_email_intake_attachments(receipt, receipt.matched_request)
        return receipt.matched_request

    eligible = _eligible_unmatched_email(receipt)
    if eligible is None:
        return None
    name, email, body, received_at, forwarded_by = eligible

    def claim_receipt(request: PublicRecordsRequest) -> None:
        try:
            inbound.match_email_intake_receipt(receipt, request=request)
        except ValidationError as exc:
            raise _EmailIntakeClaimConflict() from exc

    try:
        request = submit_intake(
            {
                "email": email,
                "name": name,
                "description": body,
            },
            channel=Channel.EMAIL,
            actor=actor,
            integration_source="email",
            idempotency_key=receipt.idempotency_key,
            on_created=claim_receipt,
            received_at=received_at,
            # The email's files are attached below, after the request exists.
            prepare_intent=False,
        )
    except _EmailIntakeClaimConflict as exc:
        current = InboundCorrespondenceReceipt.objects.select_related(
            "matched_request"
        ).get(pk=receipt.pk)
        if (
            current.matching_status
            == InboundCorrespondenceReceipt.MatchingStatus.MATCHED
            and current.match_method
            == InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
            and current.matched_request is not None
        ):
            ensure_email_intake_attachments(current, current.matched_request)
            return current.matched_request
        raise (exc.__cause__ or exc) from exc

    if forwarded_by:
        audit(
            "request.email_forward_attributed",
            target=request.number,
            actor=actor,
            receipt_id=str(receipt.pk),
            forwarded_by=forwarded_by,
            mailbox_received_at=receipt.received_at.isoformat(),
            original_sent_at=received_at.isoformat(),
        )
    refreshed = InboundCorrespondenceReceipt.objects.get(pk=receipt.pk)
    ensure_email_intake_attachments(refreshed, request)
    defer_automatic_request_intent_advisory(request.pk)
    return request


@transaction.atomic
def redirect_email_intake_receipt(
    receipt: InboundCorrespondenceReceipt,
    *,
    request: PublicRecordsRequest,
    actor,
    reason: str,
    expected_version: int,
    ip_address: str | None = None,
) -> InboundCorrespondenceReceipt:
    """Move an auto-created email case's message onto an existing case.

    A requester who writes a follow-up from a fresh thread gets a brand-new
    case opened for them automatically, because the alternative -- a machine
    deciding which existing case an email belongs to -- is exactly the guess
    this system refuses to make. A human then recognizes the case it actually
    belongs to, and this is the one action that records that recognition.

    It is deliberately one atomic operation rather than two clicks. Closing
    the leftover case and moving the message are halves of a single decision:
    a partial failure would either strand the message on a closed case or
    leave the district holding two open cases for one request, and the second
    is a real statutory exposure because both would carry their own five-day
    clock. Either both happen or neither does.

    Nothing is deleted and nothing is sent. The leftover case keeps its
    timeline and closes in writing as not a records request, naming the case
    the message moved to; the message itself keeps every attachment and every
    byte of retained evidence, now attached where the work is.
    """
    # Authorize before touching anything. The rematch below performs the same
    # check under its own locks, but failing here keeps an unauthorized caller
    # from doing the closure work first only to have it rolled back.
    require_current_human_staff(
        actor,
        roles=(StaffRole.RECORDS_OFFICER,),
        message="Records Officer approval is required.",
    )
    if receipt is None or receipt.pk is None:
        raise ValidationError("A persisted inbound receipt is required.")
    receipt = InboundCorrespondenceReceipt.objects.select_related(
        "matched_request"
    ).get(pk=receipt.pk)
    if (
        receipt.matching_status
        != InboundCorrespondenceReceipt.MatchingStatus.MATCHED
        or receipt.match_method
        != InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
        or receipt.matched_request is None
    ):
        raise ValidationError(
            "Only a message whose case was auto-created from an unmatched "
            "email may be added to an existing case."
        )
    if receipt.matched_request_id == request.pk:
        raise ValidationError(
            "Adding this message to an existing case requires a different case."
        )

    # Take BOTH request rows now, in ascending pk order.
    #
    # rematch_inbound_receipt locks this exact pair in this exact order for the
    # deadlock reason, but close_email_non_request below locks only the source.
    # Reaching it first with an unordered single lock is how two officers
    # redirecting reciprocal follow-ups -- one moving case X's message to Y
    # while the other moves Y's to X -- can each end up holding the row the
    # other needs. Acquiring the pair up front puts this caller inside the same
    # ordering discipline before either half runs.
    locked = {
        item.pk: item
        for item in PublicRecordsRequest.objects.select_for_update()
        .filter(pk__in=[receipt.matched_request_id, request.pk])
        .order_by("pk")
    }
    if request.pk not in locked:
        raise ValidationError("The destination case does not exist.")
    if receipt.matched_request_id not in locked:
        raise ValidationError("The automatically opened case no longer exists.")
    source = locked[receipt.matched_request_id]
    destination = locked[request.pk]

    # A closed case is nobody's work. Moving a requester's follow-up onto one
    # would bury it exactly where no clock runs and no queue looks, which is
    # the outcome this whole action exists to prevent.
    if destination.status in services.CLOSED_STATUSES:
        raise ValidationError(
            "A message cannot be added to a case that is already closed; "
            "reopen it first if this message belongs to it."
        )

    # An already-terminal source is left exactly as it is: its closure is
    # already on the timeline, and transition() would refuse to move it again.
    source_closed_now = source.status not in services.CLOSED_STATUSES
    if source_closed_now:
        try:
            services.close_email_non_request(
                source,
                actor=actor,
                reason=(
                    f"Message added to {destination.number}; this "
                    "automatically opened case is not a separate records "
                    f"request. {reason}"
                ),
            )
        except services.LIFECYCLE_REFUSALS as exc:
            raise ValidationError(str(exc)) from exc

    matched = inbound.rematch_inbound_receipt(
        receipt,
        request=destination,
        actor=actor,
        reason=reason,
        expected_version=expected_version,
        ip_address=ip_address,
    )
    audit(
        "records.email_intake_redirected",
        target=destination.number,
        actor=actor,
        receipt_id=str(matched.pk),
        source_request=source.number,
        destination_request=destination.number,
        source_closed=source_closed_now,
        reason=reason,
        version=matched.version,
        ip_address=ip_address,
    )
    return matched


def reconcile_stored_unmatched_email_intake(
    *,
    actor,
    max_receipts: int = _RECONCILIATION_MAX_RECEIPTS,
) -> dict[str, int | str]:
    """Convert every bounded, eligible stored receipt through canonical intake."""
    if not isinstance(max_receipts, int) or isinstance(max_receipts, bool):
        raise ValidationError("Email intake reconciliation limit is invalid.")
    if max_receipts < 1 or max_receipts > _RECONCILIATION_MAX_RECEIPTS:
        raise ValidationError("Email intake reconciliation limit is invalid.")
    actor = require_exact_inbound_identity(actor)

    examined = 0
    converted = 0
    for receipt in _stored_unmatched_email_candidates(actor=actor).iterator(
        chunk_size=100
    ):
        examined += 1
        if examined > _RECONCILIATION_MAX_SCANNED_RECEIPTS:
            raise ValidationError(
                "Email intake reconciliation scan limit was exceeded."
            )
        if _eligible_unmatched_email(receipt) is None:
            continue
        if converted >= max_receipts:
            break
        request = create_request_from_unmatched_email(receipt, actor=actor)
        if request is not None:
            converted += 1

    remaining_examined = 0
    remaining_eligible = 0
    for receipt in _stored_unmatched_email_candidates(actor=actor).iterator(
        chunk_size=100
    ):
        remaining_examined += 1  # noqa: SIM113 - read after the loop
        if remaining_examined > _RECONCILIATION_MAX_SCANNED_RECEIPTS:
            raise ValidationError(
                "Email intake reconciliation scan limit was exceeded."
            )
        if _eligible_unmatched_email(receipt) is not None:
            remaining_eligible += 1

    return {
        "status": "ok",
        "examined": examined,
        "converted": converted,
        "remaining_eligible": remaining_eligible,
    }
