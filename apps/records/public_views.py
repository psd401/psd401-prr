"""Anonymous, write-only public request intake through the canonical pipeline."""

import hashlib
import json
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.generics import GenericAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .attachments import (
    AttachmentValidationError,
    attachment_digest_descriptors,
    validate_attachments,
)
from .intake import submit_intake
from .models import Channel, PublicIntakeSubmission
from .public_serializers import (
    PublicIntakeResponseSerializer,
    PublicIntakeSerializer,
)
from .public_tokens import (
    PUBLIC_FORM_COOKIE_NAME,
    PublicFormTokenRejected,
    public_form_token_digest,
    verify_public_form_token,
)
from .throttles import (
    PublicIntakeGlobalThrottle,
    PublicIntakePerIpThrottle,
    public_intake_client_ip,
)
from .upload_handlers import IntakeAttachmentLimitUploadHandler

logger = logging.getLogger(__name__)
PUBLIC_REJECTION_BODY = {"detail": "The submission could not be accepted."}


class PublicCsrfAuthentication(SessionAuthentication):
    """Require CSRF for an anonymous browser without authenticating a user."""

    def authenticate(self, request):
        self.enforce_csrf(request)
        return


class PublicIntakeView(GenericAPIView):
    """Accept one public request without exposing any read capability."""

    authentication_classes = [PublicCsrfAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [PublicIntakePerIpThrottle, PublicIntakeGlobalThrottle]
    serializer_class = PublicIntakeSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def initialize_request(self, request, *args, **kwargs):
        content_type = request.META.get("CONTENT_TYPE", "")
        if content_type.partition(";")[0].strip().lower() == (
            "multipart/form-data"
        ):
            request.upload_handlers.insert(
                0,
                IntakeAttachmentLimitUploadHandler(request),
            )
        return super().initialize_request(request, *args, **kwargs)

    def _reject(self, reason):
        logger.warning("Public intake rejected: %s", reason)
        return Response(PUBLIC_REJECTION_BODY, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        auth=[],
        description=(
            "Submit one public records request after obtaining the CSRF and "
            "signed form cookies from GET /request. This is the sole "
            "anonymous write operation and exposes no read surface."
        ),
        request=PublicIntakeSerializer,
        responses={201: PublicIntakeResponseSerializer},
    )
    def post(self, request):
        parsed_data = request.data
        upload_limit_error = getattr(
            request._request,
            "_prr_intake_upload_limit_error",
            "",
        )
        if upload_limit_error:
            return Response(
                {
                    "code": "multipart_upload_limit_exceeded",
                    "attachments": [upload_limit_error],
                },
                status=413,
            )

        if str(parsed_data.get("website", "")):
            return self._reject("honeypot")
        try:
            nonce = verify_public_form_token(
                request.COOKIES.get(PUBLIC_FORM_COOKIE_NAME, ""),
            )
        except PublicFormTokenRejected as exc:
            return self._reject(exc.reason)

        serializer = self.get_serializer(data=parsed_data)
        serializer.is_valid(raise_exception=True)
        try:
            attachments = validate_attachments(
                request.FILES.getlist("attachments"),
            )
        except AttachmentValidationError as exc:
            return Response(
                {"attachments": [str(exc)]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated = dict(serializer.validated_data)
        validated.pop("attachments", None)
        validated.pop("website", None)
        canonical_payload = {
            "name": validated.get("name", ""),
            "email": validated["email"],
            "phone": validated.get("phone", ""),
            "street_address": validated.get("street_address", ""),
            "city": validated.get("city", ""),
            "state": validated.get("state", ""),
            "postal_code": validated.get("postal_code", ""),
            "company": validated.get("company", ""),
            "description": validated["description"],
            "attachments": attachment_digest_descriptors(attachments),
        }
        payload_sha256 = hashlib.sha256(
            json.dumps(
                canonical_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode(),
        ).hexdigest()
        token_sha256 = public_form_token_digest(nonce)
        ip_address = public_intake_client_ip(request)

        with transaction.atomic():
            receipt = (
                PublicIntakeSubmission.objects.filter(
                    token_sha256=token_sha256,
                )
                .select_related("request")
                .first()
            )
            if receipt:
                return self._existing_receipt_response(receipt, payload_sha256)
            try:
                with transaction.atomic():
                    receipt = PublicIntakeSubmission.objects.create(
                        token_sha256=token_sha256,
                        payload_sha256=payload_sha256,
                        ip_address=ip_address,
                    )
            except IntegrityError:
                receipt = PublicIntakeSubmission.objects.select_related(
                    "request",
                ).get(token_sha256=token_sha256)
                return self._existing_receipt_response(receipt, payload_sha256)

        def link_request(prr):
            receipt.request = prr
            receipt.save(update_fields=["request"])

        try:
            prr = submit_intake(
                validated,
                channel=Channel.PORTAL,
                actor=None,
                integration_source="public_form",
                idempotency_key=token_sha256,
                ip_address=ip_address,
                on_created=link_request,
                attachments=attachments,
            )
        except Exception:
            logger.exception("Public intake failed for receipt %s", receipt.pk)
            receipt.state = PublicIntakeSubmission.State.FAILED
            receipt.last_error = "intake_processing_failed"
            receipt.save(update_fields=["state", "last_error"])
            response = {
                "detail": "The submission was accepted but processing did not complete."
            }
            if receipt.request_id:
                response["number"] = receipt.request.number
            return Response(response, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        receipt.state = PublicIntakeSubmission.State.COMPLETED
        receipt.completed_at = timezone.now()
        receipt.last_error = ""
        receipt.save(update_fields=["state", "completed_at", "last_error"])
        return Response(
            {"number": prr.number},
            status=status.HTTP_201_CREATED,
        )

    def _existing_receipt_response(self, receipt, payload_sha256):
        if receipt.payload_sha256 != payload_sha256:
            return Response(
                {"detail": "This form was already submitted with different data."},
                status=status.HTTP_409_CONFLICT,
            )
        if (
            receipt.state == PublicIntakeSubmission.State.COMPLETED
            and receipt.request
        ):
            return Response(
                {"number": receipt.request.number},
                status=status.HTTP_200_OK,
            )
        if receipt.state == PublicIntakeSubmission.State.FAILED:
            response = {"detail": "The original submission did not finish processing."}
            if receipt.request:
                response["number"] = receipt.request.number
            return Response(response, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(
            {"detail": "The original submission is still processing."},
            status=status.HTTP_409_CONFLICT,
            headers={"Retry-After": "2"},
        )
