# ABOUTME: Session-first staff auth plus hashed personal-token authentication.
# ABOUTME: Scoped service tokens retain their separate exact-identity boundary.
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import (
    SessionAuthentication,
    TokenAuthentication,
    get_authorization_header,
)
from rest_framework.exceptions import AuthenticationFailed

from apps.configuration import runtime

from .models import PersonalAccessToken

PERSONAL_TOKEN_MARKER = "prr_pat_"


class StaffSessionAuthentication(SessionAuthentication):
    """Keep anonymous session requests at 403 but challenge invalid tokens."""

    def authenticate_header(self, request):
        authorization = get_authorization_header(request).split()
        token_keyword = TokenAuthentication.keyword.encode().lower()
        if authorization and authorization[0].lower() == token_keyword:
            return TokenAuthentication().authenticate_header(request)
        return None


class StaffSessionAuthenticationScheme(OpenApiAuthenticationExtension):
    """Describe the custom session adapter as ordinary cookie auth."""

    target_class = "apps.api.authentication.StaffSessionAuthentication"
    name = "cookieAuth"
    priority = 1

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": settings.SESSION_COOKIE_NAME,
        }


def _authentication_failure(code, detail):
    raise AuthenticationFailed({"code": code, "detail": detail})


class PersonalAccessTokenAuthentication(TokenAuthentication):
    """Authenticate a prefixed PAT by constant-time comparison with its hash."""

    def authenticate(self, request):
        authorization = get_authorization_header(request).split()
        if not authorization or authorization[0].lower() != self.keyword.lower().encode():
            return None
        if len(authorization) != 2:
            return None
        try:
            presented = authorization[1].decode()
        except UnicodeError:
            return None
        if not presented.startswith(PERSONAL_TOKEN_MARKER):
            return None
        return self.authenticate_credentials(presented)

    def authenticate_credentials(self, key):
        parts = key.split("_", 3)
        if (
            len(parts) != 4
            or parts[0] != "prr"
            or parts[1] != "pat"
            or len(parts[2]) != 12
            or not parts[3]
        ):
            _authentication_failure(
                "invalid_token",
                "The personal access token is invalid.",
            )
        prefix = parts[2]
        token = (
            PersonalAccessToken.objects.select_related("user")
            .filter(prefix=prefix)
            .first()
        )
        presented_hash = hashlib.sha256(key.encode()).hexdigest()
        if token is None or not hmac.compare_digest(
            token.token_hash,
            presented_hash,
        ):
            _authentication_failure(
                "invalid_token",
                "The personal access token is invalid.",
            )
        now = timezone.now()
        if token.revoked_at is not None:
            _authentication_failure(
                "token_revoked",
                "The personal access token has been revoked.",
            )
        if token.expires_at <= now:
            _authentication_failure(
                "token_expired",
                "The personal access token has expired.",
            )
        if not token.user.is_active:
            _authentication_failure(
                "invalid_token",
                "The personal access token is invalid.",
            )
        cutoff = now - timedelta(
            seconds=runtime.get("PRR_PERSONAL_TOKEN_LAST_USED_WINDOW_SECONDS"),
        )
        PersonalAccessToken.objects.filter(pk=token.pk).filter(
            Q(last_used_at__isnull=True) | Q(last_used_at__lt=cutoff),
        ).update(last_used_at=now)
        return token.user, token


class PersonalAccessTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "apps.api.authentication.PersonalAccessTokenAuthentication"
    name = "personalTokenAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "Personal token using `Token prr_pat_…`.",
        }
