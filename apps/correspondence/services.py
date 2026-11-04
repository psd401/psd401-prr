"""Transactional correspondence lifecycle and legal-effect services."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import tempfile
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.core.validators import validate_email
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Exists, Max, OuterRef, ProtectedError, Q
from django.utils import timezone

from apps.api.models import StaffRole
from apps.audit.models import AuditLog, audit
from apps.configuration import runtime
from apps.records import letters as record_letters
from apps.records.attachments import open_clean_attachment
from apps.records.deadlines import add_business_days
from apps.records.fees import CENT, DEPOSIT_PERCENT_CAP, current_fee_schedule
from apps.records.models import (
    AdequateSearchAttestation,
    AdvisoryRun,
    CollectionTask,
    EstimateRevision,
    ExemptionCitation,
    FootagePreservation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    RecordsEstimate,
    RequestStatus,
    StatusEvent,
)
from apps.records.service_identities import require_exact_outbound_identity
from apps.records.services import (
    CLOSED_STATUSES,
    record_acknowledgment,
    resolve_clarification_cycle,
    transition,
)

from . import guardrails
from .authorization import require_current_human_staff
from .automatic_templates import (
    FREE_FORM_TEMPLATE_KINDS,
    MANAGED_TEMPLATE_KINDS,
    MAX_NARROWING_OPTIONS,
    SCOPE_RESPONSE_PARAGRAPHS,
    canonical_template_name,
    fee_paragraph,
    match_count_phrase,
    narrowing_options_block,
    render_managed_reference,
    require_present_template_values,
    validate_template_source,
    validate_template_syntax,
)
from .automatic_templates import (
    render_active as render_active_automatic_template,
)
from .automatic_templates import (
    render_prefill as render_template_prefill,
)
from .denial_basis import (
    denial_basis_evidence,
    require_approved_denial_basis_snapshot,
    require_recipient_visible_denial_citations,
)
from .models import (
    AutomaticMessageTemplate,
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDenialCitation,
    CorrespondenceRecipient,
)

# Re-exported, not used here: `receiving` is an implementation module and
# `services` is the name callers and tests import inbound receiving through
# (`services.receive_inbound_message`, `services.InboundIdempotencyConflict`).
# Removing these as unused imports breaks those callers.
from .receiving import (  # noqa: F401
    InboundIdempotencyConflict,
    InboundProviderMessageConflict,
    InboundStorageUnavailable,
    InboundSubmissionInProgress,
    receive_inbound_message,
)
from .rendering import (
    TEMPLATES,
    RenderedMessage,
    canonical_body,
    render_versioned_template,
    validate_subject,
)

logger = logging.getLogger(__name__)


class VersionConflict(ValidationError):
    """The caller edited a stale representation."""


class DeliveryContractError(ValidationError):
    code = "delivery_contract_error"

    def __init__(self, message, *, code=None):
        if code is not None:
            self.code = code
        super().__init__(message, code=self.code)


class DeliveryClaimConflict(DeliveryContractError):
    code = "delivery_claim_in_progress"

    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(
            "This delivery already has an active claim.",
            code=self.code,
        )


class DeliveryNotClaimable(DeliveryContractError):
    code = "delivery_not_claimable"

    def __init__(self, delivery_status: str):
        self.delivery_status = delivery_status
        super().__init__(
            "This delivery is not eligible for a transport claim.",
            code=self.code,
        )


class OutboundTransportDisabled(DeliveryContractError):
    code = "outbound_transport_disabled"

    def __init__(self):
        super().__init__(
            "Outbound correspondence transport is disabled.",
            code=self.code,
        )


class AutomatedOutboundDisabled(OutboundTransportDisabled):
    code = "automated_outbound_disabled"

    def __init__(self):
        DeliveryContractError.__init__(
            self,
            "Automated requester correspondence is disabled; a human-approved "
            "message is still permitted when outbound transport is enabled.",
            code=self.code,
        )


class UnsafeDeliveryAttachment(DeliveryContractError):
    code = "attachment_not_safe"


class DeliveryAttachmentNotAvailable(DeliveryContractError):
    code = "delivery_attachment_not_available"


class DeliveryAttachmentStorageUnavailable(DeliveryContractError):
    code = "delivery_attachment_storage_unavailable"


class DeliveryResultConflict(DeliveryContractError):
    code = "delivery_result_conflict"


@dataclass(frozen=True)
class DeliveryClaim:
    delivery: CorrespondenceDelivery
    attempt: CorrespondenceDeliveryAttempt
    action: str
    lease_expires_at: dt.datetime
    payload: dict


@dataclass(frozen=True)
class DeliveryResult:
    delivery: CorrespondenceDelivery
    attempt: CorrespondenceDeliveryAttempt
    status: str
    idempotent: bool


@dataclass(frozen=True)
class DeliveryWatchdogResult:
    expired_claims: int
    nudged_deliveries: int
    suppressed_nudges: int
    notification_failures: int


@dataclass(frozen=True)
class AutomatedCorrespondenceQueueResult:
    delivery: CorrespondenceDelivery
    created: bool


@dataclass(frozen=True)
class OpenedDeliveryAttachment:
    attachment: CorrespondenceAttachment
    body: object


@dataclass(frozen=True)
class RequesterCorrectionRefresh:
    """What a requester-identity correction did to unsent outbound messages.

    Every list is part of what the officer must be told: which drafts now
    address the corrected requester, which drafts were left with their
    existing wording and why, which approved-but-unsent messages
    were cancelled because they were addressed to the wrong person, and which
    could not be cancelled because delivery was already in flight.
    """

    refreshed: tuple[Correspondence, ...] = ()
    needs_review: tuple[tuple[Correspondence, str], ...] = ()
    cancelled: tuple[Correspondence, ...] = ()
    uncancelled: tuple[tuple[Correspondence, str], ...] = ()


LEGAL_TEMPLATE_VERSIONS = {
    Correspondence.Kind.ACKNOWLEDGMENT: ("acknowledgment", "2"),
    Correspondence.Kind.CLARIFICATION_REQUEST: ("clarification_request", "1"),
    Correspondence.Kind.ESTIMATE_REVISION: ("estimate_revision", "2"),
    Correspondence.Kind.FEE_ESTIMATE: ("fee_estimate", "1"),
    Correspondence.Kind.INTERPRETATION_ESTIMATE: ("interpretation_estimate", "1"),
    Correspondence.Kind.INSTALLMENT_NOTICE: ("installment_notice", "3"),
    Correspondence.Kind.SUBMISSION_RECEIPT: ("submission_receipt", "1"),
    Correspondence.Kind.NO_RECORDS_CLOSURE: ("no_records_closure", "2"),
    Correspondence.Kind.COMPLETION_CLOSURE: ("completion_closure", "1"),
    Correspondence.Kind.NONPAYMENT_CLOSURE: ("nonpayment_closure", "1"),
    Correspondence.Kind.THIRD_PARTY_NOTICE: ("third_party_notice", "1"),
    Correspondence.Kind.SCOPE_CLARIFICATION: ("scope_clarification", "1"),
}

#: Outbound kinds that are addressed to somebody other than the requester.
#: They are audited on the case like every other message, but the requester
#: must not be a recipient: a third-party notice tells a named person that
#: their records are about to be disclosed, and copying the requester on it
#: would disclose the notified person's involvement before any court could
#: be asked to stop the disclosure.
NON_REQUESTER_CORRESPONDENCE_KINDS = frozenset(
    {Correspondence.Kind.THIRD_PARTY_NOTICE}
)

#: The reason code recorded on a closure the District makes because a fee,
#: deposit, or installment charge went unpaid (RCW 42.56.120(2)).
NONPAYMENT_CLOSURE_REASON_CODE = "fee_unpaid"

#: Business days a notified third party gets to seek a court order before the
#: District proceeds with disclosure. RCW 42.56.540 sets no number; ten
#: business days is the district's standing practice and is what the notice
#: states, so the letter and the case agree.
THIRD_PARTY_NOTICE_RESPONSE_BUSINESS_DAYS = 10
# How a third-party notice names a requester who gave no name.
UNNAMED_REQUESTER = "a member of the public"

#: The scope conversation exists in two forms and only one of them is a
#: statutory clarification request. Keeping the forms named here, rather than
#: as a loose string, is what lets every layer below answer "does this letter
#: stop the clock?" the same way.
SCOPE_CLARIFICATION_FORMS = ("informational", "clarification")

#: The form whose delivery enters the tolled CLARIFICATION status under
#: RCW 42.56.520(3).
TOLLING_SCOPE_CLARIFICATION_FORM = "clarification"

#: Every kind that can open a ClarificationCycle. The single-open-cycle and
#: single-claim rules are about the cycle, not about one letter, so they read
#: this rather than naming the original clarification request.
CLARIFICATION_CYCLE_KINDS = frozenset(
    {
        Correspondence.Kind.CLARIFICATION_REQUEST,
        Correspondence.Kind.SCOPE_CLARIFICATION,
    }
)

AUTOMATED_CORRESPONDENCE_KINDS = frozenset(
    {
        Correspondence.Kind.ACKNOWLEDGMENT,
        Correspondence.Kind.SUBMISSION_RECEIPT,
    }
)

#: The only kind the system may *draft* without an author (#42). Unlike an
#: automated kind it is never approved or queued by the system: a Records
#: Officer approves and sends it, and the database constraint
#: ``correspondence_system_prepared_kind_allowed`` holds the same line.
SYSTEM_PREPARED_DRAFT_KINDS = frozenset(
    {Correspondence.Kind.INTERPRETATION_ESTIMATE}
)

REQUEST_LIFECYCLE_EFFECT_KINDS = frozenset(
    {
        Correspondence.Kind.ACKNOWLEDGMENT,
        Correspondence.Kind.CLARIFICATION_REQUEST,
        Correspondence.Kind.ESTIMATE_REVISION,
        # It communicates a new estimated completion date on delivery.
        Correspondence.Kind.INTERPRETATION_ESTIMATE,
        Correspondence.Kind.DENIAL,
        Correspondence.Kind.NO_RECORDS_CLOSURE,
        Correspondence.Kind.COMPLETION_CLOSURE,
        Correspondence.Kind.NONPAYMENT_CLOSURE,
        # Only the clarification form has a lifecycle effect, but the
        # exclusivity and ordering rules this set drives are about what may
        # be outstanding at once, and an informational scope letter is still
        # a requester-facing message about the same request.
        Correspondence.Kind.SCOPE_CLARIFICATION,
    }
)

UNRESOLVED_DELIVERY_STATUSES = frozenset(
    {
        CorrespondenceDelivery.Status.PENDING,
        CorrespondenceDelivery.Status.CLAIMED,
        CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        CorrespondenceDelivery.Status.RETRYABLE,
        CorrespondenceDelivery.Status.FAILED,
    }
)

OFFICER_ONLY_DRAFT_KINDS = frozenset(
    {
        Correspondence.Kind.FEE_ESTIMATE,
        Correspondence.Kind.INTERPRETATION_ESTIMATE,
        Correspondence.Kind.SCOPE_CLARIFICATION,
        Correspondence.Kind.INSTALLMENT_NOTICE,
        Correspondence.Kind.CLOSURE,
        Correspondence.Kind.DENIAL,
        Correspondence.Kind.NO_RECORDS_CLOSURE,
        Correspondence.Kind.COMPLETION_CLOSURE,
        Correspondence.Kind.NONPAYMENT_CLOSURE,
        Correspondence.Kind.THIRD_PARTY_NOTICE,
    }
)

#: Letters whose adopted text asserts something under RCW 42.56 — a five-day
#: response, a tolling clarification, an estimate, a copying charge, a
#: statutory closure, a .540 third-party notice, or an exemption denial. They
#: belong to a public records matter and to no other kind (ADR 0004). A new
#: letter kind is safe by default: it is PRA-only the moment it is added here,
#: and otherwise available to a FERPA matter but never to a litigation pull.
PRA_STATUTORY_CORRESPONDENCE_KINDS = frozenset(
    {
        Correspondence.Kind.ACKNOWLEDGMENT,
        Correspondence.Kind.CLARIFICATION_REQUEST,
        Correspondence.Kind.SCOPE_CLARIFICATION,
        Correspondence.Kind.ESTIMATE_REVISION,
        Correspondence.Kind.INTERPRETATION_ESTIMATE,
        Correspondence.Kind.FEE_ESTIMATE,
        Correspondence.Kind.NO_RECORDS_CLOSURE,
        Correspondence.Kind.NONPAYMENT_CLOSURE,
        Correspondence.Kind.THIRD_PARTY_NOTICE,
        Correspondence.Kind.DENIAL,
    }
)

RETRYABLE_DELIVERY_FAILURES = {
    "provider_unavailable": "The mail provider was temporarily unavailable.",
    "rate_limited": "The mail provider temporarily rate-limited delivery.",
    "temporary_provider_failure": "The mail provider reported a temporary failure.",
}
PERMANENT_DELIVERY_FAILURES = {
    "attachment_rejected": "The provider rejected an approved attachment.",
    "authentication_failed": "The provider rejected the configured transport identity.",
    "message_rejected": "The mail provider permanently rejected the message.",
    "permanent_provider_failure": "The mail provider reported a permanent failure.",
    "recipient_rejected": "The mail provider rejected a recipient address.",
    "development_recipient_blocked": (
        "Development delivery refused a recipient outside the configured allowlist."
    ),
}
_PROVIDER_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,255}\Z", re.ASCII)
_PROVIDER_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", re.ASCII)
_PROVIDER_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", re.ASCII)


def _require_human_staff(actor):
    return require_current_human_staff(
        actor,
        message="A current human staff identity is required.",
    )


def _validated_estimate_revision_reason(
    *,
    actor,
    reason: str,
    automated: bool = False,
    actor_previously_authorized: bool = False,
) -> str:
    try:
        return record_letters.validate_estimate_revision(
            actor=actor,
            reason=reason,
            automated=automated,
            actor_previously_authorized=actor_previously_authorized,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _require_records_officer(actor):
    return require_current_human_staff(
        actor,
        roles=(StaffRole.RECORDS_OFFICER,),
        message="Records Officer approval is required.",
    )


def _require_outbound_transport_enabled() -> None:
    if not runtime.get("PRR_CORRESPONDENCE_OUTBOUND_ENABLED"):
        raise OutboundTransportDisabled()


def _require_automated_outbound_enabled() -> None:
    if not runtime.get("PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED"):
        raise AutomatedOutboundDisabled()


def _require_denial_delivery_exclusivity(
    correspondence: Correspondence,
    *,
    for_claim: bool = False,
) -> None:
    """Serialize denial transport against every other unresolved delivery."""
    unresolved = CorrespondenceDelivery.objects.filter(
        correspondence__request_id=correspondence.request_id,
        status__in=UNRESOLVED_DELIVERY_STATUSES,
    ).exclude(correspondence_id=correspondence.pk)
    if correspondence.kind == Correspondence.Kind.DENIAL:
        conflict = unresolved.exists()
        message = (
            "Resolve every other unresolved correspondence delivery before "
            "sending a denial."
        )
    else:
        conflict = unresolved.filter(
            correspondence__kind=Correspondence.Kind.DENIAL,
        ).exists()
        message = (
            "Resolve the outstanding denial requester-lifecycle delivery "
            "before sending other correspondence."
        )
    if not conflict:
        return
    if for_claim:
        raise DeliveryNotClaimable(
            CorrespondenceDelivery.Status.PENDING
        )
    raise ValidationError(message)


def _check_version(instance, expected_version: int) -> None:
    if instance.version != expected_version:
        raise VersionConflict(
            f"Version conflict: expected {expected_version}, current {instance.version}."
        )


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _recipient_snapshot_evidence(
    correspondence: Correspondence,
) -> tuple[str, int]:
    recipients = [
        {
            "id": str(recipient_id),
            "recipient_type": recipient_type,
            "email": email,
            "display_name": display_name,
            "requester_id": str(requester_id) if requester_id else None,
        }
        for (
            recipient_id,
            recipient_type,
            email,
            display_name,
            requester_id,
        ) in correspondence.recipients.order_by("id").values_list(
            "id",
            "recipient_type",
            "email",
            "display_name",
            "requester_id",
        )
    ]
    serialized = json.dumps(
        recipients,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _content_hash(serialized), len(recipients)


def _require_approved_recipient_snapshot(
    correspondence: Correspondence,
) -> None:
    approvals = list(
        AuditLog.objects.filter(
            action="correspondence.approved",
            detail__correspondence_id=str(correspondence.id),
        ).values_list("detail", flat=True)[:2]
    )
    actual_sha256, actual_count = _recipient_snapshot_evidence(correspondence)
    if len(approvals) != 1:
        raise ValidationError(
            "Approved recipient snapshot integrity check failed."
        )
    detail = approvals[0]
    expected_sha256 = detail.get("recipient_snapshot_sha256")
    expected_count = detail.get("recipient_count")
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count != actual_count
        or expected_sha256 != actual_sha256
    ):
        raise ValidationError(
            "Approved recipient snapshot integrity check failed."
        )


def _audit_content(action: str, correspondence: Correspondence, actor, **detail):
    return audit(
        action,
        target=correspondence.request.number,
        actor=actor,
        correspondence_id=str(correspondence.id),
        version=correspondence.version,
        subject_sha256=_content_hash(
            correspondence.approved_subject or correspondence.draft_subject
        ),
        body_sha256=_content_hash(
            correspondence.approved_body or correspondence.draft_body
        ),
        **detail,
    )


def _base_context(prr: PublicRecordsRequest) -> dict:
    return {
        "request": prr,
        "status_url": f"{runtime.get('PORTAL_BASE_URL')}/requests/{prr.number}",
        "pra_officer_email": runtime.get("PRA_OFFICER_EMAIL"),
    }


def _default_recipient(prr: PublicRecordsRequest) -> list[dict]:
    return [
        {
            "recipient_type": CorrespondenceRecipient.RecipientType.TO,
            "email": prr.requester.email,
            "display_name": prr.requester.name,
            "requester": prr.requester,
        }
    ]


def _validate_display_name(value: str) -> str:
    name = (value or "").strip()
    if len(name) > 255 or any(
        unicodedata.category(character).startswith("C")
        for character in name
    ):
        raise ValidationError(
            "Recipient display names cannot contain control characters."
        )
    return name


def _replace_recipients(
    correspondence: Correspondence,
    recipients: Iterable[dict],
) -> None:
    correspondence.recipients.all().delete()
    for supplied in recipients:
        email = str(supplied.get("email", "")).strip()
        validate_email(email)
        recipient = CorrespondenceRecipient(
            correspondence=correspondence,
            recipient_type=supplied.get(
                "recipient_type",
                CorrespondenceRecipient.RecipientType.TO,
            ),
            email=email,
            display_name=_validate_display_name(
                str(supplied.get("display_name", ""))
            ),
            requester=supplied.get("requester"),
        )
        recipient.full_clean()
        recipient.save()


def _replace_denial_citations(
    correspondence: Correspondence,
    citation_ids: Iterable[int],
    *,
    actor,
) -> None:
    normalized_ids = set()
    for citation_id in citation_ids:
        if isinstance(citation_id, bool):
            raise ValidationError(
                "A denial requires at least one active exemption citation."
            )
        try:
            normalized_ids.add(int(citation_id))
        except (TypeError, ValueError):
            raise ValidationError(
                "A denial requires at least one active exemption citation."
            ) from None
    citations = list(
        ExemptionCitation.objects.filter(
            pk__in=normalized_ids,
            active=True,
        ).order_by("citation", "pk")
    )
    if not normalized_ids or len(citations) != len(normalized_ids):
        raise ValidationError(
            "A denial requires at least one active exemption citation."
        )
    correspondence.denial_citations.all().delete()
    CorrespondenceDenialCitation.objects.bulk_create(
        [
            CorrespondenceDenialCitation(
                correspondence=correspondence,
                exemption=citation,
                selected_by=actor,
            )
            for citation in citations
        ]
    )


def _require_kind_allows_correspondence(prr: PublicRecordsRequest, kind: str) -> None:
    """Refuse a letter the matter's governing law does not permit (ADR 0004).

    A litigation or discovery pull has no requester: counsel asked for the
    records and the production reaches them through the release path, so the
    matter writes to nobody. A FERPA matter has a requester but is not an RCW
    42.56 request, so it may not send a letter whose adopted text asserts
    something under that chapter.
    """
    from apps.records.models import MatterKind

    matter_kind = prr.matter_kind
    if matter_kind == MatterKind.LITIGATION_DISCOVERY:
        raise ValidationError(
            f"{prr.number} is a litigation or discovery matter, which sends "
            "no requester correspondence; produce records to counsel through "
            "a release instead."
        )
    if (
        matter_kind != MatterKind.PUBLIC_RECORDS
        and kind in PRA_STATUTORY_CORRESPONDENCE_KINDS
    ):
        raise ValidationError(
            f"A {prr.get_matter_kind_display()} matter is not a request under "
            "RCW 42.56 and cannot send correspondence that asserts one."
        )


@transaction.atomic
def _create_draft(
    prr: PublicRecordsRequest,
    *,
    kind: str,
    rendered: RenderedMessage,
    metadata: dict,
    actor,
    recipients: Iterable[dict] | None = None,
    template_identifier: str = "",
    template_version: str = "",
    automated: bool = False,
    system_prepared: bool = False,
    replaces: Correspondence | None = None,
    denial_exemption_citation_ids: Iterable[int] | None = None,
) -> Correspondence:
    _require_kind_allows_correspondence(prr, kind)
    if system_prepared:
        if kind not in SYSTEM_PREPARED_DRAFT_KINDS or actor is not None or automated:
            raise PermissionDenied(
                "Only an interpretation-and-estimate letter may be drafted by "
                "the system, and it is never automated."
            )
    elif automated:
        if kind not in AUTOMATED_CORRESPONDENCE_KINDS or actor is not None:
            raise PermissionDenied(
                "Only an approved automated correspondence kind may be "
                "system-authored."
            )
    elif kind in OFFICER_ONLY_DRAFT_KINDS:
        actor = _require_records_officer(actor)
    else:
        actor = _require_human_staff(actor)
    if replaces is not None:
        replaces = Correspondence.objects.get(pk=replaces.pk)
        if (
            replaces.request_id != prr.pk
            or replaces.direction != Correspondence.Direction.OUTBOUND
            or replaces.status not in Correspondence.SNAPSHOT_STATUSES
        ):
            raise ValidationError(
                "A replacement must correct approved correspondence for this request."
            )

    correspondence = Correspondence.objects.create(
        request=prr,
        direction=Correspondence.Direction.OUTBOUND,
        kind=kind,
        status=Correspondence.Status.DRAFT,
        draft_subject=validate_subject(rendered.subject),
        draft_body=canonical_body(rendered.body),
        draft_metadata=metadata,
        template_identifier=template_identifier,
        template_version=template_version,
        created_by=actor,
        automated=automated,
        system_prepared=system_prepared,
        replaces=replaces,
    )
    selected_recipients = (
        _default_recipient(prr) if recipients is None else list(recipients)
    )
    _replace_recipients(correspondence, selected_recipients)
    if kind == Correspondence.Kind.DENIAL:
        _replace_denial_citations(
            correspondence,
            denial_exemption_citation_ids or (),
            actor=actor,
        )
    elif denial_exemption_citation_ids is not None:
        raise ValidationError(
            "Exemption citations are only valid for denial correspondence."
        )
    _audit_content(
        "correspondence.draft_created",
        correspondence,
        actor,
        kind=kind,
        automated=automated,
        system_prepared=system_prepared,
        template_identifier=template_identifier,
        template_version=template_version,
        replaces_id=str(replaces.id) if replaces else "",
    )
    from apps.work_queue.integration import (
        schedule_source_work_item_reconciliation,
    )

    schedule_source_work_item_reconciliation("drafts", correspondence.pk)
    return correspondence


def _render_template_draft(
    prr: PublicRecordsRequest,
    *,
    kind: str,
    context: dict,
    metadata: dict,
    actor,
    automated: bool = False,
    system_prepared: bool = False,
    recipients: Iterable[dict] | None = None,
) -> Correspondence:
    identifier, version = LEGAL_TEMPLATE_VERSIONS[kind]
    render_context = _base_context(prr) | context
    if kind in MANAGED_TEMPLATE_KINDS:
        rendered, identifier, version = render_active_automatic_template(
            kind,
            render_context,
        )
    else:
        rendered = render_versioned_template(
            identifier,
            version,
            render_context,
        )
    return _create_draft(
        prr,
        kind=kind,
        rendered=rendered,
        metadata=metadata,
        actor=actor,
        recipients=recipients,
        template_identifier=identifier,
        template_version=version,
        automated=automated,
        system_prepared=system_prepared,
    )


def _template_change_reason(value: str) -> str:
    reason = str(value or "").strip()
    if len(reason) < 10:
        raise ValidationError("Template changes require a substantive reason.")
    return reason


@transaction.atomic
def create_automatic_message_template_draft(
    *,
    kind: str,
    subject_template: str,
    body_template: str,
    change_reason: str,
    actor,
    name: str = "",
    guardrail_override_reason: str = "",
) -> AutomaticMessageTemplate:
    actor = _require_records_officer(actor)
    reason = _template_change_reason(change_reason)
    name = canonical_template_name(kind, name)
    subject, body, warnings = validate_template_source(
        kind,
        subject_template,
        body_template,
    )
    overrides, override_reason = guardrails.resolve(
        warnings,
        actor=actor,
        override_reason=guardrail_override_reason,
    )
    current_version = (
        AutomaticMessageTemplate.objects.select_for_update()
        .filter(kind=kind, name=name)
        .aggregate(value=Max("template_version"))["value"]
        or 0
    )
    template = AutomaticMessageTemplate.objects.create(
        kind=kind,
        name=name,
        template_version=current_version + 1,
        subject_template=subject,
        body_template=body,
        change_reason=reason,
        created_by=actor,
        guardrail_overrides=list(overrides),
        guardrail_override_reason=override_reason,
    )
    audit(
        "automatic_template.draft_created",
        target=str(template.pk),
        actor=actor,
        kind=kind,
        name=name,
        template_version=template.template_version,
        reason=reason,
        guardrail_overrides=list(overrides),
        guardrail_override_reason=override_reason,
        subject_sha256=hashlib.sha256(subject.encode()).hexdigest(),
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )
    return template


@transaction.atomic
def edit_automatic_message_template_draft(
    template: AutomaticMessageTemplate,
    *,
    subject_template: str,
    body_template: str,
    change_reason: str,
    actor,
    expected_version: int,
    guardrail_override_reason: str = "",
) -> AutomaticMessageTemplate:
    actor = _require_records_officer(actor)
    template = AutomaticMessageTemplate.objects.select_for_update().get(pk=template.pk)
    if template.revision != expected_version:
        raise ValidationError("The template changed; reload it before saving.")
    if template.state != AutomaticMessageTemplate.State.DRAFT:
        raise ValidationError("Only a draft template version may be edited.")
    reason = _template_change_reason(change_reason)
    subject, body, warnings = validate_template_source(
        template.kind,
        subject_template,
        body_template,
    )
    overrides, override_reason = guardrails.resolve(
        warnings,
        actor=actor,
        override_reason=guardrail_override_reason,
    )
    template.subject_template = subject
    template.body_template = body
    template.change_reason = reason
    template.guardrail_overrides = list(overrides)
    template.guardrail_override_reason = override_reason
    template.revision += 1
    template.save(
        update_fields=[
            "subject_template",
            "body_template",
            "change_reason",
            "guardrail_overrides",
            "guardrail_override_reason",
            "revision",
            "updated_at",
        ]
    )
    audit(
        "automatic_template.draft_edited",
        target=str(template.pk),
        actor=actor,
        kind=template.kind,
        name=template.name,
        template_version=template.template_version,
        reason=reason,
        guardrail_overrides=list(overrides),
        guardrail_override_reason=override_reason,
        subject_sha256=hashlib.sha256(subject.encode()).hexdigest(),
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )
    return template


@transaction.atomic
def activate_automatic_message_template(
    template: AutomaticMessageTemplate,
    *,
    actor,
    expected_version: int,
    confirmed: bool,
    reason: str,
) -> AutomaticMessageTemplate:
    actor = _require_records_officer(actor)
    if confirmed is not True:
        raise ValidationError("Confirm the exact automatic-message template before activation.")
    reason = _template_change_reason(reason)
    template = AutomaticMessageTemplate.objects.select_for_update().get(pk=template.pk)
    if template.revision != expected_version:
        raise ValidationError("The template changed; reload it before activation.")
    if template.state != AutomaticMessageTemplate.State.DRAFT:
        raise ValidationError("Only a draft template version may be activated.")
    # Syntax only. The content guardrails were resolved when this draft was
    # written, by this same role, and the answer is stored on the row. Asking
    # again here would make an officer re-justify their own decision, and
    # would reintroduce the retroactive failure this design removes.
    validate_template_syntax(
        template.kind,
        template.subject_template,
        template.body_template,
    )
    now = timezone.now()
    active = list(
        AutomaticMessageTemplate.objects.select_for_update().filter(
            kind=template.kind,
            name=template.name,
            state=AutomaticMessageTemplate.State.ACTIVE,
        )
    )
    if len(active) > 1:
        raise ValidationError("Automatic-message template activation state is inconsistent.")
    if active:
        previous = active[0]
        previous.state = AutomaticMessageTemplate.State.RETIRED
        previous.revision += 1
        previous.save(update_fields=["state", "revision", "updated_at"])
    template.state = AutomaticMessageTemplate.State.ACTIVE
    template.activated_by = actor
    template.activated_at = now
    template.change_reason = reason
    template.revision += 1
    try:
        with transaction.atomic():
            template.save(
                update_fields=[
                    "state",
                    "activated_by",
                    "activated_at",
                    "change_reason",
                    "revision",
                    "updated_at",
                ]
            )
    except IntegrityError as exc:
        # A kind with no active version yet has no row to lock, so two
        # first-time activations race down to the unique index. Surface the
        # same reload message the revision check gives instead of a 500.
        if "one_active_per_kind" not in str(exc):
            raise
        raise ValidationError(
            "The template changed; reload it before activation."
        ) from exc
    audit(
        "automatic_template.activated",
        target=str(template.pk),
        actor=actor,
        kind=template.kind,
        name=template.name,
        template_version=template.template_version,
        reason=reason,
        replaced_template_id=str(active[0].pk) if active else "",
    )
    return template


def create_acknowledgment_draft(
    prr: PublicRecordsRequest,
    *,
    actor,
    estimated_completion: dt.date | None = None,
    automated: bool = False,
) -> Correspondence:
    estimate = estimated_completion or prr.estimated_completion
    if estimate is None:
        estimate = add_business_days(
            timezone.localdate(),
            runtime.get("DEFAULT_ESTIMATE_BUSINESS_DAYS"),
        )
    if (
        prr.estimated_completion is not None
        and estimate != prr.estimated_completion
    ):
        raise ValidationError(
            "The acknowledgment estimate snapshot is stale; use the current "
            "request estimate or the human estimate-revision workflow."
        )
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.ACKNOWLEDGMENT,
        context={"estimated_completion": estimate},
        metadata={"estimated_completion": estimate.isoformat()},
        actor=actor,
        automated=automated,
    )


def create_submission_receipt_draft(
    prr: PublicRecordsRequest,
) -> Correspondence:
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.SUBMISSION_RECEIPT,
        context={},
        metadata={},
        actor=None,
        automated=True,
    )


@transaction.atomic
def create_officer_submission_receipt_draft(
    prr: PublicRecordsRequest,
    *,
    actor,
) -> Correspondence:
    """Create a staff-authored, non-statutory submission receipt draft.

    Automated requester mail is off by default, so the courtesy receipt needs a
    human path. It carries the identical non-statutory guarantees as the
    automated one: it cannot set ``initial_response_delivered``, cannot
    transition the request, and cannot satisfy or defer the day-5 statutory
    response.

    Only one receipt may exist per request. The request row is locked before
    the check so two officers -- or an officer and the automated intake path,
    which takes the same lock -- cannot both pass it. The matching DB
    constraint backs the invariant if a caller ever skips this service.
    """
    actor = _require_human_staff(actor)
    prr = (
        PublicRecordsRequest.objects.select_for_update(of=("self",))
        .select_related("requester")
        .get(pk=prr.pk)
    )
    if Correspondence.objects.filter(
        request=prr,
        kind=Correspondence.Kind.SUBMISSION_RECEIPT,
    ).exclude(status=Correspondence.Status.CANCELLED).exists():
        raise ValidationError(
            "This request already has a submission receipt."
        )
    try:
        with transaction.atomic():
            return _render_template_draft(
                prr,
                kind=Correspondence.Kind.SUBMISSION_RECEIPT,
                context={},
                metadata={},
                actor=actor,
            )
    except IntegrityError as exc:
        if "one_submission_receipt_per_request" not in str(exc):
            raise
        raise ValidationError(
            "This request already has a submission receipt."
        ) from exc


def render_free_form_prefill(
    prr: PublicRecordsRequest,
    *,
    kind: str,
    name: str = "",
) -> RenderedMessage:
    """Render the district's editable starting point for an authored kind."""
    if kind not in FREE_FORM_TEMPLATE_KINDS:
        raise ValidationError(
            "This correspondence kind is created from its structured "
            "template service, not from a drafting starting point."
        )
    return render_template_prefill(kind, _base_context(prr), name=name)


def create_clarification_draft(
    prr: PublicRecordsRequest,
    *,
    clarification_questions: str,
    clarification_due: dt.date,
    actor,
) -> Correspondence:
    questions = (clarification_questions or "").strip()
    if len(questions) < 10:
        raise ValidationError("Clarification questions must be substantive.")
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.CLARIFICATION_REQUEST,
        context={
            "clarification_questions": questions,
            "clarification_due": clarification_due,
        },
        metadata={
            "clarification_questions": questions,
            "clarification_due": clarification_due.isoformat(),
        },
        actor=actor,
    )


def _validated_scope_clarification(
    *,
    form,
    match_count,
    query_summary,
    narrowing_options,
    response_by,
) -> dict:
    """Normalize one scope conversation into its durable metadata.

    Everything the requester will read is decided here, including which of
    the two forms this is -- because that choice is the difference between a
    letter that tolls the District's response period under RCW 42.56.520(3)
    and one that leaves the clock running.
    """
    if form not in SCOPE_CLARIFICATION_FORMS:
        raise ValidationError(
            "Choose whether this scope letter is informational or a "
            "clarification request."
        )
    if isinstance(match_count, bool) or not isinstance(match_count, int) or match_count < 0:
        raise ValidationError(
            "The match count must be a non-negative whole number."
        )
    summary = str(query_summary or "").strip()
    if not 10 <= len(summary) <= 500:
        raise ValidationError(
            "Say in plain language what was searched, in 10 to 500 characters."
        )
    if not isinstance(narrowing_options, (list, tuple)):
        raise ValidationError("Narrowing options must be a list.")
    if len(narrowing_options) > MAX_NARROWING_OPTIONS:
        raise ValidationError(
            f"Offer at most {MAX_NARROWING_OPTIONS} narrowing options."
        )
    options = []
    for option in narrowing_options:
        if not isinstance(option, dict) or set(option) != {"terms", "rationale"}:
            raise ValidationError(
                "Each narrowing option needs exactly its terms and a rationale."
            )
        terms = [
            str(term).strip()
            for term in option["terms"] or ()
            if str(term).strip()
        ]
        rationale = str(option["rationale"] or "").strip()
        if not terms or len(terms) > 10 or any(len(term) > 200 for term in terms):
            raise ValidationError(
                "A narrowing option needs one to ten terms of at most 200 "
                "characters."
            )
        if not 10 <= len(rationale) <= 500:
            raise ValidationError(
                "Say what each narrowing option would cover, in 10 to 500 "
                "characters."
            )
        options.append({"terms": terms, "rationale": rationale})
    reply_by = _structured_date(response_by, field="response_by")
    return {
        "form": form,
        "match_count": match_count,
        "query_summary": summary,
        "narrowing_options": options,
        "response_by": reply_by.isoformat(),
    }


def _scope_clarification_context(metadata: dict) -> dict:
    """Render every scope-conversation fragment exactly once."""
    return {
        "match_count_phrase": match_count_phrase(metadata["match_count"]),
        "query_summary": metadata["query_summary"],
        "narrowing_options": narrowing_options_block(
            metadata["narrowing_options"]
        ),
        "response_by": _structured_date(
            metadata["response_by"],
            field="response_by",
        ),
        "fee_paragraph": fee_paragraph(),
        "scope_response_paragraph": SCOPE_RESPONSE_PARAGRAPHS[metadata["form"]],
    }


def create_scope_clarification_draft(
    prr: PublicRecordsRequest,
    *,
    form: str,
    match_count: int,
    query_summary: str,
    narrowing_options,
    response_by: dt.date,
    actor,
) -> Correspondence:
    """Draft the "your request matched N items" conversation.

    A human writes this, a human approves it, and a human sends it. No task
    may create it: an automated message that told a requester their request
    was too big would be a de facto denial, and the clarification form of it
    would stop the District's own clock without anyone deciding to.
    """
    metadata = _validated_scope_clarification(
        form=form,
        match_count=match_count,
        query_summary=query_summary,
        narrowing_options=narrowing_options,
        response_by=response_by,
    )
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.SCOPE_CLARIFICATION,
        context=_scope_clarification_context(metadata),
        metadata=metadata,
        actor=actor,
    )


def create_estimate_revision_draft(
    prr: PublicRecordsRequest,
    *,
    new_date: dt.date,
    reason: str,
    actor,
) -> Correspondence:
    reason = _validated_estimate_revision_reason(
        actor=actor,
        reason=reason,
    )
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.ESTIMATE_REVISION,
        context={
            "previous_date": prr.estimated_completion,
            "new_date": new_date,
            "reason": reason,
        },
        metadata={
            "previous_date": (
                prr.estimated_completion.isoformat()
                if prr.estimated_completion
                else None
            ),
            "new_date": new_date.isoformat(),
            "reason": reason,
        },
        actor=actor,
    )


def create_installment_notice_draft(
    prr: PublicRecordsRequest,
    *,
    installment,
    is_final: bool,
    actor,
) -> Correspondence:
    """Draft one installment notice from the installment itself.

    The download link is derived here, never supplied. A hand-typed URL in
    the composer was one transcription error away from sending a requester to
    somewhere that was not this installment -- or to somewhere that was not
    the District at all.
    """
    if installment.request_id != prr.pk:
        raise ValidationError("The installment must belong to this request.")
    metadata = _validated_installment_release_snapshot(
        installment,
        is_final=is_final,
    )
    download_url = metadata["download_url"]
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.INSTALLMENT_NOTICE,
        context={
            "installment": installment,
            "download_url": download_url,
            "item_count": metadata["item_count"],
            "has_exemptions": metadata["has_exemptions"],
            "is_final": is_final,
            "download_expires_at": installment.download_expires_at,
        },
        metadata=metadata,
        actor=actor,
    )


def _normalized_search_steps(search_steps) -> list[dict]:
    if not isinstance(search_steps, list) or not 1 <= len(search_steps) <= 100:
        raise ValidationError("At least one and at most 100 detailed search steps are required.")
    required = {"location", "custodian", "method", "date_range", "result"}
    normalized = []
    for index, supplied in enumerate(search_steps, start=1):
        if not isinstance(supplied, dict) or set(supplied) != required:
            raise ValidationError(
                f"Every search step must contain exactly {', '.join(sorted(required))}."
            )
        step = {}
        for field in sorted(required):
            value = str(supplied[field] or "").strip()
            if len(value) < 2 or len(value) > 1000:
                raise ValidationError(
                    f"Search step {index} field {field} must contain 2-1000 characters."
                )
            step[field] = value
        normalized.append(step)
    return normalized


def _task_snapshot(prr) -> tuple[dict, str]:
    tasks = list(
        CollectionTask.objects.select_for_update()
        .filter(request=prr)
        .order_by("pk")
    )
    if any(
        task.state != CollectionTask.State.COMPLETE or task.completed_at is None
        for task in tasks
    ):
        raise ValidationError(
            "All collection tasks must be complete before a no-records closure."
        )
    task_rows = [
        {
            "id": task.pk,
            "kind": task.kind,
            "state": task.state,
            "completed_at": task.completed_at.isoformat(),
            "system_slug": task.system.slug if task.system else None,
        }
        for task in tasks
    ]
    items = list(
        RecordItem.objects.select_for_update()
        .filter(request=prr)
        .order_by("created_at", "pk")
    )
    if any(
        item.review_state in {
            RecordItem.ReviewState.RESPONSIVE,
            RecordItem.ReviewState.WITHHELD,
        }
        for item in items
    ):
        raise ValidationError(
            "A no-records closure is unavailable while responsive records exist."
        )
    if any(
        item.review_state
        not in {
            RecordItem.ReviewState.NON_RESPONSIVE,
            RecordItem.ReviewState.DUPLICATE,
            # A Records Officer took these out of review with a recorded
            # reason when the collection was re-scoped (CollectionSupersession).
            RecordItem.ReviewState.SUPERSEDED,
        }
        or item.reviewed_by_id is None
        or item.reviewed_at is None
        for item in items
    ):
        raise ValidationError(
            "All located candidate records require completed human review before a no-records closure."
        )
    item_rows = [
        {
            "id": str(item.pk),
            "sha256": item.sha256,
            "review_state": item.review_state,
            "reviewed_by_id": item.reviewed_by_id,
            "reviewed_at": item.reviewed_at.isoformat(),
        }
        for item in items
    ]
    snapshot = {
        "collection_tasks": task_rows,
        "record_items": item_rows,
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return snapshot, digest


def _validate_outside_retention_checks(
    prr,
    *,
    retention_schedule_reference,
    incident_hold_checked,
    prior_exports_checked,
    email_drive_copies_checked,
    other_copies_checked,
):
    try:
        preservation = prr.footage_preservation
    except FootagePreservation.DoesNotExist as exc:
        raise ValidationError(
            "An out-of-retention determination requires an expired preservation review."
        ) from exc
    if preservation.state != FootagePreservation.State.EXPIRED:
        raise ValidationError(
            "An out-of-retention determination requires an expired preservation review."
        )
    if len(str(retention_schedule_reference or "").strip()) < 10:
        raise ValidationError(
            "An out-of-retention determination requires a verified retention schedule reference."
        )
    if not all(
        (
            incident_hold_checked,
            prior_exports_checked,
            email_drive_copies_checked,
            other_copies_checked,
        )
    ):
        raise ValidationError(
            "All out-of-retention obvious-lead checks must be completed."
        )


@transaction.atomic
def create_no_records_closure_draft(
    prr: PublicRecordsRequest,
    *,
    determination,
    search_completed_at,
    search_steps,
    adequacy_reason,
    retention_schedule_reference,
    incident_hold_checked,
    prior_exports_checked,
    email_drive_copies_checked,
    other_copies_checked,
    counsel_approval_reference,
    actor,
) -> Correspondence:
    """Create immutable adequate-search evidence and its requester draft."""
    actor = _require_records_officer(actor)
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.status in CLOSED_STATUSES:
        raise ValidationError("A closed request cannot receive a no-records determination.")
    if prr.status == RequestStatus.CLARIFICATION:
        raise ValidationError("Resolve or close the clarification before this determination.")
    if determination not in AdequateSearchAttestation.Determination.values:
        raise ValidationError("A valid no-records determination is required.")
    if timezone.is_naive(search_completed_at):
        raise ValidationError("Search completion requires an offset-aware timestamp.")
    if not prr.received_at <= search_completed_at <= timezone.now():
        raise ValidationError("Search completion must follow receipt and cannot be in the future.")
    normalized_steps = _normalized_search_steps(search_steps)
    adequacy_reason = str(adequacy_reason or "").strip()
    if len(adequacy_reason) < 20 or len(adequacy_reason) > 2000:
        raise ValidationError("A 20-2000 character adequate-search reason is required.")
    counsel_approval_reference = str(counsel_approval_reference or "").strip()
    if len(counsel_approval_reference) < 10:
        raise ValidationError("A substantive district-counsel approval reference is required.")
    retention_schedule_reference = str(retention_schedule_reference or "").strip()
    if determination == AdequateSearchAttestation.Determination.OUTSIDE_RETENTION:
        _validate_outside_retention_checks(
            prr,
            retention_schedule_reference=retention_schedule_reference,
            incident_hold_checked=incident_hold_checked,
            prior_exports_checked=prior_exports_checked,
            email_drive_copies_checked=email_drive_copies_checked,
            other_copies_checked=other_copies_checked,
        )
    task_snapshot, task_snapshot_sha256 = _task_snapshot(prr)
    metadata = {
        "determination": determination,
        "search_completed_at": search_completed_at.isoformat(),
        "search_steps": normalized_steps,
        "adequacy_reason": adequacy_reason,
        "retention_schedule_reference": retention_schedule_reference,
    }
    correspondence = _render_template_draft(
        prr,
        kind=Correspondence.Kind.NO_RECORDS_CLOSURE,
        context=metadata | {"search_completed_at": search_completed_at},
        metadata=metadata,
        actor=actor,
    )
    attestation = AdequateSearchAttestation.objects.create(
        request=prr,
        correspondence=correspondence,
        determination=determination,
        search_completed_at=search_completed_at,
        search_steps=normalized_steps,
        task_snapshot=task_snapshot,
        task_snapshot_sha256=task_snapshot_sha256,
        adequacy_reason=adequacy_reason,
        retention_schedule_reference=retention_schedule_reference,
        incident_hold_checked=bool(incident_hold_checked),
        prior_exports_checked=bool(prior_exports_checked),
        email_drive_copies_checked=bool(email_drive_copies_checked),
        other_copies_checked=bool(other_copies_checked),
        counsel_approval_reference=counsel_approval_reference,
        attested_by=actor,
    )
    audit(
        "adequate_search.attested",
        target=prr.number,
        actor=actor,
        attestation_id=str(attestation.pk),
        correspondence_id=str(correspondence.pk),
        determination=determination,
        task_snapshot_sha256=task_snapshot_sha256,
        search_step_count=len(normalized_steps),
        counsel_approval_reference=counsel_approval_reference,
    )
    return correspondence


def require_adequate_search_snapshot(
    correspondence: Correspondence,
    *,
    lock: bool = False,
) -> AdequateSearchAttestation:
    """Revalidate immutable search evidence against current completion state."""
    queryset = AdequateSearchAttestation.objects.select_related(
        "request",
        "correspondence",
        "attested_by",
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    try:
        attestation = queryset.get(
            correspondence_id=correspondence.pk,
            request_id=correspondence.request_id,
        )
    except AdequateSearchAttestation.DoesNotExist as exc:
        raise ValidationError(
            "No-records closure requires retained adequate-search evidence."
        ) from exc
    if correspondence.kind != Correspondence.Kind.NO_RECORDS_CLOSURE:
        raise ValidationError("Adequate-search evidence is linked to the wrong message kind.")
    if lock:
        PublicRecordsRequest.objects.select_for_update(of=("self",)).get(
            pk=attestation.request_id
        )
    expected_metadata = {
        "determination": attestation.determination,
        "search_completed_at": attestation.search_completed_at.isoformat(),
        "search_steps": attestation.search_steps,
        "adequacy_reason": attestation.adequacy_reason,
        "retention_schedule_reference": attestation.retention_schedule_reference,
    }
    metadata = (
        correspondence.draft_metadata
        if correspondence.status == Correspondence.Status.DRAFT
        else correspondence.approved_metadata
    )
    if metadata != expected_metadata:
        raise ValidationError("The adequate-search correspondence snapshot is inconsistent.")
    current_snapshot, current_digest = _task_snapshot(attestation.request)
    if (
        current_digest != attestation.task_snapshot_sha256
        or current_snapshot != attestation.task_snapshot
    ):
        raise ValidationError(
            "The adequate-search search snapshot changed; create reviewed replacement evidence."
        )
    if (
        attestation.determination
        == AdequateSearchAttestation.Determination.OUTSIDE_RETENTION
    ):
        _validate_outside_retention_checks(
            attestation.request,
            retention_schedule_reference=attestation.retention_schedule_reference,
            incident_hold_checked=attestation.incident_hold_checked,
            prior_exports_checked=attestation.prior_exports_checked,
            email_drive_copies_checked=attestation.email_drive_copies_checked,
            other_copies_checked=attestation.other_copies_checked,
        )
    return attestation


def _installment_has_exemptions(installment: Installment) -> bool:
    return installment.items.filter(
        Q(review_state=RecordItem.ReviewState.WITHHELD)
        | Q(redactions__isnull=False, redactions__voided_at__isnull=True)
        | Q(withholding_exemptions__isnull=False)
    ).exists()


def _validated_installment_release_snapshot(
    installment,
    *,
    is_final,
):
    if not isinstance(is_final, bool):
        raise ValidationError("Installment template flags must be booleans.")
    from apps.records.production import (
        installment_download_url,
        validate_prepared_installment_snapshot,
    )

    # Derived, never supplied: the only URL this notice can ever carry is the
    # one the current scoped token resolves to.
    download_url = _validated_installment_download_url(
        installment_download_url(installment)
    )
    if installment.revoked_at is not None:
        raise ValidationError(
            "A revoked installment download cannot be announced to the requester."
        )
    if not (
        installment.released_at
        and installment.download_expires_at
        and installment.download_expires_at > timezone.now()
        and installment.package_prepared_at
        and installment.package_storage_key
        and installment.package_sha256
        and installment.package_size is not None
        and installment.package_manifest_sha256
    ):
        raise ValidationError(
            "A verified, released, unexpired installment package is required."
        )
    if installment.fee_amount and installment.fee_paid_at is None:
        raise ValidationError(
            "Any approved installment fee must be paid before requester notice."
        )
    validate_prepared_installment_snapshot(installment)
    has_exemptions = _installment_has_exemptions(installment)
    if has_exemptions and not (
        installment.exemption_log_storage_key
        and installment.exemption_log_sha256
        and installment.exemption_log_size is not None
        and installment.exemption_log_manifest_sha256
        and installment.exemption_log_prepared_at
    ):
        raise ValidationError(
            "A verified exemption log is required before requester notice."
        )
    return {
        "installment_id": installment.pk,
        "download_url": download_url,
        "download_expires_at": installment.download_expires_at.isoformat(),
        "package_sha256": installment.package_sha256,
        "item_count": installment.items.count(),
        "has_exemptions": has_exemptions,
        "is_final": is_final,
    }


def _validated_production_summary(value: str) -> str:
    summary = str(value or "").strip()
    if len(summary) < 20 or len(summary) > 2000:
        raise ValidationError(
            "A completion closure requires a 20-2000 character summary of "
            "what the District produced."
        )
    return summary


def _validated_fee_amount(value, *, what="non-payment closure") -> str:
    """Normalize a money figure for a letter without inventing precision."""
    try:
        amount = Decimal(str(value).strip().lstrip("$").replace(",", ""))
    except (InvalidOperation, AttributeError, TypeError) as exc:
        raise ValidationError(
            f"A {what} requires the amount the District requested."
        ) from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal(1000000):
        raise ValidationError(
            f"A {what} requires a positive amount the District requested."
        )
    return f"${amount.quantize(CENT, rounding=ROUND_HALF_UP)}"


def _validated_estimate_basis(value: str) -> str:
    basis = str(value or "").strip()
    if len(basis) < 20 or len(basis) > 2000:
        raise ValidationError(
            "A fee estimate requires a 20-2000 character explanation of how "
            "the charge was calculated from the adopted schedule."
        )
    return basis


def _validated_fee_estimate(*, estimated_amount, deposit_amount, estimate_basis):
    """Check the estimate inputs once, for drafting and for re-rendering."""
    estimated = _validated_fee_amount(estimated_amount, what="fee estimate")
    basis = _validated_estimate_basis(estimate_basis)
    deposit = ""
    if deposit_amount not in (None, "", 0):
        deposit = _validated_fee_amount(deposit_amount, what="fee estimate")
        # Round the ten-percent ceiling DOWN, as the fee domain does: rounding
        # a cap up to the nearest cent admits a deposit above ten percent.
        cap = (
            Decimal(estimated.lstrip("$")) * DEPOSIT_PERCENT_CAP / Decimal(100)
        ).quantize(CENT, rounding=ROUND_DOWN)
        if Decimal(deposit.lstrip("$")) > cap:
            raise ValidationError(
                "RCW 42.56.120(2) caps a deposit at ten percent of the "
                "estimated charge."
            )
    return {
        "estimated_amount": estimated,
        "deposit_amount": deposit,
        "estimate_basis": basis,
    }


@transaction.atomic
def create_fee_estimate_draft(
    prr: PublicRecordsRequest,
    *,
    estimated_amount,
    estimate_basis: str,
    deposit_amount=None,
    actor,
) -> Correspondence:
    """Draft the RCW 42.56.120 copying estimate, with an optional deposit.

    The District may not communicate a fee it has not adopted, so this refuses
    to draft without a currently effective adopted schedule. It sends nothing,
    transitions nothing, and applies no deposit gate on its own: holding work
    is a separate, separately audited officer action.
    """
    actor = _require_records_officer(actor)
    prr = _open_case_for_closure_draft(prr, what="a fee estimate")
    schedule = current_fee_schedule()
    if schedule is None:
        raise ValidationError(
            "An adopted fee schedule is required before the District may "
            "quote a copying charge."
        )
    validated = _validated_fee_estimate(
        estimated_amount=estimated_amount,
        deposit_amount=deposit_amount,
        estimate_basis=estimate_basis,
    )
    metadata = dict(validated, schedule_id=str(schedule.pk))
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.FEE_ESTIMATE,
        context=validated,
        metadata=metadata,
        actor=actor,
    )


#: Bounds on the officer-editable interpretation paragraph.
INTERPRETATION_MIN_LENGTH = 10
INTERPRETATION_MAX_LENGTH = 2000
_INTERPRETATION_KEYS = {
    "estimate_id",
    "intent_run_id",
    "interpretation",
    "record_estimate",
    "charge_paragraph",
    "completion_date",
    "previous_date",
}


def _validated_interpretation(value) -> str:
    text = str(value or "").strip()
    if not INTERPRETATION_MIN_LENGTH <= len(text) <= INTERPRETATION_MAX_LENGTH:
        raise ValidationError(
            "The interpretation must be between "
            f"{INTERPRETATION_MIN_LENGTH} and {INTERPRETATION_MAX_LENGTH} characters."
        )
    return text


def _record_estimate_sentence(responsive: dict) -> str:
    expected, low, high = responsive["expected"], responsive["low"], responsive["high"]
    noun = "record" if expected == 1 else "records"
    sentence = f"The District expects about {expected} responsive {noun}"
    if low != high:
        sentence += f" (between {low} and {high})"
    sentence += "."
    reviewed = responsive["reviewed_responsive"]
    if reviewed:
        verb = "has" if reviewed == 1 else "have"
        sentence += f" {reviewed} {verb} already been reviewed and found responsive."
    return sentence


def _charge_paragraph(fee: dict | None) -> str:
    if fee is None:
        # Before a schedule is adopted the District may quote no cost at all
        # (see automatic_templates.fee_paragraph); it promises nothing either.
        return (
            "The District will tell you before any copying charge applies to "
            "these records."
        )
    expected, low, high = fee["expected"], fee["low"], fee["high"]
    if Decimal(expected["total"]) == 0:
        return "There is no copying charge for these records."
    charge = f"${expected['total']}"
    if low["total"] != high["total"]:
        charge += f" (between ${low['total']} and ${high['total']})"
    return (
        f"The estimated copying charge is about {charge}, calculated under "
        "RCW 42.56.120 from the District's adopted fee schedule. It covers "
        "copying only; the District does not charge for the time its staff "
        "spend locating or reviewing records. Please tell us whether you are "
        "willing to pay this charge."
    )


def _interpretation_context(metadata: dict) -> dict:
    _require_metadata_keys(metadata, _INTERPRETATION_KEYS)
    for key in ("record_estimate", "charge_paragraph"):
        if not str(metadata[key] or "").strip():
            raise ValidationError(f"Structured template field {key} is required.")
    return {
        "interpretation": _validated_interpretation(metadata["interpretation"]),
        "record_estimate": metadata["record_estimate"],
        "charge_paragraph": metadata["charge_paragraph"],
        "completion_date": _structured_date(
            metadata["completion_date"], field="completion_date",
        ),
    }


@transaction.atomic
def create_interpretation_estimate_draft(
    prr: PublicRecordsRequest,
    *,
    estimate: RecordsEstimate,
    actor=None,
) -> Correspondence:
    """Draft the interpretation-and-estimate letter from a stored estimate.

    With no ``actor`` the draft is *system-prepared*: the only requester
    letter the system may draft without an author, and never one it may
    approve or queue. A Records Officer approves and sends it
    (``approve_and_send``); until then nothing reaches the requester (rule 6).
    The interpretation is the latest complete request-intent summary, and the
    count, charge, and date come from ``estimate``. On delivery the date is
    recorded as an ``EstimateRevision`` naming the estimate.
    """
    system_prepared = actor is None
    if not system_prepared:
        actor = _require_records_officer(actor)
    prr = _open_case_for_closure_draft(prr, what="an interpretation and estimate letter")
    if estimate.request.pk != prr.pk:
        raise ValidationError("The estimate belongs to a different request.")
    result = estimate.result
    if not result.get("completion_date"):
        raise ValidationError(
            "The estimate has no completion date, so the letter cannot state one."
        )
    intent = (
        AdvisoryRun.objects.filter(
            request=prr,
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            state=AdvisoryRun.State.COMPLETE,
        )
        .order_by("-created_at")
        .first()
    )
    if intent is None or not str(intent.proposal.get("summary") or "").strip():
        raise ValidationError(
            "A completed request-intent summary is required to state the "
            "District's interpretation."
        )
    if Correspondence.objects.filter(
        request=prr,
        kind=Correspondence.Kind.INTERPRETATION_ESTIMATE,
        status__in=[
            Correspondence.Status.DRAFT,
            Correspondence.Status.APPROVED,
            Correspondence.Status.QUEUED,
        ],
    ).exists():
        raise ValidationError(
            "An interpretation and estimate letter is already outstanding."
        )
    metadata = {
        "estimate_id": str(estimate.pk),
        "intent_run_id": str(intent.pk),
        "interpretation": _validated_interpretation(intent.proposal["summary"]),
        "record_estimate": _record_estimate_sentence(result["responsive"]),
        "charge_paragraph": _charge_paragraph(result.get("fee")),
        "completion_date": result["completion_date"],
        "previous_date": (
            prr.estimated_completion.isoformat() if prr.estimated_completion else None
        ),
    }
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.INTERPRETATION_ESTIMATE,
        context=_interpretation_context(metadata),
        metadata=metadata,
        actor=actor,
        system_prepared=system_prepared,
    )


def _open_case_for_closure_draft(
    prr: PublicRecordsRequest,
    *,
    what: str,
) -> PublicRecordsRequest:
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.status in CLOSED_STATUSES:
        raise ValidationError(f"A closed request cannot receive {what}.")
    if prr.status == RequestStatus.CLARIFICATION:
        raise ValidationError(
            f"Resolve or close the open clarification before {what}."
        )
    return prr


@transaction.atomic
def create_completion_closure_draft(
    prr: PublicRecordsRequest,
    *,
    production_summary: str,
    actor,
) -> Correspondence:
    """Draft the letter that closes a case as fulfilled once it is delivered.

    Closure is still a delivered, human-approved act: this only writes the
    draft. ``apply_successful_delivery_effects`` moves the request to
    FULFILLED when the provider confirms the message actually went out.
    """
    actor = _require_records_officer(actor)
    prr = _open_case_for_closure_draft(prr, what="a completion closure")
    summary = _validated_production_summary(production_summary)
    metadata = {"production_summary": summary}
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.COMPLETION_CLOSURE,
        context=metadata,
        metadata=metadata,
        actor=actor,
    )


@transaction.atomic
def create_nonpayment_closure_draft(
    prr: PublicRecordsRequest,
    *,
    amount_due,
    payment_requested_on: dt.date,
    actor,
) -> Correspondence:
    """Draft the RCW 42.56.120(2) closure for a fee that went unpaid.

    The amount and the date the District asked for it are what make this
    closure defensible, so both are structured inputs rather than prose an
    officer might leave out.
    """
    actor = _require_records_officer(actor)
    prr = _open_case_for_closure_draft(prr, what="a non-payment closure")
    amount = _validated_fee_amount(amount_due)
    requested_on = _structured_date(
        payment_requested_on,
        field="payment_requested_on",
    )
    today = timezone.localdate()
    if requested_on > today:
        raise ValidationError(
            "The District cannot close for non-payment of a fee it has not "
            "yet requested."
        )
    if requested_on < timezone.localtime(prr.received_at).date():
        raise ValidationError(
            "The payment request cannot predate the public records request."
        )
    metadata = {
        "reason_code": NONPAYMENT_CLOSURE_REASON_CODE,
        "amount_due": amount,
        "payment_requested_on": requested_on.isoformat(),
    }
    return _render_template_draft(
        prr,
        kind=Correspondence.Kind.NONPAYMENT_CLOSURE,
        context={
            "amount_due": amount,
            "payment_requested_on": requested_on,
        },
        metadata=metadata,
        actor=actor,
    )


@transaction.atomic
def create_third_party_notice_draft(
    prr: PublicRecordsRequest,
    *,
    employee_name: str,
    employee_email: str,
    attorney_name: str = "",
    attorney_email: str = "",
    actor,
) -> Correspondence:
    """Notify a named person that records concerning them will be disclosed.

    RCW 42.56.540 lets the District give a person whose records are about to
    be released the chance to seek a court order first. The notice goes to
    that person -- and, where they have counsel, to their attorney -- and
    never to the requester. It is audited on the case because the case is
    where a court would later look for it, not because it is requester
    correspondence.
    """
    actor = _require_records_officer(actor)
    prr = PublicRecordsRequest.objects.select_for_update().get(pk=prr.pk)
    if prr.status in CLOSED_STATUSES:
        raise ValidationError(
            "A closed request cannot issue a third-party notice."
        )
    name = _validate_display_name(employee_name)
    if len(name) < 2:
        raise ValidationError(
            "A third-party notice must name the person it notifies."
        )
    employee_email = str(employee_email or "").strip()
    validate_email(employee_email)
    attorney_email = str(attorney_email or "").strip()
    attorney_name = _validate_display_name(attorney_name)
    recipients = [
        {
            "recipient_type": CorrespondenceRecipient.RecipientType.TO,
            "email": employee_email,
            "display_name": name,
        }
    ]
    if attorney_email:
        validate_email(attorney_email)
        if attorney_email.casefold() == employee_email.casefold():
            raise ValidationError(
                "The attorney address must differ from the notified "
                "person's address."
            )
        recipients.append(
            {
                "recipient_type": CorrespondenceRecipient.RecipientType.CC,
                "email": attorney_email,
                "display_name": attorney_name,
            }
        )
    elif attorney_name:
        raise ValidationError(
            "An attorney named on a third-party notice needs an address to "
            "send it to."
        )
    # A name is disclosed; an address is contact information and is not.
    disclosed_requesters = (
        (prr.requester.name or "").strip() or UNNAMED_REQUESTER
    )
    response_by = add_business_days(
        timezone.localdate(),
        THIRD_PARTY_NOTICE_RESPONSE_BUSINESS_DAYS,
    )
    metadata = {
        "employee_name": name,
        "employee_email": employee_email,
        "attorney_name": attorney_name,
        "attorney_email": attorney_email,
        "disclosed_requesters": disclosed_requesters,
        "response_by": response_by.isoformat(),
    }
    correspondence = _render_template_draft(
        prr,
        kind=Correspondence.Kind.THIRD_PARTY_NOTICE,
        context={
            "employee_name": name,
            "disclosed_requesters": disclosed_requesters,
            "response_by": response_by,
        },
        metadata=metadata,
        actor=actor,
        recipients=recipients,
    )
    audit(
        "correspondence.third_party_notice_drafted",
        target=prr.number,
        actor=actor,
        correspondence_id=str(correspondence.pk),
        notified_person=name,
        attorney_notified=bool(attorney_email),
        response_by=response_by.isoformat(),
        disclosed_requesters=disclosed_requesters,
    )
    return correspondence


#: Kinds an officer may approve and send to many cases in one action. Each
#: one either says the same thing to every requester (a general note) or
#: records a decision the officer has already made case by case. A denial is
#: absent on purpose: it asserts an exemption over specific records, and
#: nothing about reviewing fifty of them at once is the same act as reviewing
#: one. So is the no-records closure, which carries per-case search evidence.
BATCH_SENDABLE_KINDS = frozenset(
    {
        Correspondence.Kind.COMPLETION_CLOSURE,
        Correspondence.Kind.NONPAYMENT_CLOSURE,
        Correspondence.Kind.GENERAL,
    }
)

#: Kinds an officer may draft for many cases from one form. A non-payment
#: closure is batch-sendable but not batch-draftable: the amount owed differs
#: per case, and one amount typed once and applied to fifty letters is a
#: wrong number sent fifty times.
BATCH_DRAFTABLE_KINDS = frozenset(
    {
        Correspondence.Kind.COMPLETION_CLOSURE,
        Correspondence.Kind.GENERAL,
    }
)

#: How many cases one batch may touch. Not a technical limit -- it is the
#: number a person can still meaningfully confirm in a dialog.
BATCH_MAX_CASES = 50


@dataclass(frozen=True)
class BatchOutcome:
    """What a batch did, case by case.

    A batch that reports only a count hides the case it skipped, and the case
    it skipped is the one the officer needed to know about.
    """

    batch_id: str
    succeeded: tuple[tuple[str, Correspondence], ...] = ()
    failed: tuple[tuple[str, str], ...] = ()


BATCH_PERMISSION_FAILURE = "You are not permitted to act on this case."
BATCH_DATABASE_FAILURE = (
    "This case could not be saved just now; nothing was recorded for it. "
    "Try it again on its own."
)
# What one case can raise without saying anything about the others. Anything
# else is a defect and still stops the batch loudly.
BATCH_CASE_FAILURES = (ValidationError, PermissionDenied, DatabaseError)


def _batch_failure_detail(exc: Exception, *, case: str) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(exc.messages)
    if isinstance(exc, PermissionDenied):
        return str(exc) or BATCH_PERMISSION_FAILURE
    logger.error(
        "Batch correspondence case failed", extra={"case": case}, exc_info=exc
    )
    return BATCH_DATABASE_FAILURE


def _batch_selection(values, *, noun: str) -> list:
    selected = list(values or ())
    if not selected:
        raise ValidationError(f"Select at least one {noun}.")
    if len(selected) != len(set(map(str, selected))):
        raise ValidationError(f"The same {noun} appears twice in this batch.")
    if len(selected) > BATCH_MAX_CASES:
        raise ValidationError(
            f"A batch covers at most {BATCH_MAX_CASES} cases; "
            f"{len(selected)} were selected."
        )
    return selected


def batch_create_closure_drafts(
    request_numbers,
    *,
    kind: str,
    actor,
    confirmed: bool,
    production_summary: str = "",
    subject: str = "",
    body: str = "",
) -> BatchOutcome:
    """Draft the same notice for several cases from one form.

    Each case gets its own draft, rendered against its own request, and no
    draft is approved or sent here. Drafting is the safe half of the batch:
    an officer can read every one of them before anything leaves the
    District, which is exactly the review a bulk send would otherwise skip.
    """
    actor = _require_records_officer(actor)
    if confirmed is not True:
        raise ValidationError(
            "Confirm the selected cases before drafting for all of them."
        )
    if kind not in BATCH_DRAFTABLE_KINDS:
        raise ValidationError(
            "Only completion closures and general correspondence can be "
            "drafted for several cases from one form."
        )
    numbers = _batch_selection(request_numbers, noun="case")
    batch_id = str(uuid.uuid4())
    succeeded: list[tuple[str, Correspondence]] = []
    failed: list[tuple[str, str]] = []
    for number in numbers:
        try:
            with transaction.atomic():
                prr = PublicRecordsRequest.objects.get(number=number)
                if kind == Correspondence.Kind.COMPLETION_CLOSURE:
                    correspondence = create_completion_closure_draft(
                        prr,
                        production_summary=production_summary,
                        actor=actor,
                    )
                else:
                    correspondence = create_authored_draft(
                        prr,
                        kind=Correspondence.Kind.GENERAL,
                        subject=subject,
                        body=body,
                        actor=actor,
                    )
                audit(
                    "correspondence.batch_drafted",
                    target=prr.number,
                    actor=actor,
                    batch_id=batch_id,
                    kind=kind,
                    correspondence_id=str(correspondence.pk),
                )
        except PublicRecordsRequest.DoesNotExist:
            failed.append((str(number), "No case with this number."))
        except BATCH_CASE_FAILURES as exc:
            failed.append(
                (str(number), _batch_failure_detail(exc, case=str(number)))
            )
        else:
            succeeded.append((prr.number, correspondence))
    return BatchOutcome(
        batch_id=batch_id,
        succeeded=tuple(succeeded),
        failed=tuple(failed),
    )


def batch_approve_and_send(
    correspondence_ids,
    *,
    actor,
    confirmed: bool,
) -> BatchOutcome:
    """Approve and release several drafts, one independent delivery each.

    Every case keeps its own delivery row, its own idempotency key, and its
    own audit trail; the batch adds a shared ``batch_id`` so the fifty rows
    can be read back as one officer action. One case failing does not undo
    the others -- they are separate legal acts that happen to have been
    authorized together, and rolling back a message that has already reached
    a requester is not something this system can do anyway.
    """
    actor = _require_records_officer(actor)
    if confirmed is not True:
        raise ValidationError(
            "Confirm the exact messages before sending them to requesters."
        )
    _require_outbound_transport_enabled()
    selected = _batch_selection(correspondence_ids, noun="message")
    drafts = list(
        Correspondence.objects.filter(pk__in=selected).select_related(
            "request"
        )
    )
    if len(drafts) != len(selected):
        raise ValidationError(
            "Every message in a batch must exist and be visible to you."
        )
    refused = sorted(
        {
            draft.get_kind_display()
            for draft in drafts
            if draft.kind not in BATCH_SENDABLE_KINDS
        }
    )
    if refused:
        raise ValidationError(
            f"{', '.join(refused)} is decided one case at a time and cannot "
            "be sent as part of a batch."
        )
    cases = [draft.request_id for draft in drafts]
    if len(cases) != len(set(cases)):
        raise ValidationError(
            "A batch sends at most one message per case."
        )
    batch_id = str(uuid.uuid4())
    succeeded: list[tuple[str, Correspondence]] = []
    failed: list[tuple[str, str]] = []
    for draft in sorted(drafts, key=lambda row: row.request.number):
        try:
            with transaction.atomic():
                delivery = approve_and_send(
                    draft,
                    actor=actor,
                    expected_version=draft.version,
                )
                audit(
                    "correspondence.batch_approve_and_send",
                    target=draft.request.number,
                    actor=actor,
                    batch_id=batch_id,
                    kind=draft.kind,
                    correspondence_id=str(draft.pk),
                    delivery_id=str(delivery.id),
                    batch_size=len(drafts),
                )
        except BATCH_CASE_FAILURES as exc:
            number = draft.request.number
            failed.append((number, _batch_failure_detail(exc, case=number)))
        else:
            succeeded.append((draft.request.number, draft))
    return BatchOutcome(
        batch_id=batch_id,
        succeeded=tuple(succeeded),
        failed=tuple(failed),
    )


def create_authored_draft(
    prr: PublicRecordsRequest,
    *,
    kind: str,
    subject: str,
    body: str,
    actor,
    recipients: Iterable[dict] | None = None,
    replaces: Correspondence | None = None,
    denial_exemption_citation_ids: Iterable[int] | None = None,
) -> Correspondence:
    if kind not in Correspondence.Kind.values:
        raise ValidationError("Unknown correspondence kind.")
    if kind == Correspondence.Kind.CLARIFICATION_RESPONSE:
        raise ValidationError("Clarification responses are inbound correspondence.")
    if kind == Correspondence.Kind.DELIVERY_STATUS_NOTIFICATION:
        raise ValidationError(
            "Delivery-status notifications are inbound correspondence."
        )
    if kind in LEGAL_TEMPLATE_VERSIONS:
        raise ValidationError(
            "This correspondence kind requires its structured versioned-template service."
        )
    return _create_draft(
        prr,
        kind=kind,
        rendered=RenderedMessage(
            subject=validate_subject(subject),
            body=canonical_body(body),
        ),
        metadata={},
        actor=actor,
        recipients=recipients,
        replaces=replaces,
        denial_exemption_citation_ids=denial_exemption_citation_ids,
    )


def preview(correspondence: Correspondence) -> RenderedMessage:
    if correspondence.direction != Correspondence.Direction.OUTBOUND:
        raise ValidationError("Only outbound correspondence has a delivery preview.")
    if correspondence.status == Correspondence.Status.DRAFT:
        return RenderedMessage(
            subject=validate_subject(correspondence.draft_subject),
            body=canonical_body(correspondence.draft_body),
        )
    return RenderedMessage(
        subject=validate_subject(correspondence.approved_subject),
        body=canonical_body(correspondence.approved_body),
    )


def _structured_date(
    value,
    *,
    field: str,
    allow_none: bool = False,
) -> dt.date | None:
    if value in (None, ""):
        if allow_none:
            return None
        raise ValidationError(f"Structured template field {field} is required.")
    if isinstance(value, dt.datetime):
        raise ValidationError(
            f"Structured template field {field} must be a calendar date."
        )
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Structured template field {field} must be an ISO calendar date."
        ) from exc


def _require_metadata_keys(metadata: dict, expected: set[str]) -> None:
    if not isinstance(metadata, dict) or set(metadata) != expected:
        raise ValidationError(
            "Structured template metadata does not match the versioned contract."
        )


def _validated_installment_download_url(value: str) -> str:
    download_url = str(value or "")
    configured_origin = urlsplit(runtime.get("PORTAL_BASE_URL"))
    supplied_url = urlsplit(download_url)
    if (
        supplied_url.scheme != configured_origin.scheme
        or supplied_url.netloc != configured_origin.netloc
        or not supplied_url.path.startswith("/")
    ):
        raise ValidationError(
            "Installment downloads must use the configured application origin."
        )
    return download_url


def _render_structured_snapshot(
    correspondence: Correspondence,
    metadata: dict,
) -> RenderedMessage:
    try:
        expected_identifier, expected_version = LEGAL_TEMPLATE_VERSIONS[
            correspondence.kind
        ]
    except KeyError as exc:
        raise ValidationError(
            "This correspondence is not a structured template draft."
        ) from exc
    managed_template = (
        correspondence.kind in MANAGED_TEMPLATE_KINDS
        and correspondence.template_version.startswith("managed:")
    )
    # Content approved under a template version that has since been superseded
    # was frozen by an officer against that version and still renders from it;
    # a version bump must not strand it undeliverable. Drafts always use the
    # current version.
    approved_under_prior_version = (
        correspondence.status != Correspondence.Status.DRAFT
        and (expected_identifier, correspondence.template_version) in TEMPLATES
    )
    if correspondence.template_identifier != expected_identifier or (
        not managed_template
        and not approved_under_prior_version
        and correspondence.template_version != expected_version
    ):
        raise ValidationError(
            "Structured template identity does not match the correspondence kind."
        )

    if correspondence.kind == Correspondence.Kind.ACKNOWLEDGMENT:
        _require_metadata_keys(metadata, {"estimated_completion"})
        context = {
            "estimated_completion": _structured_date(
                metadata["estimated_completion"],
                field="estimated_completion",
            )
        }
    elif correspondence.kind == Correspondence.Kind.CLARIFICATION_REQUEST:
        _require_metadata_keys(
            metadata,
            {"clarification_questions", "clarification_due"},
        )
        questions = str(metadata["clarification_questions"] or "").strip()
        if len(questions) < 10:
            raise ValidationError(
                "Clarification questions must be substantive."
            )
        context = {
            "clarification_questions": questions,
            "clarification_due": _structured_date(
                metadata["clarification_due"],
                field="clarification_due",
            ),
        }
    elif correspondence.kind == Correspondence.Kind.ESTIMATE_REVISION:
        _require_metadata_keys(
            metadata,
            {"previous_date", "new_date", "reason"},
        )
        reason = _validated_estimate_revision_reason(
            actor=(
                correspondence.created_by
                if correspondence.status == Correspondence.Status.DRAFT
                else correspondence.approved_by
            ),
            reason=metadata["reason"],
            automated=correspondence.automated,
            actor_previously_authorized=True,
        )
        context = {
            "previous_date": _structured_date(
                metadata["previous_date"],
                field="previous_date",
                allow_none=True,
            ),
            "new_date": _structured_date(
                metadata["new_date"],
                field="new_date",
            ),
            "reason": reason,
        }
    elif correspondence.kind == Correspondence.Kind.NO_RECORDS_CLOSURE:
        _require_metadata_keys(
            metadata,
            {
                "determination",
                "search_completed_at",
                "search_steps",
                "adequacy_reason",
                "retention_schedule_reference",
            },
        )
        if metadata["determination"] not in AdequateSearchAttestation.Determination.values:
            raise ValidationError("A valid no-records determination is required.")
        try:
            search_completed_at = dt.datetime.fromisoformat(
                metadata["search_completed_at"]
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("Search completion timestamp is invalid.") from exc
        if timezone.is_naive(search_completed_at):
            raise ValidationError("Search completion timestamp requires an offset.")
        context = {
            "determination": metadata["determination"],
            "search_completed_at": search_completed_at,
            "search_steps": _normalized_search_steps(metadata["search_steps"]),
            "adequacy_reason": str(metadata["adequacy_reason"] or "").strip(),
            "retention_schedule_reference": str(
                metadata["retention_schedule_reference"] or ""
            ).strip(),
        }
    elif correspondence.kind == Correspondence.Kind.SCOPE_CLARIFICATION:
        _require_metadata_keys(
            metadata,
            {
                "form",
                "match_count",
                "query_summary",
                "narrowing_options",
                "response_by",
            },
        )
        context = _scope_clarification_context(
            _validated_scope_clarification(
                form=metadata["form"],
                match_count=metadata["match_count"],
                query_summary=metadata["query_summary"],
                narrowing_options=metadata["narrowing_options"],
                response_by=metadata["response_by"],
            )
        )
    elif correspondence.kind == Correspondence.Kind.SUBMISSION_RECEIPT:
        _require_metadata_keys(metadata, set())
        context = {}
    elif correspondence.kind == Correspondence.Kind.FEE_ESTIMATE:
        _require_metadata_keys(
            metadata,
            {
                "estimated_amount",
                "deposit_amount",
                "estimate_basis",
                "schedule_id",
            },
        )
        context = _validated_fee_estimate(
            estimated_amount=metadata["estimated_amount"],
            deposit_amount=metadata["deposit_amount"],
            estimate_basis=metadata["estimate_basis"],
        )
    elif correspondence.kind == Correspondence.Kind.INTERPRETATION_ESTIMATE:
        context = _interpretation_context(metadata)
    elif correspondence.kind == Correspondence.Kind.COMPLETION_CLOSURE:
        _require_metadata_keys(metadata, {"production_summary"})
        context = {
            "production_summary": _validated_production_summary(
                metadata["production_summary"]
            )
        }
    elif correspondence.kind == Correspondence.Kind.NONPAYMENT_CLOSURE:
        _require_metadata_keys(
            metadata,
            {"reason_code", "amount_due", "payment_requested_on"},
        )
        if metadata["reason_code"] != NONPAYMENT_CLOSURE_REASON_CODE:
            raise ValidationError(
                "A non-payment closure carries the fee_unpaid reason code."
            )
        context = {
            "amount_due": _validated_fee_amount(metadata["amount_due"]),
            "payment_requested_on": _structured_date(
                metadata["payment_requested_on"],
                field="payment_requested_on",
            ),
        }
    elif correspondence.kind == Correspondence.Kind.THIRD_PARTY_NOTICE:
        _require_metadata_keys(
            metadata,
            {
                "employee_name",
                "employee_email",
                "attorney_name",
                "attorney_email",
                "disclosed_requesters",
                "response_by",
            },
        )
        context = {
            "employee_name": _validate_display_name(
                metadata["employee_name"]
            ),
            "disclosed_requesters": str(
                metadata["disclosed_requesters"] or ""
            ).strip(),
            "response_by": _structured_date(
                metadata["response_by"],
                field="response_by",
            ),
        }
    else:
        _require_metadata_keys(
            metadata,
            {
                "installment_id",
                "download_url",
                "download_expires_at",
                "package_sha256",
                "item_count",
                "has_exemptions",
                "is_final",
            },
        )
        installment_id = metadata["installment_id"]
        if isinstance(installment_id, bool) or not isinstance(
            installment_id,
            int,
        ):
            raise ValidationError(
                "Structured template field installment_id must be an integer."
            )
        try:
            installment = Installment.objects.get(
                pk=installment_id,
                request_id=correspondence.request_id,
            )
        except Installment.DoesNotExist as exc:
            raise ValidationError(
                "The installment must belong to this request."
            ) from exc
        item_count = metadata["item_count"]
        if (
            isinstance(item_count, bool)
            or not isinstance(item_count, int)
            or item_count < 0
        ):
            raise ValidationError(
                "Installment item count must be a non-negative integer."
            )
        if not isinstance(metadata["has_exemptions"], bool) or not isinstance(
            metadata["is_final"],
            bool,
        ):
            raise ValidationError(
                "Installment template flags must be booleans."
            )
        current_has_exemptions = _installment_has_exemptions(installment)
        if (
            item_count != installment.items.count()
            or metadata["has_exemptions"] != current_has_exemptions
        ):
            raise ValidationError(
                "The installment snapshot is stale; edit or recreate the "
                "notice from the current installment."
            )
        context = {
            "installment": installment,
            "download_url": _validated_installment_download_url(
                metadata["download_url"]
            ),
            "item_count": item_count,
            "has_exemptions": metadata["has_exemptions"],
            "is_final": metadata["is_final"],
            "download_expires_at": installment.download_expires_at,
        }

    render_context = _base_context(correspondence.request) | context
    if managed_template:
        return render_managed_reference(
            correspondence.kind,
            correspondence.template_version,
            render_context,
        )
    # The managed path checks mandatory content inside render_source; the
    # built-in path must fail closed on the same missing content rather than
    # re-rendering an approved snapshot with a blank download link or blank
    # search evidence.
    require_present_template_values(correspondence.kind, render_context)
    return render_versioned_template(
        expected_identifier,
        correspondence.template_version,
        render_context,
    )


def _structured_metadata_for_edit(
    correspondence: Correspondence,
    structured_data: dict,
    *,
    actor,
) -> dict:
    if not isinstance(structured_data, dict):
        raise ValidationError("Structured template inputs are required.")
    if correspondence.kind == Correspondence.Kind.ACKNOWLEDGMENT:
        _require_metadata_keys(structured_data, {"estimated_completion"})
        estimate = _structured_date(
            structured_data["estimated_completion"],
            field="estimated_completion",
        )
        return {"estimated_completion": estimate.isoformat()}
    if correspondence.kind == Correspondence.Kind.CLARIFICATION_REQUEST:
        _require_metadata_keys(
            structured_data,
            {"clarification_questions", "clarification_due"},
        )
        questions = str(
            structured_data["clarification_questions"] or ""
        ).strip()
        if len(questions) < 10:
            raise ValidationError(
                "Clarification questions must be substantive."
            )
        due = _structured_date(
            structured_data["clarification_due"],
            field="clarification_due",
        )
        return {
            "clarification_questions": questions,
            "clarification_due": due.isoformat(),
        }
    if correspondence.kind == Correspondence.Kind.INTERPRETATION_ESTIMATE:
        # Only the interpretation is the officer's to reword; the count, the
        # charge, and the date are the stored estimate's.
        _require_metadata_keys(structured_data, {"interpretation"})
        return {
            **correspondence.draft_metadata,
            "interpretation": _validated_interpretation(
                structured_data["interpretation"]
            ),
        }
    if correspondence.kind == Correspondence.Kind.ESTIMATE_REVISION:
        _require_metadata_keys(structured_data, {"new_date", "reason"})
        new_date = _structured_date(
            structured_data["new_date"],
            field="new_date",
        )
        reason = _validated_estimate_revision_reason(
            actor=actor,
            reason=structured_data["reason"],
        )
        return {
            "previous_date": (
                correspondence.request.estimated_completion.isoformat()
                if correspondence.request.estimated_completion
                else None
            ),
            "new_date": new_date.isoformat(),
            "reason": reason,
        }
    if correspondence.kind == Correspondence.Kind.SCOPE_CLARIFICATION:
        _require_metadata_keys(
            structured_data,
            {
                "form",
                "match_count",
                "query_summary",
                "narrowing_options",
                "response_by",
            },
        )
        return _validated_scope_clarification(
            form=structured_data["form"],
            match_count=structured_data["match_count"],
            query_summary=structured_data["query_summary"],
            narrowing_options=structured_data["narrowing_options"],
            response_by=structured_data["response_by"],
        )
    if correspondence.kind == Correspondence.Kind.INSTALLMENT_NOTICE:
        _require_metadata_keys(structured_data, {"is_final"})
        if not isinstance(structured_data["is_final"], bool):
            raise ValidationError(
                "Installment template flags must be booleans."
            )
        installment_id = correspondence.draft_metadata.get("installment_id")
        if isinstance(installment_id, bool) or not isinstance(
            installment_id,
            int,
        ):
            raise ValidationError(
                "Structured template field installment_id must be an integer."
            )
        try:
            installment = Installment.objects.get(
                pk=installment_id,
                request_id=correspondence.request_id,
            )
        except Installment.DoesNotExist as exc:
            raise ValidationError(
                "The installment must belong to this request."
            ) from exc
        return _validated_installment_release_snapshot(
            installment,
            is_final=structured_data["is_final"],
        )
    raise ValidationError(
        "This correspondence is not a structured template draft."
    )


def _require_requester_to_recipient(
    correspondence: Correspondence,
    recipients: list[CorrespondenceRecipient],
) -> None:
    requester = correspondence.request.requester
    if correspondence.kind in NON_REQUESTER_CORRESPONDENCE_KINDS:
        # The inverse guarantee. This message is about the requester's
        # request but is not addressed to them, and the mistake worth
        # catching is the opposite one: a stray To or Cc that tells the
        # requester who was notified.
        if any(
            recipient.requester_id == requester.pk
            or recipient.email.casefold() == requester.email.casefold()
            for recipient in recipients
        ):
            raise ValidationError(
                "A third-party notice is addressed to the notified person, "
                "never to the requester."
            )
        return
    if (
        correspondence.kind not in LEGAL_TEMPLATE_VERSIONS
        and correspondence.kind != Correspondence.Kind.DENIAL
    ):
        return
    if not any(
        recipient.recipient_type
        == CorrespondenceRecipient.RecipientType.TO
        and recipient.requester_id == requester.pk
        and recipient.email.casefold() == requester.email.casefold()
        for recipient in recipients
    ):
        raise ValidationError(
            "The current requester must be a To recipient for a "
            "requester-facing notice."
        )


@transaction.atomic
def edit_draft(
    correspondence: Correspondence,
    *,
    subject: str,
    body: str,
    actor,
    expected_version: int,
    recipients: Iterable[dict] | None = None,
    denial_exemption_citation_ids: Iterable[int] | None = None,
) -> Correspondence:
    correspondence = Correspondence.objects.select_for_update(of=("self",)).get(
        pk=correspondence.pk
    )
    if correspondence.kind in OFFICER_ONLY_DRAFT_KINDS:
        actor = _require_records_officer(actor)
    else:
        actor = _require_human_staff(actor)
    _check_version(correspondence, expected_version)
    if correspondence.status != Correspondence.Status.DRAFT:
        raise ValidationError("Only a draft may be edited.")
    if correspondence.kind in LEGAL_TEMPLATE_VERSIONS:
        raise ValidationError(
            "Structured template drafts must be edited through their "
            "validated template inputs."
        )
    correspondence.draft_subject = validate_subject(subject)
    correspondence.draft_body = canonical_body(body)
    correspondence.version += 1
    correspondence.save(
        update_fields=["draft_subject", "draft_body", "version"]
    )
    if recipients is not None:
        _replace_recipients(correspondence, recipients)
    if correspondence.kind == Correspondence.Kind.DENIAL:
        if denial_exemption_citation_ids is not None:
            _replace_denial_citations(
                correspondence,
                denial_exemption_citation_ids,
                actor=actor,
            )
    elif denial_exemption_citation_ids is not None:
        raise ValidationError(
            "Exemption citations are only valid for denial correspondence."
        )
    _audit_content("correspondence.draft_edited", correspondence, actor)
    return correspondence


@transaction.atomic
def delete_draft(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
) -> None:
    """Delete a never-approved draft; approved correspondence stays forever.

    A draft is not yet a legal record (the model refuses deleting anything
    else). Structured template drafts are deletable even though they cannot
    be edited in place: discarding sends nothing, so there are no template
    inputs to validate. The draft's own recipients, denial citations, and
    search chunks cascade with it; a draft other evidence points at — an
    attachment, a search attestation, a clarification cycle — is refused
    rather than unwound.
    """
    correspondence = (
        Correspondence.objects.select_for_update(of=("self",))
        .select_related("request")
        .get(pk=correspondence.pk)
    )
    if correspondence.kind in OFFICER_ONLY_DRAFT_KINDS:
        actor = _require_records_officer(actor)
    else:
        actor = _require_human_staff(actor)
    _check_version(correspondence, expected_version)
    if correspondence.status != Correspondence.Status.DRAFT:
        raise ValidationError("Only a draft may be deleted.")
    from apps.work_queue.source_reconciliation import (
        retire_deleted_correspondence_draft_work,
    )

    _audit_content(
        "correspondence.draft_deleted",
        correspondence,
        actor,
        kind=correspondence.kind,
    )
    retire_deleted_correspondence_draft_work(correspondence)
    try:
        correspondence.delete()
    except ProtectedError as exc:
        raise ValidationError(
            "This draft is linked to other case records and cannot be deleted."
        ) from exc


@transaction.atomic
def edit_structured_draft(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
    structured_data: dict,
    recipients: Iterable[dict] | None = None,
) -> Correspondence:
    correspondence = (
        Correspondence.objects.select_for_update(of=("self",))
        .select_related("request__requester")
        .get(pk=correspondence.pk)
    )
    _check_version(correspondence, expected_version)
    if correspondence.status != Correspondence.Status.DRAFT:
        raise ValidationError("Only a draft may be edited.")
    if correspondence.kind not in LEGAL_TEMPLATE_VERSIONS:
        raise ValidationError(
            "Only a structured template draft uses this edit service."
        )
    if correspondence.kind in OFFICER_ONLY_DRAFT_KINDS:
        actor = _require_records_officer(actor)
    else:
        actor = _require_human_staff(actor)
    metadata = _structured_metadata_for_edit(
        correspondence,
        structured_data,
        actor=actor,
    )
    rendered = _render_structured_snapshot(correspondence, metadata)
    correspondence.draft_subject = rendered.subject
    correspondence.draft_body = rendered.body
    correspondence.draft_metadata = metadata
    correspondence.version += 1
    correspondence.save(
        update_fields=[
            "draft_subject",
            "draft_body",
            "draft_metadata",
            "version",
        ]
    )
    if recipients is not None:
        _replace_recipients(correspondence, recipients)
    _require_requester_to_recipient(
        correspondence,
        list(correspondence.recipients.select_related("requester")),
    )
    _audit_content("correspondence.draft_edited", correspondence, actor)
    return correspondence


def _validate_for_approval(correspondence: Correspondence) -> None:
    if correspondence.status == Correspondence.Status.DRAFT:
        subject = correspondence.draft_subject
        body = correspondence.draft_body
        metadata = correspondence.draft_metadata
    else:
        subject = correspondence.approved_subject
        body = correspondence.approved_body
        metadata = correspondence.approved_metadata
    subject = validate_subject(subject)
    body = canonical_body(body)
    if correspondence.kind in LEGAL_TEMPLATE_VERSIONS:
        rendered = _render_structured_snapshot(correspondence, metadata)
        if (
            correspondence.status == Correspondence.Status.DRAFT
            and (rendered.subject != subject or rendered.body != body)
        ):
            raise ValidationError(
                "Structured correspondence content does not match its "
                "versioned template inputs."
            )
    if correspondence.kind == Correspondence.Kind.INSTALLMENT_NOTICE:
        installment = Installment.objects.get(
            pk=metadata["installment_id"],
            request_id=correspondence.request_id,
        )
        current_snapshot = _validated_installment_release_snapshot(
            installment,
            is_final=metadata["is_final"],
        )
        if current_snapshot != metadata:
            raise ValidationError(
                "The installment release snapshot is stale; create a new notice."
            )
    if correspondence.kind == Correspondence.Kind.NO_RECORDS_CLOSURE:
        require_adequate_search_snapshot(correspondence, lock=True)
    if correspondence.kind == Correspondence.Kind.FEE_ESTIMATE:
        schedule = current_fee_schedule()
        if schedule is None or str(schedule.pk) != metadata["schedule_id"]:
            raise ValidationError(
                "The fee schedule this estimate was drafted under has been "
                "superseded; create a new estimate under the current schedule."
            )
    if correspondence.provider != "gmail":
        raise ValidationError("Outbound correspondence provider must be Gmail.")
    if correspondence.kind == Correspondence.Kind.ACKNOWLEDGMENT:
        current_estimate = correspondence.request.estimated_completion
        snapshot_estimate = _structured_date(
            metadata.get("estimated_completion"),
            field="estimated_completion",
        )
        if (
            current_estimate is not None
            and snapshot_estimate != current_estimate
        ):
            raise ValidationError(
                "The acknowledgment estimate snapshot is stale; create and "
                "approve a new acknowledgment from the current estimate."
            )
    elif correspondence.kind in (
        Correspondence.Kind.ESTIMATE_REVISION,
        Correspondence.Kind.INTERPRETATION_ESTIMATE,
    ):
        current_previous = (
            correspondence.request.estimated_completion.isoformat()
            if correspondence.request.estimated_completion
            else None
        )
        if metadata.get("previous_date") != current_previous:
            raise ValidationError(
                "The estimate snapshot is stale; create and approve a new "
                "revision from the current estimate."
            )
    elif (
        correspondence.kind == Correspondence.Kind.DENIAL
        and correspondence.request.status == RequestStatus.CLARIFICATION
    ):
        raise ValidationError(
            "Resolve or close the open clarification before approving or "
            "delivering a denial."
        )
    if correspondence.kind == Correspondence.Kind.DENIAL:
        _, _, denial_citations, _ = denial_basis_evidence(
            correspondence,
            lock=True,
        )
        require_recipient_visible_denial_citations(
            body,
            denial_citations,
        )
    if not _PROVIDER_ACCOUNT.fullmatch(correspondence.provider_account):
        raise ValidationError(
            "Outbound provider accounts must be opaque ASCII identifiers."
        )
    recipients = list(correspondence.recipients.select_related("requester"))
    if not any(
        recipient.recipient_type == CorrespondenceRecipient.RecipientType.TO
        for recipient in recipients
    ):
        raise ValidationError("At least one valid To recipient is required.")
    for recipient in recipients:
        validate_email(recipient.email)
        _validate_display_name(recipient.display_name)
        if (
            recipient.requester_id
            and recipient.requester_id != correspondence.request.requester_id
        ):
            raise ValidationError(
                "Recipient requester relationship must belong to this request."
            )
    _require_requester_to_recipient(correspondence, recipients)
    attachments = list(correspondence.attachments.all())
    if len(attachments) > runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT"):
        raise ValidationError(
            "Outbound correspondence has too many attachments."
        )
    # Read once, outside the loop: every attachment on one message is judged
    # against the same limits, and a store read per attachment would be a
    # cache round trip per attachment.
    file_limit = runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES")
    combined_limit = runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_TOTAL_MAX_BYTES")
    total_attachment_bytes = 0
    for attachment in attachments:
        attachment.validate_outbound_metadata()
        if attachment.size < 1 or attachment.size > file_limit:
            raise ValidationError(
                "An outbound attachment exceeds the per-file size limit."
            )
        total_attachment_bytes += attachment.size
        if total_attachment_bytes > combined_limit:
            raise ValidationError(
                "Outbound attachments exceed the combined size limit."
            )
        if not attachment.is_downloadable:
            raise ValidationError(
                "Every outbound attachment must be scanned clean and released "
                "from quarantine."
            )


@transaction.atomic
def approve_draft(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
) -> Correspondence:
    return _approve_draft(
        correspondence,
        actor=actor,
        expected_version=expected_version,
        automated=False,
    )


def _approve_draft(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
    automated: bool,
) -> Correspondence:
    correspondence = (
        Correspondence.objects.select_for_update(of=("self",))
        .select_related("request__requester")
        .get(pk=correspondence.pk)
    )
    _check_version(correspondence, expected_version)
    if correspondence.status != Correspondence.Status.DRAFT:
        raise ValidationError("Only a draft may be approved.")
    if automated:
        if (
            not correspondence.automated
            or correspondence.kind not in AUTOMATED_CORRESPONDENCE_KINDS
        ):
            raise PermissionDenied(
                "Only an approved automated correspondence kind may "
                "auto-approve."
            )
    else:
        actor = _require_records_officer(actor)
    _validate_for_approval(correspondence)
    recipient_snapshot_sha256, recipient_count = (
        _recipient_snapshot_evidence(correspondence)
    )
    denial_audit_detail = {}
    if correspondence.kind == Correspondence.Kind.DENIAL:
        (
            denial_snapshot_sha256,
            denial_citation_count,
            denial_exemption_citations,
            denial_basis_snapshot,
        ) = denial_basis_evidence(correspondence, lock=True)
        denial_audit_detail = {
            "denial_citation_snapshot_sha256": denial_snapshot_sha256,
            "denial_citation_count": denial_citation_count,
            "denial_exemption_citations": denial_exemption_citations,
            "denial_basis_snapshot": denial_basis_snapshot,
        }
    now = timezone.now()
    correspondence.approved_subject = validate_subject(correspondence.draft_subject)
    correspondence.approved_body = canonical_body(correspondence.draft_body)
    correspondence.approved_metadata = correspondence.draft_metadata
    correspondence.approved_by = actor
    correspondence.approved_at = now
    correspondence.status = Correspondence.Status.APPROVED
    correspondence.version += 1
    correspondence.save(
        update_fields=[
            "approved_subject",
            "approved_body",
            "approved_metadata",
            "approved_by",
            "approved_at",
            "status",
            "version",
        ]
    )
    _audit_content(
        "correspondence.approved",
        correspondence,
        actor,
        automated=automated,
        recipient_count=recipient_count,
        recipient_snapshot_sha256=recipient_snapshot_sha256,
        **denial_audit_detail,
    )
    from apps.work_queue.integration import (
        schedule_source_work_item_reconciliation,
    )

    schedule_source_work_item_reconciliation("drafts", correspondence.pk)
    return correspondence


def _enqueue_delivery_after_commit(delivery: CorrespondenceDelivery) -> None:
    delivery_id = str(delivery.id)
    attempt_count = delivery.attempt_count

    def enqueue() -> None:
        from .tasks import enqueue_queued_delivery

        try:
            enqueue_queued_delivery(
                delivery_id,
                attempt_count=attempt_count,
            )
        except Exception:  # committed queue state is recovered by watchdog
            logger.exception(
                "Could not enqueue committed correspondence delivery",
                extra={"delivery": delivery_id},
            )

    transaction.on_commit(enqueue)


@transaction.atomic
def queue_approved(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
) -> CorrespondenceDelivery:
    return _queue_approved(
        correspondence,
        actor=actor,
        expected_version=expected_version,
        automated=False,
    )


@transaction.atomic
def approve_and_send(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
) -> CorrespondenceDelivery:
    """Approve a draft and hand it to delivery in one officer action.

    Approving and queueing were separate steps, but both call
    ``_require_records_officer``: the same person, in the same screen, with the
    same authority, clicking twice. There is no workflow where a reviewer
    approves and an officer then releases -- the role check makes that
    impossible -- so the second click bought nothing and read as the app
    refusing to do the thing it had just been told to do.

    Both writes stay intact and separately audited. ``approved_by`` and
    ``queued_by`` still record who did what and when, so the correspondence
    snapshot and the delivery record are unchanged; only the number of clicks
    is. The atomic block means a delivery that cannot be created leaves no
    approved-but-unsent message behind.
    """
    # Checked before approving rather than after. Inside one transaction a
    # disabled transport would roll the approval back too, and an officer who
    # clicked "approve and send" would watch nothing at all happen.
    _require_outbound_transport_enabled()
    approved = _approve_draft(
        correspondence,
        actor=actor,
        expected_version=expected_version,
        automated=False,
    )
    return _queue_approved(
        approved,
        actor=actor,
        expected_version=approved.version,
        automated=False,
    )


def _queue_approved(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
    automated: bool,
) -> CorrespondenceDelivery:
    _require_outbound_transport_enabled()
    correspondence_id = correspondence.pk
    reference = Correspondence.objects.only("request_id").get(
        pk=correspondence_id
    )
    prr = PublicRecordsRequest.objects.select_for_update(of=("self",)).get(
        pk=reference.request_id
    )
    correspondence = (
        Correspondence.objects.select_for_update(of=("self",))
        .select_related("request")
        .get(pk=correspondence_id)
    )
    _check_version(correspondence, expected_version)
    if correspondence.status != Correspondence.Status.APPROVED:
        raise ValidationError("Only approved correspondence may be queued.")
    if prr.status in CLOSED_STATUSES:
        raise ValidationError(
            "Correspondence cannot be queued after the request is closed."
        )
    if automated:
        if (
            not correspondence.automated
            or correspondence.kind not in AUTOMATED_CORRESPONDENCE_KINDS
        ):
            raise PermissionDenied(
                "Only an approved automated correspondence kind may auto-queue."
            )
    else:
        actor = _require_records_officer(actor)
    _validate_for_approval(correspondence)
    denial_basis_evidence(correspondence, lock=True)
    require_approved_denial_basis_snapshot(correspondence, lock=True)
    if correspondence.kind in REQUEST_LIFECYCLE_EFFECT_KINDS:
        if (
            correspondence.kind == Correspondence.Kind.ACKNOWLEDGMENT
            and prr.initial_response_delivered
        ):
            raise ValidationError(
                "This request has already been acknowledged or received its "
                "initial statutory response."
            )
        outstanding = Correspondence.objects.filter(
            request_id=correspondence.request_id,
            kind__in=REQUEST_LIFECYCLE_EFFECT_KINDS,
            status__in=[
                Correspondence.Status.QUEUED,
                Correspondence.Status.FAILED,
            ],
        ).exclude(pk=correspondence.pk)
        if outstanding.exists():
            raise ValidationError(
                "Only one requester-lifecycle message may be outstanding "
                "at a time."
            )
    _require_denial_delivery_exclusivity(correspondence)
    if correspondence.kind in CLARIFICATION_CYCLE_KINDS:
        open_cycle = ClarificationCycle.objects.filter(
            request_id=correspondence.request_id,
            resolved_at__isnull=True,
            cancelled_at__isnull=True,
        )
        if open_cycle.exists():
            raise ValidationError(
                "Only one clarification request may be outstanding at a time."
            )
    now = timezone.now()
    delivery = CorrespondenceDelivery.objects.create(
        correspondence=correspondence,
        provider=correspondence.provider,
        provider_account=correspondence.provider_account,
        idempotency_key=f"correspondence-delivery:{correspondence.id}",
        queued_at=now,
        recovery_not_before=(
            now
            + dt.timedelta(
                seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"),
            )
        ),
    )
    correspondence.status = Correspondence.Status.QUEUED
    correspondence.queued_by = actor
    correspondence.queued_at = now
    correspondence.version += 1
    correspondence.save(
        update_fields=["status", "queued_by", "queued_at", "version"]
    )
    audit(
        "correspondence.queued",
        target=correspondence.request.number,
        actor=actor,
        correspondence_id=str(correspondence.id),
        delivery_id=str(delivery.id),
        automated=automated,
    )
    _enqueue_delivery_after_commit(delivery)
    return delivery


@transaction.atomic
def queue_automated_acknowledgment(
    prr: PublicRecordsRequest,
) -> AutomatedCorrespondenceQueueResult:
    """Queue the automated statutory safety net without claiming delivery."""
    _require_automated_outbound_enabled()
    prr = (
        PublicRecordsRequest.objects.select_for_update(of=("self",))
        .select_related("requester")
        .get(pk=prr.pk)
    )
    if prr.initial_response_delivered:
        raise ValidationError(
            "This request has already been acknowledged or received its "
            "initial statutory response."
        )
    existing = list(
        CorrespondenceDelivery.objects.select_related("correspondence")
        .filter(
            correspondence__request=prr,
            correspondence__kind=Correspondence.Kind.ACKNOWLEDGMENT,
        )
        .order_by("-queued_at")
    )
    active = [
        delivery
        for delivery in existing
        if delivery.status
        in {
            CorrespondenceDelivery.Status.PENDING,
            CorrespondenceDelivery.Status.CLAIMED,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        }
        and delivery.correspondence.status == Correspondence.Status.QUEUED
    ]
    if len(active) == 1:
        return AutomatedCorrespondenceQueueResult(
            delivery=active[0],
            created=False,
        )
    if existing:
        raise ValidationError(
            "The existing acknowledgment delivery requires human review."
        )
    draft = create_acknowledgment_draft(
        prr,
        actor=None,
        automated=True,
    )
    approved = _approve_draft(
        draft,
        actor=None,
        expected_version=draft.version,
        automated=True,
    )
    delivery = _queue_approved(
        approved,
        actor=None,
        expected_version=approved.version,
        automated=True,
    )
    return AutomatedCorrespondenceQueueResult(
        delivery=delivery,
        created=True,
    )


@transaction.atomic
def queue_submission_receipt(
    prr: PublicRecordsRequest,
) -> AutomatedCorrespondenceQueueResult:
    """Queue one non-statutory receipt without applying lifecycle effects."""
    _require_automated_outbound_enabled()
    prr = (
        PublicRecordsRequest.objects.select_for_update(of=("self",))
        .select_related("requester")
        .get(pk=prr.pk)
    )
    existing = list(
        CorrespondenceDelivery.objects.select_related("correspondence")
        .filter(
            correspondence__request=prr,
            correspondence__kind=Correspondence.Kind.SUBMISSION_RECEIPT,
        )
        .order_by("queued_at")
    )
    if len(existing) == 1:
        return AutomatedCorrespondenceQueueResult(
            delivery=existing[0],
            created=False,
        )
    if existing or Correspondence.objects.filter(
        request=prr,
        kind=Correspondence.Kind.SUBMISSION_RECEIPT,
    ).exists():
        raise ValidationError(
            "The existing submission receipt delivery requires human review."
        )
    draft = create_submission_receipt_draft(prr)
    approved = _approve_draft(
        draft,
        actor=None,
        expected_version=draft.version,
        automated=True,
    )
    delivery = _queue_approved(
        approved,
        actor=None,
        expected_version=approved.version,
        automated=True,
    )
    return AutomatedCorrespondenceQueueResult(
        delivery=delivery,
        created=True,
    )


@transaction.atomic
def cancel_unsent(
    correspondence: Correspondence,
    *,
    actor,
    expected_version: int,
    reason: str,
) -> Correspondence:
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValidationError("Cancellation requires a substantive reason.")
    correspondence = Correspondence.objects.select_for_update(of=("self",)).get(
        pk=correspondence.pk
    )
    actor = _require_records_officer(actor)
    _check_version(correspondence, expected_version)
    if correspondence.status not in {
        Correspondence.Status.APPROVED,
        Correspondence.Status.QUEUED,
        Correspondence.Status.FAILED,
    }:
        raise ValidationError("Only unsent correspondence may be cancelled.")
    delivery = (
        CorrespondenceDelivery.objects.select_for_update(of=("self",))
        .filter(correspondence=correspondence)
        .first()
    )
    if delivery and delivery.status not in {
        CorrespondenceDelivery.Status.PENDING,
        CorrespondenceDelivery.Status.RETRYABLE,
        CorrespondenceDelivery.Status.FAILED,
    }:
        raise ValidationError("An active or completed delivery cannot be cancelled.")
    now = timezone.now()
    if delivery:
        delivery.status = CorrespondenceDelivery.Status.CANCELLED
        delivery.completed_at = now
        delivery.claim_owner = ""
        delivery.claim_expires_at = None
        delivery.version += 1
        delivery.save(
            update_fields=[
                "status",
                "completed_at",
                "claim_owner",
                "claim_expires_at",
                "version",
            ]
        )
    correspondence.status = Correspondence.Status.CANCELLED
    correspondence.cancelled_at = now
    correspondence.version += 1
    correspondence.save(update_fields=["status", "cancelled_at", "version"])
    audit(
        "correspondence.cancelled",
        target=correspondence.request.number,
        actor=actor,
        correspondence_id=str(correspondence.id),
        delivery_id=str(delivery.id) if delivery else "",
        reason=reason,
    )
    return correspondence


UNSENT_OUTBOUND_STATUSES = (
    Correspondence.Status.DRAFT,
    Correspondence.Status.APPROVED,
    Correspondence.Status.QUEUED,
    Correspondence.Status.FAILED,
)


def _is_superseded_requester_recipient(recipient, previous_requester) -> bool:
    """Whether this row is the message's requester slot, not a copy of it.

    A recipient linked to the superseded requester row always is. An unlinked
    row counts only when it is a To recipient, because that is the slot
    ``_require_requester_to_recipient`` governs. A CC or BCC carrying the same
    address is somebody the officer deliberately copied -- often the very
    mailbox that forwarded the request, kept on the thread on purpose -- and
    correcting who the requester is says nothing about whether they should
    still be copied.
    """
    if recipient.requester_id == previous_requester.pk:
        return True
    return (
        recipient.recipient_type == CorrespondenceRecipient.RecipientType.TO
        and recipient.email.casefold()
        == previous_requester.email.casefold()
    )


def _addresses_requester(recipients, previous_requester) -> bool:
    """Whether a message is addressed to the identity being replaced."""
    return any(
        _is_superseded_requester_recipient(recipient, previous_requester)
        for recipient in recipients
    )


def _recipients_for_corrected_requester(
    prr: PublicRecordsRequest,
    recipients,
    previous_requester,
) -> list[dict]:
    """Swap the superseded requester slot for the corrected default recipient.

    Every other recipient is preserved -- a copied attorney, an internal
    watcher, or a CC of the forwarding mailbox itself. The correction is about
    who the record says the requester is, not about who else the officer chose
    to copy.
    """
    corrected = _default_recipient(prr)
    corrected_emails = {
        entry["email"].casefold() for entry in corrected
    }
    kept = [
        {
            "recipient_type": recipient.recipient_type,
            "email": recipient.email,
            "display_name": recipient.display_name,
            "requester": None,
        }
        for recipient in recipients
        if not _is_superseded_requester_recipient(
            recipient,
            previous_requester,
        )
        and recipient.email.casefold() not in corrected_emails
    ]
    return corrected + kept


@transaction.atomic
def refresh_drafts_for_requester_correction(
    prr: PublicRecordsRequest,
    *,
    previous_requester,
    actor,
    reason: str,
) -> RequesterCorrectionRefresh:
    """Repoint unsent outbound correspondence at a corrected requester.

    Sent correspondence is never touched -- an approved snapshot is the record
    of what actually went out, wrong address included. Drafts are repointed
    and, where they are versioned legal templates, re-rendered so the
    salutation names the corrected requester. Approved-but-unsent messages
    cannot be re-rendered (their content is frozen), so anything still
    addressed to the superseded identity is cancelled rather than delivered to
    the wrong person; the officer drafts a replacement.

    ``prr`` must already carry the corrected requester, because the template
    context and the default recipient are both read from it.
    """
    actor = _require_records_officer(actor)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValidationError(
            "Refreshing correspondence for a corrected requester identity "
            "requires a substantive reason."
        )
    if prr.requester_id == previous_requester.pk:
        raise ValidationError(
            "The request must already carry the corrected requester before "
            "its correspondence is refreshed."
        )

    refreshed = []
    needs_review = []
    cancelled = []
    uncancelled = []
    candidates = list(
        Correspondence.objects.select_for_update(of=("self",))
        .filter(
            request_id=prr.pk,
            direction=Correspondence.Direction.OUTBOUND,
            status__in=UNSENT_OUTBOUND_STATUSES,
        )
        .order_by("created_at", "pk")
    )
    for correspondence in candidates:
        # Render and validate against the corrected request already loaded
        # here rather than a second query that could see a stale requester.
        correspondence.request = prr
        recipients = list(correspondence.recipients.all())
        if not _addresses_requester(recipients, previous_requester):
            continue
        if correspondence.status != Correspondence.Status.DRAFT:
            try:
                cancelled.append(
                    cancel_unsent(
                        correspondence,
                        actor=actor,
                        expected_version=correspondence.version,
                        reason=(
                            "Requester identity corrected; this approved "
                            "message addressed the superseded requester. "
                            f"{reason}"
                        ),
                    )
                )
            except ValidationError as exc:
                # A delivery already claimed by a worker cannot be recalled.
                # Blocking the correction would not stop that send, so the
                # officer is told instead of being silently left with a
                # message on its way to the wrong address.
                detail = "; ".join(exc.messages)
                uncancelled.append((correspondence, detail))
                audit(
                    "correspondence.requester_correction_cancel_refused",
                    target=prr.number,
                    actor=actor,
                    correspondence_id=str(correspondence.id),
                    kind=correspondence.kind,
                    detail=detail,
                )
            continue

        _replace_recipients(
            correspondence,
            _recipients_for_corrected_requester(
                prr,
                recipients,
                previous_requester,
            ),
        )
        content_refreshed = True
        review_detail = ""
        if correspondence.kind in LEGAL_TEMPLATE_VERSIONS:
            try:
                rendered = _render_structured_snapshot(
                    correspondence,
                    correspondence.draft_metadata,
                )
            except ValidationError as exc:
                # The draft's own inputs are already stale for an unrelated
                # reason. Approval re-renders and refuses a mismatch, so
                # leaving the body alone fails closed instead of silently
                # inventing new content here -- and the officer is told why.
                content_refreshed = False
                review_detail = "; ".join(exc.messages)
            else:
                correspondence.draft_subject = rendered.subject
                correspondence.draft_body = rendered.body
        else:
            # Authored bodies are the officer's own words; this service will
            # not rewrite them, so any name inside one needs a human read.
            content_refreshed = False
            review_detail = (
                "The wording was authored by hand and left exactly as written."
            )
        correspondence.version += 1
        correspondence.save(
            update_fields=["draft_subject", "draft_body", "version"]
        )
        audit(
            "correspondence.requester_corrected",
            target=prr.number,
            actor=actor,
            correspondence_id=str(correspondence.id),
            kind=correspondence.kind,
            reason=reason,
            # The identities live once, on the request-level entry; the
            # audit log is permanent, so per-draft rows carry no PII.
            content_refreshed=content_refreshed,
            detail=review_detail,
        )
        if content_refreshed:
            refreshed.append(correspondence)
        else:
            needs_review.append((correspondence, review_detail))

    return RequesterCorrectionRefresh(
        refreshed=tuple(refreshed),
        needs_review=tuple(needs_review),
        cancelled=tuple(cancelled),
        uncancelled=tuple(uncancelled),
    )


@transaction.atomic
def retry_delivery(
    delivery: CorrespondenceDelivery,
    *,
    actor,
    expected_version: int,
) -> CorrespondenceDelivery:
    _require_outbound_transport_enabled()
    correspondence, delivery = _lock_correspondence_and_delivery(delivery)
    if correspondence.automated:
        _require_automated_outbound_enabled()
    actor = _require_records_officer(actor)
    _check_version(delivery, expected_version)
    if correspondence.request.status in CLOSED_STATUSES:
        raise ValidationError(
            "Correspondence cannot be retried after the request is closed."
        )
    if delivery.status not in {
        CorrespondenceDelivery.Status.FAILED,
        CorrespondenceDelivery.Status.RETRYABLE,
    }:
        raise ValidationError("Only a failed or retryable delivery may be retried.")
    if correspondence.status != Correspondence.Status.FAILED:
        raise ValidationError("The correspondence is not in a failed state.")
    latest_attempt = (
        CorrespondenceDeliveryAttempt.objects.select_for_update(of=("self",))
        .filter(
            delivery=delivery,
            attempt_number=delivery.attempt_count,
        )
        .first()
    )
    retryable_failure = (
        latest_attempt is not None
        and latest_attempt.result_category
        == CorrespondenceDeliveryAttempt.ResultCategory.FAILED
        and latest_attempt.error_category in RETRYABLE_DELIVERY_FAILURES
    )
    permanent_failure = (
        latest_attempt is not None
        and latest_attempt.result_category
        == CorrespondenceDeliveryAttempt.ResultCategory.FAILED
        and latest_attempt.error_category in PERMANENT_DELIVERY_FAILURES
    )
    reconciliation_not_found = (
        latest_attempt is not None
        and latest_attempt.result_category
        == CorrespondenceDeliveryAttempt.ResultCategory.NOT_FOUND
        and latest_attempt.error_category == "reconciliation_not_found"
    )
    evidence_matches_state = (
        (delivery.status == CorrespondenceDelivery.Status.RETRYABLE)
        and (retryable_failure or reconciliation_not_found)
    ) or (
        delivery.status == CorrespondenceDelivery.Status.FAILED
        and permanent_failure
    )
    if (
        not evidence_matches_state
        or latest_attempt is None
        or latest_attempt.result_at is None
        or latest_attempt.result_at != delivery.completed_at
        or latest_attempt.result_at != correspondence.failed_at
        or not latest_attempt.result_fingerprint
        or latest_attempt.actor_id is None
        or latest_attempt.claim_owner != str(latest_attempt.actor_id)
        or latest_attempt.error_category != delivery.last_failure_category
        or latest_attempt.error_category != correspondence.failure_category
        or delivery.last_failure_description
        != correspondence.failure_description
        or delivery.successful_attempt_number is not None
        or CorrespondenceDeliveryAttempt.objects.filter(
            delivery=delivery,
            result_at__isnull=True,
        ).exists()
        or CorrespondenceDeliveryAttempt.objects.filter(
            delivery=delivery,
            result_category__in=[
                CorrespondenceDeliveryAttempt.ResultCategory.SENT,
                CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED,
            ],
        ).exists()
    ):
        raise ValidationError(
            "Retry requires matching finalized failure evidence."
        )
    _validate_for_approval(correspondence)
    denial_basis_evidence(correspondence, lock=True)
    require_approved_denial_basis_snapshot(correspondence, lock=True)
    _require_denial_delivery_exclusivity(correspondence)
    now = timezone.now()
    delivery.status = CorrespondenceDelivery.Status.PENDING
    delivery.claim_owner = ""
    delivery.claim_expires_at = None
    delivery.last_failure_category = ""
    delivery.last_failure_description = ""
    delivery.completed_at = None
    delivery.recovery_not_before = (
        now
        + dt.timedelta(
            seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"),
        )
    )
    delivery.version += 1
    delivery.save(
        update_fields=[
            "status",
            "claim_owner",
            "claim_expires_at",
            "last_failure_category",
            "last_failure_description",
            "completed_at",
            "recovery_not_before",
            "version",
        ]
    )
    correspondence.status = Correspondence.Status.QUEUED
    correspondence.queued_by = actor
    correspondence.queued_at = now
    correspondence.failed_at = None
    correspondence.failure_category = ""
    correspondence.failure_description = ""
    correspondence.version += 1
    correspondence.save(
        update_fields=[
            "status",
            "queued_by",
            "queued_at",
            "failed_at",
            "failure_category",
            "failure_description",
            "version",
        ]
    )
    audit(
        "correspondence.retry_queued",
        target=correspondence.request.number,
        actor=actor,
        correspondence_id=str(correspondence.id),
        delivery_id=str(delivery.id),
        next_attempt=delivery.attempt_count + 1,
    )
    _enqueue_delivery_after_commit(delivery)
    return delivery


def _claim_owner(actor) -> str:
    return str(actor.pk)


def _safe_provider_metadata(metadata: dict | None) -> dict:
    supplied = metadata if isinstance(metadata, dict) else {}
    safe = {}
    http_status = supplied.get("http_status")
    if isinstance(http_status, int) and 100 <= http_status <= 599:
        safe["http_status"] = http_status
    provider_code = supplied.get("provider_code")
    if provider_code not in (None, ""):
        provider_code = str(provider_code)
        if provider_code != provider_code.strip():
            raise ValidationError(
                "Provider codes must be opaque ASCII identifiers."
            )
        if not _PROVIDER_CODE.fullmatch(provider_code):
            raise ValidationError(
                "Provider codes must be opaque ASCII identifiers."
            )
        safe["provider_code"] = provider_code
    return safe


def _result_fingerprint(
    *,
    outcome: str,
    provider_message_id: str,
    provider_thread_id: str,
    provider_timestamp: dt.datetime | None,
    failure_category: str,
    provider_metadata: dict,
) -> str:
    material = {
        "outcome": outcome,
        "provider_message_id": provider_message_id,
        "provider_thread_id": provider_thread_id,
        "provider_timestamp": (
            provider_timestamp.astimezone(dt.UTC).isoformat()
            if provider_timestamp
            else None
        ),
        "failure_category": failure_category,
        "provider_metadata": provider_metadata,
    }
    return hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _validated_provider_identifier(value: str, *, required: bool, label: str) -> str:
    supplied = str(value or "")
    if supplied != supplied.strip():
        raise ValidationError(
            f"{label} must be an opaque ASCII identifier."
        )
    if required and not supplied:
        raise ValidationError(f"{label} is required.")
    if supplied and not _PROVIDER_IDENTIFIER.fullmatch(supplied):
        raise ValidationError(
            f"{label} must be an opaque ASCII identifier."
        )
    return supplied


def _reply_to_address(correspondence: Correspondence) -> str:
    local_part = runtime.get("PRR_CORRESPONDENCE_REPLY_LOCAL_PART")
    domain = runtime.get("PRR_CORRESPONDENCE_REPLY_DOMAIN")
    address = f"{local_part}+{correspondence.reply_token}@{domain}"
    validate_email(address)
    return address


def _claim_payload(
    *,
    correspondence: Correspondence,
    delivery: CorrespondenceDelivery,
    attempt: CorrespondenceDeliveryAttempt,
    lease_expires_at: dt.datetime,
) -> dict:
    common = {
        "schema_version": "1",
        "action": attempt.action,
        "delivery_id": str(delivery.id),
        "correspondence_id": str(correspondence.id),
        "attempt_id": str(attempt.id),
        "attempt_number": attempt.attempt_number,
        "lease_expires_at": lease_expires_at.isoformat(),
        "provider": delivery.provider,
        "provider_account": delivery.provider_account,
    }
    if attempt.action == CorrespondenceDeliveryAttempt.Action.RECONCILE:
        message_id = correspondence.rfc_message_id
        return common | {
            "reconciliation": {
                "strategy": "gmail_rfc822_message_id",
                "query": f"rfc822msgid:{message_id}",
                "message_id": message_id,
            }
        }

    recipients = {
        CorrespondenceRecipient.RecipientType.TO: [],
        CorrespondenceRecipient.RecipientType.CC: [],
        CorrespondenceRecipient.RecipientType.BCC: [],
    }
    for recipient in correspondence.recipients.order_by("created_at", "id"):
        recipients[recipient.recipient_type].append(
            {
                "email": recipient.email,
                "display_name": recipient.display_name,
            }
        )

    attachments = []
    for attachment in correspondence.attachments.order_by("created_at", "id"):
        if not attachment.is_downloadable:
            raise UnsafeDeliveryAttachment(
                "Every outbound attachment must remain clean and released."
            )
        attachments.append(
            {
                "id": str(attachment.id),
                "filename": attachment.original_filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "sha256": attachment.sha256,
            }
        )

    return common | {
        "message": {
            "subject": correspondence.approved_subject,
            "body": correspondence.approved_body,
            "recipients": recipients,
            "headers": {
                "Message-ID": correspondence.rfc_message_id,
                "Reply-To": _reply_to_address(correspondence),
            },
            "attachments": attachments,
        }
    }


def _lock_correspondence_and_delivery(
    delivery: CorrespondenceDelivery | str,
) -> tuple[Correspondence, CorrespondenceDelivery]:
    delivery_id = getattr(delivery, "pk", delivery)
    reference = CorrespondenceDelivery.objects.only(
        "correspondence_id"
    ).get(pk=delivery_id)
    correspondence_reference = Correspondence.objects.only(
        "request_id"
    ).get(pk=reference.correspondence_id)
    PublicRecordsRequest.objects.select_for_update(of=("self",)).get(
        pk=correspondence_reference.request_id
    )
    correspondence = (
        Correspondence.objects.select_for_update(of=("self",))
        .select_related("request")
        .get(pk=reference.correspondence_id)
    )
    locked_delivery = (
        CorrespondenceDelivery.objects.select_for_update(of=("self",))
        .get(pk=delivery_id)
    )
    return correspondence, locked_delivery


def _open_locked_attachment(
    attachment: CorrespondenceAttachment,
) -> OpenedDeliveryAttachment:
    """Materialize and verify released bytes while the safety row is locked."""
    if (
        not attachment.is_downloadable
        or not attachment.storage_key
        or attachment.size < 0
        or attachment.size
        > runtime.get("PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES")
    ):
        raise DeliveryAttachmentNotAvailable(
            "The correspondence attachment is unavailable."
        )
    source = None
    verified = None
    verified_ok = False
    try:
        source = open_clean_attachment(attachment)
        if source is None:
            raise KeyError("Body")
        verified = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - the spool outlives this block
            max_size=1024 * 1024,
            mode="w+b",
        )
        digest = hashlib.sha256()
        bytes_seen = 0
        while True:
            remaining = attachment.size + 1 - bytes_seen
            if remaining <= 0:
                raise DeliveryAttachmentNotAvailable(
                    "The correspondence attachment is unavailable."
                )
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise TypeError("Attachment storage returned non-byte content.")
            bytes_seen += len(chunk)
            digest.update(chunk)
            verified.write(chunk)
        if (
            bytes_seen != attachment.size
            or digest.hexdigest() != attachment.sha256
        ):
            raise DeliveryAttachmentNotAvailable(
                "The correspondence attachment is unavailable."
            )
        verified.seek(0)
        verified_ok = True
    except ClientError as exc:
        error_code = str(
            exc.response.get("Error", {}).get("Code", "")
        )
        if error_code in {"NoSuchKey", "NoSuchObject", "404", "NotFound"}:
            raise DeliveryAttachmentNotAvailable(
                "The correspondence attachment is unavailable."
            ) from None
        raise DeliveryAttachmentStorageUnavailable(
            "Correspondence attachment storage is temporarily unavailable."
        ) from None
    except DeliveryAttachmentNotAvailable:
        raise
    except ValueError:
        raise DeliveryAttachmentNotAvailable(
            "The correspondence attachment is unavailable."
        ) from None
    except (
        BotoCoreError,
        OSError,
        KeyError,
        TypeError,
        ImproperlyConfigured,
    ):
        raise DeliveryAttachmentStorageUnavailable(
            "Correspondence attachment storage is temporarily unavailable."
        ) from None
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001 — the verified local copy is authoritative
                logger.warning(
                    "Could not close attachment source stream",
                    extra={"attachment": str(attachment.id)},
                )
        if verified is not None and not verified_ok:
            verified.close()
    if verified is None or not verified_ok:
        raise DeliveryAttachmentStorageUnavailable(
            "Correspondence attachment storage is temporarily unavailable."
        )
    return OpenedDeliveryAttachment(attachment=attachment, body=verified)


@transaction.atomic
def open_claim_attachment(
    delivery: CorrespondenceDelivery | str,
    *,
    attempt_id,
    attachment_id,
    actor,
    credential=None,
    now: dt.datetime | None = None,
) -> OpenedDeliveryAttachment:
    """Atomically revalidate and open a file bound to an active send lease."""
    actor = require_exact_outbound_identity(actor, credential=credential)
    opened_at = now or timezone.now()
    try:
        correspondence, delivery = _lock_correspondence_and_delivery(delivery)
    except CorrespondenceDelivery.DoesNotExist:
        raise DeliveryAttachmentNotAvailable(
            "The correspondence attachment is unavailable."
        ) from None

    owner = _claim_owner(actor)
    if (
        delivery.status != CorrespondenceDelivery.Status.CLAIMED
        or delivery.claim_owner != owner
        or delivery.claim_expires_at is None
        or delivery.claim_expires_at <= opened_at
    ):
        raise DeliveryAttachmentNotAvailable(
            "The correspondence attachment is unavailable."
        )
    attempt = (
        CorrespondenceDeliveryAttempt.objects.select_for_update(of=("self",))
        .filter(
            pk=attempt_id,
            delivery=delivery,
            attempt_number=delivery.attempt_count,
            action=CorrespondenceDeliveryAttempt.Action.SEND,
            actor=actor,
            claim_owner=owner,
            result_at__isnull=True,
        )
        .first()
    )
    if attempt is None:
        raise DeliveryAttachmentNotAvailable(
            "The correspondence attachment is unavailable."
        )
    attachment = (
        CorrespondenceAttachment.objects.select_for_update(of=("self",))
        .filter(
            pk=attachment_id,
            correspondence=correspondence,
        )
        .first()
    )
    if attachment is None:
        raise DeliveryAttachmentNotAvailable(
            "The correspondence attachment is unavailable."
        )
    return _open_locked_attachment(attachment)


@transaction.atomic
def open_released_attachment(
    attachment: CorrespondenceAttachment | str,
    *,
    actor,
) -> OpenedDeliveryAttachment:
    """Atomically revalidate and open a staff-authorized attachment."""
    attachment_id = getattr(attachment, "pk", attachment)
    locked_attachment = (
        CorrespondenceAttachment.objects.select_for_update(of=("self",))
        .select_related("correspondence__request")
        .filter(pk=attachment_id)
        .first()
    )
    if locked_attachment is None:
        raise DeliveryAttachmentNotAvailable(
            "The correspondence attachment is unavailable."
        )
    _require_human_staff(actor)
    return _open_locked_attachment(locked_attachment)


def _finalize_expired_attempt(
    delivery: CorrespondenceDelivery,
    *,
    now: dt.datetime,
) -> CorrespondenceDeliveryAttempt:
    expected_action = {
        CorrespondenceDelivery.Status.CLAIMED: (
            CorrespondenceDeliveryAttempt.Action.SEND
        ),
        CorrespondenceDelivery.Status.AWAITING_RECONCILIATION: (
            CorrespondenceDeliveryAttempt.Action.RECONCILE
        ),
    }.get(delivery.status)
    if (
        expected_action is None
        or not delivery.claim_owner
        or delivery.claim_expires_at is None
        or delivery.claim_expires_at > now
    ):
        raise DeliveryNotClaimable(delivery.status)
    attempt = (
        CorrespondenceDeliveryAttempt.objects.select_for_update(of=("self",))
        .filter(
            delivery=delivery,
            attempt_number=delivery.attempt_count,
            action=expected_action,
            claim_owner=delivery.claim_owner,
            result_at__isnull=True,
        )
        .first()
    )
    if attempt is None:
        raise ValidationError(
            "The expired delivery claim does not match its open attempt."
        )
    if now < attempt.claimed_at:
        raise ValidationError("A delivery result cannot predate its claim.")
    safe_metadata = {}
    attempt.result_at = now
    attempt.result_category = CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED
    attempt.error_category = "lease_expired"
    attempt.provider_response_metadata = safe_metadata
    attempt.result_fingerprint = _result_fingerprint(
        outcome="lease_expired",
        provider_message_id="",
        provider_thread_id="",
        provider_timestamp=None,
        failure_category="lease_expired",
        provider_metadata=safe_metadata,
    )
    attempt.save(
        update_fields=[
            "result_at",
            "result_category",
            "error_category",
            "provider_response_metadata",
            "result_fingerprint",
        ]
    )
    return attempt


def _transition_expired_claim_to_reconciliation(
    correspondence: Correspondence,
    delivery: CorrespondenceDelivery,
    *,
    now: dt.datetime,
    actor=None,
) -> CorrespondenceDeliveryAttempt:
    attempt = _finalize_expired_attempt(delivery, now=now)
    delivery.status = CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
    delivery.claim_owner = ""
    delivery.claim_expires_at = None
    delivery.last_failure_category = "lease_expired"
    delivery.last_failure_description = (
        "The delivery lease expired; reconcile by deterministic Message-ID."
    )
    delivery.completed_at = None
    delivery.version += 1
    delivery.save(
        update_fields=[
            "status",
            "claim_owner",
            "claim_expires_at",
            "last_failure_category",
            "last_failure_description",
            "completed_at",
            "version",
        ]
    )
    audit(
        "correspondence.delivery_lease_expired",
        target=str(delivery.id),
        actor=actor,
        correspondence_id=str(correspondence.id),
        delivery_id=str(delivery.id),
        attempt_id=str(attempt.id),
        attempt_number=attempt.attempt_number,
        claim_action=attempt.action,
    )
    return attempt


def _latest_delivery_activity(
    delivery: CorrespondenceDelivery,
) -> dt.datetime:
    latest_attempt = (
        CorrespondenceDeliveryAttempt.objects.filter(delivery=delivery)
        .only("claimed_at", "result_at", "attempt_number")
        .order_by("-attempt_number")
        .first()
    )
    if latest_attempt is None:
        return delivery.queued_at
    return max(
        delivery.queued_at,
        latest_attempt.result_at or latest_attempt.claimed_at,
    )


def _watchdog_candidate_ids(
    *,
    now: dt.datetime,
    limit: int,
    outbound_enabled: bool,
    automated_outbound_enabled: bool,
) -> list:
    due_recovery = Q(recovery_not_before__isnull=True) | Q(
        recovery_not_before__lte=now
    )
    matching_open_attempt = CorrespondenceDeliveryAttempt.objects.filter(
        delivery_id=OuterRef("pk"),
        attempt_number=OuterRef("attempt_count"),
        claim_owner=OuterRef("claim_owner"),
        result_at__isnull=True,
    )
    candidates = CorrespondenceDelivery.objects.annotate(
        watchdog_open_send_attempt=Exists(
            matching_open_attempt.filter(
                action=CorrespondenceDeliveryAttempt.Action.SEND,
            )
        ),
        watchdog_open_reconcile_attempt=Exists(
            matching_open_attempt.filter(
                action=CorrespondenceDeliveryAttempt.Action.RECONCILE,
            )
        ),
    )
    matching_expired_evidence = Q(
        status=CorrespondenceDelivery.Status.CLAIMED,
        watchdog_open_send_attempt=True,
    ) | Q(
        status=CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        watchdog_open_reconcile_attempt=True,
    )
    expired_claim = Q(
        status__in=[
            CorrespondenceDelivery.Status.CLAIMED,
            CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
        ],
        claim_expires_at__lte=now,
    ) & (matching_expired_evidence | due_recovery)
    if outbound_enabled:
        stale_before = now - dt.timedelta(
            seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"),
        )
        unclaimed_stale = Q(
            status__in=[
                CorrespondenceDelivery.Status.PENDING,
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
            ],
            claim_owner="",
            claim_expires_at__isnull=True,
            queued_at__lte=stale_before,
        ) & due_recovery
        if not automated_outbound_enabled:
            unclaimed_stale &= Q(correspondence__automated=False)
        candidates = candidates.filter(expired_claim | unclaimed_stale)
    else:
        candidates = candidates.filter(expired_claim)
    return list(
        candidates.order_by("queued_at", "id").values_list("id", flat=True)[
            :limit
        ]
    )


def _defer_inconsistent_expired_claim(
    delivery_id,
    *,
    observed_at: dt.datetime,
    next_recovery_at: dt.datetime,
) -> None:
    with transaction.atomic():
        _, delivery = _lock_correspondence_and_delivery(delivery_id)
        still_expired = (
            delivery.claim_expires_at is not None
            and delivery.claim_expires_at <= observed_at
            and delivery.status
            in {
                CorrespondenceDelivery.Status.CLAIMED,
                CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
            }
        )
        if not still_expired:
            return
        if (
            delivery.recovery_not_before is not None
            and delivery.recovery_not_before >= next_recovery_at
        ):
            return
        delivery.recovery_not_before = next_recovery_at
        delivery.version += 1
        delivery.save(update_fields=["recovery_not_before", "version"])


def watch_stuck_deliveries(
    *,
    now: dt.datetime | None = None,
    limit: int = 100,
) -> DeliveryWatchdogResult:
    """Recover stuck deliveries without ever creating a replacement send."""
    observed_at = now or timezone.now()
    bounded_limit = max(1, min(int(limit), 1000))
    outbound_enabled = runtime.get("PRR_CORRESPONDENCE_OUTBOUND_ENABLED")
    candidate_ids = _watchdog_candidate_ids(
        now=observed_at,
        limit=bounded_limit,
        outbound_enabled=outbound_enabled,
        automated_outbound_enabled=(
            runtime.get("PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED")
        ),
    )
    expired_claims = 0
    nudged_deliveries = 0
    suppressed_nudges = 0
    notification_failures = 0
    stale_before = observed_at - dt.timedelta(
        seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"),
    )
    next_recovery_at = observed_at + dt.timedelta(
        seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"),
    )

    for delivery_id in candidate_ids:
        enqueue_details = None
        try:
            with transaction.atomic():
                correspondence, delivery = _lock_correspondence_and_delivery(
                    delivery_id
                )
                expired = (
                    delivery.claim_expires_at is not None
                    and delivery.claim_expires_at <= observed_at
                    and delivery.status
                    in {
                        CorrespondenceDelivery.Status.CLAIMED,
                        CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
                    }
                )
                if expired:
                    _transition_expired_claim_to_reconciliation(
                        correspondence,
                        delivery,
                        now=observed_at,
                    )
                    expired_claims += 1

                if not outbound_enabled:
                    if expired:
                        delivery.recovery_not_before = next_recovery_at
                        delivery.version += 1
                        delivery.save(
                            update_fields=["recovery_not_before", "version"]
                        )
                        suppressed_nudges += 1
                    continue
                if (
                    delivery.status
                    not in {
                        CorrespondenceDelivery.Status.PENDING,
                        CorrespondenceDelivery.Status.AWAITING_RECONCILIATION,
                    }
                    or delivery.claim_expires_at is not None
                ):
                    continue
                if not expired:
                    if (
                        delivery.recovery_not_before is not None
                        and delivery.recovery_not_before > observed_at
                    ):
                        continue
                    if _latest_delivery_activity(delivery) > stale_before:
                        continue

                delivery.recovery_not_before = next_recovery_at
                delivery.version += 1
                delivery.save(
                    update_fields=["recovery_not_before", "version"]
                )
                enqueue_details = {
                    "delivery_id": str(delivery.id),
                    "attempt_count": delivery.attempt_count,
                    "correspondence_id": str(correspondence.id),
                    "delivery_status": delivery.status,
                }
        except ValidationError:
            _defer_inconsistent_expired_claim(
                delivery_id,
                observed_at=observed_at,
                next_recovery_at=next_recovery_at,
            )
            logger.warning(
                "Correspondence delivery watchdog found inconsistent evidence",
                extra={"delivery": str(delivery_id)},
            )
            continue

        if enqueue_details is None:
            continue
        from .tasks import enqueue_queued_delivery

        try:
            enqueued = enqueue_queued_delivery(
                enqueue_details["delivery_id"],
                attempt_count=enqueue_details["attempt_count"],
            )
        except Exception:  # watchdog records broker/cache failure
            notification_failures += 1
            logger.exception(
                "Correspondence delivery watchdog enqueue failed",
                extra={"delivery": enqueue_details["delivery_id"]},
            )
            continue
        if not enqueued:
            suppressed_nudges += 1
            continue
        audit(
            "correspondence.delivery_watchdog_nudged",
            target=enqueue_details["delivery_id"],
            correspondence_id=enqueue_details["correspondence_id"],
            delivery_id=enqueue_details["delivery_id"],
            delivery_status=enqueue_details["delivery_status"],
        )
        nudged_deliveries += 1

    return DeliveryWatchdogResult(
        expired_claims=expired_claims,
        nudged_deliveries=nudged_deliveries,
        suppressed_nudges=suppressed_nudges,
        notification_failures=notification_failures,
    )


def _require_single_clarification_claim(
    correspondence: Correspondence,
    delivery: CorrespondenceDelivery,
) -> None:
    if correspondence.kind not in CLARIFICATION_CYCLE_KINDS:
        return
    PublicRecordsRequest.objects.select_for_update(of=("self",)).get(
        pk=correspondence.request_id
    )
    if ClarificationCycle.objects.filter(
        request_id=correspondence.request_id,
        resolved_at__isnull=True,
        cancelled_at__isnull=True,
    ).exists():
        raise DeliveryNotClaimable(delivery.status)
    if Correspondence.objects.filter(
        request_id=correspondence.request_id,
        kind__in=CLARIFICATION_CYCLE_KINDS,
        status=Correspondence.Status.FAILED,
    ).exclude(pk=correspondence.pk).exists():
        raise DeliveryNotClaimable(delivery.status)
    first_queued_id = (
        Correspondence.objects.filter(
            request_id=correspondence.request_id,
            kind__in=CLARIFICATION_CYCLE_KINDS,
            status=Correspondence.Status.QUEUED,
        )
        .order_by("queued_at", "id")
        .values_list("pk", flat=True)
        .first()
    )
    if first_queued_id != correspondence.pk:
        raise DeliveryNotClaimable(delivery.status)


@transaction.atomic
def claim_delivery(
    delivery: CorrespondenceDelivery | str,
    *,
    actor,
    credential=None,
    now: dt.datetime | None = None,
    ip_address=None,
) -> DeliveryClaim:
    """Lease one frozen send or reconciliation action to the exact transport."""
    actor = require_exact_outbound_identity(actor, credential=credential)
    _require_outbound_transport_enabled()
    claimed_at = now or timezone.now()
    correspondence, delivery = _lock_correspondence_and_delivery(delivery)
    if correspondence.automated:
        _require_automated_outbound_enabled()
    _require_single_clarification_claim(correspondence, delivery)
    if (
        correspondence.request.status in CLOSED_STATUSES
        and delivery.status == CorrespondenceDelivery.Status.PENDING
    ):
        raise DeliveryNotClaimable(delivery.status)
    if (
        delivery.provider != correspondence.provider
        or delivery.provider_account != correspondence.provider_account
    ):
        raise DeliveryNotClaimable(delivery.status)
    _require_approved_recipient_snapshot(correspondence)
    require_approved_denial_basis_snapshot(correspondence, lock=True)

    if delivery.claim_expires_at is not None:
        if delivery.claim_expires_at > claimed_at:
            retry_after = int(
                (delivery.claim_expires_at - claimed_at).total_seconds()
            )
            raise DeliveryClaimConflict(retry_after)
        _transition_expired_claim_to_reconciliation(
            correspondence,
            delivery,
            now=claimed_at,
            actor=actor,
        )

    if delivery.status == CorrespondenceDelivery.Status.PENDING:
        action = CorrespondenceDeliveryAttempt.Action.SEND
        if correspondence.status != Correspondence.Status.QUEUED:
            raise DeliveryNotClaimable(delivery.status)
        _require_denial_delivery_exclusivity(
            correspondence,
            for_claim=True,
        )
        if any(
            not attachment.is_downloadable
            for attachment in correspondence.attachments.all()
        ):
            raise UnsafeDeliveryAttachment(
                "Every outbound attachment must remain clean and released."
            )
        _validate_for_approval(correspondence)
        denial_basis_evidence(correspondence, lock=True)
    elif delivery.status == CorrespondenceDelivery.Status.AWAITING_RECONCILIATION:
        action = CorrespondenceDeliveryAttempt.Action.RECONCILE
    else:
        raise DeliveryNotClaimable(delivery.status)

    lease_expires_at = claimed_at + dt.timedelta(
        seconds=runtime.get("PRR_CORRESPONDENCE_LEASE_SECONDS")
    )
    attempt_number = delivery.attempt_count + 1
    attempt = CorrespondenceDeliveryAttempt.objects.create(
        delivery=delivery,
        attempt_number=attempt_number,
        action=action,
        claim_owner=_claim_owner(actor),
        claimed_at=claimed_at,
        actor=actor,
    )
    delivery.status = (
        CorrespondenceDelivery.Status.CLAIMED
        if action == CorrespondenceDeliveryAttempt.Action.SEND
        else CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
    )
    delivery.claim_owner = _claim_owner(actor)
    delivery.claim_expires_at = lease_expires_at
    delivery.attempt_count = attempt_number
    delivery.version += 1
    delivery.save(
        update_fields=[
            "status",
            "claim_owner",
            "claim_expires_at",
            "attempt_count",
            "version",
        ]
    )
    payload = _claim_payload(
        correspondence=correspondence,
        delivery=delivery,
        attempt=attempt,
        lease_expires_at=lease_expires_at,
    )
    audit(
        "correspondence.delivery_claimed",
        target=str(delivery.id),
        actor=actor,
        ip_address=ip_address,
        delivery_id=str(delivery.id),
        correspondence_id=str(correspondence.id),
        attempt_id=str(attempt.id),
        attempt_number=attempt.attempt_number,
        claim_action=attempt.action,
        lease_expires_at=lease_expires_at.isoformat(),
    )
    return DeliveryClaim(
        delivery=delivery,
        attempt=attempt,
        action=action,
        lease_expires_at=lease_expires_at,
        payload=payload,
    )


def _finish_attempt(
    attempt: CorrespondenceDeliveryAttempt,
    *,
    result_at: dt.datetime,
    result_category: str,
    provider_message_id: str,
    provider_thread_id: str,
    provider_timestamp: dt.datetime | None,
    error_category: str,
    provider_metadata: dict,
    fingerprint: str,
) -> None:
    if result_at < attempt.claimed_at:
        raise ValidationError("A delivery result cannot predate its claim.")
    attempt.result_at = result_at
    attempt.result_category = result_category
    attempt.provider_message_id = provider_message_id
    attempt.provider_thread_id = provider_thread_id
    attempt.provider_timestamp = provider_timestamp
    attempt.error_category = error_category
    attempt.provider_response_metadata = provider_metadata
    attempt.result_fingerprint = fingerprint
    attempt.save(
        update_fields=[
            "result_at",
            "result_category",
            "provider_message_id",
            "provider_thread_id",
            "provider_timestamp",
            "error_category",
            "provider_response_metadata",
            "result_fingerprint",
        ]
    )


def _clear_claim(delivery: CorrespondenceDelivery) -> None:
    delivery.claim_owner = ""
    delivery.claim_expires_at = None


def _record_successful_result(
    *,
    correspondence: Correspondence,
    delivery: CorrespondenceDelivery,
    attempt: CorrespondenceDeliveryAttempt,
    provider_message_id: str,
    provider_thread_id: str,
    provider_timestamp: dt.datetime,
    provider_metadata: dict,
    fingerprint: str,
    now: dt.datetime,
) -> None:
    result_category = (
        CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED
        if attempt.action == CorrespondenceDeliveryAttempt.Action.RECONCILE
        else CorrespondenceDeliveryAttempt.ResultCategory.SENT
    )
    _finish_attempt(
        attempt,
        result_at=now,
        result_category=result_category,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        provider_timestamp=provider_timestamp,
        error_category="",
        provider_metadata=provider_metadata,
        fingerprint=fingerprint,
    )
    delivery.status = CorrespondenceDelivery.Status.SENT
    _clear_claim(delivery)
    delivery.successful_attempt_number = attempt.attempt_number
    delivery.last_failure_category = ""
    delivery.last_failure_description = ""
    delivery.provider_message_id = provider_message_id
    delivery.provider_thread_id = provider_thread_id
    delivery.completed_at = now
    delivery.version += 1
    delivery.save(
        update_fields=[
            "status",
            "claim_owner",
            "claim_expires_at",
            "successful_attempt_number",
            "last_failure_category",
            "last_failure_description",
            "provider_message_id",
            "provider_thread_id",
            "completed_at",
            "version",
        ]
    )
    correspondence.status = Correspondence.Status.SENT
    correspondence.sent_at = provider_timestamp
    correspondence.failed_at = None
    correspondence.provider_message_id = provider_message_id
    correspondence.provider_thread_id = provider_thread_id
    correspondence.failure_category = ""
    correspondence.failure_description = ""
    correspondence.version += 1
    correspondence.save(
        update_fields=[
            "status",
            "sent_at",
            "failed_at",
            "provider_message_id",
            "provider_thread_id",
            "failure_category",
            "failure_description",
            "version",
        ]
    )
    # The message has left the building; its delivery evidence must commit
    # even when the lifecycle effect cannot be applied. The effect runs in
    # its own savepoint so a refusal rolls back only the effect, and the
    # refusal becomes a Records Officer exception instead of a rollback that
    # reconciliation would repeat forever.
    try:
        with transaction.atomic():
            apply_successful_delivery_effects(correspondence)
    except Exception as exc:  # noqa: BLE001 - the officer sees the unapplied effect
        _surface_unapplied_delivery_effects(
            correspondence,
            delivery=delivery,
            attempt=attempt,
            error=exc,
        )


def _surface_unapplied_delivery_effects(
    correspondence: Correspondence,
    *,
    delivery: CorrespondenceDelivery,
    attempt: CorrespondenceDeliveryAttempt,
    error: Exception,
) -> None:
    from apps.work_queue.models import WorkItem
    from apps.work_queue.services import materialize_work_item

    logger.error(
        "Delivered correspondence lifecycle effect could not be applied",
        exc_info=error,
        extra={
            "correspondence": str(correspondence.pk),
            "delivery": str(delivery.pk),
        },
    )
    work_item = materialize_work_item(
        source_type=WorkItem.SourceType.CORRESPONDENCE,
        source_id=correspondence.pk,
        work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
        request=correspondence.request,
    ).work_item
    if isinstance(error, ValidationError):
        message = "; ".join(error.messages)
    else:
        message = f"{type(error).__name__}: {error}"
    audit(
        "correspondence.delivery_effects_failed",
        target=(
            correspondence.request.number
            if correspondence.request_id
            else str(correspondence.pk)
        ),
        correspondence_id=str(correspondence.pk),
        correspondence_kind=correspondence.kind,
        delivery_id=str(delivery.pk),
        attempt_id=str(attempt.pk),
        provider_message_id=correspondence.provider_message_id,
        work_item_id=str(work_item.pk),
        error=message[:1000],
    )
    request_id = correspondence.request_id
    correspondence_id = str(correspondence.pk)
    correspondence_kind = correspondence.kind

    def alert_records_officers():
        # Internal staff alert only; it never reaches the requester. It runs
        # after commit so it describes evidence that exists, and its own
        # failure can never undo that evidence.
        from apps.records import notifications

        try:
            notifications.notify_delivery_effects_failed(
                PublicRecordsRequest.objects.get(pk=request_id),
                correspondence_id=correspondence_id,
                correspondence_kind=correspondence_kind,
                error=message,
            )
        except Exception:
            logger.exception(
                "Unapplied delivery effect staff alert failed",
                extra={"correspondence": correspondence_id},
            )

    if request_id:
        transaction.on_commit(alert_records_officers)


def _record_failed_result(
    *,
    correspondence: Correspondence,
    delivery: CorrespondenceDelivery,
    attempt: CorrespondenceDeliveryAttempt,
    failure_category: str,
    description: str,
    retryable: bool,
    provider_metadata: dict,
    fingerprint: str,
    now: dt.datetime,
) -> None:
    _finish_attempt(
        attempt,
        result_at=now,
        result_category=CorrespondenceDeliveryAttempt.ResultCategory.FAILED,
        provider_message_id="",
        provider_thread_id="",
        provider_timestamp=None,
        error_category=failure_category,
        provider_metadata=provider_metadata,
        fingerprint=fingerprint,
    )
    delivery.status = (
        CorrespondenceDelivery.Status.RETRYABLE
        if retryable
        else CorrespondenceDelivery.Status.FAILED
    )
    _clear_claim(delivery)
    delivery.last_failure_category = failure_category
    delivery.last_failure_description = description
    delivery.completed_at = now
    delivery.version += 1
    delivery.save(
        update_fields=[
            "status",
            "claim_owner",
            "claim_expires_at",
            "last_failure_category",
            "last_failure_description",
            "completed_at",
            "version",
        ]
    )
    correspondence.status = Correspondence.Status.FAILED
    correspondence.failed_at = now
    correspondence.failure_category = failure_category
    correspondence.failure_description = description
    correspondence.version += 1
    correspondence.save(
        update_fields=[
            "status",
            "failed_at",
            "failure_category",
            "failure_description",
            "version",
        ]
    )


def _record_unknown_result(
    *,
    delivery: CorrespondenceDelivery,
    attempt: CorrespondenceDeliveryAttempt,
    provider_metadata: dict,
    fingerprint: str,
    now: dt.datetime,
) -> None:
    _finish_attempt(
        attempt,
        result_at=now,
        result_category=CorrespondenceDeliveryAttempt.ResultCategory.UNKNOWN,
        provider_message_id="",
        provider_thread_id="",
        provider_timestamp=None,
        error_category="outcome_unknown",
        provider_metadata=provider_metadata,
        fingerprint=fingerprint,
    )
    delivery.status = CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
    _clear_claim(delivery)
    delivery.last_failure_category = "outcome_unknown"
    delivery.last_failure_description = (
        "The provider outcome is unknown; reconcile by deterministic Message-ID."
    )
    delivery.completed_at = None
    delivery.recovery_not_before = (
        now
        + dt.timedelta(
            seconds=runtime.get("PRR_CORRESPONDENCE_STUCK_SECONDS"),
        )
    )
    delivery.version += 1
    delivery.save(
        update_fields=[
            "status",
            "claim_owner",
            "claim_expires_at",
            "last_failure_category",
            "last_failure_description",
            "completed_at",
            "recovery_not_before",
            "version",
        ]
    )


def _record_not_found_result(
    *,
    correspondence: Correspondence,
    delivery: CorrespondenceDelivery,
    attempt: CorrespondenceDeliveryAttempt,
    provider_metadata: dict,
    fingerprint: str,
    now: dt.datetime,
) -> None:
    category = "reconciliation_not_found"
    description = (
        "Gmail contains no message with the deterministic Message-ID; "
        "a human must queue the existing delivery for retry."
    )
    _finish_attempt(
        attempt,
        result_at=now,
        result_category=CorrespondenceDeliveryAttempt.ResultCategory.NOT_FOUND,
        provider_message_id="",
        provider_thread_id="",
        provider_timestamp=None,
        error_category=category,
        provider_metadata=provider_metadata,
        fingerprint=fingerprint,
    )
    delivery.status = CorrespondenceDelivery.Status.RETRYABLE
    _clear_claim(delivery)
    delivery.last_failure_category = category
    delivery.last_failure_description = description
    delivery.completed_at = now
    delivery.version += 1
    delivery.save(
        update_fields=[
            "status",
            "claim_owner",
            "claim_expires_at",
            "last_failure_category",
            "last_failure_description",
            "completed_at",
            "version",
        ]
    )
    correspondence.status = Correspondence.Status.FAILED
    correspondence.failed_at = now
    correspondence.failure_category = category
    correspondence.failure_description = description
    correspondence.version += 1
    correspondence.save(
        update_fields=[
            "status",
            "failed_at",
            "failure_category",
            "failure_description",
            "version",
        ]
    )


def _recorded_delivery_status(
    attempt: CorrespondenceDeliveryAttempt,
) -> str:
    if attempt.result_category in {
        CorrespondenceDeliveryAttempt.ResultCategory.SENT,
        CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED,
    }:
        return CorrespondenceDelivery.Status.SENT
    if attempt.result_category == CorrespondenceDeliveryAttempt.ResultCategory.FAILED:
        if attempt.error_category in RETRYABLE_DELIVERY_FAILURES:
            return CorrespondenceDelivery.Status.RETRYABLE
        return CorrespondenceDelivery.Status.FAILED
    if (
        attempt.result_category
        == CorrespondenceDeliveryAttempt.ResultCategory.NOT_FOUND
    ):
        return CorrespondenceDelivery.Status.RETRYABLE
    if attempt.result_category in {
        CorrespondenceDeliveryAttempt.ResultCategory.UNKNOWN,
        CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED,
    }:
        return CorrespondenceDelivery.Status.AWAITING_RECONCILIATION
    if (
        attempt.result_category
        == CorrespondenceDeliveryAttempt.ResultCategory.CANCELLED
    ):
        return CorrespondenceDelivery.Status.CANCELLED
    raise DeliveryResultConflict(
        "The completed attempt has no canonical delivery result.",
        code="delivery_result_conflict",
    )


def _validate_lifecycle_provider_timestamp(
    correspondence: Correspondence,
    provider_timestamp: dt.datetime,
) -> None:
    if correspondence.kind not in REQUEST_LIFECYCLE_EFFECT_KINDS:
        return
    prior_sent_at = (
        Correspondence.objects.filter(
            request_id=correspondence.request_id,
            kind__in=REQUEST_LIFECYCLE_EFFECT_KINDS,
            status=Correspondence.Status.SENT,
            sent_at__isnull=False,
        )
        .exclude(pk=correspondence.pk)
        .order_by("-sent_at", "-id")
        .values_list("sent_at", flat=True)
        .first()
    )
    latest_estimate_at = (
        EstimateRevision.objects.filter(
            request_id=correspondence.request_id,
            communicated_at__isnull=False,
        )
        .order_by("-communicated_at", "-pk")
        .values_list("communicated_at", flat=True)
        .first()
    )
    latest_clarification_at = (
        ClarificationCycle.objects.filter(
            request_id=correspondence.request_id,
            opened_at__isnull=False,
        )
        .order_by("-opened_at", "-pk")
        .values_list("opened_at", flat=True)
        .first()
    )
    latest_legacy_transition_at = (
        StatusEvent.objects.filter(
            request_id=correspondence.request_id,
            to_status__in=[
                RequestStatus.ACKNOWLEDGED,
                RequestStatus.CLARIFICATION,
            ],
        )
        .order_by("-occurred_at", "-pk")
        .values_list("occurred_at", flat=True)
        .first()
    )
    candidates = [
        correspondence.request.received_at,
        correspondence.request.acknowledgment_sent_at,
        prior_sent_at,
        latest_estimate_at,
        latest_clarification_at,
        latest_legacy_transition_at,
    ]
    earliest_legal_instant = max(
        value for value in candidates if value is not None
    )
    if provider_timestamp < earliest_legal_instant:
        raise ValidationError(
            "The provider timestamp predates the request or a prior lifecycle "
            "communication."
        )


@transaction.atomic
def report_delivery_result(
    delivery: CorrespondenceDelivery | str,
    *,
    actor,
    credential=None,
    attempt_id,
    outcome: str,
    provider_message_id: str = "",
    provider_thread_id: str = "",
    provider_timestamp: dt.datetime | None = None,
    failure_category: str = "",
    provider_metadata: dict | None = None,
    now: dt.datetime | None = None,
    ip_address=None,
) -> DeliveryResult:
    """Record one categorized transport result and apply first-success effects."""
    actor = require_exact_outbound_identity(actor, credential=credential)
    result_at = now or timezone.now()
    correspondence, delivery = _lock_correspondence_and_delivery(delivery)
    attempt = (
        CorrespondenceDeliveryAttempt.objects.select_for_update(of=("self",))
        .filter(delivery=delivery, pk=attempt_id)
        .first()
    )
    if attempt is None:
        raise DeliveryResultConflict(
            "The callback does not identify an attempt for this delivery.",
            code="delivery_attempt_mismatch",
        )
    if attempt.actor_id != actor.pk or attempt.claim_owner != _claim_owner(actor):
        raise DeliveryResultConflict(
            "The callback identity does not own this delivery attempt.",
            code="delivery_claim_owner_mismatch",
        )
    if result_at < attempt.claimed_at:
        raise ValidationError("A delivery result cannot predate its claim.")

    outcome = str(outcome or "")
    if outcome != outcome.strip():
        raise ValidationError("The result outcome must not contain outer whitespace.")
    if attempt.action == CorrespondenceDeliveryAttempt.Action.SEND:
        allowed_outcomes = {"sent", "failed", "unknown"}
        effective_outcome = outcome
    else:
        allowed_outcomes = {"sent", "reconciled", "not_found", "unknown"}
        effective_outcome = "reconciled" if outcome in {"sent", "reconciled"} else outcome
    if outcome not in allowed_outcomes:
        raise ValidationError("This result is not valid for the claimed action.")

    success = effective_outcome in {"sent", "reconciled"}
    provider_message_id = _validated_provider_identifier(
        provider_message_id,
        required=success,
        label="provider_message_id",
    )
    provider_thread_id = _validated_provider_identifier(
        provider_thread_id,
        required=success,
        label="provider_thread_id",
    )
    if not success and (provider_message_id or provider_thread_id):
        raise ValidationError(
            "Provider success identifiers are accepted only for successful results."
        )
    if success:
        if provider_timestamp is None or timezone.is_naive(provider_timestamp):
            raise ValidationError(
                "Successful results require an aware provider timestamp."
            )
        earliest = (
            attempt.claimed_at
            if attempt.action == CorrespondenceDeliveryAttempt.Action.SEND
            else delivery.queued_at
        ) - dt.timedelta(minutes=5)
        latest = result_at
        if not earliest <= provider_timestamp <= latest:
            raise ValidationError(
                "The provider timestamp falls outside the delivery evidence window."
            )
    elif provider_timestamp is not None:
        raise ValidationError(
            "Provider timestamps are accepted only for successful results."
        )

    safe_metadata = _safe_provider_metadata(provider_metadata)
    failure_category = str(failure_category or "")
    if failure_category != failure_category.strip():
        raise ValidationError(
            "The failure category must not contain outer whitespace."
        )
    if effective_outcome == "failed":
        if failure_category in RETRYABLE_DELIVERY_FAILURES:
            description = RETRYABLE_DELIVERY_FAILURES[failure_category]
            retryable = True
        elif failure_category in PERMANENT_DELIVERY_FAILURES:
            description = PERMANENT_DELIVERY_FAILURES[failure_category]
            retryable = False
        else:
            raise ValidationError("The failure category is not supported.")
    else:
        if failure_category:
            raise ValidationError(
                "Failure categories are accepted only for failed results."
            )
        description = ""
        retryable = False

    fingerprint = _result_fingerprint(
        outcome=effective_outcome,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
        provider_timestamp=provider_timestamp,
        failure_category=failure_category,
        provider_metadata=safe_metadata,
    )
    if attempt.result_at is not None:
        if (
            attempt.result_category
            == CorrespondenceDeliveryAttempt.ResultCategory.LEASE_EXPIRED
        ):
            raise DeliveryResultConflict(
                "This delivery lease expired and requires Message-ID reconciliation.",
                code="delivery_claim_expired",
            )
        if attempt.result_fingerprint == fingerprint:
            return DeliveryResult(
                delivery=delivery,
                attempt=attempt,
                status=_recorded_delivery_status(attempt),
                idempotent=True,
            )
        raise DeliveryResultConflict(
            "This attempt already has a different completed result.",
            code="delivery_result_conflict",
        )

    if success:
        _validate_lifecycle_provider_timestamp(
            correspondence,
            provider_timestamp,
        )

    if (
        delivery.claim_expires_at is not None
        and delivery.claim_expires_at <= result_at
    ):
        raise DeliveryResultConflict(
            "This delivery lease expired and requires Message-ID reconciliation.",
            code="delivery_claim_expired",
        )

    if (
        delivery.claim_owner != _claim_owner(actor)
        or delivery.claim_expires_at is None
        or attempt.attempt_number != delivery.attempt_count
    ):
        raise DeliveryResultConflict(
            "This is not the delivery's active attempt.",
            code="delivery_attempt_mismatch",
        )

    try:
        with transaction.atomic():
            if success:
                _record_successful_result(
                    correspondence=correspondence,
                    delivery=delivery,
                    attempt=attempt,
                    provider_message_id=provider_message_id,
                    provider_thread_id=provider_thread_id,
                    provider_timestamp=provider_timestamp,
                    provider_metadata=safe_metadata,
                    fingerprint=fingerprint,
                    now=result_at,
                )
            elif effective_outcome == "failed":
                _record_failed_result(
                    correspondence=correspondence,
                    delivery=delivery,
                    attempt=attempt,
                    failure_category=failure_category,
                    description=description,
                    retryable=retryable,
                    provider_metadata=safe_metadata,
                    fingerprint=fingerprint,
                    now=result_at,
                )
            elif effective_outcome == "not_found":
                _record_not_found_result(
                    correspondence=correspondence,
                    delivery=delivery,
                    attempt=attempt,
                    provider_metadata=safe_metadata,
                    fingerprint=fingerprint,
                    now=result_at,
                )
            else:
                _record_unknown_result(
                    delivery=delivery,
                    attempt=attempt,
                    provider_metadata=safe_metadata,
                    fingerprint=fingerprint,
                    now=result_at,
                )
    except IntegrityError as exc:
        raise DeliveryResultConflict(
            "The provider result conflicts with existing delivery evidence.",
            code="delivery_result_conflict",
        ) from exc

    audit(
        "correspondence.delivery_result_recorded",
        target=str(delivery.id),
        actor=actor,
        ip_address=ip_address,
        delivery_id=str(delivery.id),
        correspondence_id=str(correspondence.id),
        attempt_id=str(attempt.id),
        attempt_number=attempt.attempt_number,
        claim_action=attempt.action,
        outcome=effective_outcome,
        failure_category=failure_category,
        provider_message_id=provider_message_id,
        provider_thread_id=provider_thread_id,
    )
    return DeliveryResult(
        delivery=delivery,
        attempt=attempt,
        status=delivery.status,
        idempotent=False,
    )


def _result_event(prr, *, to_status: str = ""):
    events = StatusEvent.objects.filter(request=prr)
    if to_status:
        events = events.filter(to_status=to_status)
    return events.order_by("-occurred_at", "-pk").first()


def _record_acknowledgment_estimate_evidence(
    prr: PublicRecordsRequest,
    correspondence: Correspondence,
    *,
    actor,
) -> None:
    estimate = dt.date.fromisoformat(
        correspondence.approved_metadata["estimated_completion"]
    )
    reason = (
        "Initial estimate — system default applied by acknowledgment "
        "safety net; officer must review and refine."
        if correspondence.automated
        else "Initial estimate communicated with acknowledgment."
    )
    if prr.estimated_completion is None:
        EstimateRevision.objects.create(
            request=prr,
            previous_date=None,
            new_date=estimate,
            reason=reason,
            revised_by=actor,
            automated=correspondence.automated,
            correspondence=correspondence,
            communicated_at=correspondence.sent_at,
        )
        prr.estimated_completion = estimate
        prr.save(update_fields=["estimated_completion"])
        return
    if prr.estimated_completion != estimate:
        raise ValidationError(
            "Acknowledgment estimate evidence does not match the current "
            "request estimate."
        )

    revision = (
        EstimateRevision.objects.select_for_update(of=("self",))
        .filter(request=prr, new_date=estimate)
        .order_by("-created_at", "-pk")
        .first()
    )
    if revision is None:
        EstimateRevision.objects.create(
            request=prr,
            previous_date=None,
            new_date=estimate,
            reason=reason,
            revised_by=actor,
            automated=correspondence.automated,
            correspondence=correspondence,
            communicated_at=correspondence.sent_at,
        )
        return
    if revision.communicated_at is not None:
        return
    if revision.correspondence_id not in (None, correspondence.pk):
        raise ValidationError(
            "Uncommunicated estimate evidence is already bound to another "
            "correspondence."
        )
    revision.correspondence = correspondence
    revision.communicated_at = correspondence.sent_at
    revision.save(update_fields=["correspondence", "communicated_at"])


@transaction.atomic
def apply_successful_delivery_effects(
    correspondence: Correspondence,
) -> Correspondence:
    reference = Correspondence.objects.only("request_id").get(
        pk=correspondence.pk
    )
    prr = PublicRecordsRequest.objects.select_for_update(of=("self",)).get(
        pk=reference.request_id
    )
    correspondence = (
        Correspondence.objects.select_for_update(of=("self",))
        .select_related("request", "approved_by")
        .get(pk=correspondence.pk)
    )
    delivery = CorrespondenceDelivery.objects.select_for_update(of=("self",)).get(
        correspondence=correspondence
    )
    successful_attempt = None
    if delivery.successful_attempt_number is not None:
        successful_attempt = (
            CorrespondenceDeliveryAttempt.objects.select_for_update(
                of=("self",)
            )
            .filter(
                delivery=delivery,
                attempt_number=delivery.successful_attempt_number,
            )
            .first()
        )
    if (
        correspondence.status != Correspondence.Status.SENT
        or delivery.status != CorrespondenceDelivery.Status.SENT
        or correspondence.sent_at is None
        or delivery.attempt_count != delivery.successful_attempt_number
        or successful_attempt is None
        or successful_attempt.result_category
        not in {
            CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            CorrespondenceDeliveryAttempt.ResultCategory.RECONCILED,
        }
        or successful_attempt.result_at is None
        or successful_attempt.result_at != delivery.completed_at
        or not successful_attempt.result_fingerprint
        or successful_attempt.actor_id is None
        or successful_attempt.claim_owner != str(successful_attempt.actor_id)
        or successful_attempt.provider_message_id
        != delivery.provider_message_id
        or successful_attempt.provider_message_id
        != correspondence.provider_message_id
        or successful_attempt.provider_thread_id
        != delivery.provider_thread_id
        or successful_attempt.provider_thread_id
        != correspondence.provider_thread_id
        or successful_attempt.provider_timestamp != correspondence.sent_at
    ):
        raise ValidationError(
            "Legal effects require matching finalized provider evidence."
        )
    require_approved_denial_basis_snapshot(correspondence, lock=True)
    if correspondence.resulting_status_event_id:
        return Correspondence.objects.filter(
            pk=correspondence.pk
        )._validate_locked_resulting_status_event()

    if (
        correspondence.kind in REQUEST_LIFECYCLE_EFFECT_KINDS
        and prr.status in CLOSED_STATUSES
    ):
        raise ValidationError(
            "Requester-facing lifecycle effects cannot be applied after the "
            "request is closed."
        )
    event = None
    actor = correspondence.approved_by

    if correspondence.kind == Correspondence.Kind.ACKNOWLEDGMENT:
        _record_acknowledgment_estimate_evidence(
            prr,
            correspondence,
            actor=actor,
        )
        if prr.acknowledgment_sent_at is None:
            prr = record_acknowledgment(
                prr,
                actor=actor,
                note=(
                    "Statutory acknowledgment delivered automatically (safety net)."
                    if correspondence.automated
                    else "Statutory acknowledgment delivered."
                ),
                communicated_at=correspondence.sent_at,
                confirmed_correspondence_id=correspondence.pk,
            )
        event = _result_event(prr, to_status=RequestStatus.ACKNOWLEDGED)
        if event is None:
            event = _result_event(prr)
        if correspondence.automated:
            audit(
                "request.auto_acknowledged",
                target=prr.number,
                correspondence_id=str(correspondence.id),
                delivery_id=str(delivery.id),
            )

    elif correspondence.kind == Correspondence.Kind.CLARIFICATION_REQUEST:
        prr = transition(
            prr,
            RequestStatus.CLARIFICATION,
            actor=actor,
            note="Clarification request delivered to requester.",
            visible_to_requester=True,
            occurred_at=correspondence.sent_at,
            confirmed_correspondence_id=correspondence.pk,
        )
        event = _result_event(prr, to_status=RequestStatus.CLARIFICATION)
        ClarificationCycle.objects.get(
            request=prr,
            outbound_correspondence=correspondence,
            opened_at=correspondence.sent_at,
            opened_status_event=event,
        )

    elif correspondence.kind == Correspondence.Kind.SCOPE_CLARIFICATION:
        # Only the clarification form is an RCW 42.56.520(3) clarification
        # request, so only it stops the clock. An informational scope letter
        # is a message about the request, not a change to it, and it leaves
        # the request exactly where it was.
        if (
            correspondence.approved_metadata.get("form")
            == TOLLING_SCOPE_CLARIFICATION_FORM
        ):
            prr = transition(
                prr,
                RequestStatus.CLARIFICATION,
                actor=actor,
                note="Scope clarification request delivered to requester.",
                visible_to_requester=True,
                occurred_at=correspondence.sent_at,
                confirmed_correspondence_id=correspondence.pk,
            )
            event = _result_event(prr, to_status=RequestStatus.CLARIFICATION)
            ClarificationCycle.objects.get(
                request=prr,
                outbound_correspondence=correspondence,
                opened_at=correspondence.sent_at,
                opened_status_event=event,
            )

    elif correspondence.kind == Correspondence.Kind.ESTIMATE_REVISION:
        metadata = correspondence.approved_metadata
        reason = _validated_estimate_revision_reason(
            actor=actor,
            reason=metadata.get("reason", ""),
            automated=correspondence.automated,
            actor_previously_authorized=True,
        )
        new_date = dt.date.fromisoformat(metadata["new_date"])
        _revision, created = EstimateRevision.objects.get_or_create(
            correspondence=correspondence,
            defaults={
                "request": prr,
                "previous_date": prr.estimated_completion,
                "new_date": new_date,
                "reason": reason,
                "revised_by": actor,
                "automated": False,
                "communicated_at": correspondence.sent_at,
            },
        )
        if created:
            previous = prr.estimated_completion
            prr.estimated_completion = new_date
            prr.save(update_fields=["estimated_completion"])
            event = StatusEvent.objects.create(
                request=prr,
                actor=actor,
                note=(
                    f"Estimated completion revised to {new_date}. "
                    f"Reason: {reason}"
                ),
                visible_to_requester=True,
                occurred_at=correspondence.sent_at,
                correspondence_effect=(
                    StatusEvent.CorrespondenceEffect.ESTIMATE_REVISION_DELIVERY
                ),
            )
            audit(
                "request.estimate_revised",
                target=prr.number,
                actor=actor,
                previous=str(previous or ""),
                new=str(new_date),
                reason=reason,
                correspondence_id=str(correspondence.id),
            )
        else:
            event = correspondence.resulting_status_event

    elif correspondence.kind == Correspondence.Kind.INTERPRETATION_ESTIMATE:
        # The letter states an estimated completion date, so its delivery is
        # an estimate revision whose reason names the computed estimate the
        # date came from (#41); the RecordsEstimate row holds every input.
        metadata = correspondence.approved_metadata
        new_date = dt.date.fromisoformat(metadata["completion_date"])
        reason = (
            "Interpretation and estimate letter, from computed estimate "
            f"{metadata['estimate_id']}."
        )
        _revision, created = EstimateRevision.objects.get_or_create(
            correspondence=correspondence,
            defaults={
                "request": prr,
                "previous_date": prr.estimated_completion,
                "new_date": new_date,
                "reason": reason,
                "revised_by": actor,
                "automated": False,
                "communicated_at": correspondence.sent_at,
            },
        )
        if created:
            previous = prr.estimated_completion
            prr.estimated_completion = new_date
            prr.save(update_fields=["estimated_completion"])
            event = StatusEvent.objects.create(
                request=prr,
                actor=actor,
                note=f"Estimated completion set to {new_date}. Reason: {reason}",
                visible_to_requester=True,
                occurred_at=correspondence.sent_at,
                correspondence_effect=(
                    StatusEvent.CorrespondenceEffect.ESTIMATE_REVISION_DELIVERY
                ),
            )
            audit(
                "request.estimate_revised",
                target=prr.number,
                actor=actor,
                previous=str(previous or ""),
                new=str(new_date),
                reason=reason,
                correspondence_id=str(correspondence.id),
                estimate_id=metadata["estimate_id"],
            )
        else:
            event = correspondence.resulting_status_event

    elif correspondence.kind == Correspondence.Kind.DENIAL:
        if prr.status == RequestStatus.CLARIFICATION:
            raise ValidationError(
                "Resolve or close the open clarification before delivering "
                "a denial."
            )
        prr = transition(
            prr,
            RequestStatus.DENIED,
            actor=actor,
            note="Denial correspondence delivered to requester.",
            visible_to_requester=True,
            occurred_at=correspondence.sent_at,
            confirmed_correspondence_id=correspondence.pk,
        )
        event = _result_event(prr, to_status=RequestStatus.DENIED)

    elif correspondence.kind == Correspondence.Kind.NO_RECORDS_CLOSURE:
        attestation = require_adequate_search_snapshot(
            correspondence,
            lock=True,
        )
        prr = transition(
            prr,
            RequestStatus.CLOSED_NO_RECORDS,
            actor=actor,
            note=(
                "No responsive records closure delivered after a documented "
                f"reasonable search: {attestation.adequacy_reason}"
            ),
            visible_to_requester=True,
            occurred_at=correspondence.sent_at,
            confirmed_correspondence_id=correspondence.pk,
        )
        event = _result_event(prr, to_status=RequestStatus.CLOSED_NO_RECORDS)
        audit(
            "request.closed_no_records",
            target=prr.number,
            actor=actor,
            correspondence_id=str(correspondence.pk),
            attestation_id=str(attestation.pk),
            determination=attestation.determination,
            task_snapshot_sha256=attestation.task_snapshot_sha256,
        )

    elif correspondence.kind == Correspondence.Kind.COMPLETION_CLOSURE:
        summary = _validated_production_summary(
            correspondence.approved_metadata["production_summary"]
        )
        prr = transition(
            prr,
            RequestStatus.FULFILLED,
            actor=actor,
            note=f"Completion closure delivered to requester: {summary}",
            visible_to_requester=True,
            occurred_at=correspondence.sent_at,
        )
        event = _result_event(prr, to_status=RequestStatus.FULFILLED)
        audit(
            "request.closed_complete",
            target=prr.number,
            actor=actor,
            correspondence_id=str(correspondence.pk),
        )

    elif correspondence.kind == Correspondence.Kind.NONPAYMENT_CLOSURE:
        metadata = correspondence.approved_metadata
        prr = transition(
            prr,
            RequestStatus.CLOSED_ABANDONED,
            actor=actor,
            note=(
                "Non-payment closure delivered to requester "
                f"({NONPAYMENT_CLOSURE_REASON_CODE}): "
                f"{metadata['amount_due']} requested on "
                f"{metadata['payment_requested_on']} was not received."
            ),
            visible_to_requester=True,
            occurred_at=correspondence.sent_at,
        )
        event = _result_event(prr, to_status=RequestStatus.CLOSED_ABANDONED)
        audit(
            "request.closed_nonpayment",
            target=prr.number,
            actor=actor,
            correspondence_id=str(correspondence.pk),
            reason_code=NONPAYMENT_CLOSURE_REASON_CODE,
            amount_due=metadata["amount_due"],
            payment_requested_on=metadata["payment_requested_on"],
        )

    if event is not None:
        correspondence = Correspondence.objects.filter(
            pk=correspondence.pk
        )._bind_locked_resulting_status_event(status_event=event)
    return correspondence


@transaction.atomic
def resolve_clarification(
    cycle: ClarificationCycle,
    *,
    actor,
    resolution_notes: str,
) -> ClarificationCycle:
    return resolve_clarification_cycle(
        cycle,
        actor=actor,
        resolution_notes=resolution_notes,
    )
