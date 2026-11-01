# ABOUTME: Records Officer-only, malware-gated downloads for requester-supplied
# ABOUTME: files using five-minute user-bound URLs; private object keys stay private.
import hashlib
import secrets

from django.core.cache import cache
from django.http import FileResponse
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.reverse import reverse

from apps.api.identity import StaffRole
from apps.audit.models import audit
from apps.records.attachments import open_clean_attachment
from apps.records.models import IntakeAttachment


class IntakeAttachmentSerializer(serializers.ModelSerializer):
    request_number = serializers.CharField(source="request.number", read_only=True)
    scan_status = serializers.CharField(
        source="effective_scan_status",
        read_only=True,
    )
    scanned_at = serializers.DateTimeField(
        source="effective_scanned_at",
        read_only=True,
    )

    class Meta:
        model = IntakeAttachment
        fields = [
            "id",
            "request_number",
            "original_filename",
            "content_type",
            "size",
            "sha256",
            "scan_status",
            "created_at",
            "scanned_at",
        ]


class IntakeAttachmentViewSet(viewsets.GenericViewSet):
    queryset = IntakeAttachment.objects.select_related(
        "request",
        "source_correspondence_attachment",
    ).all()
    serializer_class = IntakeAttachmentSerializer
    role_permissions = {
        "download": {StaffRole.RECORDS_OFFICER},
        "content": {StaffRole.RECORDS_OFFICER},
    }
    allow_readonly_agent = False
    http_method_names = ["get", "post", "head", "options"]

    @staticmethod
    def _cache_key(token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        return f"intake-attachment-download:{digest}"

    @action(detail=True, methods=["post"])
    def download(self, request, pk=None):
        attachment = self.get_object()
        if not attachment.is_downloadable:
            return Response(
                {
                    "code": "attachment_not_clean",
                    "detail": "The attachment is not available unless malware scanning succeeds.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        expires_in = 300
        token = secrets.token_urlsafe(32)
        cache.set(
            self._cache_key(token),
            {"attachment_id": str(attachment.id), "user_id": str(request.user.pk)},
            timeout=expires_in,
        )
        url = reverse(
            "intake-attachment-content",
            kwargs={"pk": attachment.pk, "download_token": token},
        )
        audit(
            "intake_attachment.download_authorized",
            target=attachment.request.number,
            actor=request.user,
            attachment_id=str(attachment.id),
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
        if not attachment.is_downloadable:
            return Response(status=status.HTTP_404_NOT_FOUND)

        body = open_clean_attachment(attachment)
        response = FileResponse(
            body,
            as_attachment=True,
            filename=attachment.original_filename,
            content_type="application/octet-stream",
        )
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        response["X-Content-Type-Options"] = "nosniff"
        audit(
            "intake_attachment.downloaded",
            target=attachment.request.number,
            actor=request.user,
            attachment_id=str(attachment.id),
            sha256=attachment.sha256,
        )
        return response


def register(router):
    router.register(
        "intake-attachments",
        IntakeAttachmentViewSet,
        basename="intake-attachment",
    )
