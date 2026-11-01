# ABOUTME: Staff read-only view of footage preservation holds plus the
# ABOUTME: gated camera-exclusion action (triage.exclude_camera, human+reason).
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import StaffRole
from apps.records import triage
from apps.records.models import CameraHold, FootagePreservation


class PreservationCameraHoldSerializer(serializers.ModelSerializer):
    excluded_by = serializers.SerializerMethodField()

    class Meta:
        model = CameraHold
        fields = ["id", "camera_id", "camera_name", "site", "clip_id", "state",
                  "error", "excluded_at", "excluded_by", "exclusion_reason"]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_excluded_by(self, obj):
        return obj.excluded_by.username if obj.excluded_by_id else None


class PreservationSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    held_count = serializers.IntegerField(read_only=True)
    camera_holds = PreservationCameraHoldSerializer(many=True, read_only=True)

    class Meta:
        model = FootagePreservation
        fields = ["id", "request_number", "state", "window_start", "window_end",
                  "detected_dates_note", "held_count", "camera_holds"]


class PreservationViewSet(viewsets.ReadOnlyModelViewSet):
    """List/retrieve only — preservations are evidence records; no
    create/update/destroy routes exist for this resource."""
    queryset = (FootagePreservation.objects
                .select_related("request")
                .prefetch_related("camera_holds")
                .order_by("-created_at"))
    serializer_class = PreservationSerializer
    role_permissions = {
        "list": {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN},
        "retrieve": {StaffRole.RECORDS_OFFICER, StaffRole.ADMIN},
        "exclude_camera": {StaffRole.RECORDS_OFFICER},
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
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PreservationCameraHoldSerializer(camera_hold).data)


def register(router):
    router.register("preservations", PreservationViewSet, basename="preservation")
