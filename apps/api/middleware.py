from django.contrib.auth import logout
from django.http import Http404, JsonResponse
from django.utils import timezone

from apps.configuration import runtime

from .identity import (
    GoogleDirectoryUnavailable,
    refresh_google_roles,
    revoke_user_sessions,
)
from .models import StaffIdentity


class ApplicationSecurityHeadersMiddleware:
    """Prevent caching of sensitive surfaces and harden the staff SPA shell."""

    SPA_CONTENT_SECURITY_POLICY = "default-src 'self'; base-uri 'self'; connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        )

        if request.path.startswith(("/api/", "/admin/")):
            response.headers["Cache-Control"] = "no-store"
        elif response.headers.get("Content-Type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = (
                self.SPA_CONTENT_SECURITY_POLICY
            )
        return response


def admin_route_enabled() -> bool:
    """Whether the break-glass Django admin route answers, right now.

    Read on the request rather than at process start. The route used to be
    left out of the URLconf unless the container booted with it enabled,
    which meant opening the escape hatch during an incident required editing
    the host's environment and restarting — the operation the escape hatch
    exists so nobody has to perform. An unreadable settings store closes the
    route: a high-privilege bypass is the last thing that may fail open.
    """
    try:
        return bool(runtime.get("PRR_ENABLE_ADMIN"))
    except Exception:  # noqa: BLE001
        return False


class BreakGlassAdminRouteMiddleware:
    """404 every ``/admin/`` request while the break-glass route is off.

    A whole-prefix refusal rather than a per-view one, so nothing under the
    admin — not its login page, not a static asset, not a reversed URL — is
    reachable while the switch is off, which is the same surface the route
    had when it was simply absent from the URLconf.
    """

    PREFIX = "/admin/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if (
            path == self.PREFIX.rstrip("/")
            or path.startswith(self.PREFIX)
        ) and not admin_route_enabled():
            raise Http404("Not found.")
        return self.get_response(request)


class StaffIdentityRevalidationMiddleware:
    """Recheck Google group roles during long-running Django sessions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        identity = getattr(user, "staff_identity", None) if user and user.is_authenticated else None
        if identity and identity.source == StaffIdentity.Source.GOOGLE:
            now = timezone.now()
            last_check = identity.last_group_check_at
            age = (now - last_check).total_seconds() if last_check else float("inf")
            if age >= runtime.get("PRR_GOOGLE_GROUP_RECHECK_SECONDS"):
                try:
                    roles = refresh_google_roles(user)
                except GoogleDirectoryUnavailable:
                    if age >= runtime.get("PRR_GOOGLE_GROUP_MAX_STALE_SECONDS"):
                        logout(request)
                        return JsonResponse(
                            {"detail": "Staff authorization could not be verified."},
                            status=503,
                        )
                else:
                    if not roles:
                        revoke_user_sessions(user)
                        logout(request)
                        return JsonResponse(
                            {"detail": "Your PRR staff access has been removed."},
                            status=403,
                        )
        return self.get_response(request)
