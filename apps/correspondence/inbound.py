"""Deterministic inbound correspondence matching and human correction."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from email.utils import parseaddr

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.api.models import StaffRole
from apps.audit.models import audit
from apps.configuration import runtime
from apps.records.models import Channel, PublicRecordsRequest, RequestStatus

from .authorization import require_current_human_staff
from .mime import ParsedDeliveryStatusReport
from .models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryNotification,
    CorrespondenceRecipient,
    InboundCorrespondenceReceipt,
)

_MESSAGE_ID = re.compile(r"<[^<>\s]+>", re.ASCII)
# Every matter kind's prefix (ADR 0004): a FERPA parent quoting their
# FER- number in a reply must match the same way a PRA requester does.
_REQUEST_NUMBER = re.compile(
    r"\b(?:PRR|LIT|FER)-[0-9]{4}-[0-9]{4,8}\b", re.IGNORECASE
)
_STRONG_METHODS = frozenset(
    {
        InboundCorrespondenceReceipt.MatchMethod.REPLY_TOKEN,
        InboundCorrespondenceReceipt.MatchMethod.IN_REPLY_TO,
        InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD,
        InboundCorrespondenceReceipt.MatchMethod.REFERENCES,
    }
)


@dataclass(frozen=True)
class _EvidenceMatch:
    method: str
    request_ids: frozenset
    outbound_ids: tuple

    @property
    def unique_request_id(self):
        if len(self.request_ids) != 1:
            return None
        return next(iter(self.request_ids))

    @property
    def exact_outbound_id(self):
        if len(self.outbound_ids) != 1:
            return None
        return self.outbound_ids[0]


def _require_records_officer(actor):
    return require_current_human_staff(
        actor,
        roles=(StaffRole.RECORDS_OFFICER,),
        message="Records Officer approval is required.",
    )


def _substantive_reason(reason: str) -> str:
    normalized = (reason or "").strip()
    if len(normalized) < 10:
        raise ValidationError(
            "Inbound matching and rematching require a substantive reason."
        )
    return normalized


def _check_version(receipt, expected_version) -> None:
    if receipt.version != expected_version:
        # Imported lazily so correspondence.services may later delegate its
        # receive orchestration to this module without creating an import cycle.
        from .services import VersionConflict

        raise VersionConflict(
            f"Version conflict: expected {expected_version}, "
            f"current {receipt.version}."
        )


def _message_ids(value) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple)) else (value,)
    found = []
    for item in values:
        if not isinstance(item, str):
            continue
        found.extend(_MESSAGE_ID.findall(item))
    return tuple(dict.fromkeys(found))


def _reply_tokens(envelope_recipients) -> tuple[uuid.UUID, ...]:
    expected_local = runtime.get("PRR_CORRESPONDENCE_REPLY_LOCAL_PART").casefold()
    expected_domain = runtime.get("PRR_CORRESPONDENCE_REPLY_DOMAIN").casefold()
    tokens = []
    for raw_recipient in envelope_recipients or ():
        if not isinstance(raw_recipient, str) or any(
            ord(character) < 32 or ord(character) == 127
            for character in raw_recipient
        ):
            continue
        address = parseaddr(raw_recipient)[1] or raw_recipient.strip()
        local, separator, domain = address.rpartition("@")
        if not separator or domain.casefold() != expected_domain:
            continue
        base, plus, token_text = local.partition("+")
        if not plus or base.casefold() != expected_local:
            continue
        try:
            token = uuid.UUID(token_text)
        except (ValueError, AttributeError):
            continue
        if str(token) != token_text.casefold():
            continue
        tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def _evidence_from_queryset(method, queryset) -> _EvidenceMatch:
    rows = tuple(
        queryset.order_by("created_at", "id").values_list("request_id", "id")
    )
    return _EvidenceMatch(
        method=method,
        request_ids=frozenset(request_id for request_id, _ in rows),
        outbound_ids=tuple(outbound_id for _, outbound_id in rows),
    )


def _sent_outbound():
    return Correspondence.objects.filter(
        direction=Correspondence.Direction.OUTBOUND,
        status=Correspondence.Status.SENT,
        request__isnull=False,
    )


def _reply_token_evidence(receipt) -> _EvidenceMatch:
    tokens = _reply_tokens(receipt.envelope_recipients)
    queryset = _sent_outbound().filter(reply_token__in=tokens)
    return _evidence_from_queryset(
        InboundCorrespondenceReceipt.MatchMethod.REPLY_TOKEN,
        queryset,
    )


def _in_reply_to_evidence(receipt) -> _EvidenceMatch:
    message_ids = _message_ids(receipt.in_reply_to)
    queryset = _sent_outbound().filter(rfc_message_id__in=message_ids)
    return _evidence_from_queryset(
        InboundCorrespondenceReceipt.MatchMethod.IN_REPLY_TO,
        queryset,
    )


def _provider_thread_evidence(receipt) -> _EvidenceMatch:
    queryset = _sent_outbound().none()
    if receipt.provider_thread_id:
        queryset = _sent_outbound().filter(
            provider=receipt.provider,
            provider_account=receipt.provider_account,
            provider_thread_id=receipt.provider_thread_id,
        )
    return _evidence_from_queryset(
        InboundCorrespondenceReceipt.MatchMethod.PROVIDER_THREAD,
        queryset,
    )


def _references_evidence(receipt) -> _EvidenceMatch:
    message_ids = _message_ids(receipt.references)
    queryset = _sent_outbound().filter(rfc_message_id__in=message_ids)
    return _evidence_from_queryset(
        InboundCorrespondenceReceipt.MatchMethod.REFERENCES,
        queryset,
    )


def _locked_receipt(receipt):
    if receipt is None or receipt.pk is None:
        raise ValidationError("A persisted inbound receipt is required.")
    return InboundCorrespondenceReceipt.objects.select_for_update().get(
        pk=receipt.pk
    )


def _cited_request_numbers(receipt):
    """Request numbers the requester wrote, and where: subject first, body
    only when the subject names none.

    A follow-up sent from a fresh thread carries no reply token and no
    threading headers, so the number the requester typed is the only evidence
    there is. People put it in the body at least as often as the subject
    ("I submitted a records request (PRR-2026-0048) on Monday"), and until
    this read the body such a message became a brand-new case. Either
    placement yields a *candidate* for a person to confirm, never a match:
    a quoted district footer can name a case the message is not about.
    """
    if receipt.matched_correspondence_id is None:
        return None
    correspondence = receipt.matched_correspondence
    for text, method in (
        (
            correspondence.received_subject,
            InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
        ),
        (
            correspondence.received_body,
            InboundCorrespondenceReceipt.MatchMethod.BODY,
        ),
    ):
        numbers = {value.upper() for value in _REQUEST_NUMBER.findall(text or "")}
        if numbers:
            return numbers, method
    return None


def _merge_target(request):
    """Follow ``merged_into`` so evidence naming a merged duplicate lands on
    the case of record. A reply to a message the duplicate sent, or a subject
    citing the retired number, belongs with the rest of that requester's
    exchange, not on a closed case nobody is watching."""
    seen = set()
    while request.merged_into_id is not None and request.pk not in seen:
        seen.add(request.pk)
        request = PublicRecordsRequest.objects.select_for_update().get(
            pk=request.merged_into_id
        )
    return request


def _is_delivery_status_receipt(receipt) -> bool:
    return (
        receipt.matched_correspondence_id is not None
        and Correspondence.objects.filter(
            pk=receipt.matched_correspondence_id,
            kind=Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        ).exists()
    )


def automatic_processing_blocked(receipt) -> bool:
    """Return whether sender policy requires human-only inbound review."""
    correspondence = receipt.matched_correspondence
    if correspondence is None:
        return False
    address = parseaddr(correspondence.received_sender)[1].strip().casefold()
    _local, separator, domain = address.rpartition("@")
    if not separator:
        return False
    return any(
        domain == blocked or domain.endswith(f".{blocked}")
        for blocked in runtime.get("PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS")
    )


def _set_nonmatch_state(
    receipt,
    *,
    matching_status,
    candidate_request_id=None,
    candidate_method=InboundCorrespondenceReceipt.MatchMethod.SUBJECT,
):
    match_method = (
        candidate_method
        if matching_status
        == InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
        else ""
    )
    InboundCorrespondenceReceipt.objects.filter(
        pk=receipt.pk
    )._set_locked_matching_state(
        matching_status=matching_status,
        match_method=match_method,
        candidate_request_id=candidate_request_id,
    )
    return InboundCorrespondenceReceipt.objects.get(pk=receipt.pk)


def _set_delivery_status_nonmatch(
    receipt,
    report,
    *,
    matching_status,
):
    InboundCorrespondenceReceipt.objects.filter(
        pk=receipt.pk
    )._set_locked_delivery_status_state(
        original_message_id=report.original_message_id,
        matching_status=matching_status,
    )
    return InboundCorrespondenceReceipt.objects.get(pk=receipt.pk)


def _associate_inbound_correspondence(
    *,
    receipt,
    request,
    method,
    matched_by,
    exact_outbound_id=None,
):
    if receipt.matched_correspondence_id is None:
        raise ValidationError(
            "A parsed inbound correspondence snapshot is required before matching."
        )
    correspondence = Correspondence.objects.select_for_update().get(
        pk=receipt.matched_correspondence_id
    )
    if correspondence.direction != Correspondence.Direction.INBOUND:
        raise ValidationError(
            "Inbound receipt content must reference inbound correspondence."
        )
    if correspondence.request_id not in {None, request.pk}:
        raise ValidationError(
            "Already-associated inbound correspondence requires rematching."
        )

    matched_at = timezone.now()
    cycle = None
    if exact_outbound_id is not None:
        cycle = (
            ClarificationCycle.objects.select_for_update()
            .filter(
                request_id=request.pk,
                outbound_correspondence_id=exact_outbound_id,
                opened_at__lte=receipt.received_at,
                resolved_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .first()
        )
        if (
            cycle is not None
            and cycle.inbound_response_correspondence_id
            not in {None, correspondence.pk}
        ):
            cycle = None
        if receipt.received_at > matched_at:
            cycle = None

    kind = (
        Correspondence.Kind.CLARIFICATION_RESPONSE
        if cycle is not None
        else Correspondence.Kind.GENERAL
    )
    Correspondence.objects.filter(
        pk=correspondence.pk
    )._set_locked_inbound_association(
        request_id=request.pk,
        kind=kind,
        matched_at=matched_at,
        matched_by_id=matched_by.pk if matched_by is not None else None,
    )
    if cycle is not None:
        ClarificationCycle.objects.filter(pk=cycle.pk).update(
            inbound_response_correspondence_id=correspondence.pk,
            responded_at=receipt.received_at,
        )
    InboundCorrespondenceReceipt.objects.filter(
        pk=receipt.pk
    )._set_locked_matching_state(
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        match_method=method,
        matched_request_id=request.pk,
        matched_at=matched_at,
        matched_by_id=matched_by.pk if matched_by is not None else None,
    )
    return InboundCorrespondenceReceipt.objects.get(pk=receipt.pk)


@transaction.atomic
def match_delivery_status_report(
    receipt,
    report: ParsedDeliveryStatusReport,
):
    """Attach a recognized DSN only to its stable application Message-ID."""
    if not isinstance(report, ParsedDeliveryStatusReport):
        raise ValidationError("A parsed delivery-status report is required.")
    locked = _locked_receipt(receipt)
    if (
        locked.parsing_status
        != InboundCorrespondenceReceipt.ParsingStatus.PARSED
        or locked.quarantined
        or locked.matching_status
        in {
            InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE,
            InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED,
        }
    ):
        return locked
    if not _is_delivery_status_receipt(locked):
        raise ValidationError(
            "Only recognized delivery-status correspondence may use DSN matching."
        )

    outbound_references = list(
        Correspondence.objects
        .filter(
            direction=Correspondence.Direction.OUTBOUND,
            rfc_message_id=report.original_message_id,
        )
        .order_by("pk")
        .values_list("pk", "request_id")[:2]
    )
    if len(outbound_references) != 1:
        return _set_delivery_status_nonmatch(
            locked,
            report,
            matching_status=(
                InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS
                if len(outbound_references) > 1
                else InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
            ),
        )

    outbound_id, request_id = outbound_references[0]
    if request_id is None:
        return _set_delivery_status_nonmatch(
            locked,
            report,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
    request = PublicRecordsRequest.objects.select_for_update(of=("self",)).get(
        pk=request_id
    )
    outbound = Correspondence.objects.select_for_update(of=("self",)).get(
        pk=outbound_id,
        request_id=request.pk,
        direction=Correspondence.Direction.OUTBOUND,
        rfc_message_id=report.original_message_id,
    )
    try:
        delivery = CorrespondenceDelivery.objects.select_for_update(
            of=("self",)
        ).get(correspondence=outbound)
    except CorrespondenceDelivery.DoesNotExist:
        return _set_delivery_status_nonmatch(
            locked,
            report,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
    outbound_recipients = list(
        CorrespondenceRecipient.objects.select_for_update()
        .filter(correspondence=outbound)
        .order_by("pk")
    )
    matched_at = timezone.now()
    inbound_correspondence = Correspondence.objects.select_for_update(
        of=("self",)
    ).get(pk=locked.matched_correspondence_id)
    Correspondence.objects.filter(
        pk=inbound_correspondence.pk
    )._set_locked_inbound_association(
        request_id=request.pk,
        kind=Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION,
        matched_at=matched_at,
        matched_by_id=None,
    )
    InboundCorrespondenceReceipt.objects.filter(
        pk=locked.pk
    )._set_locked_delivery_status_state(
        original_message_id=report.original_message_id,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        matched_request_id=request.pk,
        matched_delivery_id=delivery.pk,
        matched_at=matched_at,
    )

    normalized_recipients = []
    for reported_recipient in report.recipients:
        matches = [
            recipient
            for recipient in outbound_recipients
            if reported_recipient.matches_recipient_email(recipient.email)
        ]
        if len(matches) == 1:
            matched_recipient = matches[0]
            recipient_match_status = (
                CorrespondenceDeliveryNotification.RecipientMatchStatus.EXACT
            )
        elif len(matches) > 1:
            matched_recipient = None
            recipient_match_status = (
                CorrespondenceDeliveryNotification
                .RecipientMatchStatus.AMBIGUOUS
            )
        else:
            matched_recipient = None
            recipient_match_status = (
                CorrespondenceDeliveryNotification
                .RecipientMatchStatus.UNMATCHED
            )
        normalized_recipients.append(
            CorrespondenceDeliveryNotification(
                inbound_receipt_id=locked.pk,
                delivery_id=delivery.pk,
                recipient_index=reported_recipient.recipient_index,
                recipient=matched_recipient,
                recipient_match_status=recipient_match_status,
                action=reported_recipient.action,
                enhanced_status=reported_recipient.status,
            )
        )
    CorrespondenceDeliveryNotification.objects.bulk_create(
        normalized_recipients
    )
    return InboundCorrespondenceReceipt.objects.get(pk=locked.pk)


@transaction.atomic
def match_inbound_evidence(receipt):
    """Apply the deterministic automatic evidence order to one parsed receipt."""
    locked = _locked_receipt(receipt)
    if (
        locked.parsing_status
        != InboundCorrespondenceReceipt.ParsingStatus.PARSED
        or locked.quarantined
        or locked.matching_status
        in {
            InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
            InboundCorrespondenceReceipt.MatchingStatus.DUPLICATE,
            InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED,
        }
    ):
        return locked
    if _is_delivery_status_receipt(locked):
        return locked
    if automatic_processing_blocked(locked):
        blocked = _set_nonmatch_state(
            locked,
            matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
        )
        audit(
            "correspondence.inbound_automatic_processing_blocked",
            target=str(blocked.pk),
            reason_code="blocked_sender_domain",
        )
        return blocked

    for evidence in (
        _reply_token_evidence(locked),
        _in_reply_to_evidence(locked),
        _provider_thread_evidence(locked),
        _references_evidence(locked),
    ):
        if len(evidence.request_ids) > 1:
            return _set_nonmatch_state(
                locked,
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS
                ),
            )
        request_id = evidence.unique_request_id
        if request_id is not None:
            request = _merge_target(
                PublicRecordsRequest.objects.select_for_update().get(
                    pk=request_id
                )
            )
            return _associate_inbound_correspondence(
                receipt=locked,
                request=request,
                method=evidence.method,
                matched_by=None,
                exact_outbound_id=evidence.exact_outbound_id,
            )

    cited = _cited_request_numbers(locked)
    if cited is not None:
        numbers, method = cited
        if len(numbers) > 1:
            return _set_nonmatch_state(
                locked,
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS
                ),
            )
        request = PublicRecordsRequest.objects.filter(
            number=next(iter(numbers))
        ).only("pk", "merged_into").first()
        if request is not None:
            request = _merge_target(request)
            return _set_nonmatch_state(
                locked,
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE
                ),
                candidate_request_id=request.pk,
                candidate_method=method,
            )
    return _set_nonmatch_state(
        locked,
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
    )


@transaction.atomic
def match_email_intake_receipt(receipt, *, request):
    """Atomically bind one plain unmatched email to its newly created case."""
    request = PublicRecordsRequest.objects.select_for_update().get(pk=request.pk)
    locked = _locked_receipt(receipt)
    if request.channel != Channel.EMAIL:
        raise ValidationError("Email intake must create an email-channel request.")
    if (
        locked.parsing_status
        != InboundCorrespondenceReceipt.ParsingStatus.PARSED
        or locked.quarantined
        or locked.matching_status
        != InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED
        or _is_delivery_status_receipt(locked)
    ):
        raise ValidationError(
            "Only parsed, released, unmatched requester email may create a request."
        )
    matched = _associate_inbound_correspondence(
        receipt=locked,
        request=request,
        method=InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE,
        matched_by=None,
    )
    from apps.work_queue.integration import (
        schedule_inbound_receipt_reconciliation,
    )

    schedule_inbound_receipt_reconciliation(matched.pk)
    return matched


@transaction.atomic
def match_inbound_receipt(
    receipt,
    *,
    request,
    actor,
    reason,
    expected_version,
    ip_address=None,
):
    """Human-confirm a candidate or unmatched receipt for one request."""
    reason = _substantive_reason(reason)
    request = PublicRecordsRequest.objects.select_for_update().get(pk=request.pk)
    locked = _locked_receipt(receipt)
    actor = _require_records_officer(actor)
    _check_version(locked, expected_version)
    if _is_delivery_status_receipt(locked):
        raise ValidationError(
            "Delivery-status evidence cannot use the generic match workflow."
        )
    if locked.matching_status == InboundCorrespondenceReceipt.MatchingStatus.MATCHED:
        raise ValidationError(
            "This inbound receipt is already matched; use the rematch workflow."
        )
    if (
        locked.parsing_status
        != InboundCorrespondenceReceipt.ParsingStatus.PARSED
        or locked.quarantined
    ):
        raise ValidationError("Only a parsed, released receipt may be matched.")
    matched = _associate_inbound_correspondence(
        receipt=locked,
        request=request,
        method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
        matched_by=actor,
    )
    audit(
        "correspondence.inbound_matched",
        target=request.number,
        actor=actor,
        receipt_id=str(locked.pk),
        previous_request=None,
        new_request=request.number,
        reason=reason,
        version=matched.version,
        ip_address=ip_address,
    )
    from apps.work_queue.integration import (
        schedule_inbound_receipt_reconciliation,
    )

    schedule_inbound_receipt_reconciliation(matched.pk)
    return matched


@transaction.atomic
def associate_clarification_response(
    receipt,
    *,
    clarification_cycle,
    actor,
    reason,
    expected_version,
    ip_address=None,
):
    """Explicitly classify one already-matched message as a cycle response."""
    reason = _substantive_reason(reason)
    if clarification_cycle is None or clarification_cycle.pk is None:
        raise ValidationError("A persisted clarification cycle is required.")

    cycle_reference = ClarificationCycle.objects.only("request_id").get(
        pk=clarification_cycle.pk
    )
    # Human inbound corrections share one lock order: request(s), cycle(s),
    # receipt, then correspondence.
    request = PublicRecordsRequest.objects.select_for_update().get(
        pk=cycle_reference.request_id
    )
    cycle = ClarificationCycle.objects.select_for_update().get(
        pk=clarification_cycle.pk
    )
    if cycle.request_id != request.pk:
        raise ValidationError(
            "The clarification cycle request changed during association."
        )
    locked = _locked_receipt(receipt)
    actor = _require_records_officer(actor)
    _check_version(locked, expected_version)
    if _is_delivery_status_receipt(locked):
        raise ValidationError(
            "Delivery-status evidence cannot become a clarification response."
        )

    if (
        locked.matching_status
        != InboundCorrespondenceReceipt.MatchingStatus.MATCHED
        or locked.matched_request_id != request.pk
        or locked.matched_correspondence_id is None
    ):
        raise ValidationError(
            "The inbound receipt must already be matched to the cycle request."
        )
    if request.status != RequestStatus.CLARIFICATION:
        raise ValidationError(
            "The request is not awaiting an open clarification response."
        )
    if cycle.resolved_at is not None or cycle.cancelled_at is not None:
        raise ValidationError("Only an open clarification cycle may receive a response.")
    if cycle.inbound_response_correspondence_id is not None:
        raise ValidationError(
            "This clarification cycle already has an associated response."
        )
    reviewed_at = timezone.now()
    if (
        locked.received_at < cycle.opened_at
        or locked.received_at > reviewed_at
    ):
        raise ValidationError(
            "The inbound message timestamp is outside the open clarification "
            "response window."
        )

    Correspondence.objects.select_for_update(of=("self",)).get(
        pk=locked.matched_correspondence_id
    )
    Correspondence.objects.filter(
        pk=locked.matched_correspondence_id
    )._set_locked_clarification_response(request_id=request.pk)
    ClarificationCycle.objects.filter(pk=cycle.pk).update(
        inbound_response_correspondence_id=locked.matched_correspondence_id,
        responded_at=locked.received_at,
    )
    InboundCorrespondenceReceipt.objects.filter(
        pk=locked.pk
    )._record_locked_clarification_association(
        matched_request_id=request.pk,
        matched_correspondence_id=locked.matched_correspondence_id,
    )
    associated = InboundCorrespondenceReceipt.objects.get(pk=locked.pk)
    audit(
        "correspondence.clarification_response_associated",
        target=request.number,
        actor=actor,
        receipt_id=str(locked.pk),
        correspondence_id=str(locked.matched_correspondence_id),
        clarification_cycle_id=str(cycle.pk),
        reason=reason,
        version=associated.version,
        ip_address=ip_address,
    )
    return associated


@transaction.atomic
def rematch_inbound_receipt(
    receipt,
    *,
    request,
    actor,
    reason,
    expected_version,
    ip_address=None,
):
    """Move only an inbound association after an audited human correction."""
    reason = _substantive_reason(reason)
    if receipt is None or receipt.pk is None:
        raise ValidationError("A persisted inbound receipt is required.")
    # Read association references without a lock so the transaction can
    # acquire every mutable row in the shared correction lock order below;
    # the locked receipt version and references are revalidated afterward.
    receipt_reference = InboundCorrespondenceReceipt.objects.only(
        "version",
        "matched_request_id",
        "matched_correspondence_id",
    ).get(pk=receipt.pk)
    _check_version(receipt_reference, expected_version)
    if (
        receipt_reference.matched_request_id is None
        or receipt_reference.matched_correspondence_id is None
    ):
        raise ValidationError("Only a matched inbound receipt may be rematched.")
    if request.pk == receipt_reference.matched_request_id:
        raise ValidationError("Rematching requires a different request.")

    requests = {
        item.pk: item
        for item in PublicRecordsRequest.objects.select_for_update()
        .filter(pk__in=[receipt_reference.matched_request_id, request.pk])
        .order_by("pk")
    }
    if request.pk not in requests:
        raise ValidationError("The requested rematch target does not exist.")
    if receipt_reference.matched_request_id not in requests:
        raise ValidationError("The currently matched request does not exist.")
    previous_request = requests[receipt_reference.matched_request_id]
    request = requests[request.pk]
    locked_cycles = list(
        ClarificationCycle.objects.select_for_update(of=("self",))
        .filter(
            inbound_response_correspondence_id=(
                receipt_reference.matched_correspondence_id
            )
        )
        .order_by("pk")
    )
    locked = _locked_receipt(receipt)
    actor = _require_records_officer(actor)
    _check_version(locked, expected_version)
    if _is_delivery_status_receipt(locked):
        raise ValidationError(
            "Delivery-status evidence cannot use the generic rematch workflow."
        )
    if (
        locked.matching_status
        != InboundCorrespondenceReceipt.MatchingStatus.MATCHED
        or locked.matched_request_id is None
        or locked.matched_correspondence_id is None
    ):
        raise ValidationError("Only a matched inbound receipt may be rematched.")
    if request.pk == locked.matched_request_id:
        raise ValidationError("Rematching requires a different request.")
    if (
        locked.matched_request_id != receipt_reference.matched_request_id
        or locked.matched_correspondence_id
        != receipt_reference.matched_correspondence_id
    ):
        raise ValidationError(
            "The inbound association changed during rematching."
        )
    correspondence = Correspondence.objects.select_for_update(
        of=("self",)
    ).get(pk=locked.matched_correspondence_id)
    cycle = locked_cycles[0] if locked_cycles else None
    if cycle is not None and (
        cycle.resolved_at is not None or cycle.cancelled_at is not None
    ):
        raise ValidationError(
            "Resolved or cancelled clarification evidence cannot be rematched."
        )
    previous_kind = correspondence.kind
    previous_cycle_id = str(cycle.pk) if cycle is not None else None
    previous_responded_at = (
        cycle.responded_at.isoformat()
        if cycle is not None and cycle.responded_at is not None
        else None
    )
    if cycle is not None:
        ClarificationCycle.objects.filter(pk=cycle.pk).update(
            inbound_response_correspondence_id=None,
            responded_at=None,
        )

    matched_at = timezone.now()
    Correspondence.objects.filter(
        pk=correspondence.pk
    )._set_locked_inbound_association(
        request_id=request.pk,
        kind=Correspondence.Kind.GENERAL,
        matched_at=matched_at,
        matched_by_id=actor.pk,
    )
    InboundCorrespondenceReceipt.objects.filter(
        pk=locked.pk
    )._set_locked_matching_state(
        matching_status=InboundCorrespondenceReceipt.MatchingStatus.MATCHED,
        match_method=InboundCorrespondenceReceipt.MatchMethod.MANUAL,
        matched_request_id=request.pk,
        matched_at=matched_at,
        matched_by_id=actor.pk,
    )
    matched = InboundCorrespondenceReceipt.objects.get(pk=locked.pk)
    audit(
        "correspondence.inbound_rematched",
        target=request.number,
        actor=actor,
        receipt_id=str(locked.pk),
        previous_request=previous_request.number,
        new_request=request.number,
        previous_kind=previous_kind,
        previous_clarification_cycle_id=previous_cycle_id,
        previous_responded_at=previous_responded_at,
        reason=reason,
        version=matched.version,
        ip_address=ip_address,
    )
    from apps.work_queue.integration import (
        schedule_inbound_receipt_reconciliation,
    )

    schedule_inbound_receipt_reconciliation(matched.pk)
    return matched
