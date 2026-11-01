# ABOUTME: Staff CRUD for the district record-system registry (RecordSystem)
# ABOUTME: — deletion is blocked because rows are referenced by CollectionTasks.
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import filters, serializers, status, viewsets
from rest_framework.response import Response

from apps.api.identity import (
    WORKFLOW_STAFF_ROLES,
    StaffRole,
    has_staff_role,
)
from apps.audit.models import audit
from apps.records.models import RecordSystem

TRUE_VALUES = {"true", "1"}
ROUTING_CONTROL_FIELDS = frozenset(
    {"active", "urgent_hold", "triage_keywords"}
)
SNAPSHOT_FIELDS = (
    "slug",
    "name",
    "category",
    "vendor",
    "integration",
    "custodian_title",
    "custodian_email",
    "default_assignee_id",
    "retention_days",
    "retention_note",
    "urgent_hold",
    "triage_keywords",
    "search_checklist",
    "needs_verification",
    "active",
)


def _snapshot(system):
    return {
        field: getattr(system, field)
        for field in SNAPSHOT_FIELDS
    }


def _require_routing_change_reason(instance, attrs):
    routing_change = instance is None or any(
        field in attrs
        and attrs[field] != getattr(instance, field)
        for field in ROUTING_CONTROL_FIELDS
    )
    reason = attrs.get("change_reason", "").strip()
    if routing_change and len(reason) < 10:
        raise serializers.ValidationError(
            {
                "change_reason": (
                    "A substantive reason of at least 10 characters is "
                    "required for preservation-routing changes."
                )
            }
        )
    return reason


class RecordSystemSerializer(serializers.ModelSerializer):
    default_assignee_id = serializers.PrimaryKeyRelatedField(
        source="default_assignee",
        queryset=get_user_model().objects.filter(is_active=True, is_staff=True),
        allow_null=True,
        required=False,
    )
    default_assignee_username = serializers.CharField(
        source="default_assignee.username",
        read_only=True,
        allow_null=True,
    )
    change_reason = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=1000,
        trim_whitespace=True,
    )

    class Meta:
        model = RecordSystem
        fields = ["id", "slug", "name", "category", "vendor", "integration",
                  "custodian_title", "custodian_email", "retention_days",
                  "retention_note", "urgent_hold", "triage_keywords",
                  "search_checklist", "needs_verification", "active",
                  "default_assignee_id", "default_assignee_username",
                  "change_reason"]

    def validate(self, attrs):
        _require_routing_change_reason(self.instance, attrs)
        assignee = attrs.get("default_assignee")
        if assignee is not None and not has_staff_role(
            assignee,
            StaffRole.RECORDS_OFFICER,
            StaffRole.LIAISON,
        ):
            raise serializers.ValidationError(
                {
                    "default_assignee_id": (
                        "The default assignee must be a records officer or liaison."
                    )
                }
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("change_reason", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # slug is settable at creation but immutable afterward — other rows
        # (CollectionTasks, triage routing) reference systems by slug.
        validated_data.pop("slug", None)
        validated_data.pop("change_reason", None)
        return super().update(instance, validated_data)


class RecordSystemViewSet(viewsets.ModelViewSet):
    queryset = RecordSystem.objects.order_by("name")
    serializer_class = RecordSystemSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "vendor", "custodian_title"]
    role_permissions = {
        "list": WORKFLOW_STAFF_ROLES,
        "retrieve": WORKFLOW_STAFF_ROLES,
        "create": {StaffRole.RECORDS_OFFICER},
        "update": {StaffRole.RECORDS_OFFICER},
        "partial_update": {StaffRole.RECORDS_OFFICER},
        "destroy": {StaffRole.ADMIN},
    }

    def perform_create(self, serializer):
        reason = serializer.validated_data["change_reason"].strip()
        with transaction.atomic():
            system = serializer.save()
            audit(
                "record_system.created",
                target=str(system.pk),
                actor=self.request.user,
                reason=reason,
                after=_snapshot(system),
            )

    def perform_update(self, serializer):
        reason = serializer.validated_data.get(
            "change_reason",
            "",
        ).strip()
        with transaction.atomic():
            system = RecordSystem.objects.select_for_update().get(
                pk=serializer.instance.pk
            )
            serializer.instance = system
            reason = _require_routing_change_reason(
                system,
                serializer.validated_data,
            )
            before = _snapshot(system)
            system = serializer.save()
            audit(
                "record_system.updated",
                target=str(system.pk),
                actor=self.request.user,
                reason=reason,
                before=before,
                after=_snapshot(system),
            )

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        if "needs_verification" in params:
            qs = qs.filter(needs_verification=_as_bool(params["needs_verification"]))
        if "active" in params:
            qs = qs.filter(active=_as_bool(params["active"]))
        category = params.get("category")
        if category:
            qs = qs.filter(category=category)

        return qs

    def destroy(self, request, *args, **kwargs):
        return Response(
            {"detail": "Record systems can't be deleted — registry rows are "
                       "referenced by collection tasks. Set active=false instead."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


def _as_bool(value):
    return value.lower() in TRUE_VALUES


def register(router):
    router.register("record-systems", RecordSystemViewSet, basename="record-system")
