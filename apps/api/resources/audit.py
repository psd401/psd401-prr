# ABOUTME: Staff read-only API for the append-only audit log — list/retrieve
# ABOUTME: only; no write route exists since AuditLog may never be mutated.
from rest_framework import serializers, viewsets

from apps.api.identity import StaffRole
from apps.api.pagination import CountedKeysetPagination
from apps.audit.models import AuditLog


class AuditLogPagination(CountedKeysetPagination):
    cursor_salt = "apps.api.resources.audit.AuditLogPagination"
    ordering = ("-at", "-id")


class AuditLogSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(
        source="actor.username",
        read_only=True,
        default=None,
    )

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "at",
            "actor",
            "actor_username",
            "action",
            "target",
            "detail",
            "ip_address",
        ]


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditLogSerializer
    pagination_class = AuditLogPagination
    allowed_roles = {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN}

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor").order_by("-at", "-id")
        params = self.request.query_params

        action = params.get("action")
        if action:
            qs = qs.filter(action__icontains=action)

        target = params.get("target")
        if target:
            qs = qs.filter(target__icontains=target)

        since = params.get("since")
        if since:
            qs = qs.filter(at__date__gte=since)

        return qs


def register(router):
    router.register("audit-log", AuditLogViewSet, basename="audit-log")
