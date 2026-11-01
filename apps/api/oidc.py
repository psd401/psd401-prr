# ABOUTME: Google OIDC backend that authorizes by configured direct group
# ABOUTME: membership plus named direct grants, then provisions a passwordless user.
from django.core.exceptions import SuspiciousOperation
from django.http import Http404
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.utils import import_from_settings
from mozilla_django_oidc.views import (
    OIDCAuthenticationCallbackView,
    OIDCAuthenticationRequestView,
)
from rest_framework.exceptions import PermissionDenied

from apps.configuration import runtime

from .identity import (
    GoogleDirectoryUnavailable,
    provision_google_user,
    resolved_roles_for_email,
    validate_google_claims,
)

#: The three OIDC settings the district now owns, and the library name each
#: answers to. Everything else about the protocol -- Google's endpoints, the
#: signing algorithm, PKCE, nonce -- is not a district decision and stays in
#: ``config/settings.py``.
_RUNTIME_OIDC_SETTINGS = {
    "OIDC_RP_CLIENT_ID": "PRR_GOOGLE_OIDC_CLIENT_ID",
    "OIDC_TIMEOUT": "PRR_GOOGLE_OIDC_TIMEOUT_SECONDS",
}


def runtime_oidc_setting(attr, *args):
    """``import_from_settings``, with the application's answer where it has one.

    ``mozilla_django_oidc`` reads its configuration through this one hook, on
    every backend instantiation and every view instantiation -- which is to
    say once per authentication attempt. Overriding it is what lets an
    administrator rotate the sign-in client without a redeploy, and it is a
    smaller surface than mirroring three values into Django settings and
    keeping them in step.
    """
    if attr == "OIDC_RP_CLIENT_SECRET":
        return runtime.get_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET")
    key = _RUNTIME_OIDC_SETTINGS.get(attr)
    if key is not None:
        return runtime.get(key)
    return import_from_settings(attr, *args)


class StaffOIDCBackend(OIDCAuthenticationBackend):
    GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

    get_settings = staticmethod(runtime_oidc_setting)

    def verify_token(self, token, **kwargs):
        payload = super().verify_token(token, **kwargs)
        audiences = payload.get("aud", ())
        if isinstance(audiences, str):
            audiences = (audiences,)
        if (
            payload.get("iss") not in self.GOOGLE_ISSUERS
            or self.OIDC_RP_CLIENT_ID not in audiences
            or payload.get("azp", self.OIDC_RP_CLIENT_ID) != self.OIDC_RP_CLIENT_ID
        ):
            raise SuspiciousOperation("Google ID token issuer or audience is invalid.")
        return payload

    def verify_claims(self, claims):
        # Full verification and a helpful denial happen in provision_google_user.
        # This early gate prevents the base backend from ever matching by email.
        return bool(claims.get("sub") and claims.get("email") and claims.get("hd"))

    def filter_users_by_claims(self, claims):
        subject = claims.get("sub")
        return self.UserModel.objects.filter(staff_identity__google_subject=subject)

    def get_or_create_user(self, access_token, id_token, payload):
        try:
            identity = validate_google_claims(
                payload, runtime.get("PRR_GOOGLE_HOSTED_DOMAIN")
            )
            roles = resolved_roles_for_email(identity.email)
            return provision_google_user(payload, roles)
        except (PermissionDenied, GoogleDirectoryUnavailable) as exc:
            raise SuspiciousOperation(str(exc)) from exc


class _GoogleModeOnly:
    """404 unless Google is the sign-in mode the district has configured.

    The mirror image of the local login endpoint's guard: both routes are
    always wired, and the store decides which one answers.
    """

    def dispatch(self, request, *args, **kwargs):
        if str(runtime.get("PRR_AUTH_MODE")).lower() != "google":
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class StaffOIDCAuthenticationRequestView(
    _GoogleModeOnly, OIDCAuthenticationRequestView
):
    """The sign-in redirect, reading the client the district configured."""

    get_settings = staticmethod(runtime_oidc_setting)


class StaffOIDCAuthenticationCallbackView(
    _GoogleModeOnly, OIDCAuthenticationCallbackView
):
    """The sign-in callback, reading the client the district configured."""

    get_settings = staticmethod(runtime_oidc_setting)
