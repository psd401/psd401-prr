# ABOUTME: Officer/admin-only PII-free correspondence operations API.
# ABOUTME: Provides aggregate queue metrics and fixed-code readiness results.
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.identity import StaffRole
from apps.correspondence.operations import (
    operations_snapshot,
    readiness_snapshot,
)

OPERATIONS_ROLES = {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN}


class CorrespondenceOperationCountsSerializer(serializers.Serializer):
    queued = serializers.IntegerField(min_value=0)
    claimed = serializers.IntegerField(min_value=0)
    awaiting_reconciliation = serializers.IntegerField(min_value=0)
    retryable = serializers.IntegerField(min_value=0)
    failed = serializers.IntegerField(min_value=0)
    sent = serializers.IntegerField(min_value=0)
    cancelled = serializers.IntegerField(min_value=0)
    stuck = serializers.IntegerField(min_value=0)
    inbound = serializers.IntegerField(min_value=0)
    unmatched = serializers.IntegerField(min_value=0)
    quarantined_messages = serializers.IntegerField(min_value=0)
    quarantined_attachments = serializers.IntegerField(min_value=0)
    delivery_exceptions = serializers.IntegerField(min_value=0)


class CorrespondenceOperationAgesSerializer(serializers.Serializer):
    oldest_delivery = serializers.IntegerField(
        min_value=0,
        allow_null=True,
    )
    oldest_claim = serializers.IntegerField(
        min_value=0,
        allow_null=True,
    )
    oldest_stuck = serializers.IntegerField(
        min_value=0,
        allow_null=True,
    )


class CorrespondenceOperationsSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    counts = CorrespondenceOperationCountsSerializer()
    ages_seconds = CorrespondenceOperationAgesSerializer()
    failure_categories = serializers.DictField(
        child=serializers.IntegerField(min_value=0),
    )


class CorrespondenceReadinessCheckSerializer(serializers.Serializer):
    component = serializers.ChoiceField(
        choices=[
            "outbound_transport",
            "object_storage",
            "malware_scanner",
            "task_broker",
            "gmail_transport",
        ]
    )
    status = serializers.ChoiceField(choices=["ready", "not_ready"])
    code = serializers.ChoiceField(
        choices=[
            "enabled",
            "disabled",
            "available",
            "unavailable",
            "configured",
        ]
    )


class CorrespondenceReadinessSerializer(serializers.Serializer):
    ready = serializers.BooleanField()
    checks = CorrespondenceReadinessCheckSerializer(many=True)


class CorrespondenceOperationsViewSet(viewsets.GenericViewSet):
    serializer_class = CorrespondenceOperationsSerializer
    allow_readonly_agent = False
    http_method_names = ["get", "head", "options"]
    role_permissions = {
        "list": OPERATIONS_ROLES,
        "readiness": OPERATIONS_ROLES,
    }

    @extend_schema(responses={200: CorrespondenceOperationsSerializer})
    def list(self, request):
        return Response(operations_snapshot())

    @action(detail=False, methods=["get"])
    @extend_schema(responses={200: CorrespondenceReadinessSerializer})
    def readiness(self, request):
        return Response(readiness_snapshot())


def register(router):
    router.register(
        "correspondence-operations",
        CorrespondenceOperationsViewSet,
        basename="correspondence-operations",
    )
