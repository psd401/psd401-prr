# ABOUTME: URL wiring for the staff API — auth endpoints plus one router that
# ABOUTME: auto-discovers every module in resources/ (each exposes register()).
import pkgutil
from importlib import import_module

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import access_tokens, auth, capabilities, oidc, resources

router = DefaultRouter()
for _mod in sorted(pkgutil.iter_modules(resources.__path__), key=lambda m: m.name):
    import_module(f"{resources.__name__}.{_mod.name}").register(router)

# Both sign-in doors are wired, always, and each one refuses unless the
# settings store says it is the mode in use. The URLconf is built once per
# process; the sign-in mode is now a setting an Application Admin can change
# in seconds, and a route that disappeared until a redeploy would make that
# change a lie. Expose only the protocol endpoints the application uses: the
# package's GET-based logout route is intentionally omitted, because the
# staff SPA uses the CSRF-protected POST logout endpoint below.
urlpatterns = [
    path("auth/login", auth.LoginView.as_view()),
    path(
        "auth/oidc/authenticate/",
        oidc.StaffOIDCAuthenticationRequestView.as_view(),
        name="oidc_authentication_init",
    ),
    path(
        "auth/oidc/callback/",
        oidc.StaffOIDCAuthenticationCallbackView.as_view(),
        name="oidc_authentication_callback",
    ),
    path("auth/logout", auth.LogoutView.as_view()),
    path("auth/me", auth.MeView.as_view()),
    path(
        "auth/api-tokens/",
        access_tokens.PersonalAccessTokenCollectionView.as_view(),
    ),
    path(
        "auth/api-tokens/<int:token_id>",
        access_tokens.PersonalAccessTokenDetailView.as_view(),
    ),
    path(
        "auth/api-tokens/<int:token_id>/rotate",
        access_tokens.PersonalAccessTokenRotateView.as_view(),
    ),
    path("capabilities/", capabilities.AgentCapabilityCatalogView.as_view()),
    path("", include(router.urls)),
]
