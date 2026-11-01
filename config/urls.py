# ABOUTME: Root URL config — admin, public intake, staff API + schema,
# ABOUTME: and a catch-all that serves the React staff app shell.
from django.contrib import admin
from django.urls import include, path, re_path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.api import spa
from apps.api.middleware import admin_route_enabled
from apps.records.requester_downloads import (
    InstallmentDownloadPageView,
    InstallmentPackageView,
)


def _break_glass_admin_permission(request):
    user = request.user
    return bool(
        user.is_active and user.is_superuser and admin_route_enabled()
    )


# The application-admin role configures identity through the role-protected
# staff API. Django admin is a separate break-glass surface for superusers only.
admin.site.has_permission = _break_glass_admin_permission

urlpatterns = [
    path("api/staff/", include("apps.api.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("api/public/", include("apps.records.public_urls")),
    # The token URL is the page the requester reads; the bytes are one path
    # deeper so an optional access code has something to guard. Links already
    # in requesters' inboxes still land on the page.
    path(
        "requests/<str:request_number>/installments/<int:sequence>/download/"
        "<str:token>/",
        InstallmentDownloadPageView.as_view(),
        name="installment-download",
    ),
    path(
        "requests/<str:request_number>/installments/<int:sequence>/download/"
        "<str:token>/package/",
        InstallmentPackageView.as_view(),
        name="installment-download-package",
    ),
    re_path(r"^(?!api/|admin/|static/|media/)(?P<path>.*)$", spa.index),
]

# The admin route is always wired and always gated. Whether it answers is
# decided per request by ``BreakGlassAdminRouteMiddleware``, which 404s the
# whole prefix while the switch is off, so a disabled route looks exactly as
# absent as it used to when it was left out of this list.
urlpatterns.insert(0, path("admin/", admin.site.urls))
