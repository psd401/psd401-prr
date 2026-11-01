"""Shared serializers for bounded, permanently non-applying AI proposals."""

from rest_framework import serializers

from apps.records.models import AdvisoryRun


class AdvisoryRunSerializer(serializers.ModelSerializer):
    requested_by_username = serializers.CharField(
        source="requested_by.username",
        read_only=True,
        allow_null=True,
        default=None,
    )

    class Meta:
        model = AdvisoryRun
        fields = [
            "id",
            "kind",
            "state",
            "request",
            "record_item",
            "trigger",
            "requested_by_username",
            "model_identifier",
            "input_sha256",
            "input_truncated",
            "proposal",
            "applied",
            "failure_code",
            "failure_detail",
            "created_at",
            "started_at",
            "completed_at",
        ]
        read_only_fields = fields


class AdvisoryLatestSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    latest = AdvisoryRunSerializer(allow_null=True)
