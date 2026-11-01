"""Narrow possession-token delivery for one released requester installment.

Two surfaces share one capability. The token URL is a *page*: it tells the
requester what the installment contains, when the link expires, and who to
contact, because a bare ZIP arriving from a link in an email explains
nothing. The bytes live one path deeper, and are the only thing an optional
access code guards.

Neither surface authenticates anybody. Possession of the token is the
capability, exactly as before; the page adds no authority and discloses
nothing a holder of the token could not already infer.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from django.contrib.auth.hashers import check_password
from django.db.models import F
from django.http import FileResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import audit
from apps.configuration import runtime

from .models import Installment
from .storage import ObjectIntegrityError, load_verified_object
from .throttles import (
    InstallmentAccessCodePerInstallmentThrottle,
    InstallmentAccessCodePerIpThrottle,
    InstallmentDownloadPerIpThrottle,
    public_intake_client_ip,
)

logger = logging.getLogger(__name__)


#: Why a token that resolves to a real installment still cannot be served.
UNAVAILABLE_REVOKED = "revoked"
UNAVAILABLE_EXPIRED = "expired"


def _resolve(request_number, sequence, token):
    """Find the installment this capability names, or ``None``.

    A malformed token, an unknown token, and an installment that was never
    released are deliberately indistinguishable: all three are simply absent.
    """
    try:
        capability = uuid.UUID(str(token))
    except (AttributeError, TypeError, ValueError):
        return None
    installment = (
        Installment.objects.select_related("request")
        .filter(
            request__number=request_number,
            sequence=sequence,
            download_token=capability,
        )
        .first()
    )
    if installment is None or not _is_released(installment):
        return None
    return installment


def _is_released(installment):
    return bool(
        installment.released_at
        and installment.package_prepared_at
        and installment.package_storage_key
        and installment.package_sha256
        and installment.package_size is not None
        and installment.download_expires_at
        and (
            installment.fee_amount == Decimal(0)
            or installment.fee_paid_at is not None
        )
    )


def _unavailable_reason(installment):
    if installment.revoked_at is not None:
        return UNAVAILABLE_REVOKED
    if installment.download_expires_at <= timezone.now():
        return UNAVAILABLE_EXPIRED
    return None


def download_page_context(installment, *, package_url, attempt_failed=False):
    """What the requester is told before they download anything."""
    return {
        "request_number": installment.request.number,
        "sequence": installment.sequence,
        "item_count": installment.items.count(),
        "package_size": installment.package_size,
        "expires_at": installment.download_expires_at,
        "contact_email": runtime.get("PRA_OFFICER_EMAIL"),
        "requires_access_code": bool(installment.access_code_hash),
        "package_url": package_url,
        "attempt_failed": attempt_failed,
    }


def _requester_safe_headers(response):
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class InstallmentDownloadPageView(APIView):
    """Explain one released installment, then offer its package."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [InstallmentDownloadPerIpThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        return _requester_safe_headers(
            super().finalize_response(request, response, *args, **kwargs)
        )

    @extend_schema(
        responses={
            (200, "text/html"): OpenApiTypes.STR,
            404: OpenApiTypes.STR,
            410: OpenApiTypes.STR,
        },
        description=(
            "The requester-facing page for one released installment: what it "
            "contains, when the link expires, who to contact, and the package."
        ),
    )
    def get(self, request, request_number, sequence, token):
        installment = _resolve(request_number, sequence, token)
        if installment is None:
            return self._page(
                request,
                "requester/installment_unavailable.html",
                {"reason": "missing"},
                status.HTTP_404_NOT_FOUND,
            )
        client_ip = public_intake_client_ip(request)
        reason = _unavailable_reason(installment)
        if reason is not None:
            audit(
                f"installment.download_{reason}",
                target=installment.request.number,
                ip_address=client_ip,
                installment_id=installment.pk,
                sequence=installment.sequence,
            )
            return self._page(
                request,
                "requester/installment_unavailable.html",
                {"reason": reason, "contact_email": runtime.get("PRA_OFFICER_EMAIL")},
                status.HTTP_410_GONE,
            )
        return self._page(
            request,
            "requester/installment_download.html",
            download_page_context(
                installment,
                package_url=request.path + "package/",
            ),
            status.HTTP_200_OK,
        )

    @staticmethod
    def _page(request, template, context, status_code):
        # Rendered through Django's template engine rather than DRF so the
        # response is a plain HTML document: this page is read by a member of
        # the public in a browser, not by an API client.
        return _requester_safe_headers(
            render(request, template, context, status=status_code)
        )


@method_decorator(csrf_protect, name="dispatch")
class InstallmentPackageView(APIView):
    """Stream one verified installment package to the token's holder."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [
        InstallmentDownloadPerIpThrottle,
        InstallmentAccessCodePerIpThrottle,
        InstallmentAccessCodePerInstallmentThrottle,
    ]

    def finalize_response(self, request, response, *args, **kwargs):
        return _requester_safe_headers(
            super().finalize_response(request, response, *args, **kwargs)
        )

    @extend_schema(
        responses={
            (200, "application/zip"): OpenApiTypes.BINARY,
            403: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            410: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
        description=(
            "Download one released installment using its scoped, expiring "
            "possession capability, plus the officer's access code when set."
        ),
    )
    def get(self, request, request_number, sequence, token):
        return self._deliver(request, request_number, sequence, token)

    @extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={
            (200, "application/zip"): OpenApiTypes.BINARY,
            403: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            410: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
        description="Submit the officer-set access code and download.",
    )
    def post(self, request, request_number, sequence, token):
        return self._deliver(request, request_number, sequence, token)

    def _deliver(self, request, request_number, sequence, token):
        installment = _resolve(request_number, sequence, token)
        if installment is None:
            return Response(
                {"detail": "Not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        client_ip = public_intake_client_ip(request)
        reason = _unavailable_reason(installment)
        if reason is not None:
            audit(
                f"installment.download_{reason}",
                target=installment.request.number,
                ip_address=client_ip,
                installment_id=installment.pk,
                sequence=installment.sequence,
            )
            return Response(
                {
                    "detail": (
                        "This download link was revoked by the District."
                        if reason == UNAVAILABLE_REVOKED
                        else "This download link has expired."
                    )
                },
                status=status.HTTP_410_GONE,
            )

        if installment.access_code_hash:
            # A code is read from a POST and from nothing else. DRF parses a
            # body by content type, not by method, so without this a GET
            # carrying a code would be checked while the POST-only guess
            # throttles stayed silent. A GET is a bookmark or a direct
            # visit, not an attempt: it gets the form, and no rejection is
            # recorded.
            if request.method != "POST":
                return render(
                    request,
                    "requester/installment_download.html",
                    download_page_context(
                        installment,
                        package_url=request.path,
                    ),
                    status=status.HTTP_403_FORBIDDEN,
                )
            # Stripped the same way the officer's code was when it was set,
            # so a code pasted with a trailing space or newline is the code.
            supplied = str(request.data.get("access_code") or "").strip()
            if not supplied or not check_password(
                supplied,
                installment.access_code_hash,
            ):
                audit(
                    "installment.download_code_rejected",
                    target=installment.request.number,
                    ip_address=client_ip,
                    installment_id=installment.pk,
                    sequence=installment.sequence,
                )
                # The requester typed this into a browser form, so answer in
                # the same page they came from rather than a JSON blob. The
                # message never says whether the code was merely wrong or
                # never set, and the attempt throttle applies either way.
                # ``finalize_response`` adds the requester-safe headers.
                return render(
                    request,
                    "requester/installment_download.html",
                    download_page_context(
                        installment,
                        package_url=request.path,
                        attempt_failed=True,
                    ),
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            verified = load_verified_object(
                installment.package_storage_key,
                expected_size=installment.package_size,
                expected_sha256=installment.package_sha256,
            )
        except ObjectIntegrityError:
            logger.exception(
                "Requester installment download failed integrity verification"
            )
            return self._blocked(installment, client_ip, "integrity_check_failed")
        except Exception:  # any storage fault fails closed
            logger.exception("Requester installment storage read failed")
            return self._blocked(installment, client_ip, "storage_unavailable")

        downloaded_at = timezone.now()
        Installment.objects.filter(pk=installment.pk).update(
            download_count=F("download_count") + 1,
            last_downloaded_at=downloaded_at,
        )
        installment.refresh_from_db(
            fields=["download_count", "last_downloaded_at"]
        )
        audit(
            "installment.downloaded",
            target=installment.request.number,
            ip_address=client_ip,
            installment_id=installment.pk,
            sequence=installment.sequence,
            package_sha256=installment.package_sha256,
            download_count=installment.download_count,
        )
        response = FileResponse(
            verified,
            as_attachment=True,
            filename=(
                f"{installment.request.number}-installment-"
                f"{installment.sequence}.zip"
            ),
            content_type="application/zip",
        )
        response["Content-Length"] = str(installment.package_size)
        return response

    @staticmethod
    def _blocked(installment, client_ip, reason):
        audit(
            "installment.download_blocked",
            target=installment.request.number,
            ip_address=client_ip,
            installment_id=installment.pk,
            sequence=installment.sequence,
            reason=reason,
        )
        return Response(
            {"detail": "The download is temporarily unavailable."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
