# ABOUTME: Officer-only staff queues for retained inbound correspondence evidence.
# ABOUTME: Manual match/rematch actions delegate to the locked inbound domain.
from django.core.exceptions import ValidationError
from django.db.models import (
    CharField,
    Count,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    UUIDField,
    Value,
)
from django.db.models.functions import Cast, Replace
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import StaffRole
from apps.correspondence import inbound, services
from apps.correspondence.models import (
    ClarificationCycle,
    CorrespondenceAttachment,
    CorrespondenceDeliveryNotification,
    InboundCorrespondenceReceipt,
)
from apps.records import email_intake
from apps.records.models import PublicRecordsRequest
from apps.work_queue.models import WorkItem

OFFICER_ONLY = {StaffRole.RECORDS_OFFICER}
UNMATCHED_STATES = {
    InboundCorrespondenceReceipt.MatchingStatus.UNMATCHED,
    InboundCorrespondenceReceipt.MatchingStatus.CANDIDATE,
    InboundCorrespondenceReceipt.MatchingStatus.AMBIGUOUS,
}
SAFE_MIME_REASON_CODES = {
    "message_too_large",
    "malformed_message",
    "too_many_headers",
    "header_line_too_long",
    "too_many_mime_parts",
    "mime_depth_exceeded",
    "body_too_large",
    "too_many_attachments",
    "attachment_too_large",
    "attachment_total_too_large",
    "attachment_content_type_mismatch",
}
SAFE_PARSING_REASON_CODES = SAFE_MIME_REASON_CODES | {
    "raw_storage_unavailable",
    "attachment_storage_unavailable",
    "parser_unavailable",
    "parsing_failed",
}
SAFE_RECEIVED_HEADERS = {
    "from",
    "to",
    "cc",
    "reply-to",
    "date",
    "subject",
    "message-id",
    "in-reply-to",
    "references",
    "auto-submitted",
}


def _safe_reason_code(value, *, fallback, allowlist) -> str:
    candidate = str(value or "").partition(":")[0].strip().lower()
    if candidate in allowlist:
        return candidate
    return fallback if value else ""


def _validation_detail(exc: ValidationError) -> str:
    if hasattr(exc, "message_dict"):
        messages = [
            message
            for field_messages in exc.message_dict.values()
            for message in field_messages
        ]
    else:
        messages = exc.messages
    return str(messages[0] if messages else "Inbound association failed.")


def _with_work_items(queryset):
    """Attach each receipt's open work item, and whether a person decided it.

    A receipt an officer marked no action (or handled) leaves the triage queues;
    its work item keeps the decision, reachable under Inbox -> No action.
    """
    work_items = WorkItem.objects.filter(
        source_type=WorkItem.SourceType.INBOUND_RECEIPT,
    ).annotate(
        _receipt_key=Replace("source_id", Value("-"), Value("")),
    )
    receipt_key = Replace(
        Cast(OuterRef("pk"), output_field=CharField()),
        Value("-"),
        Value(""),
    )
    open_items = work_items.filter(
        _receipt_key=receipt_key,
        disposition__in=[
            WorkItem.Disposition.NEEDS_ATTENTION,
            WorkItem.Disposition.WAITING,
        ],
    ).order_by("created_at", "id")
    decided = work_items.filter(
        _receipt_key=receipt_key,
        disposition__in=[
            WorkItem.Disposition.HANDLED,
            WorkItem.Disposition.NO_ACTION_REQUIRED,
        ],
    )
    return queryset.annotate(
        work_item_id=Subquery(
            open_items.values("pk")[:1],
            output_field=UUIDField(),
        ),
        work_item_version=Subquery(
            open_items.values("version")[:1],
            output_field=IntegerField(),
        ),
        _work_item_decided=Exists(decided),
    )


class InboundAttachmentSerializer(serializers.ModelSerializer):
    is_downloadable = serializers.BooleanField(read_only=True)
    quarantine_reason_code = serializers.SerializerMethodField()

    class Meta:
        model = CorrespondenceAttachment
        fields = [
            "id",
            "original_filename",
            "content_type",
            "size",
            "scan_status",
            "quarantined",
            "quarantine_reason_code",
            "is_downloadable",
            "created_at",
            "scanned_at",
        ]

    def get_quarantine_reason_code(self, obj) -> str:
        if obj.scan_status == CorrespondenceAttachment.ScanStatus.INFECTED:
            return "malware_detected"
        if obj.scan_status == CorrespondenceAttachment.ScanStatus.ERROR:
            return "scan_unavailable"
        return ""


class DeliveryStatusNotificationSerializer(serializers.ModelSerializer):
    delivery_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = CorrespondenceDeliveryNotification
        fields = [
            "id",
            "delivery_id",
            "recipient_index",
            "recipient_match_status",
            "action",
            "enhanced_status",
            "created_at",
        ]
        read_only_fields = fields


class InboundReceiptListSerializer(serializers.ModelSerializer):
    correspondence_id = serializers.UUIDField(
        source="matched_correspondence_id",
        read_only=True,
        allow_null=True,
    )
    candidate_request_number = serializers.CharField(
        source="candidate_request.number",
        read_only=True,
        allow_null=True,
    )
    matched_request_number = serializers.CharField(
        source="matched_request.number",
        read_only=True,
        allow_null=True,
    )
    attachment_count = serializers.IntegerField(read_only=True)
    downloadable_attachment_count = serializers.IntegerField(read_only=True)
    quarantined_attachment_count = serializers.IntegerField(read_only=True)
    quarantine_reason_code = serializers.SerializerMethodField()
    parsing_error_code = serializers.SerializerMethodField()
    # Who wrote and what about, so an officer can triage from the list. The
    # queue is officer-only, the same audience as the detail view.
    received_subject = serializers.CharField(
        source="matched_correspondence.received_subject",
        read_only=True,
        allow_null=True,
    )
    received_sender = serializers.CharField(
        source="matched_correspondence.received_sender",
        read_only=True,
        allow_null=True,
    )
    # The open work item behind this row, so the row can record a no-action
    # decision through the registered work-item primitive.
    work_item_id = serializers.UUIDField(read_only=True, allow_null=True)
    work_item_version = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = InboundCorrespondenceReceipt
        fields = [
            "id",
            "received_sender",
            "received_subject",
            "work_item_id",
            "work_item_version",
            "correspondence_id",
            "parsing_status",
            "matching_status",
            "match_method",
            "candidate_request_number",
            "matched_request_number",
            "quarantined",
            "quarantine_reason_code",
            "parsing_error_code",
            "received_at",
            "created_at",
            "completed_at",
            "attachment_count",
            "downloadable_attachment_count",
            "quarantined_attachment_count",
            "version",
        ]

    def get_quarantine_reason_code(self, obj) -> str:
        return _safe_reason_code(
            obj.quarantine_reason,
            fallback="message_quarantined",
            allowlist=SAFE_PARSING_REASON_CODES,
        )

    def get_parsing_error_code(self, obj) -> str:
        return _safe_reason_code(
            obj.parsing_error,
            fallback="parsing_failed",
            allowlist=SAFE_PARSING_REASON_CODES,
        )


class InboundReceiptDetailSerializer(InboundReceiptListSerializer):
    provider_thread_id = serializers.CharField(read_only=True)
    matched_delivery_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
    )
    correspondence_status = serializers.CharField(
        source="matched_correspondence.status",
        read_only=True,
        allow_null=True,
    )
    correspondence_kind = serializers.CharField(
        source="matched_correspondence.kind",
        read_only=True,
        allow_null=True,
    )
    received_body = serializers.CharField(
        source="matched_correspondence.received_body",
        read_only=True,
        allow_null=True,
    )
    received_recipients = serializers.JSONField(
        source="matched_correspondence.received_recipients",
        read_only=True,
        allow_null=True,
    )
    received_headers = serializers.SerializerMethodField()
    attachments = InboundAttachmentSerializer(many=True, read_only=True)
    delivery_notifications = DeliveryStatusNotificationSerializer(
        many=True,
        read_only=True,
    )

    class Meta(InboundReceiptListSerializer.Meta):
        fields = [*InboundReceiptListSerializer.Meta.fields, "provider", "provider_account", "provider_thread_id", "rfc_message_id", "delivery_status_original_message_id", "matched_delivery_id", "in_reply_to", "references", "envelope_sender", "envelope_recipients", "received_body", "received_recipients", "received_headers", "correspondence_status", "correspondence_kind", "attachments", "delivery_notifications"]

    def get_received_headers(self, obj) -> dict[str, str]:
        correspondence = obj.matched_correspondence
        if correspondence is None:
            return {}
        return {
            name: value
            for name, value in correspondence.received_headers.items()
            if str(name).lower() in SAFE_RECEIVED_HEADERS
        }


class ManualInboundAssociationSerializer(serializers.Serializer):
    request_number = serializers.CharField(max_length=20)
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )
    expected_version = serializers.IntegerField(min_value=1)

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


class ClarificationResponseAssociationSerializer(serializers.Serializer):
    clarification_cycle_id = serializers.UUIDField()
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )
    expected_version = serializers.IntegerField(min_value=1)

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


class InboundCorrespondenceReceiptViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = InboundReceiptListSerializer
    http_method_names = ["get", "post", "head", "options"]
    allow_readonly_agent = False
    role_permissions = {
        "list": OFFICER_ONLY,
        "retrieve": OFFICER_ONLY,
        "match": OFFICER_ONLY,
        "rematch": OFFICER_ONLY,
        # Deliberately NOT widened with the case-closure roles. Reaching this
        # action at all means reading a retained inbound message and its
        # version out of the officer-only receipt queue, which exists to keep
        # requester correspondence off screens that do not need it. Closing a
        # case is now everyone's work; correcting where retained inbound
        # evidence is filed is still the officer's.
        "redirect": OFFICER_ONLY,
        "associate_clarification": OFFICER_ONLY,
    }

    def get_queryset(self):
        queryset = (
            InboundCorrespondenceReceipt.objects.select_related(
                "candidate_request",
                "matched_request",
                "matched_correspondence",
            )
            .prefetch_related("attachments", "delivery_notifications")
            .annotate(
                attachment_count=Count("attachments", distinct=True),
                downloadable_attachment_count=Count(
                    "attachments",
                    filter=Q(
                        attachments__scan_status=(
                            CorrespondenceAttachment.ScanStatus.CLEAN
                        ),
                        attachments__quarantined=False,
                    ),
                    distinct=True,
                ),
                quarantined_attachment_count=Count(
                    "attachments",
                    filter=Q(attachments__quarantined=True),
                    distinct=True,
                ),
            )
        )
        if self.action != "list":
            return queryset
        queryset = _with_work_items(queryset)
        queue = self.request.query_params.get("queue")
        if queue == "unmatched":
            return queryset.filter(
                matching_status__in=UNMATCHED_STATES,
                quarantined=False,
                _work_item_decided=False,
            ).order_by("received_at", "id")
        if queue == "quarantined":
            return queryset.filter(_work_item_decided=False).filter(
                Q(quarantined=True)
                | Q(
                    matching_status=(
                        InboundCorrespondenceReceipt.MatchingStatus.QUARANTINED
                    )
                )
                | Q(attachments__quarantined=True)
            ).distinct().order_by("received_at", "id")
        return queryset.none()

    def get_serializer_class(self):
        if self.action == "list":
            return InboundReceiptListSerializer
        return InboundReceiptDetailSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "queue",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=True,
                enum=["unmatched", "quarantined"],
                description=(
                    "Select the PII-minimal staff review queue. No implicit "
                    "all-receipts view is available."
                ),
            )
        ],
        responses={200: InboundReceiptListSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        queue = request.query_params.get("queue")
        if queue is None:
            return Response(
                {
                    "code": "queue_required",
                    "detail": "Select the unmatched or quarantined queue.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if queue not in {"unmatched", "quarantined"}:
            return Response(
                {
                    "code": "invalid_queue",
                    "detail": "Queue must be unmatched or quarantined.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().list(request, *args, **kwargs)

    def _detail_response(self, receipt):
        refreshed = self.get_queryset().get(pk=receipt.pk)
        return Response(InboundReceiptDetailSerializer(refreshed).data)

    def _associate(self, request, *, operation, capability_id, action_text):
        handoff = human_only_handoff(
            request,
            capability_id=capability_id,
            action=action_text,
        )
        if handoff:
            return handoff
        receipt = self.get_object()
        serializer = ManualInboundAssociationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        destination = get_object_or_404(
            PublicRecordsRequest.objects.all(),
            number=data["request_number"],
        )
        try:
            receipt = operation(
                receipt,
                request=destination,
                actor=request.user,
                reason=data["reason"],
                expected_version=data["expected_version"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except services.VersionConflict as exc:
            return Response(
                {
                    "code": "version_conflict",
                    "detail": _validation_detail(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ValidationError as exc:
            return Response(
                {
                    "code": "validation_error",
                    "detail": _validation_detail(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return self._detail_response(receipt)

    @extend_schema(
        request=ManualInboundAssociationSerializer,
        responses={200: InboundReceiptDetailSerializer},
    )
    @action(detail=True, methods=["post"])
    def match(self, request, pk=None):
        return self._associate(
            request,
            operation=inbound.match_inbound_receipt,
            capability_id="inbound_correspondence.match",
            action_text="Match retained inbound correspondence to a request.",
        )

    @extend_schema(
        request=ManualInboundAssociationSerializer,
        responses={200: InboundReceiptDetailSerializer},
    )
    @action(detail=True, methods=["post"])
    def rematch(self, request, pk=None):
        return self._associate(
            request,
            operation=inbound.rematch_inbound_receipt,
            capability_id="inbound_correspondence.rematch",
            action_text=(
                "Correct the request association for retained inbound "
                "correspondence."
            ),
        )

    @extend_schema(
        request=ManualInboundAssociationSerializer,
        responses={200: InboundReceiptDetailSerializer},
    )
    @action(detail=True, methods=["post"])
    def redirect(self, request, pk=None):
        """Add an auto-created email case's message to an existing case."""
        return self._associate(
            request,
            operation=email_intake.redirect_email_intake_receipt,
            capability_id="inbound_correspondence.redirect_email_intake",
            action_text=(
                "Add an automatically opened email case's retained message to "
                "an existing request and close the leftover case."
            ),
        )

    @extend_schema(
        request=ClarificationResponseAssociationSerializer,
        responses={200: InboundReceiptDetailSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="associate-clarification",
    )
    def associate_clarification(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id=(
                "inbound_correspondence.associate_clarification_response"
            ),
            action=(
                "Associate retained inbound correspondence with an open "
                "clarification cycle."
            ),
        )
        if handoff:
            return handoff
        receipt = self.get_object()
        serializer = ClarificationResponseAssociationSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cycle = get_object_or_404(
            ClarificationCycle.objects.all(),
            pk=data["clarification_cycle_id"],
        )
        try:
            receipt = inbound.associate_clarification_response(
                receipt,
                clarification_cycle=cycle,
                actor=request.user,
                reason=data["reason"],
                expected_version=data["expected_version"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except services.VersionConflict as exc:
            return Response(
                {
                    "code": "version_conflict",
                    "detail": _validation_detail(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ValidationError as exc:
            return Response(
                {
                    "code": "validation_error",
                    "detail": _validation_detail(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return self._detail_response(receipt)


def register(router):
    router.register(
        "inbound-correspondence-receipts",
        InboundCorrespondenceReceiptViewSet,
        basename="inbound-correspondence-receipt",
    )
