"""Durable, idempotent orchestration for hostile inbound RFC822 evidence."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from typing import cast

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps import secure_files
from apps.audit.models import audit
from apps.configuration import runtime
from apps.records.service_identities import require_exact_inbound_identity

from .inbound import (
    match_delivery_status_report,
    match_inbound_evidence,
)
from .mime import (
    InboundMimeLimits,
    InboundMimeSecurityError,
    parse_inbound_message,
)
from .models import (
    Correspondence,
    CorrespondenceAttachment,
    InboundCorrespondenceReceipt,
    InboundCorrespondenceReceiptQuerySet,
    InboundQuarantineCleanupInProgress,
    InboundQuarantineReservationAlreadyAdopted,
    InboundQuarantineReservationConflict,
    InboundQuarantineTransportAlias,
    InboundQuarantineWriteReservation,
)

logger = logging.getLogger(__name__)

_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]+$", re.ASCII)
_PROVIDER_ACCOUNT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$",
    re.ASCII,
)
_IDEMPOTENCY_KEY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    re.ASCII,
)
_READ_CHUNK_SIZE = 1024 * 1024
_PARSER_UNAVAILABLE = "parser_unavailable"
_PROCESSING_LEASE_SECONDS = 60 * 60


class InboundIdempotencyConflict(Exception):
    """An inbound Idempotency-Key was reused for different evidence."""


class InboundProviderMessageConflict(Exception):
    """A provider message identifier conflicts with retained evidence."""


class InboundSubmissionInProgress(Exception):
    """The first process still owns this inbound receipt."""


class InboundStorageUnavailable(Exception):
    """Private raw-message or attachment storage is unavailable."""

    def __init__(self):
        super().__init__(
            "Private inbound evidence storage is temporarily unavailable."
        )


@dataclass(frozen=True)
class InboundReceiveResult:
    receipt: InboundCorrespondenceReceipt
    receipt_id: object
    correspondence_id: object | None
    outcome: str
    parsing_status: str
    matching_status: str
    match_method: str
    quarantine_status: str
    deduplicated: bool


def _mime_limits() -> InboundMimeLimits:
    return InboundMimeLimits(
        max_raw_bytes=runtime.get("PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES"),
        max_header_count=runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_HEADERS"),
        max_header_line_bytes=(
            runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES")
        ),
        max_parts=runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_MIME_PARTS"),
        max_depth=runtime.get("PRR_CORRESPONDENCE_INBOUND_MAX_MIME_DEPTH"),
        max_body_bytes=runtime.get("PRR_CORRESPONDENCE_INBOUND_BODY_MAX_BYTES"),
        max_attachment_count=runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT"),
        max_attachment_bytes=runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES"),
        max_total_attachment_bytes=(
            runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES")
        ),
    )


def _validate_transport_metadata(
    *,
    provider,
    provider_account,
    provider_message_id,
    provider_thread_id,
    envelope_sender,
    envelope_recipients,
    received_at,
    idempotency_key,
    ip_address,
):
    if provider != "gmail":
        raise ValidationError("The inbound provider is not supported.")
    provider_account_is_mailbox = False
    if (
        provider == "gmail"
        and provider_account == runtime.get("PRR_GMAIL_MAILBOX")
        and len(provider_account) <= 100
    ):
        try:
            validate_email(provider_account)
        except ValidationError:
            pass
        else:
            provider_account_is_mailbox = True
    if not (
        provider_account_is_mailbox
        or _PROVIDER_ACCOUNT.fullmatch(provider_account or "")
    ):
        raise ValidationError("The inbound provider account is invalid.")
    if not _PROVIDER_ID.fullmatch(provider_message_id or ""):
        raise ValidationError("The provider message identifier is invalid.")
    if not _PROVIDER_ID.fullmatch(provider_thread_id or ""):
        raise ValidationError("The provider thread identifier is invalid.")
    if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key or ""):
        raise ValidationError("The inbound Idempotency-Key is invalid.")
    if received_at is None or timezone.is_naive(received_at):
        raise ValidationError(
            "The provider received timestamp requires an explicit offset."
        )

    if envelope_sender:
        validate_email(envelope_sender)
    recipients = list(envelope_recipients or ())
    if not 1 <= len(recipients) <= 50:
        raise ValidationError("Inbound envelope recipients are invalid.")
    normalized_recipients = []
    seen = set()
    for recipient in recipients:
        validate_email(recipient)
        if recipient in seen:
            raise ValidationError(
                "Inbound envelope recipients must be unique."
            )
        seen.add(recipient)
        normalized_recipients.append(recipient)

    normalized_ip = None
    if ip_address:
        try:
            normalized_ip = str(ipaddress.ip_address(ip_address))
        except ValueError as exc:
            raise ValidationError(
                "The inbound transport IP address is invalid."
            ) from exc
    return normalized_recipients, normalized_ip


def _read_bounded_raw_message(raw_message) -> bytes:
    limit = runtime.get("PRR_CORRESPONDENCE_INBOUND_MESSAGE_MAX_BYTES")
    if isinstance(raw_message, bytes):
        payload = raw_message
    else:
        if raw_message is None or not hasattr(raw_message, "read"):
            raise ValidationError("The original RFC822 message is required.")
        with contextlib.suppress(AttributeError, OSError):
            raw_message.seek(0)
        chunks = []
        size = 0
        while True:
            chunk = raw_message.read(
                min(_READ_CHUNK_SIZE, limit + 1 - size)
            )
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ValidationError(
                    "The original RFC822 message must contain bytes."
                )
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValidationError(
                    "The original RFC822 message exceeds its size limit."
                )
        payload = b"".join(chunks)
        with contextlib.suppress(AttributeError, OSError):
            raw_message.seek(0)
    if not payload:
        raise ValidationError(
            "The original RFC822 message is required and cannot be empty."
        )
    if len(payload) > limit:
        raise ValidationError(
            "The original RFC822 message exceeds its size limit."
        )
    return payload


def _payload_digest(
    *,
    raw_sha256,
    provider,
    provider_account,
    provider_message_id,
    provider_thread_id,
    envelope_sender,
    envelope_recipients,
    received_at,
):
    normalized_timestamp = (
        received_at.astimezone(dt.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    canonical = json.dumps(
        {
            "envelope_recipients": list(envelope_recipients),
            "envelope_sender": envelope_sender,
            "provider": provider,
            "provider_account": provider_account,
            "provider_message_id": provider_message_id,
            "provider_received_at": normalized_timestamp,
            "provider_thread_id": provider_thread_id,
            "raw_message_sha256": raw_sha256,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _outcome_for(receipt) -> str:
    if receipt.parsing_status in {
        InboundCorrespondenceReceipt.ParsingStatus.FAILED,
        InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
    }:
        return "quarantined"
    return {
        InboundCorrespondenceReceipt.MatchingStatus.MATCHED: "matched",
        InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE: "candidate",
        InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS: "ambiguous",
        InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED: "unmatched",
        InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED: "quarantined",
        InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE: "duplicate",
    }[receipt.matching_status]


def _result(receipt, *, deduplicated=False):
    return InboundReceiveResult(
        receipt=receipt,
        receipt_id=receipt.pk,
        correspondence_id=receipt.matched_correspondence_id,
        outcome="duplicate" if deduplicated else _outcome_for(receipt),
        parsing_status=receipt.parsing_status,
        matching_status=receipt.matching_status,
        match_method=receipt.match_method,
        quarantine_status=(
            "quarantined" if receipt.quarantined else "released"
        ),
        deduplicated=deduplicated,
    )


def _existing_result(receipt, payload_sha256):
    if receipt.payload_sha256 != payload_sha256:
        return None
    if receipt.completed_at is None:
        raise InboundSubmissionInProgress()
    return _result(receipt, deduplicated=True)


def _resolve_existing(
    *,
    actor,
    idempotency_key,
    provider,
    provider_account,
    provider_message_id,
    payload_sha256,
):
    receipt = (
        InboundCorrespondenceReceipt.objects.select_for_update()
        .filter(submitted_by=actor, idempotency_key=idempotency_key)
        .first()
    )
    if receipt is not None:
        existing = _existing_result(receipt, payload_sha256)
        if existing is None:
            raise InboundIdempotencyConflict()
        return existing

    receipt = (
        InboundCorrespondenceReceipt.objects.select_for_update()
        .filter(
            provider=provider,
            provider_account=provider_account,
            provider_message_id=provider_message_id,
        )
        .first()
    )
    if receipt is not None:
        existing = _existing_result(receipt, payload_sha256)
        if existing is None:
            raise InboundProviderMessageConflict()
        return existing
    return None


def _finalize_quarantined_receipt(
    receipt,
    *,
    parsing_status,
    reason_code,
):
    return InboundCorrespondenceReceipt.objects.filter(
        pk=receipt.pk
    ).finalize_locked_parsing(
        parsing_status=parsing_status,
        completed_at=timezone.now(),
        quarantine_reason=reason_code,
        parsing_error=reason_code,
    )


def _create_correspondence(receipt, parsed, actor):
    sender = parsed.headers.get("From", parsed.from_address)
    recipients = [*parsed.to_addresses, *parsed.cc_addresses]
    return Correspondence.objects.create(
        request=None,
        direction=Correspondence.Direction.INBOUND,
        kind=(
            Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION
            if parsed.delivery_status_report is not None
            else Correspondence.Kind.GENERAL
        ),
        status=Correspondence.Status.UNMATCHED,
        received_subject=parsed.subject,
        received_body=parsed.body,
        received_sender=sender,
        received_recipients=recipients,
        received_headers=dict(parsed.headers),
        created_by=actor,
        received_at=receipt.received_at,
        rfc_message_id=None,
        provider=receipt.provider,
        provider_account=receipt.provider_account,
        provider_message_id=receipt.provider_message_id,
        provider_thread_id=receipt.provider_thread_id,
    )


def _enqueue_attachment_scan(attachment_id):
    # Imported lazily so the task module can delegate delivery work to
    # correspondence.services without forming a module-import cycle.
    from .tasks import scan_correspondence_attachment

    try:
        scan_correspondence_attachment.delay(attachment_id)
    except Exception:  # noqa: BLE001 - the scan watchdog re-enqueues
        logger.error(
            "Could not enqueue a quarantined correspondence attachment scan",
            extra={"attachment_id": attachment_id},
        )


def _reservation_identity(*parts) -> str:
    canonical = "\0".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@transaction.atomic
def _claim_transport_aliases(
    *,
    logical_identity,
    idempotency_identity,
    provider_identity,
):
    InboundQuarantineTransportAlias.objects.claim(
        kind=InboundQuarantineTransportAlias.Kind.IDEMPOTENCY,
        identity=idempotency_identity,
        logical_identity=logical_identity,
    )
    InboundQuarantineTransportAlias.objects.claim(
        kind=InboundQuarantineTransportAlias.Kind.PROVIDER,
        identity=provider_identity,
        logical_identity=logical_identity,
    )


def _processing_expires_at():
    return timezone.now() + dt.timedelta(seconds=_PROCESSING_LEASE_SECONDS)


def _refresh_processing_reservations(reservations):
    try:
        with transaction.atomic():
            refreshed = []
            for reservation in reservations:
                refreshed.append(  # noqa: PERF401 - each refresh takes a row lock
                    InboundQuarantineWriteReservation.objects
                    .refresh_processing_locked(
                        reservation_id=reservation.pk,
                        processing_expires_at=_processing_expires_at(),
                    )
                )
            return refreshed
    except InboundQuarantineCleanupInProgress as exc:
        raise InboundSubmissionInProgress() from exc


def _reserve_and_store(
    *,
    logical_identity,
    kind,
    size,
    sha256,
    content,
    transport_idempotency_identity=None,
    transport_provider_identity=None,
):
    try:
        while True:
            reservation = InboundQuarantineWriteReservation.objects.reserve(
                logical_identity=logical_identity,
                kind=kind,
                size=size,
                sha256=sha256,
                transport_idempotency_identity=transport_idempotency_identity,
                transport_provider_identity=transport_provider_identity,
            )
            with transaction.atomic():
                reservation = (
                    InboundQuarantineWriteReservation.objects
                    .select_for_update()
                    .get(pk=reservation.pk)
                )
                if reservation.completed_at is not None:
                    continue
                if (
                    reservation.lease_token is not None
                    or reservation.attempt_count > 0
                ):
                    raise InboundQuarantineCleanupInProgress(
                        "Stale cleanup owns this quarantine reservation."
                    )
                if reservation.adopted_at is not None:
                    return reservation
                if reservation.stored_at is not None:
                    return (
                        InboundQuarantineWriteReservation.objects
                        .refresh_processing_locked(
                            reservation_id=reservation.pk,
                            processing_expires_at=_processing_expires_at(),
                        )
                    )
                reservation = (
                    InboundQuarantineWriteReservation.objects
                    .refresh_processing_locked(
                        reservation_id=reservation.pk,
                        processing_expires_at=_processing_expires_at(),
                    )
                )

            # The processing lease above must commit before storage I/O. If the
            # database connection or its row lock disappears during PutObject,
            # stale cleanup still sees the durable lease and cannot complete
            # this generation underneath a late successful write.
            with transaction.atomic():
                reservation = (
                    InboundQuarantineWriteReservation.objects
                    .select_for_update()
                    .get(pk=reservation.pk)
                )
                if reservation.completed_at is not None:
                    continue
                if (
                    reservation.lease_token is not None
                    or reservation.attempt_count > 0
                ):
                    raise InboundQuarantineCleanupInProgress(
                        "Stale cleanup owns this quarantine reservation."
                    )
                if reservation.adopted_at is not None:
                    return reservation
                if reservation.stored_at is not None:
                    return (
                        InboundQuarantineWriteReservation.objects
                        .refresh_processing_locked(
                            reservation_id=reservation.pk,
                            processing_expires_at=_processing_expires_at(),
                        )
                    )
                try:
                    secure_files.store_quarantined_object(
                        reservation.storage_key,
                        BytesIO(content),
                        content_length=size,
                        sha256=sha256,
                    )
                except Exception as exc:
                    raise InboundStorageUnavailable() from exc
                reservation = (
                    InboundQuarantineWriteReservation.objects
                    .mark_stored_locked(
                        reservation_id=reservation.pk,
                        stored_at=timezone.now(),
                    )
                )
                return (
                    InboundQuarantineWriteReservation.objects
                    .refresh_processing_locked(
                        reservation_id=reservation.pk,
                        processing_expires_at=_processing_expires_at(),
                    )
                )
    except InboundQuarantineCleanupInProgress as exc:
        raise InboundSubmissionInProgress() from exc


def _adopt_attachments_and_match(
    receipt, correspondence, parsed, attachment_reservations, actor
):
    """Adopt stored attachment evidence, then match the parsed receipt."""
    attachments = []
    for parsed_attachment, reservation in zip(
        parsed.attachments,
        attachment_reservations,
        strict=True,
    ):
        attachment = (
            CorrespondenceAttachment.objects
            .create_from_quarantine_reservation(
                reservation=reservation,
                correspondence=correspondence,
                inbound_receipt=receipt,
                original_filename=parsed_attachment.filename,
                content_type=parsed_attachment.content_type,
                size=parsed_attachment.size,
                sha256=parsed_attachment.sha256,
                uploaded_by=actor,
            )
        )
        attachments.append(attachment)
        transaction.on_commit(
            lambda attachment_id=str(attachment.pk): _enqueue_attachment_scan(
                attachment_id
            )
        )

    if parsed.delivery_status_report is not None:
        receipt = match_delivery_status_report(
            receipt,
            parsed.delivery_status_report,
        )
    else:
        receipt = match_inbound_evidence(receipt)
    return receipt, attachments


@transaction.atomic
def _receive_reserved_atomic(
    *,
    raw_reservation,
    attachment_reservations,
    parsed,
    parsing_failure,
    raw_sha256,
    payload_sha256,
    provider,
    provider_account,
    provider_message_id,
    provider_thread_id,
    envelope_sender,
    envelope_recipients,
    received_at,
    idempotency_key,
    actor,
    credential,
    ip_address,
):
    actor = require_exact_inbound_identity(actor, credential=credential)
    existing = _resolve_existing(
        actor=actor,
        idempotency_key=idempotency_key,
        provider=provider,
        provider_account=provider_account,
        provider_message_id=provider_message_id,
        payload_sha256=payload_sha256,
    )
    if existing is not None:
        return existing

    try:
        with transaction.atomic():
            receipt = (
                InboundCorrespondenceReceipt.objects
                .create_from_quarantine_reservation(
                    reservation=raw_reservation,
                    provider=provider,
                    provider_account=provider_account,
                    provider_message_id=provider_message_id,
                    provider_thread_id=provider_thread_id,
                    received_at=received_at,
                    envelope_sender=envelope_sender,
                    envelope_recipients=envelope_recipients,
                    raw_message_size=raw_reservation.size,
                    raw_message_sha256=raw_sha256,
                    payload_sha256=payload_sha256,
                    idempotency_key=idempotency_key,
                    submitted_by=actor,
                    ip_address=ip_address,
                )
            )
    except (IntegrityError, InboundQuarantineReservationAlreadyAdopted):
        existing = _resolve_existing(
            actor=actor,
            idempotency_key=idempotency_key,
            provider=provider,
            provider_account=provider_account,
            provider_message_id=provider_message_id,
            payload_sha256=payload_sha256,
        )
        if existing is None:
            raise
        return existing

    if parsing_failure is not None:
        parsing_status, reason_code = parsing_failure
        receipt = _finalize_quarantined_receipt(
            receipt,
            parsing_status=parsing_status,
            reason_code=reason_code,
        )
        audit(
            "correspondence.inbound_received",
            target=str(receipt.pk),
            actor=actor,
            ip_address=ip_address,
            receipt_id=str(receipt.pk),
            correspondence_id=None,
            provider=provider,
            parsing_status=receipt.parsing_status,
            matching_status=receipt.matching_status,
            match_method="",
            raw_message_size=receipt.raw_message_size,
            attachment_count=0,
        )
        return _result(receipt)

    correspondence = _create_correspondence(receipt, parsed, actor)
    receipt = InboundCorrespondenceReceipt.objects.filter(
        pk=receipt.pk
    ).finalize_locked_parsing(
        parsing_status=InboundCorrespondenceReceipt.ParsingStatus.PARSED,
        completed_at=timezone.now(),
        matched_correspondence_id=correspondence.pk,
        rfc_message_id=parsed.rfc_message_id,
        in_reply_to=" ".join(parsed.in_reply_to),
        references=parsed.references,
    )

    receipt, attachments = _adopt_attachments_and_match(
        receipt, correspondence, parsed, attachment_reservations, actor
    )
    audit(
        "correspondence.inbound_received",
        target=str(receipt.pk),
        actor=actor,
        ip_address=ip_address,
        receipt_id=str(receipt.pk),
        correspondence_id=str(correspondence.pk),
        provider=provider,
        parsing_status=receipt.parsing_status,
        matching_status=receipt.matching_status,
        match_method=receipt.match_method,
        raw_message_size=receipt.raw_message_size,
        attachment_count=len(attachments),
    )
    return _result(receipt)


def receive_inbound_message(
    *,
    raw_message,
    provider,
    provider_account,
    provider_message_id,
    provider_thread_id,
    envelope_sender,
    envelope_recipients,
    received_at,
    idempotency_key,
    actor,
    credential=None,
    ip_address=None,
):
    """Retain raw evidence, parse safely, match in Django, and return opaque state."""
    actor = require_exact_inbound_identity(actor, credential=credential)
    recipients, normalized_ip = _validate_transport_metadata(
        provider=provider,
        provider_account=provider_account,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        envelope_sender=envelope_sender,
        envelope_recipients=envelope_recipients,
        received_at=received_at,
        idempotency_key=idempotency_key,
        ip_address=ip_address,
    )
    raw_bytes = _read_bounded_raw_message(raw_message)
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    payload_sha256 = _payload_digest(
        raw_sha256=raw_sha256,
        provider=provider,
        provider_account=provider_account,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        envelope_sender=envelope_sender,
        envelope_recipients=recipients,
        received_at=received_at,
    )
    raw_logical_identity = _reservation_identity(
        "raw_message",
        provider,
        provider_account,
        provider_message_id,
        payload_sha256,
    )
    idempotency_identity = _reservation_identity(
        "idempotency",
        actor.pk,
        idempotency_key,
    )
    provider_identity = _reservation_identity(
        "provider",
        provider,
        provider_account,
        provider_message_id,
    )
    with transaction.atomic():
        actor = require_exact_inbound_identity(
            actor,
            credential=credential,
        )
        existing = _resolve_existing(
            actor=actor,
            idempotency_key=idempotency_key,
            provider=provider,
            provider_account=provider_account,
            provider_message_id=provider_message_id,
            payload_sha256=payload_sha256,
        )
        if existing is not None:
            try:
                _claim_transport_aliases(
                    logical_identity=raw_logical_identity,
                    idempotency_identity=idempotency_identity,
                    provider_identity=provider_identity,
                )
            except InboundQuarantineReservationConflict as exc:
                if exc.conflict_kind == "idempotency":
                    raise InboundIdempotencyConflict() from exc
                raise InboundProviderMessageConflict() from exc
    if existing is not None:
        from apps.work_queue.integration import (
            schedule_inbound_receipt_reconciliation,
        )

        schedule_inbound_receipt_reconciliation(existing.receipt_id)
        return existing

    try:
        raw_reservation = _reserve_and_store(
            logical_identity=raw_logical_identity,
            kind=InboundQuarantineWriteReservation.Kind.RAW_MESSAGE,
            size=len(raw_bytes),
            sha256=raw_sha256,
            content=raw_bytes,
            transport_idempotency_identity=idempotency_identity,
            transport_provider_identity=provider_identity,
        )
    except InboundQuarantineReservationConflict as exc:
        if exc.conflict_kind == "idempotency":
            raise InboundIdempotencyConflict() from exc
        raise InboundProviderMessageConflict() from exc
    parsed = None
    parsing_failure = None
    try:
        parsed = parse_inbound_message(raw_bytes, limits=_mime_limits())
    except InboundMimeSecurityError as exc:
        parsing_failure = (
            InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
            exc.code,
        )
    except Exception:  # noqa: BLE001 - an unparseable message quarantines
        logger.error("Inbound MIME parsing failed closed")
        parsing_failure = (
            InboundCorrespondenceReceipt.ParsingStatus.FAILED,
            _PARSER_UNAVAILABLE,
        )

    raw_reservation = _refresh_processing_reservations(
        [raw_reservation]
    )[0]
    attachment_reservations = []
    if parsed is not None:
        for index, parsed_attachment in enumerate(parsed.attachments):
            attachment_reservations.append(
                _reserve_and_store(
                    logical_identity=_reservation_identity(
                        "attachment",
                        provider,
                        provider_account,
                        provider_message_id,
                        payload_sha256,
                        index,
                        parsed_attachment.size,
                        parsed_attachment.sha256,
                    ),
                    kind=(
                        InboundQuarantineWriteReservation.Kind.ATTACHMENT
                    ),
                    size=parsed_attachment.size,
                    sha256=parsed_attachment.sha256,
                    content=parsed_attachment.content,
                )
            )
            refreshed = _refresh_processing_reservations(
                [raw_reservation, *attachment_reservations]
            )
            raw_reservation = refreshed[0]
            attachment_reservations = refreshed[1:]

    refreshed = _refresh_processing_reservations(
        [raw_reservation, *attachment_reservations]
    )
    raw_reservation = refreshed[0]
    attachment_reservations = refreshed[1:]
    result = _receive_reserved_atomic(
        raw_reservation=raw_reservation,
        attachment_reservations=attachment_reservations,
        parsed=parsed,
        parsing_failure=parsing_failure,
        raw_sha256=raw_sha256,
        payload_sha256=payload_sha256,
        provider=provider,
        provider_account=provider_account,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        envelope_sender=envelope_sender,
        envelope_recipients=recipients,
        received_at=received_at,
        idempotency_key=idempotency_key,
        actor=actor,
        credential=credential,
        ip_address=normalized_ip,
    )
    from apps.work_queue.integration import (
        schedule_inbound_receipt_reconciliation,
    )

    schedule_inbound_receipt_reconciliation(result.receipt_id)
    return result


@dataclass(frozen=True)
class InboundReparseResult:
    receipt_id: object
    outcome: str
    reason: str = ""


def verified_raw_message(receipt) -> bytes | None:
    """Return a receipt's retained raw bytes, or None if they fail its digest."""
    try:
        body = secure_files.open_private_object(receipt.raw_storage_key)
        raw_bytes = body.read(receipt.raw_message_size + 1)
    except Exception as exc:
        raise InboundStorageUnavailable() from exc
    if (
        len(raw_bytes) != receipt.raw_message_size
        or hashlib.sha256(raw_bytes).hexdigest() != receipt.raw_message_sha256
    ):
        return None
    return raw_bytes


def reparse_quarantined_receipt(receipt_id, *, actor) -> InboundReparseResult:
    """Re-read one whole-message quarantine with the current parser.

    Returns ``released`` when the retained raw message now parses and the
    receipt joins the normal matching path; ``still_quarantined`` (with the
    parser's reason) when it still does not; ``evidence_mismatch`` when the
    stored raw bytes no longer hash to the retained digest; and
    ``not_quarantined`` for any receipt that is not a parsing quarantine.
    """
    actor = require_exact_inbound_identity(actor)
    receipt = InboundCorrespondenceReceipt.objects.get(pk=receipt_id)

    def eligible(candidate) -> bool:
        return (
            candidate.parsing_status
            in {
                InboundCorrespondenceReceipt.ParsingStatus.FAILED,
                InboundCorrespondenceReceipt.ParsingStatus.QUARANTINED,
            }
            and candidate.matching_status
            == InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
            and candidate.matched_correspondence_id is None
        )

    if not eligible(receipt):
        return InboundReparseResult(receipt.pk, "not_quarantined")
    raw_bytes = verified_raw_message(receipt)
    if raw_bytes is None:
        return InboundReparseResult(receipt.pk, "evidence_mismatch")
    try:
        parsed = parse_inbound_message(raw_bytes, limits=_mime_limits())
    except InboundMimeSecurityError as exc:
        return InboundReparseResult(receipt.pk, "still_quarantined", exc.code)
    except Exception:  # noqa: BLE001 - an unparseable message stays quarantined
        logger.error("Inbound MIME re-parse failed closed")
        return InboundReparseResult(
            receipt.pk, "still_quarantined", _PARSER_UNAVAILABLE
        )

    attachment_reservations = [
        _reserve_and_store(
            logical_identity=_reservation_identity(
                "attachment",
                receipt.provider,
                receipt.provider_account,
                receipt.provider_message_id,
                receipt.payload_sha256,
                index,
                parsed_attachment.size,
                parsed_attachment.sha256,
            ),
            kind=InboundQuarantineWriteReservation.Kind.ATTACHMENT,
            size=parsed_attachment.size,
            sha256=parsed_attachment.sha256,
            content=parsed_attachment.content,
        )
        for index, parsed_attachment in enumerate(parsed.attachments)
    ]
    attachment_reservations = _refresh_processing_reservations(
        attachment_reservations
    )

    with transaction.atomic():
        locked = InboundCorrespondenceReceipt.objects.select_for_update().get(
            pk=receipt.pk
        )
        if not eligible(locked):
            return InboundReparseResult(receipt.pk, "not_quarantined")
        previous_reason = locked.quarantine_reason or locked.parsing_error
        correspondence = _create_correspondence(locked, parsed, actor)
        released = cast(
            InboundCorrespondenceReceiptQuerySet,
            InboundCorrespondenceReceipt.objects.filter(pk=locked.pk),
        ).release_reparsed_locked(
            completed_at=timezone.now(),
            matched_correspondence_id=correspondence.pk,
            rfc_message_id=parsed.rfc_message_id,
            in_reply_to=" ".join(parsed.in_reply_to),
            references=parsed.references,
        )
        released, attachments = _adopt_attachments_and_match(
            released, correspondence, parsed, attachment_reservations, actor
        )
        audit(
            "correspondence.inbound_reparsed",
            target=str(released.pk),
            actor=actor,
            receipt_id=str(released.pk),
            correspondence_id=str(correspondence.pk),
            previous_reason=previous_reason,
            matching_status=released.matching_status,
            match_method=released.match_method,
            attachment_count=len(attachments),
        )
    from apps.work_queue.integration import (
        schedule_inbound_receipt_reconciliation,
    )

    schedule_inbound_receipt_reconciliation(released.pk)
    return InboundReparseResult(released.pk, "released")
