# ABOUTME: Read-only staff API for model-comparison evaluation runs (#32):
# ABOUTME: metrics per model, side by side, for Records Officers.
from rest_framework import serializers, viewsets

from apps.api.identity import StaffRole
from apps.records.models import EvaluationRun

_LIST_LIMIT = 50


class EvaluationRunSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    kind = serializers.CharField(source="evaluation_set.kind", read_only=True)
    items = serializers.SerializerMethodField()
    label_counts = serializers.JSONField(source="evaluation_set.label_counts", read_only=True)
    model_identifiers = serializers.JSONField(read_only=True)
    results = serializers.JSONField(read_only=True)
    created_by = serializers.CharField(source="created_by.email", read_only=True, default=None)
    created_at = serializers.DateTimeField(read_only=True)

    def get_items(self, run) -> int:
        return len(run.evaluation_set.examples)


class EvaluationRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = EvaluationRunSerializer
    pagination_class = None
    allowed_roles = {StaffRole.RECORDS_OFFICER}

    def get_queryset(self):
        return (
            EvaluationRun.objects.select_related("evaluation_set", "created_by")
            .order_by("-created_at")[:_LIST_LIMIT]
        )


def register(router):
    router.register("advisory-evaluations", EvaluationRunViewSet, basename="advisory-evaluations")
