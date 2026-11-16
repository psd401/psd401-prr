# ABOUTME: Staff read-only view of footage preservation holds plus the gated
# ABOUTME: camera-exclusion and widen actions; release is automatic (triage.py).
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import StaffRole
from apps.records import triage
from apps.records.models import CameraHold, FootagePreservation
from apps.records.services import CLOSED_STATUSES


class PreservationCameraHoldSerializer(serializers.ModelSerializer):
    excluded_by = serializers.SerializerMethodField()

    class Meta:
        model = CameraHold
        fields = ["id", "camera_id", "camera_name", "site", "clip_id", "state",
                  "error", "coverage_fraction", "coverage_note",
                  "excluded_at", "excluded_by", "exclusion_reason"]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_excluded_by(self, obj):
        return obj.excluded_by.username if obj.excluded_by_id else None


class PreservationSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    held_count = serializers.IntegerField(read_only=True)
    camera_holds = PreservationCameraHoldSerializer(many=True, read_only=True)
    released_by = serializers.SerializerMethodField()
    request_closed = serializers.SerializerMethodField()
    confirmation_text = serializers.SerializerMethodField()
    widenable = serializers.SerializerMethodField()
    release_eligible_at = serializers.SerializerMethodField()

    class Meta:
        model = FootagePreservation
        fields = ["id", "request_number", "state", "window_start", "window_end",
                  "detected_dates_note", "site_codes", "site_resolution",
                  "held_count", "camera_holds", "confirmation_text",
                  "request_closed", "release_eligible_at", "widenable", "released_at",
                  "released_by", "release_reason"]

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_release_eligible_at(self, obj):
        # When the automatic release lets the footage go; null while it won't.
        at = triage.release_eligible_at(obj)
        return serializers.DateTimeField().to_representation(at) if at else None

    @extend_schema_field(serializers.BooleanField())
    def get_widenable(self, obj):
        # PENDING: the first run is still bookmarking; widen once it finishes.
        return bool(obj.site_codes) and obj.state not in (
            FootagePreservation.State.RELEASED, FootagePreservation.State.PENDING,
        )

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_released_by(self, obj):
        return obj.released_by.username if obj.released_by_id else None

    @extend_schema_field(serializers.BooleanField())
    def get_request_closed(self, obj):
        return obj.request.status in CLOSED_STATUSES

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_confirmation_text(self, obj):
        # Read-only: staff copy it into correspondence by hand, never sent.
        return triage.preservation_confirmation_text(obj)


class PreservationViewSet(viewsets.ReadOnlyModelViewSet):
    """List/retrieve only — preservations are evidence records; no
    create/update/destroy routes exist for this resource."""
    queryset = (FootagePreservation.objects
                .select_related("request__merged_into", "released_by")
                .prefetch_related("camera_holds__excluded_by")
                .order_by("-created_at"))
    serializer_class = PreservationSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN},
        "exclude_camera": {StaffRole.RECORDS_OFFICER},
        "widen": {StaffRole.RECORDS_OFFICER},
    }

    def get_queryset(self):
        qs = super().get_queryset()
        request_number = self.request.query_params.get("request")
        if request_number:
            qs = qs.filter(request__number=request_number)
        state = self.request.query_params.get("state")
        if state:
            qs = qs.filter(state=state)
        return qs

    @action(detail=True, methods=["post"], url_path="exclude-camera")
    def exclude_camera(self, request, pk=None):
        handoff = human_only_handoff(
            request,
            capability_id="preservations.exclude_camera",
            action="Exclude a camera from an active preservation hold.",
        )
        if handoff:
            return handoff
        preservation = self.get_object()
        camera_hold_id = request.data.get("camera_hold_id")
        reason = request.data.get("reason", "")

        try:
            camera_hold = preservation.camera_holds.get(pk=camera_hold_id)
        except (CameraHold.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Camera hold not found."},
                            status=status.HTTP_404_NOT_FOUND)

        try:
            triage.exclude_camera(camera_hold, reason, actor=request.user)
        except triage.PreservationRefused as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PreservationCameraHoldSerializer(camera_hold).data)

    @action(detail=True, methods=["post"], url_path="widen")
    def widen(self, request, pk=None):
        # Widening only preserves more, so it is not a human-only handoff.
        preservation = self.get_object()
        try:
            triage.widen_preservation(
                preservation, actor=request.user, reason=request.data.get("reason"),
            )
        except triage.PreservationRefused as exc:
            return Response({"detail": exc.message}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            PreservationSerializer(self.get_queryset().get(pk=preservation.pk)).data
        )


def register(router):
    router.register("preservations", PreservationViewSet, basename="preservation")
