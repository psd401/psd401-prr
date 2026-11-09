# ABOUTME: Staff correspondence REST primitives; serializers stay thin and
# ABOUTME: every mutation delegates to apps.correspondence.services.
import hashlib
import re
import secrets
from decimal import Decimal

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import (
    Case,
    CharField,
    Count,
    F,
    FloatField,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Concat, Replace, Substr
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils.crypto import salted_hmac
from django.utils.functional import cached_property
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    extend_schema_field,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.response import Response
from rest_framework.reverse import reverse

from apps.api.case_scope import scope_case_queryset
from apps.api.handoffs import human_only_handoff
from apps.api.identity import WORKFLOW_STAFF_ROLES, StaffRole, has_staff_role
from apps.api.pagination import ScopedKeysetPagination
from apps.api.resources.requests import (
    compute_initial_response_plan,
    safety_net_automatic_enabled,
)
from apps.api.resources.work_items import role_scoped_work_items
from apps.audit.models import audit
from apps.correspondence import services
from apps.correspondence.automatic_templates import (
    FREE_FORM_TEMPLATE_KINDS,
    MAX_NARROWING_OPTIONS,
)
from apps.correspondence.models import (
    ClarificationCycle,
    Correspondence,
    CorrespondenceAttachment,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
    CorrespondenceDenialCitation,
    CorrespondenceRecipient,
    CorrespondenceSearchChunk,
    InboundCorrespondenceReceipt,
)
from apps.correspondence.services import SCOPE_CLARIFICATION_FORMS
from apps.records.models import (
    AdequateSearchAttestation,
    Installment,
    PublicRecordsRequest,
    awaiting_initial_response,
)
from apps.records.services import CLOSED_STATUSES
from apps.work_queue.models import WorkItem, WorkItemEvent, WorkItemReceipt

DRAFT_ROLES = {StaffRole.RECORDS_OFFICER, StaffRole.LIAISON}
CORRESPONDENCE_VIEW_ROLES = {
    StaffRole.RECORDS_OFFICER,
    StaffRole.LIAISON,
}
MESSAGE_SNIPPET_LENGTH = 240
MESSAGE_SEARCH_MAX_LENGTH = 200
MESSAGE_SEARCH_MAX_ATOMS = 12
MESSAGE_SEARCH_MAX_GROUPS = 6
WEBSEARCH_TOKEN_RE = re.compile(
    r'(?P<negated>-)?(?:"(?P<phrase>[^"]+)"|(?P<word>\S+))'
)


def _actor_name(actor):
    return actor.username if actor else None


def _websearch_groups(term):
    """Split PostgreSQL websearch syntax into OR groups of document clauses."""
    groups = [[]]
    for match in WEBSEARCH_TOKEN_RE.finditer(term):
        phrase = match.group("phrase")
        word = match.group("word")
        atom = f'"{phrase}"' if phrase is not None else word
        if not atom:
            continue
        if phrase is None and atom.upper() == "OR" and groups[-1]:
            groups.append([])
            continue
        groups[-1].append((bool(match.group("negated")), atom))
    return [group for group in groups if group]


def _deduplicated_websearch_groups(term):
    """Remove boolean no-ops before building the PostgreSQL search query."""
    groups = []
    seen_groups = set()
    for parsed_group in _websearch_groups(term):
        group = []
        seen_clauses = set()
        for clause in parsed_group:
            if clause in seen_clauses:
                continue
            seen_clauses.add(clause)
            group.append(clause)
        group_key = tuple(group)
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        groups.append(group)
    return groups


def _accessible_request_queryset(user):
    queryset = PublicRecordsRequest.objects.select_related("requester")
    return scope_case_queryset(queryset, user)


def _validation_detail(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        messages = [
            message
            for field_messages in exc.message_dict.values()
            for message in field_messages
        ]
    else:
        messages = exc.messages
    return str(messages[0] if messages else "Correspondence validation failed.")


def _service_error(exc):
    if isinstance(exc, services.OutboundTransportDisabled):
        return Response(
            {
                "code": "outbound_transport_disabled",
                "detail": _validation_detail(exc),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if isinstance(exc, services.VersionConflict):
        return Response(
            {"code": "version_conflict", "detail": _validation_detail(exc)},
            status=status.HTTP_409_CONFLICT,
        )
    return Response(
        {"code": "validation_error", "detail": _validation_detail(exc)},
        status=status.HTTP_400_BAD_REQUEST,
    )


class RecipientInputSerializer(serializers.Serializer):
    recipient_type = serializers.ChoiceField(
        choices=CorrespondenceRecipient.RecipientType.choices,
    )
    email = serializers.EmailField()
    display_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
    )


class RecipientSerializer(serializers.ModelSerializer):
    requester_relationship = serializers.BooleanField(
        source="requester_id",
        read_only=True,
    )

    class Meta:
        model = CorrespondenceRecipient
        fields = [
            "id",
            "recipient_type",
            "email",
            "display_name",
            "requester_relationship",
            "created_at",
        ]


class CorrespondenceAttachmentSerializer(serializers.ModelSerializer):
    is_downloadable = serializers.BooleanField(read_only=True)

    class Meta:
        model = CorrespondenceAttachment
        fields = [
            "id",
            "original_filename",
            "content_type",
            "size",
            "sha256",
            "scan_status",
            "quarantined",
            "quarantine_reason",
            "is_downloadable",
            "created_at",
            "scanned_at",
        ]


class DenialExemptionCitationSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="exemption_id", read_only=True)
    citation = serializers.CharField(
        source="citation_snapshot",
        read_only=True,
    )
    short_name = serializers.CharField(
        source="short_name_snapshot",
        read_only=True,
    )
    description = serializers.CharField(
        source="description_snapshot",
        read_only=True,
    )
    active = serializers.BooleanField(
        source="exemption.active",
        read_only=True,
    )
    requires_second_review = serializers.BooleanField(
        source="requires_second_review_snapshot",
        read_only=True,
    )

    class Meta:
        model = CorrespondenceDenialCitation
        fields = [
            "id",
            "citation",
            "short_name",
            "description",
            "active",
            "requires_second_review",
        ]


class DeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = CorrespondenceDelivery
        fields = [
            "id",
            "status",
            "provider",
            "provider_account",
            "claim_expires_at",
            "attempt_count",
            "successful_attempt_number",
            "last_failure_category",
            "last_failure_description",
            "provider_message_id",
            "provider_thread_id",
            "queued_at",
            "completed_at",
            "version",
        ]


class DeliveryAttemptSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    class Meta:
        model = CorrespondenceDeliveryAttempt
        fields = [
            "id",
            "attempt_number",
            "claim_owner",
            "claimed_at",
            "result_at",
            "result_category",
            "provider_message_id",
            "provider_thread_id",
            "provider_timestamp",
            "error_category",
            "actor",
            "created_at",
        ]

    def get_actor(self, obj) -> str | None:
        return _actor_name(obj.actor)


class CorrespondenceListSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    subject = serializers.CharField(source="summary_subject", read_only=True)
    snippet = serializers.SerializerMethodField()
    recipient_count = serializers.IntegerField(read_only=True)
    attachment_count = serializers.IntegerField(read_only=True)
    delivery_status = serializers.SerializerMethodField()
    matching_status = serializers.SerializerMethodField()
    inbound_receipt_id = serializers.SerializerMethodField()
    work_item = serializers.SerializerMethodField()
    case_context = serializers.SerializerMethodField()

    class Meta:
        model = Correspondence
        fields = [
            "id",
            "request_number",
            "direction",
            "kind",
            "status",
            "subject",
            "snippet",
            "automated",
            "created_at",
            "approved_at",
            "queued_at",
            "sent_at",
            "received_at",
            "matched_at",
            "failed_at",
            "cancelled_at",
            "failure_category",
            "version",
            "recipient_count",
            "attachment_count",
            "delivery_status",
            "matching_status",
            "resulting_status_event_id",
            "inbound_receipt_id",
            "work_item",
            "case_context",
        ]

    @extend_schema_field(serializers.CharField())
    def get_snippet(self, obj) -> str:
        snippet = getattr(obj, "summary_snippet", "") or ""
        return (
            f"{snippet[:MESSAGE_SNIPPET_LENGTH]}…"
            if len(snippet) > MESSAGE_SNIPPET_LENGTH
            else snippet
        )

    @extend_schema_field(
        serializers.ChoiceField(
            choices=CorrespondenceDelivery.Status.choices,
            allow_null=True,
        )
    )
    def get_delivery_status(self, obj):
        try:
            return obj.delivery.status
        except CorrespondenceDelivery.DoesNotExist:
            return None

    @extend_schema_field(
        serializers.ChoiceField(
            choices=InboundCorrespondenceReceipt.MatchingStatus.choices,
            allow_null=True,
        )
    )
    def get_matching_status(self, obj):
        receipts = getattr(obj, "_matched_inbound_receipts", ())
        return receipts[0].matching_status if receipts else None

    @cached_property
    def _viewer_is_records_officer(self):
        """Resolve the officer check once per response, not once per row.

        ``staff_roles`` reads the viewer's managed groups from the database on
        every call, so asking per row turned a 50-row Inbox page into 50
        identical group lookups. One serializer instance serves exactly one
        response, so caching no wider than this leaves the fail-closed
        per-request session revalidation authoritative: roles are re-read on
        the next request, they simply are not re-read between two rows of the
        same page.
        """
        request = self.context.get("request")
        return request is not None and has_staff_role(
            request.user,
            StaffRole.RECORDS_OFFICER,
        )

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_inbound_receipt_id(self, obj):
        if not self._viewer_is_records_officer:
            return None
        receipts = getattr(obj, "_matched_inbound_receipts", ())
        return receipts[0].pk if receipts else None

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_case_context(self, obj):
        """Bounded case state so a row can be triaged without opening it.

        Legal status and the statutory response state are case metadata, not
        requester identity: nothing here is drawn from the requester record or
        from message bodies. It is attached in one batched pass by the view,
        never queried per row.
        """
        return getattr(obj, "_case_context", None)

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_work_item(self, obj):
        work_item = getattr(obj, "_message_work_item", None)
        if work_item is None:
            return None
        receipt = getattr(work_item, "_message_receipt", None)
        return {
            "id": str(work_item.pk),
            "work_kind": work_item.work_kind,
            "assignee_id": work_item.assignee_id,
            "assignee_username": (
                work_item.assignee.username if work_item.assignee else None
            ),
            "disposition": work_item.disposition,
            "version": work_item.version,
            "is_read": bool(receipt and receipt.read_at),
            "read_at": receipt.read_at if receipt else None,
        }


class CorrespondenceDetailSerializer(CorrespondenceListSerializer):
    created_by = serializers.SerializerMethodField()
    approved_by = serializers.SerializerMethodField()
    queued_by = serializers.SerializerMethodField()
    matched_by = serializers.SerializerMethodField()
    received_sender = serializers.CharField(read_only=True)
    received_recipients = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    resulting_status = serializers.CharField(
        source="resulting_status_event.to_status",
        read_only=True,
        allow_null=True,
    )
    recipients = RecipientSerializer(many=True, read_only=True)
    attachments = CorrespondenceAttachmentSerializer(many=True, read_only=True)
    denial_exemption_citations = serializers.SerializerMethodField()
    delivery = serializers.SerializerMethodField()

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_work_item(self, obj):
        compact = super().get_work_item(obj)
        work_item = getattr(obj, "_message_work_item", None)
        if compact is None or work_item is None:
            return None
        compact.update(
            {
                "disposed_at": work_item.disposed_at,
                "disposed_by_username": (
                    work_item.disposed_by.username
                    if work_item.disposed_by
                    else None
                ),
                "no_action_category": work_item.no_action_category,
                "no_action_reason": work_item.no_action_reason,
                "no_action_by_username": (
                    work_item.no_action_by.username
                    if work_item.no_action_by
                    else None
                ),
                "no_action_at": work_item.no_action_at,
                "events": [
                    {
                        "id": str(event.pk),
                        "event_type": event.event_type,
                        "actor_username": (
                            event.actor.username if event.actor else None
                        ),
                        "occurred_at": event.occurred_at,
                        "resulting_version": event.resulting_version,
                        "metadata": event.metadata,
                    }
                    for event in getattr(work_item, "_message_events", ())
                ],
            }
        )
        return compact

    class Meta(CorrespondenceListSerializer.Meta):
        fields = [*CorrespondenceListSerializer.Meta.fields, "draft_subject", "draft_body", "draft_metadata", "approved_subject", "approved_body", "approved_metadata", "received_subject", "received_body", "received_sender", "received_recipients", "template_identifier", "template_version", "created_by", "approved_by", "queued_by", "matched_by", "rfc_message_id", "provider", "provider_account", "provider_message_id", "provider_thread_id", "failure_description", "replaces_id", "resulting_status", "recipients", "attachments", "denial_exemption_citations", "delivery"]

    def get_created_by(self, obj) -> str | None:
        return _actor_name(obj.created_by)

    def get_approved_by(self, obj) -> str | None:
        return _actor_name(obj.approved_by)

    def get_queued_by(self, obj) -> str | None:
        return _actor_name(obj.queued_by)

    def get_matched_by(self, obj) -> str | None:
        return _actor_name(obj.matched_by)

    @extend_schema_field(DenialExemptionCitationSerializer(many=True))
    def get_denial_exemption_citations(self, obj):
        return DenialExemptionCitationSerializer(
            obj.denial_citations.all(),
            many=True,
        ).data

    @extend_schema_field(DeliverySerializer(allow_null=True))
    def get_delivery(self, obj):
        try:
            delivery = obj.delivery
        except CorrespondenceDelivery.DoesNotExist:
            return None
        return DeliverySerializer(delivery).data


class RequestDraftSerializer(serializers.Serializer):
    request_number = serializers.CharField(max_length=20)


class AuthoredDraftSerializer(RequestDraftSerializer):
    subject = serializers.CharField(max_length=998)
    body = serializers.CharField()
    recipients = RecipientInputSerializer(many=True, required=False)


class DenialDraftSerializer(AuthoredDraftSerializer):
    exemption_citation_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )


class TemplatePrefillSerializer(serializers.Serializer):
    request_number = serializers.CharField(max_length=40)
    kind = serializers.ChoiceField(choices=sorted(FREE_FORM_TEMPLATE_KINDS))
    name = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=120,
        help_text=(
            "Which named starting point to render. Blank is the district "
            "default; only general correspondence has more than one."
        ),
    )


#: Each free-form kind's drafting aid answers to the role that may create the
#: draft itself. ``test_prefill_role_matches_creation_role`` keeps this
#: complete as free-form kinds are added.
PREFILL_CREATE_ACTIONS = {
    Correspondence.Kind.GENERAL: "create_general_draft",
    Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE: "create_responsive_records_draft",
    Correspondence.Kind.CLOSURE: "create_closure_draft",
    Correspondence.Kind.DENIAL: "create_denial_draft",
}


class AcknowledgmentDraftSerializer(RequestDraftSerializer):
    estimated_completion = serializers.DateField(required=False)


class ClarificationDraftSerializer(RequestDraftSerializer):
    clarification_questions = serializers.CharField()
    clarification_due = serializers.DateField()


class NarrowingOptionSerializer(serializers.Serializer):
    terms = serializers.ListField(
        child=serializers.CharField(max_length=200),
        min_length=1,
        max_length=10,
    )
    rationale = serializers.CharField(max_length=500)


class ScopeClarificationFieldsSerializer(serializers.Serializer):
    #: Which form this is decides whether delivery tolls the statutory clock,
    #: so it is an explicit officer choice with no default.
    form = serializers.ChoiceField(choices=SCOPE_CLARIFICATION_FORMS)
    match_count = serializers.IntegerField(min_value=0)
    query_summary = serializers.CharField(max_length=500)
    narrowing_options = serializers.ListField(
        child=NarrowingOptionSerializer(),
        max_length=MAX_NARROWING_OPTIONS,
        allow_empty=True,
    )
    response_by = serializers.DateField()


class ScopeClarificationDraftSerializer(
    RequestDraftSerializer,
    ScopeClarificationFieldsSerializer,
):
    pass


class EstimateRevisionDraftSerializer(RequestDraftSerializer):
    new_date = serializers.DateField()
    reason = serializers.CharField()


class InstallmentDraftSerializer(RequestDraftSerializer):
    #: No download URL. The server derives the link from the installment's
    #: own scoped token, so neither a human nor an agent can point a
    #: requester notice at anything else.
    installment_id = serializers.IntegerField(min_value=1)
    is_final = serializers.BooleanField()


class AdequateSearchStepSerializer(serializers.Serializer):
    location = serializers.CharField(max_length=1000)
    custodian = serializers.CharField(max_length=1000)
    method = serializers.CharField(max_length=1000)
    date_range = serializers.CharField(max_length=1000)
    result = serializers.CharField(max_length=1000)


class NoRecordsClosureDraftSerializer(RequestDraftSerializer):
    determination = serializers.ChoiceField(
        choices=AdequateSearchAttestation.Determination.choices,
    )
    search_completed_at = serializers.DateTimeField()
    search_steps = AdequateSearchStepSerializer(many=True, allow_empty=False)
    adequacy_reason = serializers.CharField(min_length=20, max_length=2000)
    retention_schedule_reference = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
    )
    incident_hold_checked = serializers.BooleanField(default=False)
    prior_exports_checked = serializers.BooleanField(default=False)
    email_drive_copies_checked = serializers.BooleanField(default=False)
    other_copies_checked = serializers.BooleanField(default=False)
    counsel_approval_reference = serializers.CharField(
        min_length=10,
        max_length=1000,
    )


class FeeEstimateDraftSerializer(RequestDraftSerializer):
    estimated_amount = serializers.DecimalField(
        max_digits=9,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    estimate_basis = serializers.CharField(min_length=20, max_length=2000)
    deposit_amount = serializers.DecimalField(
        max_digits=9,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
        allow_null=True,
    )


class CompletionClosureDraftSerializer(RequestDraftSerializer):
    production_summary = serializers.CharField(min_length=20, max_length=2000)


class NonpaymentClosureDraftSerializer(RequestDraftSerializer):
    amount_due = serializers.DecimalField(
        max_digits=9,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    payment_requested_on = serializers.DateField()


class ThirdPartyNoticeDraftSerializer(RequestDraftSerializer):
    employee_name = serializers.CharField(min_length=2, max_length=255)
    employee_email = serializers.EmailField()
    attorney_name = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
    )
    attorney_email = serializers.EmailField(
        required=False,
        allow_blank=True,
    )


class BatchOutcomeSerializer(serializers.Serializer):
    """What the batch did, case by case, so nothing is silently skipped."""

    batch_id = serializers.CharField()
    succeeded = serializers.ListField(child=serializers.DictField())
    failed = serializers.ListField(child=serializers.DictField())


class BatchClosureDraftSerializer(serializers.Serializer):
    request_numbers = serializers.ListField(
        child=serializers.CharField(max_length=40),
        allow_empty=False,
        max_length=services.BATCH_MAX_CASES,
    )
    kind = serializers.ChoiceField(
        choices=sorted(services.BATCH_DRAFTABLE_KINDS),
    )
    confirmed = serializers.BooleanField()
    production_summary = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2000,
    )
    subject = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=998,
    )
    body = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )


class BatchApproveAndSendSerializer(serializers.Serializer):
    correspondence_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        max_length=services.BATCH_MAX_CASES,
    )
    confirmed = serializers.BooleanField()


class StrictEditSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = set(data.keys()) - set(self.fields)
            if unknown:
                raise serializers.ValidationError(
                    {
                        field: ["This field is not valid for this draft kind."]
                        for field in sorted(unknown)
                    }
                )
        return super().to_internal_value(data)


class EditDraftSerializer(StrictEditSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    subject = serializers.CharField(max_length=998)
    body = serializers.CharField()
    recipients = RecipientInputSerializer(many=True, required=False)


class DenialEditSerializer(EditDraftSerializer):
    exemption_citation_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )


class StructuredEditSerializer(StrictEditSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    recipients = RecipientInputSerializer(many=True, required=False)


class AcknowledgmentEditSerializer(StructuredEditSerializer):
    estimated_completion = serializers.DateField()


class ClarificationEditSerializer(StructuredEditSerializer):
    clarification_questions = serializers.CharField()
    clarification_due = serializers.DateField()


class ScopeClarificationEditSerializer(
    StructuredEditSerializer,
    ScopeClarificationFieldsSerializer,
):
    pass


class EstimateRevisionEditSerializer(StructuredEditSerializer):
    new_date = serializers.DateField()
    reason = serializers.CharField()


class InterpretationEstimateEditSerializer(StructuredEditSerializer):
    # Only the interpretation is editable; the count, charge, and date are
    # the stored estimate's.
    interpretation = serializers.CharField()


class InstallmentEditSerializer(StructuredEditSerializer):
    is_final = serializers.BooleanField()


STRUCTURED_EDIT_CONTRACTS = {
    Correspondence.Kind.ACKNOWLEDGMENT: (
        AcknowledgmentEditSerializer,
        ("estimated_completion",),
    ),
    Correspondence.Kind.CLARIFICATION_REQUEST: (
        ClarificationEditSerializer,
        ("clarification_questions", "clarification_due"),
    ),
    Correspondence.Kind.SCOPE_CLARIFICATION: (
        ScopeClarificationEditSerializer,
        (
            "form",
            "match_count",
            "query_summary",
            "narrowing_options",
            "response_by",
        ),
    ),
    Correspondence.Kind.ESTIMATE_REVISION: (
        EstimateRevisionEditSerializer,
        ("new_date", "reason"),
    ),
    Correspondence.Kind.INTERPRETATION_ESTIMATE: (
        InterpretationEstimateEditSerializer,
        ("interpretation",),
    ),
    Correspondence.Kind.INSTALLMENT_NOTICE: (
        InstallmentEditSerializer,
        ("is_final",),
    ),
}


_EDIT_RECIPIENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["recipient_type", "email"],
        "properties": {
            "recipient_type": {
                "type": "string",
                "enum": ["to", "cc", "bcc"],
            },
            "email": {"type": "string", "format": "email"},
            "display_name": {
                "type": "string",
                "maxLength": 255,
            },
        },
    },
}
_EDIT_EXPECTED_VERSION_SCHEMA = {
    "type": "integer",
    "minimum": 1,
}


def _edit_request_shape(*, title, required, properties):
    return {
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": ["expected_version", *required],
        "properties": {
            "expected_version": _EDIT_EXPECTED_VERSION_SCHEMA,
            "recipients": _EDIT_RECIPIENTS_SCHEMA,
            **properties,
        },
    }


CORRESPONDENCE_DRAFT_EDIT_REQUEST_SCHEMA = {
    "oneOf": [
        _edit_request_shape(
            title="Authored correspondence draft edit",
            required=["subject", "body"],
            properties={
                "subject": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 998,
                },
                "body": {"type": "string", "minLength": 1},
            },
        ),
        _edit_request_shape(
            title="Denial correspondence draft edit",
            required=[
                "subject",
                "body",
                "exemption_citation_ids",
            ],
            properties={
                "subject": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 998,
                },
                "body": {"type": "string", "minLength": 1},
                "exemption_citation_ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
        ),
        _edit_request_shape(
            title="Acknowledgment draft edit",
            required=["estimated_completion"],
            properties={
                "estimated_completion": {
                    "type": "string",
                    "format": "date",
                }
            },
        ),
        _edit_request_shape(
            title="Clarification draft edit",
            required=["clarification_questions", "clarification_due"],
            properties={
                "clarification_questions": {
                    "type": "string",
                    "minLength": 1,
                },
                "clarification_due": {
                    "type": "string",
                    "format": "date",
                },
            },
        ),
        _edit_request_shape(
            title="Estimate-revision draft edit",
            required=["new_date", "reason"],
            properties={
                "new_date": {
                    "type": "string",
                    "format": "date",
                },
                "reason": {"type": "string", "minLength": 1},
            },
        ),
        _edit_request_shape(
            title="Scope-conversation draft edit",
            required=[
                "form",
                "match_count",
                "query_summary",
                "narrowing_options",
                "response_by",
            ],
            properties={
                "form": {
                    "type": "string",
                    "enum": list(SCOPE_CLARIFICATION_FORMS),
                },
                "match_count": {"type": "integer", "minimum": 0},
                "query_summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                },
                "narrowing_options": {
                    "type": "array",
                    "maxItems": MAX_NARROWING_OPTIONS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["terms", "rationale"],
                        "properties": {
                            "terms": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 10,
                                "items": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 200,
                                },
                            },
                            "rationale": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500,
                            },
                        },
                    },
                },
                "response_by": {"type": "string", "format": "date"},
            },
        ),
        _edit_request_shape(
            title="Installment draft edit",
            required=["is_final"],
            properties={
                "is_final": {"type": "boolean"},
            },
        ),
    ]
}


class ExpectedVersionSerializer(serializers.Serializer):
    expected_version = serializers.IntegerField(min_value=1)


class CancelSerializer(ExpectedVersionSerializer):
    reason = serializers.CharField()


class RetrySerializer(serializers.Serializer):
    expected_delivery_version = serializers.IntegerField(min_value=1)


class PreviewSerializer(serializers.Serializer):
    correspondence_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=Correspondence.Status.choices)
    version = serializers.IntegerField()
    subject = serializers.CharField()
    body = serializers.CharField()
    recipients = RecipientSerializer(many=True)
    attachments = CorrespondenceAttachmentSerializer(many=True)
    denial_exemption_citations = DenialExemptionCitationSerializer(many=True)


class AttachmentDownloadAuthorizationSerializer(serializers.Serializer):
    url = serializers.CharField()
    expires_in = serializers.IntegerField()


class CorrespondenceFilterSerializer(serializers.Serializer):
    request = serializers.CharField(required=False, max_length=20)
    direction = serializers.ChoiceField(
        choices=Correspondence.Direction.choices,
        required=False,
    )
    kind = serializers.ChoiceField(
        choices=Correspondence.Kind.choices,
        required=False,
    )
    status = serializers.ChoiceField(
        choices=Correspondence.Status.choices,
        required=False,
    )
    work_state = serializers.ChoiceField(
        # "unresolved" is Needs attention or Waiting together: the rows an
        # officer still owes something on, whichever of the two they are in.
        choices=[*WorkItem.Disposition.choices, ("unresolved", "Unresolved")],
        required=False,
    )
    work_kind = serializers.MultipleChoiceField(
        choices=WorkItem.WorkKind.choices,
        required=False,
    )
    read_state = serializers.ChoiceField(
        choices=["read", "unread"],
        required=False,
    )
    delivery_state = serializers.ChoiceField(
        choices=CorrespondenceDelivery.Status.choices,
        required=False,
    )
    # Whether the message's case is still open. Every row here already has a
    # case (_authorized_queryset requires one); unmatched mail is its own view.
    case_state = serializers.ChoiceField(
        choices=["open", "closed"],
        required=False,
    )
    matching_state = serializers.ChoiceField(
        choices=InboundCorrespondenceReceipt.MatchingStatus.choices,
        required=False,
    )
    # QueryDict is treated as HTML input by DRF, where an omitted checkbox
    # otherwise becomes false. None preserves the distinction between an
    # absent filter and an explicit `has_attachments=false` query.
    has_attachments = serializers.BooleanField(
        required=False,
        allow_null=True,
        default=None,
    )
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    assignee = serializers.IntegerField(required=False, min_value=1)
    sort = serializers.ChoiceField(
        choices=["newest", "oldest"],
        required=False,
        default="newest",
    )

    def validate(self, attrs):
        if (
            attrs.get("date_from")
            and attrs.get("date_to")
            and attrs["date_from"] > attrs["date_to"]
        ):
            raise serializers.ValidationError(
                {"date_to": ["End date must be on or after start date."]}
            )
        return attrs


class CorrespondenceSearchSerializer(CorrespondenceFilterSerializer):
    request = serializers.CharField(max_length=20)
    query = serializers.CharField(
        min_length=1,
        max_length=MESSAGE_SEARCH_MAX_LENGTH,
        trim_whitespace=True,
    )
    cursor = serializers.CharField(required=False, max_length=4096)

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = set(data.keys()) - set(self.fields)
            if unknown:
                raise serializers.ValidationError(
                    {
                        field: ["This field is not accepted."]
                        for field in sorted(unknown)
                    }
                )
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        groups = _websearch_groups(attrs["query"])
        if not groups:
            raise serializers.ValidationError(
                {"query": ["Enter at least one searchable term."]}
            )
        if len(groups) > MESSAGE_SEARCH_MAX_GROUPS:
            raise serializers.ValidationError(
                {
                    "query": [
                        f"Use no more than {MESSAGE_SEARCH_MAX_GROUPS} OR groups."
                    ]
                }
            )
        atom_count = sum(len(group) for group in groups)
        if atom_count > MESSAGE_SEARCH_MAX_ATOMS:
            raise serializers.ValidationError(
                {
                    "query": [
                        f"Use no more than {MESSAGE_SEARCH_MAX_ATOMS} search terms."
                    ]
                }
            )
        return attrs


class CorrespondenceThreadSerializer(serializers.Serializer):
    id = serializers.CharField()
    evidence_id = serializers.RegexField(r"^[0-9a-f]{64}$")
    message_count = serializers.IntegerField(min_value=1)
    latest_at = serializers.DateTimeField()
    subject = serializers.CharField()


class CorrespondenceViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = CorrespondenceListSerializer
    pagination_class = ScopedKeysetPagination
    http_method_names = ["get", "post", "patch", "head", "options"]
    allow_readonly_agent = False
    role_permissions = {
        "list": CORRESPONDENCE_VIEW_ROLES,
        "retrieve": CORRESPONDENCE_VIEW_ROLES,
        "search": CORRESPONDENCE_VIEW_ROLES,
        "threads": CORRESPONDENCE_VIEW_ROLES,
        "thread_messages": CORRESPONDENCE_VIEW_ROLES,
        "partial_update": DRAFT_ROLES,
        "delete_draft": DRAFT_ROLES,
        "preview": CORRESPONDENCE_VIEW_ROLES,
        "attempts": CORRESPONDENCE_VIEW_ROLES,
        "create_general_draft": DRAFT_ROLES,
        "create_submission_receipt_draft": DRAFT_ROLES,
        "template_prefill": DRAFT_ROLES,
        "create_acknowledgment_draft": DRAFT_ROLES,
        "create_clarification_draft": DRAFT_ROLES,
        "create_estimate_revision_draft": DRAFT_ROLES,
        "create_scope_clarification_draft": {StaffRole.RECORDS_OFFICER},
        "create_installment_draft": {StaffRole.RECORDS_OFFICER},
        "create_responsive_records_draft": DRAFT_ROLES,
        "create_closure_draft": {StaffRole.RECORDS_OFFICER},
        "create_no_records_closure_draft": {StaffRole.RECORDS_OFFICER},
        "create_fee_estimate_draft": {StaffRole.RECORDS_OFFICER},
        "create_completion_closure_draft": {StaffRole.RECORDS_OFFICER},
        "create_nonpayment_closure_draft": {StaffRole.RECORDS_OFFICER},
        "create_third_party_notice_draft": {StaffRole.RECORDS_OFFICER},
        "create_denial_draft": {StaffRole.RECORDS_OFFICER},
        "batch_closure_drafts": {StaffRole.RECORDS_OFFICER},
        "batch_approve_and_send": {StaffRole.RECORDS_OFFICER},
        "approve": {StaffRole.RECORDS_OFFICER},
        "queue": {StaffRole.RECORDS_OFFICER},
        "approve_and_send": {StaffRole.RECORDS_OFFICER},
        "cancel": {StaffRole.RECORDS_OFFICER},
        "retry": {StaffRole.RECORDS_OFFICER},
    }
    pagination_cursor_in_body = False

    @staticmethod
    def _summary_subject_expression():
        return Case(
            When(
                direction=Correspondence.Direction.INBOUND,
                then=F("received_subject"),
            ),
            When(
                status__in=Correspondence.SNAPSHOT_STATUSES,
                then=F("approved_subject"),
            ),
            default=F("draft_subject"),
            output_field=CharField(),
        )

    @staticmethod
    def _summary_body_expression():
        return Case(
            When(
                direction=Correspondence.Direction.INBOUND,
                then=F("received_body"),
            ),
            When(
                status__in=Correspondence.SNAPSHOT_STATUSES,
                then=F("approved_body"),
            ),
            default=F("draft_body"),
            output_field=TextField(),
        )

    @staticmethod
    def _matched_receipt_prefetch():
        return Prefetch(
            "inbound_receipts",
            queryset=InboundCorrespondenceReceipt.objects.only(
                "id",
                "matched_correspondence_id",
                "matching_status",
            ).order_by("-matched_at", "id"),
            to_attr="_matched_inbound_receipts",
        )

    def _validated_filters(self):
        if hasattr(self, "_message_filters"):
            return self._message_filters
        if self.request.query_params.get("search"):
            raise ParseError(
                "Message content search requires POST /correspondence/search/."
            )
        serializer = CorrespondenceFilterSerializer(
            data=self.request.query_params,
        )
        serializer.is_valid(raise_exception=True)
        self._message_filters = dict(serializer.validated_data)
        if not self._message_filters.get("work_kind"):
            self._message_filters.pop("work_kind", None)
        return self._message_filters

    def _authorized_queryset(self, filters):
        queryset = Correspondence.objects.filter(request__isnull=False)
        user = self.request.user
        queryset = scope_case_queryset(
            queryset,
            user,
            request_prefix="request",
        )

        request_number = filters.get("request")
        self._pagination_request_id = None
        if request_number:
            accessible_request_id = _accessible_request_queryset(user).filter(
                number=request_number
            ).values_list("id", flat=True).first()
            if accessible_request_id is None:
                queryset = queryset.none()
                self._pagination_request_id = "inaccessible"
            else:
                # A merged duplicate's sent correspondence stays on the closed
                # case it was written for; the case of record shows it too, so
                # the requester's whole exchange reads in one place. Its unsent
                # drafts stay behind: nothing on a closed case should be sent.
                queryset = queryset.filter(
                    Q(request_id=accessible_request_id)
                    | (
                        Q(request__merged_into_id=accessible_request_id)
                        & ~Q(status=Correspondence.Status.DRAFT)
                    )
                )
                self._pagination_request_id = str(accessible_request_id)
        return queryset

    def _filtered_queryset(self, filters):
        queryset = self._authorized_queryset(filters)
        for field in ("direction", "kind", "status"):
            if field in filters:
                queryset = queryset.filter(**{field: filters[field]})
        if "delivery_state" in filters:
            queryset = queryset.filter(delivery__status=filters["delivery_state"])
        if filters.get("case_state") == "open":
            queryset = queryset.exclude(request__status__in=CLOSED_STATUSES)
        elif filters.get("case_state") == "closed":
            queryset = queryset.filter(request__status__in=CLOSED_STATUSES)
        if "matching_state" in filters:
            queryset = queryset.filter(
                inbound_receipts__matching_status=filters["matching_state"]
            )
        if filters.get("has_attachments") is True:
            queryset = queryset.filter(attachments__isnull=False)
        elif filters.get("has_attachments") is False:
            queryset = queryset.filter(attachments__isnull=True)
        if "date_from" in filters:
            queryset = queryset.filter(created_at__date__gte=filters["date_from"])
        if "date_to" in filters:
            queryset = queryset.filter(created_at__date__lte=filters["date_to"])

        work_filter_names = {"work_state", "work_kind", "read_state", "assignee"}
        if work_filter_names.intersection(filters):
            work_items = role_scoped_work_items(
                WorkItem.objects.filter(
                    source_type=WorkItem.SourceType.INBOUND_RECEIPT,
                ),
                self.request.user,
            )
            if filters.get("work_state") == "unresolved":
                work_items = work_items.filter(
                    disposition__in=[
                        WorkItem.Disposition.NEEDS_ATTENTION,
                        WorkItem.Disposition.WAITING,
                    ]
                )
            elif "work_state" in filters:
                work_items = work_items.filter(
                    disposition=filters["work_state"]
                )
            if "work_kind" in filters:
                work_items = work_items.filter(
                    work_kind__in=filters["work_kind"]
                )
            if "assignee" in filters:
                work_items = work_items.filter(assignee_id=filters["assignee"])
            if filters.get("read_state") == "read":
                work_items = work_items.filter(
                    receipts__user=self.request.user,
                    receipts__read_at__isnull=False,
                )
            elif filters.get("read_state") == "unread":
                work_items = work_items.exclude(
                    pk__in=WorkItemReceipt.objects.filter(
                        user=self.request.user,
                        read_at__isnull=False,
                    ).values("work_item_id")
                )
            receipt_keys = work_items.annotate(
                source_key=Replace("source_id", Value("-"), Value(""))
            ).values("source_key")
            queryset = queryset.alias(
                inbound_receipt_key=Replace(
                    Cast("inbound_receipts__id", output_field=CharField()),
                    Value("-"),
                    Value(""),
                )
            ).filter(
                inbound_receipt_key__in=Subquery(receipt_keys)
            )
        return queryset.distinct()

    def _apply_search(self, queryset, term):
        if connection.vendor == "postgresql":
            document_filter = Q(pk__in=[])
            rank = Value(0.0, output_field=FloatField())
            for group in _deduplicated_websearch_groups(term):
                group_filter = Q()
                for negated, atom in group:
                    atom_query = SearchQuery(
                        atom,
                        search_type="websearch",
                        config="english",
                    )
                    matching_ids = CorrespondenceSearchChunk.objects.filter(
                        search_vector=atom_query,
                    ).values("correspondence_id")
                    clause = Q(pk__in=Subquery(matching_ids))
                    group_filter &= ~clause if negated else clause
                    if not negated:
                        ranked_chunks = (
                            CorrespondenceSearchChunk.objects.filter(
                                correspondence_id=OuterRef("pk"),
                                search_vector=atom_query,
                            )
                            .annotate(
                                chunk_rank=SearchRank(
                                    F("search_vector"),
                                    atom_query,
                                )
                            )
                            .order_by("-chunk_rank", "id")
                            .values("chunk_rank")[:1]
                        )
                        rank += Coalesce(
                            Subquery(
                                ranked_chunks,
                                output_field=FloatField(),
                            ),
                            Value(0.0),
                        )
                document_filter |= group_filter
            return queryset.filter(document_filter).annotate(search_rank=rank)
        return queryset.filter(
            Q(received_subject__icontains=term)
            | Q(received_body__icontains=term)
            | Q(approved_subject__icontains=term)
            | Q(approved_body__icontains=term)
        ).annotate(search_rank=Value(1.0, output_field=FloatField()))

    def _summary_queryset(self, filters, *, expose_content=None):
        queryset = self._filtered_queryset(filters)
        term = getattr(self, "_message_search_term", "")
        if term:
            queryset = self._apply_search(queryset, term)
        if expose_content is None:
            # Request-scoped reads are inside one authorized case, so content
            # is exposed. Cross-case reads normally blank the subject and
            # snippet. Records Officers are the exception: they are already
            # authorized for every case's full correspondence, and without the
            # subject line and bounded snippet the cross-case Inbox cannot be
            # triaged without opening every case one at a time. Every other
            # role - liaison, reviewer, intake clerk, admin - still gets blank
            # cross-case content, and no role gets bodies, attachments, or
            # requester identity from a cross-case row.
            expose_content = bool(filters.get("request")) or has_staff_role(
                self.request.user,
                StaffRole.RECORDS_OFFICER,
            )
        subject = (
            self._summary_subject_expression()
            if expose_content
            else Value("", output_field=CharField())
        )
        body = (
            self._summary_body_expression()
            if expose_content
            else Value("", output_field=TextField())
        )
        return (
            queryset.select_related("request", "delivery")
            .prefetch_related(self._matched_receipt_prefetch())
            .annotate(
                summary_subject=Coalesce(subject, Value("")),
                summary_snippet=Substr(
                    Coalesce(body, Value("")),
                    1,
                    MESSAGE_SNIPPET_LENGTH + 1,
                ),
                recipient_count=Count("recipients", distinct=True),
                attachment_count=Count("attachments", distinct=True),
            )
            .defer(
                "draft_body",
                "draft_metadata",
                "approved_body",
                "approved_metadata",
                "received_body",
                "received_sender",
                "received_recipients",
                "received_headers",
            )
        )

    def _detail_queryset(self):
        filters = self._validated_filters()
        return (
            self._authorized_queryset(filters)
            .select_related(
                "request",
                "created_by",
                "approved_by",
                "queued_by",
                "matched_by",
                "resulting_status_event",
                "delivery",
            )
            .prefetch_related(
                "recipients",
                "attachments",
                "denial_citations__exemption",
                "delivery__attempts",
                self._matched_receipt_prefetch(),
            )
            .annotate(
                summary_subject=Coalesce(
                    self._summary_subject_expression(),
                    Value(""),
                ),
                summary_snippet=Substr(
                    Coalesce(self._summary_body_expression(), Value("")),
                    1,
                    MESSAGE_SNIPPET_LENGTH + 1,
                ),
                recipient_count=Count("recipients", distinct=True),
                attachment_count=Count("attachments", distinct=True),
            )
        )

    def get_queryset(self):
        if self.action in {"list", "search", "thread_messages"}:
            return self._summary_queryset(self._validated_filters())
        return self._detail_queryset()

    def get_serializer_class(self):
        if self.action in {"list", "search", "thread_messages"}:
            return CorrespondenceListSerializer
        return CorrespondenceDetailSerializer

    def get_pagination_ordering(self):
        if self.action == "threads":
            return ("-latest_at", "representative_id")
        if getattr(self, "_message_search_term", ""):
            return ("-search_rank", "-created_at", "-id")
        if self._validated_filters().get("sort") == "oldest":
            return ("created_at", "id")
        return ("-created_at", "-id")

    def get_pagination_scope(self):
        filters = {
            key: (
                value.isoformat()
                if hasattr(value, "isoformat")
                else sorted(value)
                if isinstance(value, set)
                else value
            )
            for key, value in self._validated_filters().items()
        }
        scope = {
            "actor_id": self.request.user.pk,
            "action": self.action,
            "request_id": getattr(self, "_pagination_request_id", None),
            "filters": filters,
        }
        term = getattr(self, "_message_search_term", "")
        if term:
            scope["search_sha256"] = hashlib.sha256(term.encode()).hexdigest()
        if hasattr(self, "_thread_representative_id"):
            scope["thread_representative_id"] = self._thread_representative_id
        return scope

    def get_pagination_cursor(self):
        if self.action == "search":
            return getattr(self, "_search_cursor", None)
        return self.request.query_params.get("cursor")

    def _attach_case_context(self, messages):
        """Batch-load the bounded per-case triage context for a page of rows.

        Three extra queries regardless of page size: the requests themselves
        are already select_related, and the latest outbound acknowledgment per
        case, the email-intake provenance, and the statutory-delivery answer
        are each loaded in one pass. The statutory state is decided by the one
        shared plan function, so an Inbox row can never disagree with the case
        it links to.

        The delivery pass matters most: read one case at a time,
        ``initial_response_delivered`` costs two queries per row (the legacy
        import row, then the acknowledged/clarification status events), which
        is 100 queries on a full 50-row page. Selecting the complement with
        ``awaiting_initial_response`` -- the predicate the day-5 safety-net
        worker already selects on -- answers the whole page in one query and
        keeps the Inbox and the safety net reading from the same rule.
        """
        requests_by_id = {}
        for message in messages:
            if message.request_id is not None:
                requests_by_id.setdefault(message.request_id, message.request)
        if not requests_by_id:
            return
        latest_acknowledgments = {}
        for candidate in (
            Correspondence.objects.filter(
                request_id__in=requests_by_id,
                direction=Correspondence.Direction.OUTBOUND,
                kind=Correspondence.Kind.ACKNOWLEDGMENT,
            )
            .select_related("delivery")
            .order_by("request_id", "-created_at", "-pk")
        ):
            latest_acknowledgments.setdefault(candidate.request_id, candidate)
        # Mirrors services.close_email_non_request exactly, so the Inbox only
        # offers the non-request closure where that closure would succeed.
        email_intake_request_ids = set(
            InboundCorrespondenceReceipt.objects.filter(
                matched_request_id__in=requests_by_id,
                matched_correspondence__request_id=F("matched_request_id"),
                matching_status=(
                    InboundCorrespondenceReceipt.MatchingStatus.MATCHED
                ),
                match_method=(
                    InboundCorrespondenceReceipt.MatchMethod.EMAIL_INTAKE
                ),
            ).values_list("matched_request_id", flat=True)
        )
        awaiting_ids = set(
            awaiting_initial_response(
                PublicRecordsRequest.objects.filter(pk__in=requests_by_id)
            ).values_list("pk", flat=True)
        )
        automatic_enabled = safety_net_automatic_enabled()
        for message in messages:
            prr = requests_by_id.get(message.request_id)
            if prr is None:
                continue
            plan = compute_initial_response_plan(
                prr,
                latest_acknowledgment=latest_acknowledgments.get(prr.pk),
                initial_response_delivered=prr.pk not in awaiting_ids,
                automatic_enabled=automatic_enabled,
            )
            message._case_context = {
                "legal_status": prr.status,
                "statutory_response_state": plan["state"],
                "statutory_response_due": plan["due_date"],
                "email_intake_auto_created": (
                    prr.pk in email_intake_request_ids
                ),
            }

    def _attach_work_items(self, messages, *, detail=False):
        receipt_by_message = {}
        for message in messages:
            receipts = getattr(message, "_matched_inbound_receipts", ())
            if receipts:
                receipt_by_message[message.pk] = receipts[0]
        if not receipt_by_message:
            return
        source_ids = [str(receipt.pk) for receipt in receipt_by_message.values()]
        work_items = list(
            role_scoped_work_items(
                WorkItem.objects.filter(
                    source_type=WorkItem.SourceType.INBOUND_RECEIPT,
                    source_id__in=source_ids,
                ),
                self.request.user,
            ).select_related("assignee", "disposed_by", "no_action_by")
        )
        work_by_source = {item.source_id: item for item in work_items}
        receipts = {
            receipt.work_item_id: receipt
            for receipt in WorkItemReceipt.objects.filter(
                work_item__in=work_items,
                user=self.request.user,
            )
        }
        events = {}
        if detail:
            for event in WorkItemEvent.objects.filter(
                work_item__in=work_items,
            ).select_related("actor").order_by("occurred_at", "id"):
                events.setdefault(event.work_item_id, []).append(event)
        for message in messages:
            inbound_receipt = receipt_by_message.get(message.pk)
            if inbound_receipt is None:
                continue
            work_item = work_by_source.get(str(inbound_receipt.pk))
            if work_item is None:
                continue
            work_item._message_receipt = receipts.get(work_item.pk)
            work_item._message_events = events.get(work_item.pk, [])
            message._message_work_item = work_item

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            self._attach_work_items(page)
            self._attach_case_context(page)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        rows = list(queryset)
        self._attach_work_items(rows)
        self._attach_case_context(rows)
        return Response(self.get_serializer(rows, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        self._attach_work_items([instance], detail=True)
        self._attach_case_context([instance])
        return Response(self.get_serializer(instance).data)

    @extend_schema(
        request=CorrespondenceSearchSerializer,
        responses={200: CorrespondenceListSerializer(many=True)},
    )
    @action(detail=False, methods=["post"])
    def search(self, request):
        serializer = CorrespondenceSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        self._message_search_term = payload.pop("query")
        self._search_cursor = payload.pop("cursor", None)
        self._message_filters = payload
        self.pagination_cursor_in_body = True
        queryset = self._summary_queryset(payload, expose_content=True)
        page = self.paginate_queryset(queryset)
        self._attach_work_items(page)
        self._attach_case_context(page)
        data = CorrespondenceListSerializer(
            page,
            many=True,
            context={"request": request},
        ).data
        return self.get_paginated_response(data)

    @classmethod
    def _thread_group_expression(cls):
        marker = Case(
            When(provider_thread_id="", then=Cast("id", CharField())),
            default=F("provider_thread_id"),
            output_field=CharField(),
        )
        return Concat(
            F("provider"),
            Value("\x1f"),
            F("provider_account"),
            Value("\x1f"),
            marker,
            output_field=CharField(),
        )

    def _thread_token(self, representative_id):
        return signing.dumps(
            {
                "version": 1,
                "request_id": self._pagination_request_id,
                "representative_id": str(representative_id),
            },
            salt="apps.api.resources.correspondence.thread",
        )

    def _thread_evidence_id(self, representative_id):
        return salted_hmac(
            "apps.api.resources.correspondence.thread-evidence",
            f"{self._pagination_request_id}:{representative_id}",
            algorithm="sha256",
        ).hexdigest()

    def _thread_payload(self, token):
        try:
            payload = signing.loads(
                token,
                salt="apps.api.resources.correspondence.thread",
            )
        except signing.BadSignature as exc:
            raise NotFound("Invalid thread") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("request_id") != self._pagination_request_id
            or not isinstance(payload.get("representative_id"), str)
        ):
            raise NotFound("Invalid thread")
        return payload

    @extend_schema(responses={200: CorrespondenceThreadSerializer(many=True)})
    @action(detail=False, methods=["get"])
    def threads(self, request):
        filters = self._validated_filters()
        if not filters.get("request"):
            raise ParseError("Thread summaries require one authorized request.")
        base = self._filtered_queryset(filters)
        thread_group = self._thread_group_expression()
        candidate = base.annotate(
            candidate_thread_group=self._thread_group_expression()
        ).filter(candidate_thread_group=OuterRef("thread_group"))
        grouped = (
            base.annotate(thread_group=thread_group)
            .values("thread_group")
            .annotate(
                message_count=Count("id", distinct=True),
                latest_at=Max("created_at"),
                representative_id=Subquery(
                    candidate.order_by("id").values("id")[:1]
                ),
                latest_subject=Subquery(
                    candidate.annotate(
                        candidate_subject=self._summary_subject_expression()
                    ).order_by("-created_at", "-id").values(
                        "candidate_subject"
                    )[:1]
                ),
            )
        )
        page = self.paginate_queryset(grouped)
        data = [
            {
                "id": self._thread_token(row["representative_id"]),
                "evidence_id": self._thread_evidence_id(
                    row["representative_id"]
                ),
                "message_count": row["message_count"],
                "latest_at": row["latest_at"],
                "subject": row["latest_subject"] or "(No subject)",
            }
            for row in page
        ]
        return self.get_paginated_response(
            CorrespondenceThreadSerializer(data, many=True).data
        )

    @extend_schema(responses={200: CorrespondenceListSerializer(many=True)})
    @action(detail=False, methods=["get"], url_path="thread-messages")
    def thread_messages(self, request):
        filters = self._validated_filters()
        if not filters.get("request"):
            raise ParseError("Thread messages require one authorized request.")
        authorized = self._authorized_queryset(filters)
        token = request.query_params.get("thread", "")
        payload = self._thread_payload(token)
        representative = get_object_or_404(
            authorized,
            pk=payload["representative_id"],
        )
        self._thread_representative_id = str(representative.pk)
        queryset = self._summary_queryset(filters, expose_content=True)
        if representative.provider_thread_id:
            queryset = queryset.filter(
                provider=representative.provider,
                provider_account=representative.provider_account,
                provider_thread_id=representative.provider_thread_id,
            )
        else:
            queryset = queryset.filter(pk=representative.pk)
        page = self.paginate_queryset(queryset)
        self._attach_work_items(page)
        self._attach_case_context(page)
        data = CorrespondenceListSerializer(
            page,
            many=True,
            context={"request": request},
        ).data
        return self.get_paginated_response(data)

    def _request_for_action(self, request_number):
        return get_object_or_404(
            _accessible_request_queryset(self.request.user),
            number=request_number,
        )

    def _require_kind_draft_role(self, user, kind):
        """Hold a drafting aid to the same role as the draft it leads to.

        Closure and denial drafts are records-officer only, so their starting
        point is too. Reading the roles back out of ``role_permissions`` keeps
        the two from drifting apart.
        """
        create_action = PREFILL_CREATE_ACTIONS.get(kind)
        if create_action is None:
            return
        required = self.role_permissions.get(create_action)
        if required and not has_staff_role(user, *required):
            raise PermissionDenied(
                "Your role cannot draft this kind of correspondence."
            )

    @staticmethod
    def _recipient_inputs(validated_data, prr):
        if "recipients" not in validated_data:
            return None
        recipients = []
        for supplied in validated_data["recipients"]:
            email = supplied["email"]
            recipients.append(
                {
                    "recipient_type": supplied["recipient_type"],
                    "email": email,
                    "display_name": supplied.get("display_name", ""),
                    "requester": (
                        prr.requester
                        if email.lower() == prr.requester.email.lower()
                        else None
                    ),
                }
            )
        return recipients

    def _detail_response(self, correspondence, response_status=status.HTTP_200_OK):
        refreshed = self.get_queryset().get(pk=correspondence.pk)
        self._attach_work_items([refreshed], detail=True)
        return Response(
            CorrespondenceDetailSerializer(
                refreshed,
                context={"request": self.request},
            ).data,
            status=response_status,
        )

    def _authored_draft(self, request, kind):
        serializer_class = (
            DenialDraftSerializer
            if kind == Correspondence.Kind.DENIAL
            else AuthoredDraftSerializer
        )
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        prr = self._request_for_action(data["request_number"])
        try:
            correspondence = services.create_authored_draft(
                prr,
                kind=kind,
                subject=data["subject"],
                body=data["body"],
                actor=request.user,
                recipients=self._recipient_inputs(data, prr),
                denial_exemption_citation_ids=data.get(
                    "exemption_citation_ids"
                ),
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="drafts/general")
    @extend_schema(
        request=AuthoredDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_general_draft(self, request):
        return self._authored_draft(request, Correspondence.Kind.GENERAL)

    @action(
        detail=False,
        methods=["post"],
        url_path="drafts/submission-receipt",
    )
    @extend_schema(
        request=RequestDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_submission_receipt_draft(self, request):
        """Create the non-statutory courtesy receipt as a staff draft.

        Automated requester mail is off by default, so this is the human path
        to the same non-statutory message. It cannot satisfy or defer the
        five-business-day RCW 42.56.520 response.
        """
        serializer = RequestDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        prr = self._request_for_action(
            serializer.validated_data["request_number"]
        )
        try:
            correspondence = services.create_officer_submission_receipt_draft(
                prr,
                actor=request.user,
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path="template-prefill")
    @extend_schema(
        parameters=[TemplatePrefillSerializer],
        responses={200: PreviewSerializer},
    )
    def template_prefill(self, request):
        """Render the district's editable starting point for an authored kind."""
        serializer = TemplatePrefillSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        kind = serializer.validated_data["kind"]
        self._require_kind_draft_role(request.user, kind)
        prr = self._request_for_action(
            serializer.validated_data["request_number"]
        )
        try:
            rendered = services.render_free_form_prefill(
                prr,
                kind=kind,
                name=serializer.validated_data["name"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return Response(
            {"subject": rendered.subject, "body": rendered.body}
        )

    @action(detail=False, methods=["post"], url_path="drafts/acknowledgment")
    @extend_schema(
        request=AcknowledgmentDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_acknowledgment_draft(self, request):
        serializer = AcknowledgmentDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        prr = self._request_for_action(data["request_number"])
        try:
            correspondence = services.create_acknowledgment_draft(
                prr,
                actor=request.user,
                estimated_completion=data.get("estimated_completion"),
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="drafts/clarification")
    @extend_schema(
        request=ClarificationDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_clarification_draft(self, request):
        serializer = ClarificationDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        prr = self._request_for_action(data["request_number"])
        try:
            correspondence = services.create_clarification_draft(
                prr,
                clarification_questions=data["clarification_questions"],
                clarification_due=data["clarification_due"],
                actor=request.user,
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="drafts/scope-clarification",
    )
    @extend_schema(
        request=ScopeClarificationDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_scope_clarification_draft(self, request):
        handoff = human_only_handoff(
            request,
            capability_id="correspondence.draft_scope_clarification",
            action=(
                "Decide what to tell this requester about the size of their "
                "request, and whether to ask them to clarify it."
            ),
        )
        if handoff:
            return handoff
        serializer = ScopeClarificationDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        prr = self._request_for_action(data["request_number"])
        try:
            correspondence = services.create_scope_clarification_draft(
                prr,
                form=data["form"],
                match_count=data["match_count"],
                query_summary=data["query_summary"],
                narrowing_options=[
                    dict(option) for option in data["narrowing_options"]
                ],
                response_by=data["response_by"],
                actor=request.user,
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="drafts/estimate-revision")
    @extend_schema(
        request=EstimateRevisionDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_estimate_revision_draft(self, request):
        serializer = EstimateRevisionDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        prr = self._request_for_action(data["request_number"])
        try:
            correspondence = services.create_estimate_revision_draft(
                prr,
                new_date=data["new_date"],
                reason=data["reason"],
                actor=request.user,
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="drafts/installment")
    @extend_schema(
        request=InstallmentDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_installment_draft(self, request):
        serializer = InstallmentDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        prr = self._request_for_action(data["request_number"])
        installment = get_object_or_404(
            Installment,
            pk=data["installment_id"],
            request=prr,
        )
        try:
            correspondence = services.create_installment_notice_draft(
                prr,
                installment=installment,
                is_final=data["is_final"],
                actor=request.user,
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="drafts/responsive-records")
    @extend_schema(
        request=AuthoredDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_responsive_records_draft(self, request):
        return self._authored_draft(
            request,
            Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE,
        )

    @action(detail=False, methods=["post"], url_path="drafts/closure")
    @extend_schema(
        request=AuthoredDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_closure_draft(self, request):
        return self._authored_draft(request, Correspondence.Kind.CLOSURE)

    @action(
        detail=False,
        methods=["post"],
        url_path="drafts/no-records-closure",
    )
    @extend_schema(
        request=NoRecordsClosureDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_no_records_closure_draft(self, request):
        return self._structured_officer_draft(
            request,
            serializer_class=NoRecordsClosureDraftSerializer,
            service=services.create_no_records_closure_draft,
            capability_id="correspondence.draft_no_records_closure",
            action_description=(
                "Attest to an adequate search and create its closure draft."
            ),
        )

    def _structured_officer_draft(
        self,
        request,
        *,
        serializer_class,
        service,
        capability_id,
        action_description,
    ):
        """One accountable officer action: validate, delegate, return detail."""
        handoff = human_only_handoff(
            request,
            capability_id=capability_id,
            action=action_description,
        )
        if handoff:
            return handoff
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        prr = self._request_for_action(data.pop("request_number"))
        try:
            correspondence = service(prr, actor=request.user, **data)
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(
            correspondence,
            response_status=status.HTTP_201_CREATED,
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="drafts/fee-estimate",
    )
    @extend_schema(
        request=FeeEstimateDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_fee_estimate_draft(self, request):
        return self._structured_officer_draft(
            request,
            serializer_class=FeeEstimateDraftSerializer,
            service=services.create_fee_estimate_draft,
            capability_id="correspondence.draft_fee_estimate",
            action_description=(
                "Quote the RCW 42.56.120 copying charge, with the deposit "
                "the District asks for before it does the work."
            ),
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="drafts/completion-closure",
    )
    @extend_schema(
        request=CompletionClosureDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_completion_closure_draft(self, request):
        return self._structured_officer_draft(
            request,
            serializer_class=CompletionClosureDraftSerializer,
            service=services.create_completion_closure_draft,
            capability_id="correspondence.draft_completion_closure",
            action_description=(
                "Create the closure notice that fulfills the request on "
                "confirmed delivery."
            ),
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="drafts/nonpayment-closure",
    )
    @extend_schema(
        request=NonpaymentClosureDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_nonpayment_closure_draft(self, request):
        return self._structured_officer_draft(
            request,
            serializer_class=NonpaymentClosureDraftSerializer,
            service=services.create_nonpayment_closure_draft,
            capability_id="correspondence.draft_nonpayment_closure",
            action_description=(
                "Create the RCW 42.56.120(2) closure notice for an unpaid "
                "fee or deposit."
            ),
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="drafts/third-party-notice",
    )
    @extend_schema(
        request=ThirdPartyNoticeDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_third_party_notice_draft(self, request):
        return self._structured_officer_draft(
            request,
            serializer_class=ThirdPartyNoticeDraftSerializer,
            service=services.create_third_party_notice_draft,
            capability_id="correspondence.draft_third_party_notice",
            action_description=(
                "Notify a named person under RCW 42.56.540 that records "
                "concerning them will be disclosed."
            ),
        )

    @staticmethod
    def _batch_response(outcome):
        return Response(
            BatchOutcomeSerializer(
                {
                    "batch_id": outcome.batch_id,
                    "succeeded": [
                        {
                            "request_number": number,
                            "correspondence_id": str(correspondence.pk),
                            # Each case's own rendering, so the officer reads
                            # every notice before one send covers them all.
                            "subject": correspondence.draft_subject,
                            "body": correspondence.draft_body,
                        }
                        for number, correspondence in outcome.succeeded
                    ],
                    "failed": [
                        {"request_number": number, "detail": detail}
                        for number, detail in outcome.failed
                    ],
                }
            ).data
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="batch/closure-drafts",
    )
    @extend_schema(
        request=BatchClosureDraftSerializer,
        responses={200: BatchOutcomeSerializer},
    )
    def batch_closure_drafts(self, request):
        handoff = human_only_handoff(
            request,
            capability_id="correspondence.batch_draft_closures",
            action=(
                "Draft the same closure or general notice for several "
                "selected cases."
            ),
        )
        if handoff:
            return handoff
        serializer = BatchClosureDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            outcome = services.batch_create_closure_drafts(
                data.pop("request_numbers"),
                actor=request.user,
                **data,
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._batch_response(outcome)

    @action(
        detail=False,
        methods=["post"],
        url_path="batch/approve-and-send",
    )
    @extend_schema(
        request=BatchApproveAndSendSerializer,
        responses={200: BatchOutcomeSerializer},
    )
    def batch_approve_and_send(self, request):
        handoff = human_only_handoff(
            request,
            capability_id="correspondence.batch_approve_and_send",
            action=(
                "Approve the selected drafts and release one independent "
                "delivery per case."
            ),
        )
        if handoff:
            return handoff
        serializer = BatchApproveAndSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            outcome = services.batch_approve_and_send(
                data["correspondence_ids"],
                actor=request.user,
                confirmed=data["confirmed"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._batch_response(outcome)

    @action(detail=False, methods=["post"], url_path="drafts/denial")
    @extend_schema(
        request=DenialDraftSerializer,
        responses={201: CorrespondenceDetailSerializer},
    )
    def create_denial_draft(self, request):
        return self._authored_draft(request, Correspondence.Kind.DENIAL)

    @extend_schema(
        request={
            "application/json": CORRESPONDENCE_DRAFT_EDIT_REQUEST_SCHEMA,
        },
        responses={200: CorrespondenceDetailSerializer},
    )
    def partial_update(self, request, pk=None):
        correspondence = self.get_object()
        if (
            correspondence.kind == Correspondence.Kind.DENIAL
            and not has_staff_role(request.user, StaffRole.RECORDS_OFFICER)
        ):
            raise PermissionDenied(
                "Records Officer approval is required."
            )
        structured_contract = STRUCTURED_EDIT_CONTRACTS.get(
            correspondence.kind
        )
        serializer_class = (
            DenialEditSerializer
            if correspondence.kind == Correspondence.Kind.DENIAL
            else (
                structured_contract[0]
                if structured_contract is not None
                else EditDraftSerializer
            )
        )
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if structured_contract is not None:
                correspondence = services.edit_structured_draft(
                    correspondence,
                    actor=request.user,
                    expected_version=data["expected_version"],
                    structured_data={
                        field: data[field]
                        for field in structured_contract[1]
                    },
                    recipients=self._recipient_inputs(
                        data,
                        correspondence.request,
                    ),
                )
            else:
                correspondence = services.edit_draft(
                    correspondence,
                    subject=data["subject"],
                    body=data["body"],
                    actor=request.user,
                    expected_version=data["expected_version"],
                    recipients=self._recipient_inputs(
                        data,
                        correspondence.request,
                    ),
                    denial_exemption_citation_ids=data.get(
                        "exemption_citation_ids"
                    ),
                )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(correspondence)

    @action(detail=True, methods=["get"])
    @extend_schema(responses={200: PreviewSerializer})
    def preview(self, request, pk=None):
        correspondence = self.get_object()
        try:
            rendered = services.preview(correspondence)
        except ValidationError as exc:
            return _service_error(exc)
        return Response(
            {
                "correspondence_id": str(correspondence.id),
                "status": correspondence.status,
                "version": correspondence.version,
                "subject": rendered.subject,
                "body": rendered.body,
                "recipients": RecipientSerializer(
                    correspondence.recipients.all(),
                    many=True,
                ).data,
                "attachments": CorrespondenceAttachmentSerializer(
                    correspondence.attachments.all(),
                    many=True,
                ).data,
                "denial_exemption_citations": (
                    DenialExemptionCitationSerializer(
                        correspondence.denial_citations.select_related(
                            "exemption"
                        ),
                        many=True,
                    ).data
                ),
            }
        )

    @action(detail=True, methods=["post"])
    @extend_schema(
        request=ExpectedVersionSerializer,
        responses={200: CorrespondenceDetailSerializer},
    )
    def approve(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="correspondence.approve",
            action="Approve the immutable correspondence snapshot.",
        )
        if handoff:
            return handoff
        correspondence = self.get_object()
        serializer = ExpectedVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            correspondence = services.approve_draft(
                correspondence,
                actor=request.user,
                expected_version=serializer.validated_data["expected_version"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(correspondence)

    @action(
        detail=True,
        methods=["post"],
        url_path="approve-and-send",
    )
    @extend_schema(
        request=ExpectedVersionSerializer,
        responses={200: CorrespondenceDetailSerializer},
    )
    def approve_and_send(self, request, pk=None):
        """Approve and queue in one action, because it is one decision.

        Kept alongside the separate ``approve`` and ``queue`` primitives rather
        than replacing them: an agent, a recovery path, or a future workflow
        that really does split the two still has each half addressable.
        """
        handoff = human_only_handoff(
            request,
            capability_id="correspondence.approve_and_send",
            action=(
                "Approve the correspondence snapshot and release it for "
                "external delivery."
            ),
        )
        if handoff:
            return handoff
        correspondence = self.get_object()
        serializer = ExpectedVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.approve_and_send(
                correspondence,
                actor=request.user,
                expected_version=serializer.validated_data["expected_version"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(correspondence)

    @action(detail=True, methods=["post"])
    @extend_schema(
        request=ExpectedVersionSerializer,
        responses={200: CorrespondenceDetailSerializer},
    )
    def queue(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="correspondence.queue",
            action="Queue approved correspondence for external delivery.",
        )
        if handoff:
            return handoff
        correspondence = self.get_object()
        serializer = ExpectedVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.queue_approved(
                correspondence,
                actor=request.user,
                expected_version=serializer.validated_data["expected_version"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(correspondence)

    @action(detail=True, methods=["post"], url_path="delete")
    @extend_schema(
        request=ExpectedVersionSerializer,
        responses={204: None},
    )
    def delete_draft(self, request, pk=None):
        correspondence = self.get_object()
        serializer = ExpectedVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.delete_draft(
                correspondence,
                actor=request.user,
                expected_version=serializer.validated_data["expected_version"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    @extend_schema(
        request=CancelSerializer,
        responses={200: CorrespondenceDetailSerializer},
    )
    def cancel(self, request, pk=None):
        correspondence = self.get_object()
        serializer = CancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            correspondence = services.cancel_unsent(
                correspondence,
                actor=request.user,
                expected_version=serializer.validated_data["expected_version"],
                reason=serializer.validated_data["reason"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(correspondence)

    @action(detail=True, methods=["post"])
    @extend_schema(
        request=RetrySerializer,
        responses={200: CorrespondenceDetailSerializer},
    )
    def retry(self, request, pk=None):
        correspondence = self.get_object()
        serializer = RetrySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            delivery = correspondence.delivery
        except CorrespondenceDelivery.DoesNotExist:
            return Response(
                {
                    "code": "delivery_not_found",
                    "detail": "This correspondence has no delivery job.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            services.retry_delivery(
                delivery,
                actor=request.user,
                expected_version=serializer.validated_data[
                    "expected_delivery_version"
                ],
            )
        except ValidationError as exc:
            return _service_error(exc)
        return self._detail_response(correspondence)

    @action(detail=True, methods=["get"])
    @extend_schema(responses={200: DeliveryAttemptSerializer(many=True)})
    def attempts(self, request, pk=None):
        correspondence = self.get_object()
        try:
            delivery = correspondence.delivery
        except CorrespondenceDelivery.DoesNotExist:
            return Response([])
        attempts = delivery.attempts.select_related("actor").order_by(
            "attempt_number"
        )
        return Response(DeliveryAttemptSerializer(attempts, many=True).data)


class CorrespondenceAttachmentViewSet(viewsets.GenericViewSet):
    serializer_class = CorrespondenceAttachmentSerializer
    role_permissions = {
        "download": CORRESPONDENCE_VIEW_ROLES,
        "content": CORRESPONDENCE_VIEW_ROLES,
    }
    allow_readonly_agent = False
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = CorrespondenceAttachment.objects.select_related(
            "correspondence__request"
        )
        user = self.request.user
        if not has_staff_role(user, StaffRole.RECORDS_OFFICER):
            queryset = queryset.filter(
                correspondence__request__isnull=False
            )
        return scope_case_queryset(
            queryset,
            user,
            request_prefix="correspondence__request",
        )

    @staticmethod
    def _audit_target(attachment):
        request = attachment.correspondence.request
        return request.number if request else str(attachment.correspondence_id)

    @staticmethod
    def _cache_key(token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        return f"correspondence-attachment-download:{digest}"

    @action(detail=True, methods=["post"])
    @extend_schema(responses={200: AttachmentDownloadAuthorizationSerializer})
    def download(self, request, pk=None):
        attachment = self.get_object()
        if not attachment.is_downloadable:
            return Response(
                {
                    "code": "attachment_not_clean",
                    "detail": (
                        "The attachment is unavailable unless malware "
                        "scanning succeeds and quarantine is released."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        expires_in = 300
        token = secrets.token_urlsafe(32)
        cache.set(
            self._cache_key(token),
            {
                "attachment_id": str(attachment.id),
                "user_id": str(request.user.pk),
            },
            timeout=expires_in,
        )
        url = reverse(
            "correspondence-attachment-content",
            kwargs={
                "pk": attachment.pk,
                "download_token": token,
            },
        )
        audit(
            "correspondence_attachment.download_authorized",
            target=self._audit_target(attachment),
            actor=request.user,
            attachment_id=str(attachment.id),
            correspondence_id=str(attachment.correspondence_id),
            sha256=attachment.sha256,
        )
        return Response({"url": url, "expires_in": expires_in})

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "download_token",
                OpenApiTypes.STR,
                OpenApiParameter.PATH,
                description="Five-minute user-bound download token.",
            )
        ],
        responses={200: OpenApiTypes.BINARY, 404: None},
    )
    @action(
        detail=True,
        methods=["get"],
        url_path=r"content/(?P<download_token>[^/.]+)",
        url_name="content",
    )
    def content(self, request, pk=None, download_token=None):
        attachment = self.get_object()
        authorization = cache.get(self._cache_key(download_token or ""))
        if authorization != {
            "attachment_id": str(attachment.id),
            "user_id": str(request.user.pk),
        }:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            opened = services.open_released_attachment(
                attachment,
                actor=request.user,
            )
        except services.DeliveryAttachmentNotAvailable:
            return Response(
                {
                    "code": "not_found",
                    "detail": "Attachment is unavailable.",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except services.DeliveryAttachmentStorageUnavailable:
            return Response(
                {
                    "code": "attachment_unavailable",
                    "detail": "Attachment storage is temporarily unavailable.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
                headers={"Retry-After": "5"},
            )
        attachment = opened.attachment
        response = FileResponse(
            opened.body,
            as_attachment=True,
            filename=attachment.original_filename,
            content_type="application/octet-stream",
        )
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        response["X-Content-Type-Options"] = "nosniff"
        audit(
            "correspondence_attachment.downloaded",
            target=self._audit_target(attachment),
            actor=request.user,
            attachment_id=str(attachment.id),
            correspondence_id=str(attachment.correspondence_id),
            sha256=attachment.sha256,
        )
        return response


class ClarificationCycleListSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    outbound_correspondence_id = serializers.UUIDField(read_only=True)
    inbound_response_correspondence_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = ClarificationCycle
        fields = [
            "id",
            "request_number",
            "outbound_correspondence_id",
            "inbound_response_correspondence_id",
            "opened_at",
            "responded_at",
            "resolved_at",
            "cancelled_at",
        ]


class ClarificationCycleDetailSerializer(ClarificationCycleListSerializer):
    resolved_by = serializers.SerializerMethodField()

    class Meta(ClarificationCycleListSerializer.Meta):
        fields = [*ClarificationCycleListSerializer.Meta.fields, "resolved_by", "resolution_notes", "opened_status_event_id", "resolved_status_event_id"]

    def get_resolved_by(self, obj) -> str | None:
        return _actor_name(obj.resolved_by)


class ResolveClarificationSerializer(serializers.Serializer):
    resolution_notes = serializers.CharField()


class ClarificationCycleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ClarificationCycleListSerializer
    http_method_names = ["get", "post", "head", "options"]
    allow_readonly_agent = False
    role_permissions = {
        "list": WORKFLOW_STAFF_ROLES,
        "retrieve": WORKFLOW_STAFF_ROLES,
        "resolve": {StaffRole.RECORDS_OFFICER},
    }

    def get_queryset(self):
        queryset = ClarificationCycle.objects.select_related(
            "request",
            "outbound_correspondence",
            "inbound_response_correspondence",
            "resolved_by",
            "opened_status_event",
            "resolved_status_event",
        )
        user = self.request.user
        queryset = scope_case_queryset(
            queryset,
            user,
            request_prefix="request",
        )
        request_number = self.request.query_params.get("request")
        if request_number:
            queryset = queryset.filter(request__number=request_number)
        state = self.request.query_params.get("state")
        if state == "open":
            queryset = queryset.filter(
                resolved_at__isnull=True,
                cancelled_at__isnull=True,
            )
        elif state == "responded":
            queryset = queryset.filter(
                responded_at__isnull=False,
                resolved_at__isnull=True,
                cancelled_at__isnull=True,
            )
        return queryset.order_by("-opened_at", "-id")

    def get_serializer_class(self):
        if self.action == "list":
            return ClarificationCycleListSerializer
        return ClarificationCycleDetailSerializer

    @action(detail=True, methods=["post"])
    @extend_schema(
        request=ResolveClarificationSerializer,
        responses={200: ClarificationCycleDetailSerializer},
    )
    def resolve(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="clarifications.resolve",
            action="Resolve a received clarification response.",
        )
        if handoff:
            return handoff
        cycle = self.get_object()
        serializer = ResolveClarificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            cycle = services.resolve_clarification(
                cycle,
                actor=request.user,
                resolution_notes=serializer.validated_data["resolution_notes"],
            )
        except ValidationError as exc:
            return _service_error(exc)
        cycle = self.get_queryset().get(pk=cycle.pk)
        return Response(ClarificationCycleDetailSerializer(cycle).data)


def register(router):
    router.register(
        "correspondence",
        CorrespondenceViewSet,
        basename="correspondence",
    )
    router.register(
        "correspondence-attachments",
        CorrespondenceAttachmentViewSet,
        basename="correspondence-attachment",
    )
    router.register(
        "clarification-cycles",
        ClarificationCycleViewSet,
        basename="clarification-cycle",
    )
