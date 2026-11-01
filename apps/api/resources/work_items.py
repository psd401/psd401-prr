import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import (
    Case,
    CharField,
    Count,
    DateField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Cast, Replace
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_field,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import WORKFLOW_STAFF_ROLES, StaffRole, staff_roles
from apps.correspondence.models import InboundCorrespondenceReceipt
from apps.records.models import CollectionTask, RequestStatus
from apps.records.services import CLOSED_STATUSES
from apps.work_queue import services
from apps.work_queue.models import WorkItem, WorkItemEvent, WorkItemReceipt

BULK_METADATA_MAX_ITEMS = 50
UNRESOLVED_DISPOSITIONS = (
    WorkItem.Disposition.NEEDS_ATTENTION,
    WorkItem.Disposition.WAITING,
)
REVIEW_WORK_KINDS = (
    WorkItem.WorkKind.RECORD_REVIEW,
    WorkItem.WorkKind.SECOND_REVIEW,
)
INBOX_MESSAGE_WORK_KINDS = (
    WorkItem.WorkKind.INBOUND_REPLY,
    WorkItem.WorkKind.EMAIL_INTAKE,
)
WORK_QUEUE_NAMES = {
    "personal",
    "team",
    "unassigned",
    "waiting",
    "critical",
    "recently_changed",
    "review",
    "inbox_needs_attention",
    "inbox_awaiting_statutory_response",
    "inbox_new",
    "inbox_unmatched",
    "inbox_quarantined",
    "inbox_delivery_exceptions",
    "inbox_fee_unpaid",
    "inbox_waiting",
    "inbox_handled",
    "inbox_no_action",
    "inbox_all",
}
NEXT_ACTION_LABELS = {
    WorkItem.WorkKind.REQUEST_ACTION: "Respond to request",
    WorkItem.WorkKind.CORRESPONDENCE_DRAFT: "Prepare correspondence",
    WorkItem.WorkKind.CORRESPONDENCE_APPROVAL: "Review correspondence",
    WorkItem.WorkKind.INBOUND_REPLY: "Review requester reply",
    WorkItem.WorkKind.EMAIL_INTAKE: "Review emailed request",
    WorkItem.WorkKind.INBOUND_UNMATCHED: "Match inbound message",
    WorkItem.WorkKind.INBOUND_CANDIDATE: "Confirm message match",
    WorkItem.WorkKind.INBOUND_AMBIGUOUS: "Resolve ambiguous message",
    WorkItem.WorkKind.INBOUND_QUARANTINED: "Review quarantined message",
    WorkItem.WorkKind.DELIVERY_EXCEPTION: "Resolve delivery exception",
    WorkItem.WorkKind.FEE_UNPAID: "Decide on an unpaid fee or deposit",
    WorkItem.WorkKind.COLLECTION: "Complete record search",
    WorkItem.WorkKind.RECORD_REVIEW: "Review record",
    WorkItem.WorkKind.SECOND_REVIEW: "Complete second review",
}
NEXT_ACTION_PRIORITY = {
    WorkItem.WorkKind.DELIVERY_EXCEPTION: 0,
    WorkItem.WorkKind.REQUEST_ACTION: 1,
    WorkItem.WorkKind.INBOUND_QUARANTINED: 2,
    WorkItem.WorkKind.INBOUND_UNMATCHED: 3,
    WorkItem.WorkKind.INBOUND_AMBIGUOUS: 3,
    WorkItem.WorkKind.INBOUND_CANDIDATE: 3,
    WorkItem.WorkKind.INBOUND_REPLY: 4,
    WorkItem.WorkKind.EMAIL_INTAKE: 4,
    WorkItem.WorkKind.CORRESPONDENCE_APPROVAL: 5,
    WorkItem.WorkKind.CORRESPONDENCE_DRAFT: 6,
    WorkItem.WorkKind.FEE_UNPAID: 6,
    WorkItem.WorkKind.COLLECTION: 7,
    WorkItem.WorkKind.SECOND_REVIEW: 8,
    WorkItem.WorkKind.RECORD_REVIEW: 9,
}


def role_scoped_work_items(queryset, user):
    """Apply the canonical per-role visibility policy to work items."""
    roles = staff_roles(user)
    if StaffRole.RECORDS_OFFICER in roles:
        return queryset
    filters = Q(pk__in=[])
    if StaffRole.ADMIN in roles:
        filters |= Q(work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION)
    if StaffRole.REVIEWER in roles:
        filters |= Q(
            work_kind__in=[
                WorkItem.WorkKind.RECORD_REVIEW,
                WorkItem.WorkKind.SECOND_REVIEW,
            ]
        )
    if StaffRole.LIAISON in roles:
        filters |= Q(
            assignee=user,
            work_kind__in=[
                WorkItem.WorkKind.COLLECTION,
                WorkItem.WorkKind.CORRESPONDENCE_DRAFT,
            ],
        )
    return queryset.filter(filters)


def annotate_work_item_sources(queryset):
    """Attach the bounded source facts used by every work ordering surface."""
    collection_due_date = (
        CollectionTask.objects.annotate(
            _source_id_text=Cast("pk", output_field=CharField()),
        )
        .filter(_source_id_text=OuterRef("source_id"))
        .values("due_date")[:1]
    )
    matched_correspondence_id = (
        InboundCorrespondenceReceipt.objects.annotate(
            _source_id_text=Replace(
                Cast("pk", output_field=CharField()),
                Value("-"),
                Value(""),
            ),
            _correspondence_id_text=Cast(
                "matched_correspondence_id",
                output_field=CharField(),
            ),
        )
        .filter(
            _source_id_text=Replace(
                OuterRef("source_id"),
                Value("-"),
                Value(""),
            )
        )
        .values("_correspondence_id_text")[:1]
    )
    return queryset.annotate(
        _source_due_date=Subquery(
            collection_due_date,
            output_field=DateField(),
        ),
        _source_correspondence_id=Subquery(
            matched_correspondence_id,
            output_field=CharField(),
        ),
    )


def order_work_items_for_next_action(queryset):
    """Apply the canonical UX-01 urgency and work-kind ordering."""
    priority = Case(
        *[
            When(work_kind=work_kind, then=Value(value))
            for work_kind, value in NEXT_ACTION_PRIORITY.items()
        ],
        default=Value(99),
        output_field=IntegerField(),
    )
    urgency_date = Case(
        When(
            work_kind=WorkItem.WorkKind.COLLECTION,
            then=F("_source_due_date"),
        ),
        When(
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request__status=RequestStatus.RECEIVED,
            then=F("request__statutory_response_due"),
        ),
        default=F("request__estimated_completion"),
        output_field=DateField(),
    )
    return queryset.annotate(
        _next_action_priority=priority,
        _urgency_date=urgency_date,
    ).order_by(
        F("_urgency_date").asc(nulls_last=True),
        "_next_action_priority",
        "-updated_at",
        "-id",
    )


class StrictSerializer(serializers.Serializer):
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


class WorkItemEventSerializer(serializers.ModelSerializer):
    actor_id = serializers.IntegerField(read_only=True, allow_null=True)
    actor_username = serializers.CharField(
        source="actor.username",
        read_only=True,
        allow_null=True,
    )
    metadata = serializers.SerializerMethodField()

    class Meta:
        model = WorkItemEvent
        fields = [
            "id",
            "event_type",
            "actor_id",
            "actor_username",
            "occurred_at",
            "expected_version",
            "resulting_version",
            "metadata",
        ]

    @extend_schema_field(serializers.JSONField())
    def get_metadata(self, obj) -> dict:
        metadata = dict(obj.metadata)
        request = self.context.get("request")
        if (
            "reason" in metadata
            and (
                request is None
                or StaffRole.RECORDS_OFFICER not in staff_roles(request.user)
            )
        ):
            metadata.pop("reason")
        return metadata


class WorkItemListSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(
        source="request.number",
        read_only=True,
        allow_null=True,
    )
    assignee_id = serializers.IntegerField(read_only=True, allow_null=True)
    assignee_username = serializers.CharField(
        source="assignee.username",
        read_only=True,
        allow_null=True,
    )
    is_read = serializers.SerializerMethodField()
    read_at = serializers.SerializerMethodField()
    urgency_date = serializers.SerializerMethodField()
    next_action = serializers.SerializerMethodField()
    detail_url = serializers.SerializerMethodField()
    source_url = serializers.SerializerMethodField()

    class Meta:
        model = WorkItem
        fields = [
            "id",
            "source_type",
            "source_id",
            "work_kind",
            "request_number",
            "assignee_id",
            "assignee_username",
            "disposition",
            "version",
            "is_read",
            "read_at",
            "urgency_date",
            "next_action",
            "detail_url",
            "source_url",
            "disposed_at",
            "created_at",
            "updated_at",
        ]

    @staticmethod
    def _receipt(obj):
        receipts = getattr(obj, "_current_user_receipts", ())
        return receipts[0] if receipts else None

    @extend_schema_field(serializers.BooleanField())
    def get_is_read(self, obj):
        receipt = self._receipt(obj)
        return bool(receipt and receipt.read_at)

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_read_at(self, obj):
        receipt = self._receipt(obj)
        return receipt.read_at if receipt else None

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_urgency_date(self, obj):
        if obj.work_kind == WorkItem.WorkKind.COLLECTION:
            return getattr(obj, "_source_due_date", None)
        if obj.request is None:
            return None
        if (
            obj.work_kind == WorkItem.WorkKind.REQUEST_ACTION
            and obj.request.status == RequestStatus.RECEIVED
        ):
            return obj.request.statutory_response_due
        return obj.request.estimated_completion

    @extend_schema_field(serializers.CharField())
    def get_next_action(self, obj):
        return NEXT_ACTION_LABELS[obj.work_kind]

    @extend_schema_field(serializers.CharField())
    def get_detail_url(self, obj):
        return f"/work/{obj.pk}"

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_source_url(self, obj):
        request_number = obj.request.number if obj.request else None
        if obj.source_type == WorkItem.SourceType.INBOUND_RECEIPT:
            if obj.work_kind in {
                WorkItem.WorkKind.INBOUND_UNMATCHED,
                WorkItem.WorkKind.INBOUND_CANDIDATE,
                WorkItem.WorkKind.INBOUND_AMBIGUOUS,
                WorkItem.WorkKind.INBOUND_QUARANTINED,
            }:
                view = (
                    "quarantined"
                    if obj.work_kind == WorkItem.WorkKind.INBOUND_QUARANTINED
                    else "unmatched"
                )
                return f"/inbox?view={view}&inbound_receipt={obj.source_id}"
            correspondence_id = getattr(obj, "_source_correspondence_id", None)
            if request_number and correspondence_id:
                correspondence_id = str(uuid.UUID(str(correspondence_id)))
                return (
                    f"/requests/{request_number}/messages"
                    f"?message={correspondence_id}"
                )
            return f"/inbox?view=unmatched&inbound_receipt={obj.source_id}"
        if obj.source_type == WorkItem.SourceType.RECORD_ITEM:
            if not request_number:
                return None
            return f"/requests/{request_number}/records?item={obj.source_id}"
        if obj.source_type == WorkItem.SourceType.COLLECTION_TASK:
            if not request_number:
                return None
            return f"/requests/{request_number}/collection?task={obj.source_id}"
        if obj.source_type == WorkItem.SourceType.CORRESPONDENCE:
            if not request_number:
                return None
            return f"/requests/{request_number}/messages?message={obj.source_id}"
        if obj.source_type == WorkItem.SourceType.REQUEST:
            return (
                f"/requests/{request_number}/overview"
                if request_number
                else None
            )
        if request_number:
            return f"/requests/{request_number}/activity?workItem={obj.pk}"
        return None


class WorkItemDetailSerializer(WorkItemListSerializer):
    events = WorkItemEventSerializer(many=True, read_only=True)
    disposed_by_username = serializers.CharField(
        source="disposed_by.username",
        read_only=True,
        allow_null=True,
    )
    no_action_by_id = serializers.IntegerField(read_only=True, allow_null=True)
    no_action_by_username = serializers.CharField(
        source="no_action_by.username",
        read_only=True,
        allow_null=True,
    )
    no_action_reason = serializers.SerializerMethodField()

    class Meta(WorkItemListSerializer.Meta):
        fields = [*WorkItemListSerializer.Meta.fields, "disposed_by_id", "disposed_by_username", "no_action_category", "no_action_reason", "no_action_by_id", "no_action_by_username", "no_action_at", "events"]

    def get_no_action_reason(self, obj) -> str:
        request = self.context.get("request")
        if (
            request is not None
            and StaffRole.RECORDS_OFFICER in staff_roles(request.user)
        ):
            return obj.no_action_reason
        return ""


class IdempotencySerializer(StrictSerializer):
    idempotency_key = serializers.CharField(
        min_length=1,
        max_length=200,
        trim_whitespace=True,
    )


class VersionedMutationSerializer(IdempotencySerializer):
    expected_version = serializers.IntegerField(min_value=1)


class AssignmentSerializer(VersionedMutationSerializer):
    assignee_id = serializers.IntegerField(allow_null=True)


class DispositionSerializer(VersionedMutationSerializer):
    disposition = serializers.ChoiceField(
        choices=[
            WorkItem.Disposition.WAITING,
            WorkItem.Disposition.HANDLED,
        ]
    )


class NoActionRequiredSerializer(VersionedMutationSerializer):
    category = serializers.ChoiceField(choices=WorkItem.NoActionCategory.choices)
    reason = serializers.CharField(
        min_length=10,
        max_length=2000,
        trim_whitespace=True,
    )


class BulkMetadataItemSerializer(VersionedMutationSerializer):
    work_item_id = serializers.UUIDField()
    action = serializers.ChoiceField(
        choices=[
            "read",
            "unread",
            "assign",
            "unassign",
            "waiting",
            "handled",
            "reopen",
        ]
    )
    assignee_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if attrs["action"] == "assign" and "assignee_id" not in attrs:
            raise serializers.ValidationError(
                {"assignee_id": ["This field is required for assignment."]}
            )
        if attrs["action"] != "assign" and "assignee_id" in attrs:
            raise serializers.ValidationError(
                {"assignee_id": ["This field is accepted only for assignment."]}
            )
        return attrs


class BulkMetadataSerializer(StrictSerializer):
    items = BulkMetadataItemSerializer(
        many=True,
        allow_empty=False,
        max_length=BULK_METADATA_MAX_ITEMS,
        help_text=(
            "At most 50 read, assignment, waiting, handled, or reopen metadata "
            "mutations. Legal and content actions are not accepted."
        ),
    )

    def validate_items(self, items):
        ids = [item["work_item_id"] for item in items]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError(
                "Each work item may appear at most once in a bulk mutation."
            )
        return items


class WorkItemCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField(min_value=0)
    unresolved = serializers.IntegerField(min_value=0)
    needs_attention = serializers.IntegerField(min_value=0)
    waiting = serializers.IntegerField(min_value=0)
    handled = serializers.IntegerField(min_value=0)
    no_action_required = serializers.IntegerField(min_value=0)
    source_excluded = serializers.IntegerField(min_value=0)
    personal = serializers.IntegerField(min_value=0)
    team = serializers.IntegerField(min_value=0)
    unassigned = serializers.IntegerField(min_value=0)
    critical = serializers.IntegerField(min_value=0)
    recently_changed = serializers.IntegerField(min_value=0)
    review = serializers.IntegerField(min_value=0)
    inbox_needs_attention = serializers.IntegerField(min_value=0)
    inbox_updated_cases = serializers.IntegerField(min_value=0)
    inbox_open_cases = serializers.IntegerField(min_value=0)
    inbox_awaiting_statutory_response = serializers.IntegerField(min_value=0)
    inbox_new = serializers.IntegerField(min_value=0)
    inbox_unmatched = serializers.IntegerField(min_value=0)
    inbox_quarantined = serializers.IntegerField(min_value=0)
    inbox_delivery_exceptions = serializers.IntegerField(min_value=0)
    inbox_fee_unpaid = serializers.IntegerField(min_value=0)
    inbox_waiting = serializers.IntegerField(min_value=0)
    inbox_handled = serializers.IntegerField(min_value=0)
    inbox_no_action = serializers.IntegerField(min_value=0)
    inbox_all = serializers.IntegerField(min_value=0)


class AssignmentTargetSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    display_name = serializers.SerializerMethodField()

    def get_display_name(self, obj) -> str:
        return obj.get_full_name().strip() or obj.username


class WorkItemMutationResponseSerializer(WorkItemListSerializer):
    mutation_event_id = serializers.UUIDField()
    idempotent = serializers.BooleanField()

    class Meta(WorkItemListSerializer.Meta):
        fields = [*WorkItemListSerializer.Meta.fields, "mutation_event_id", "idempotent"]


class BulkMetadataResultSerializer(serializers.Serializer):
    work_item_id = serializers.UUIDField()
    mutation_event_id = serializers.UUIDField()
    idempotent = serializers.BooleanField()


class BulkMetadataResponseSerializer(serializers.Serializer):
    results = BulkMetadataResultSerializer(many=True)


def _error_response(exc):
    if isinstance(exc, services.VersionConflict):
        return Response(
            {
                "code": "version_conflict",
                "detail": str(exc),
                "expected_version": exc.expected,
                "current_version": exc.current,
            },
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, services.IdempotencyConflict):
        return Response(
            {"code": "idempotency_conflict", "detail": str(exc)},
            status=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, PermissionDenied):
        return Response(
            {"code": "permission_denied", "detail": str(exc)},
            status=status.HTTP_403_FORBIDDEN,
        )
    if isinstance(exc, ValidationError):
        detail = (
            next(iter(exc.message_dict.values()))[0]
            if hasattr(exc, "message_dict")
            else exc.messages[0]
        )
        return Response(
            {"code": "validation_error", "detail": str(detail)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    raise exc


class WorkItemViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = WorkItem.objects.none()
    serializer_class = WorkItemListSerializer
    allow_readonly_agent = False
    human_only_handoff_actions = {"no_action_required"}
    role_permissions = {
        "list": WORKFLOW_STAFF_ROLES,
        "retrieve": WORKFLOW_STAFF_ROLES,
        "counts": WORKFLOW_STAFF_ROLES,
        "assignment_targets": WORKFLOW_STAFF_ROLES,
        "read": WORKFLOW_STAFF_ROLES,
        "unread": WORKFLOW_STAFF_ROLES,
        "assignment": WORKFLOW_STAFF_ROLES,
        "disposition": WORKFLOW_STAFF_ROLES,
        "reopen": WORKFLOW_STAFF_ROLES,
        "no_action_required": {StaffRole.RECORDS_OFFICER},
        "bulk_metadata": WORKFLOW_STAFF_ROLES,
    }

    def _response_queryset(self):
        return annotate_work_item_sources(WorkItem.objects.all()).select_related(
            "request",
            "assignee",
            "disposed_by",
            "no_action_by",
        ).prefetch_related(
            Prefetch(
                "receipts",
                queryset=WorkItemReceipt.objects.filter(user=self.request.user),
                to_attr="_current_user_receipts",
            )
        )

    def _role_scoped_queryset(self):
        return role_scoped_work_items(
            self._response_queryset(),
            self.request.user,
        )

    def get_queryset(self):
        queryset = self._role_scoped_queryset()
        if self.action == "retrieve":
            queryset = queryset.prefetch_related(
                Prefetch(
                    "events",
                    queryset=WorkItemEvent.objects.select_related("actor"),
                )
            )
        if self.action != "list":
            return queryset
        queue = self.request.query_params.get("queue")
        if queue is not None:
            queryset = self._filter_named_queue(queryset, queue)
        else:
            disposition = self.request.query_params.get("disposition")
            if disposition is None:
                queryset = queryset.filter(
                    disposition__in=UNRESOLVED_DISPOSITIONS,
                )
            elif disposition != "all":
                if disposition not in WorkItem.Disposition.values:
                    return queryset.none()
                queryset = queryset.filter(disposition=disposition)
        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(source_id__icontains=search)
                | Q(request__number__icontains=search)
                | Q(assignee__username__icontains=search)
                | Q(work_kind__icontains=search)
            )
        if queue == "recently_changed" or queue in {
            "inbox_new",
            "inbox_unmatched",
            "inbox_quarantined",
            "inbox_delivery_exceptions",
            "inbox_waiting",
            "inbox_handled",
            "inbox_no_action",
            "inbox_all",
        }:
            return queryset.order_by("-updated_at", "-id")
        return order_work_items_for_next_action(queryset)

    def _filter_named_queue(self, queryset, queue):
        if queue not in WORK_QUEUE_NAMES:
            return queryset.none()
        unresolved = Q(disposition__in=UNRESOLVED_DISPOSITIONS)
        # Same definition as the chip counts: a closed case is not Inbox
        # business, so the named Inbox queues leave its mail out as well.
        inbox_messages = Q(
            work_kind__in=INBOX_MESSAGE_WORK_KINDS,
            _source_correspondence_id__isnull=False,
        ) & ~Q(request__status__in=CLOSED_STATUSES)
        if queue == "personal":
            return queryset.filter(unresolved, assignee=self.request.user)
        if queue == "team":
            return queryset.filter(unresolved)
        if queue == "unassigned":
            return queryset.filter(unresolved, assignee__isnull=True)
        if queue == "waiting":
            return queryset.filter(disposition=WorkItem.Disposition.WAITING)
        if queue == "critical":
            return queryset.filter(unresolved).filter(
                Q(
                    work_kind=WorkItem.WorkKind.REQUEST_ACTION,
                    request__status=RequestStatus.RECEIVED,
                    request__statutory_response_due__lte=timezone.localdate(),
                )
                | Q(
                    work_kind=WorkItem.WorkKind.COLLECTION,
                    _source_due_date__lte=timezone.localdate(),
                )
                | Q(
                    work_kind__in=[
                        WorkItem.WorkKind.DELIVERY_EXCEPTION,
                        WorkItem.WorkKind.INBOUND_QUARANTINED,
                    ]
                )
            )
        if queue == "recently_changed":
            return queryset.filter(
                updated_at__gte=timezone.now() - timedelta(days=7)
            )
        if queue == "review":
            return queryset.filter(unresolved, work_kind__in=REVIEW_WORK_KINDS)
        if queue == "inbox_needs_attention":
            return queryset.filter(
                inbox_messages,
                disposition=WorkItem.Disposition.NEEDS_ATTENTION,
            )
        if queue == "inbox_awaiting_statutory_response":
            # A REQUEST_ACTION item exists exactly while the request is
            # received with no delivered initial response, so this is the
            # authoritative day-5 backlog rather than a parallel definition.
            return queryset.filter(
                unresolved,
                work_kind=WorkItem.WorkKind.REQUEST_ACTION,
                request__status=RequestStatus.RECEIVED,
            )
        if queue == "inbox_new":
            current_read = WorkItemReceipt.objects.filter(
                work_item=OuterRef("pk"),
                user=self.request.user,
                read_at__isnull=False,
            )
            # Unread AND still waiting. A handled or closed-out row you never
            # happened to open is not news; it is done.
            return queryset.annotate(_current_user_read=Exists(current_read)).filter(
                inbox_messages,
                unresolved,
                _current_user_read=False,
            )
        if queue == "inbox_unmatched":
            return queryset.filter(
                unresolved,
                work_kind__in=[
                    WorkItem.WorkKind.INBOUND_UNMATCHED,
                    WorkItem.WorkKind.INBOUND_CANDIDATE,
                    WorkItem.WorkKind.INBOUND_AMBIGUOUS,
                ],
            )
        if queue == "inbox_quarantined":
            return queryset.filter(
                unresolved,
                work_kind=WorkItem.WorkKind.INBOUND_QUARANTINED,
            )
        if queue == "inbox_delivery_exceptions":
            return queryset.filter(
                unresolved,
                work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION,
            )
        if queue == "inbox_fee_unpaid":
            return queryset.filter(
                unresolved,
                work_kind=WorkItem.WorkKind.FEE_UNPAID,
            )
        if queue == "inbox_waiting":
            return queryset.filter(
                inbox_messages,
                disposition=WorkItem.Disposition.WAITING,
            )
        if queue == "inbox_handled":
            return queryset.filter(
                inbox_messages,
                disposition=WorkItem.Disposition.HANDLED,
            )
        if queue == "inbox_no_action":
            return queryset.filter(
                inbox_messages,
                disposition=WorkItem.Disposition.NO_ACTION_REQUIRED,
            )
        return queryset.filter(inbox_messages)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return WorkItemDetailSerializer
        return WorkItemListSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "queue",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                enum=sorted(WORK_QUEUE_NAMES),
                description=(
                    "Named role-scoped shell queue. My Work queues use statutory "
                    "urgency and next-action ordering."
                ),
            ),
            OpenApiParameter(
                "disposition",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                enum=["all", *WorkItem.Disposition.values],
                description=(
                    "Filter by one shared handling state, or use all to include "
                    "disposed and source-excluded work. Defaults to unresolved."
                ),
            ),
            OpenApiParameter(
                "search",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description=(
                    "Search source ID, request number, assignee username, or "
                    "work kind without exposing requester or message content."
                ),
            ),
        ],
        responses={200: WorkItemListSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        queue = request.query_params.get("queue")
        if queue is not None and queue not in WORK_QUEUE_NAMES:
            raise serializers.ValidationError({"queue": ["Unknown named queue."]})
        if queue is not None and "disposition" in request.query_params:
            raise serializers.ValidationError(
                {
                    "disposition": [
                        "Do not combine disposition with a named queue."
                    ]
                }
            )
        return super().list(request, *args, **kwargs)

    @extend_schema(responses={200: WorkItemDetailSerializer})
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def _mutation_response(self, result):
        # Authorization was performed against the pre-mutation role scope and
        # rechecked under lock in the service. An unassignment or reassignment
        # may intentionally remove a liaison from the post-mutation scope.
        refreshed = self._response_queryset().get(pk=result.work_item.pk)
        payload = WorkItemListSerializer(refreshed).data
        payload["mutation_event_id"] = str(result.event.pk)
        payload["idempotent"] = result.idempotent
        return Response(payload)

    def _perform(self, operation):
        try:
            return self._mutation_response(operation())
        except (
            ValidationError,
            PermissionDenied,
            services.VersionConflict,
            services.IdempotencyConflict,
        ) as exc:
            return _error_response(exc)

    @extend_schema(
        request=None,
        responses={200: WorkItemCountsSerializer},
    )
    @action(detail=False, methods=["get"])
    def counts(self, request):
        queryset = self._role_scoped_queryset().annotate(
            _current_user_read=Exists(
                WorkItemReceipt.objects.filter(
                    work_item=OuterRef("pk"),
                    user=request.user,
                    read_at__isnull=False,
                )
            )
        )
        unresolved = Q(disposition__in=UNRESOLVED_DISPOSITIONS)
        # The message chips count what their lists show by default: messages
        # on cases that are still open. A closed case is not Inbox business.
        inbox_messages = Q(
            work_kind__in=INBOX_MESSAGE_WORK_KINDS,
            _source_correspondence_id__isnull=False,
        ) & ~Q(request__status__in=CLOSED_STATUSES)
        values = queryset.aggregate(
            total=Count("id"),
            unresolved=Count(
                "id",
                filter=unresolved,
            ),
            needs_attention=Count(
                "id",
                filter=Q(disposition=WorkItem.Disposition.NEEDS_ATTENTION),
            ),
            waiting=Count(
                "id",
                filter=Q(disposition=WorkItem.Disposition.WAITING),
            ),
            handled=Count(
                "id",
                filter=Q(disposition=WorkItem.Disposition.HANDLED),
            ),
            no_action_required=Count(
                "id",
                filter=Q(disposition=WorkItem.Disposition.NO_ACTION_REQUIRED),
            ),
            source_excluded=Count(
                "id",
                filter=Q(disposition=WorkItem.Disposition.SOURCE_EXCLUDED),
            ),
            personal=Count(
                "id",
                filter=unresolved & Q(assignee=request.user),
            ),
            team=Count("id", filter=unresolved),
            unassigned=Count(
                "id",
                filter=unresolved & Q(assignee__isnull=True),
            ),
            critical=Count(
                "id",
                filter=(
                    unresolved
                    & (
                        Q(
                            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
                            request__status=RequestStatus.RECEIVED,
                            request__statutory_response_due__lte=(
                                timezone.localdate()
                            ),
                        )
                        | Q(
                            work_kind=WorkItem.WorkKind.COLLECTION,
                            _source_due_date__lte=timezone.localdate(),
                        )
                        | Q(
                            work_kind__in=[
                                WorkItem.WorkKind.DELIVERY_EXCEPTION,
                                WorkItem.WorkKind.INBOUND_QUARANTINED,
                            ]
                        )
                    )
                ),
            ),
            recently_changed=Count(
                "id",
                filter=Q(updated_at__gte=timezone.now() - timedelta(days=7)),
            ),
            review=Count(
                "id",
                filter=unresolved & Q(work_kind__in=REVIEW_WORK_KINDS),
            ),
            inbox_needs_attention=Count(
                "id",
                filter=(
                    inbox_messages
                    & Q(disposition=WorkItem.Disposition.NEEDS_ATTENTION)
                ),
            ),
            # Cases, not messages: the per-case Inbox view flags a case once
            # however many requester emails are waiting on it.
            inbox_updated_cases=Count(
                "request",
                distinct=True,
                filter=(
                    inbox_messages
                    & Q(disposition=WorkItem.Disposition.NEEDS_ATTENTION)
                ),
            ),
            inbox_awaiting_statutory_response=Count(
                "id",
                filter=(
                    unresolved
                    & Q(
                        work_kind=WorkItem.WorkKind.REQUEST_ACTION,
                        request__status=RequestStatus.RECEIVED,
                    )
                ),
            ),
            inbox_new=Count(
                "id",
                filter=(
                    inbox_messages
                    & unresolved
                    & Q(_current_user_read=False)
                ),
            ),
            inbox_unmatched=Count(
                "id",
                filter=(
                    unresolved
                    & Q(
                        work_kind__in=[
                            WorkItem.WorkKind.INBOUND_UNMATCHED,
                            WorkItem.WorkKind.INBOUND_CANDIDATE,
                            WorkItem.WorkKind.INBOUND_AMBIGUOUS,
                        ]
                    )
                ),
            ),
            inbox_quarantined=Count(
                "id",
                filter=(
                    unresolved
                    & Q(work_kind=WorkItem.WorkKind.INBOUND_QUARANTINED)
                ),
            ),
            inbox_delivery_exceptions=Count(
                "id",
                filter=(
                    unresolved
                    & Q(work_kind=WorkItem.WorkKind.DELIVERY_EXCEPTION)
                ),
            ),
            inbox_fee_unpaid=Count(
                "id",
                filter=(
                    unresolved & Q(work_kind=WorkItem.WorkKind.FEE_UNPAID)
                ),
            ),
            inbox_waiting=Count(
                "id",
                filter=(
                    inbox_messages
                    & Q(disposition=WorkItem.Disposition.WAITING)
                ),
            ),
            inbox_handled=Count(
                "id",
                filter=(
                    inbox_messages
                    & Q(disposition=WorkItem.Disposition.HANDLED)
                ),
            ),
            inbox_no_action=Count(
                "id",
                filter=(
                    inbox_messages
                    & Q(disposition=WorkItem.Disposition.NO_ACTION_REQUIRED)
                ),
            ),
            inbox_all=Count("id", filter=inbox_messages),
        )
        # The Cases chip counts what the Cases view lists: every open case
        # this user may see, flagged or not. That is a case query, not a
        # work-item aggregate, because an open case may carry no work item.
        from apps.api.case_scope import scope_case_queryset
        from apps.records.models import PublicRecordsRequest

        values["inbox_open_cases"] = (
            scope_case_queryset(PublicRecordsRequest.objects.all(), request.user)
            .exclude(status__in=CLOSED_STATUSES)
            .count()
        )
        return Response(WorkItemCountsSerializer(values).data)

    @extend_schema(responses={200: AssignmentTargetSerializer(many=True)})
    @action(detail=True, methods=["get"], url_path="assignment-targets")
    def assignment_targets(self, request, pk=None):
        work_item = self.get_object()
        allowed_roles = services.roles_for_work_kind(work_item.work_kind)
        candidates = get_user_model().objects.filter(
            is_active=True,
            is_staff=True,
        ).order_by("first_name", "last_name", "username", "id")
        targets = [
            candidate
            for candidate in candidates
            if staff_roles(candidate).intersection(allowed_roles)
        ]
        return Response(AssignmentTargetSerializer(targets, many=True).data)

    @extend_schema(
        request=IdempotencySerializer,
        responses={200: WorkItemMutationResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        serializer = IdempotencySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = self.get_object()
        return self._perform(
            lambda: services.mark_work_item_read(
                item,
                actor=request.user,
                **serializer.validated_data,
            )
        )

    @extend_schema(
        request=IdempotencySerializer,
        responses={200: WorkItemMutationResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def unread(self, request, pk=None):
        serializer = IdempotencySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = self.get_object()
        return self._perform(
            lambda: services.mark_work_item_unread(
                item,
                actor=request.user,
                **serializer.validated_data,
            )
        )

    @extend_schema(
        request=AssignmentSerializer,
        responses={200: WorkItemMutationResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def assignment(self, request, pk=None):
        serializer = AssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = self.get_object()
        data = serializer.validated_data
        if data["assignee_id"] is None:
            def operation():
                return services.unassign_work_item(
                            item,
                            actor=request.user,
                            expected_version=data["expected_version"],
                            idempotency_key=data["idempotency_key"],
                        )
        else:
            assignee = get_object_or_404(
                get_user_model().objects.all(),
                pk=data["assignee_id"],
            )
            def operation():
                return services.assign_work_item(
                            item,
                            assignee=assignee,
                            actor=request.user,
                            expected_version=data["expected_version"],
                            idempotency_key=data["idempotency_key"],
                        )
        return self._perform(operation)

    @extend_schema(
        request=DispositionSerializer,
        responses={200: WorkItemMutationResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def disposition(self, request, pk=None):
        serializer = DispositionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = self.get_object()
        return self._perform(
            lambda: services.set_work_item_disposition(
                item,
                actor=request.user,
                **serializer.validated_data,
            )
        )

    @extend_schema(
        request=VersionedMutationSerializer,
        responses={200: WorkItemMutationResponseSerializer},
    )
    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        serializer = VersionedMutationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = self.get_object()
        return self._perform(
            lambda: services.reopen_work_item(
                item,
                actor=request.user,
                **serializer.validated_data,
            )
        )

    @extend_schema(
        request=NoActionRequiredSerializer,
        responses={200: WorkItemMutationResponseSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="no-action-required",
    )
    def no_action_required(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="work_items.no_action_required",
            action="Record that a work item requires no team action.",
        )
        if handoff:
            return handoff
        serializer = NoActionRequiredSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = self.get_object()
        return self._perform(
            lambda: services.set_no_action_required(
                item,
                actor=request.user,
                **serializer.validated_data,
            )
        )

    @extend_schema(
        request=BulkMetadataSerializer,
        responses={200: BulkMetadataResponseSerializer},
    )
    @action(detail=False, methods=["post"], url_path="bulk-metadata")
    def bulk_metadata(self, request):
        serializer = BulkMetadataSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        supplied = serializer.validated_data["items"]
        item_ids = {item["work_item_id"] for item in supplied}
        accessible = {
            item.pk: item
            for item in self._role_scoped_queryset().filter(pk__in=item_ids)
        }
        if set(accessible) != item_ids:
            return Response(
                {"code": "not_found", "detail": "A work item was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        user_model = get_user_model()
        try:
            with transaction.atomic():
                results = []
                for mutation in sorted(
                    supplied,
                    key=lambda value: str(value["work_item_id"]),
                ):
                    item = accessible[mutation["work_item_id"]]
                    action_name = mutation["action"]
                    common = {
                        "actor": request.user,
                        "idempotency_key": mutation["idempotency_key"],
                    }
                    if action_name in {"read", "unread"}:
                        operation = (
                            services.mark_work_item_read
                            if action_name == "read"
                            else services.mark_work_item_unread
                        )
                        result = operation(
                            item,
                            expected_version=mutation["expected_version"],
                            **common,
                        )
                    elif action_name == "assign":
                        assignee = get_object_or_404(
                            user_model.objects.all(),
                            pk=mutation["assignee_id"],
                        )
                        result = services.assign_work_item(
                            item,
                            assignee=assignee,
                            expected_version=mutation["expected_version"],
                            **common,
                        )
                    elif action_name == "unassign":
                        result = services.unassign_work_item(
                            item,
                            expected_version=mutation["expected_version"],
                            **common,
                        )
                    elif action_name in {"waiting", "handled"}:
                        result = services.set_work_item_disposition(
                            item,
                            disposition=action_name,
                            expected_version=mutation["expected_version"],
                            **common,
                        )
                    else:
                        result = services.reopen_work_item(
                            item,
                            expected_version=mutation["expected_version"],
                            **common,
                        )
                    results.append(
                        {
                            "work_item_id": str(item.pk),
                            "mutation_event_id": str(result.event.pk),
                            "idempotent": result.idempotent,
                        }
                    )
        except (
            ValidationError,
            PermissionDenied,
            services.VersionConflict,
            services.IdempotencyConflict,
        ) as exc:
            return _error_response(exc)
        return Response({"results": results})


def register(router):
    router.register("work-items", WorkItemViewSet, basename="work-items")
