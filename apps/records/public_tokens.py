"""Short-lived, single-form tokens for the public intake abuse boundary."""

import hashlib
import secrets
import time

from django.conf import settings
from django.core import signing

from apps.configuration import runtime

PUBLIC_FORM_COOKIE_NAME = "prr_public_intake"
PUBLIC_INTAKE_PATH = "/api/public/requests/"
_SIGNING_SALT = "records.public-intake.v1"


class PublicFormTokenRejected(ValueError):
    """A public form token was missing, invalid, too new, or expired."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def issue_public_form_token():
    return signing.dumps(
        {
            "issued_at": time.time(),
            "nonce": secrets.token_urlsafe(32),
        },
        salt=_SIGNING_SALT,
    )


def set_public_form_cookie(response):
    response.set_cookie(
        PUBLIC_FORM_COOKIE_NAME,
        issue_public_form_token(),
        max_age=runtime.get("PRR_PUBLIC_INTAKE_TOKEN_MAX_AGE_SECONDS"),
        path=PUBLIC_INTAKE_PATH,
        secure=settings.CSRF_COOKIE_SECURE,
        httponly=True,
        samesite="Strict",
    )


def verify_public_form_token(value):
    if not value:
        raise PublicFormTokenRejected("missing")
    try:
        payload = signing.loads(
            value,
            salt=_SIGNING_SALT,
            max_age=runtime.get("PRR_PUBLIC_INTAKE_TOKEN_MAX_AGE_SECONDS"),
        )
    except signing.SignatureExpired as exc:
        raise PublicFormTokenRejected("expired") from exc
    except signing.BadSignature as exc:
        raise PublicFormTokenRejected("invalid") from exc

    nonce = payload.get("nonce") if isinstance(payload, dict) else None
    issued_at = payload.get("issued_at") if isinstance(payload, dict) else None
    if not isinstance(nonce, str) or not nonce or not isinstance(
        issued_at,
        (int, float),
    ):
        raise PublicFormTokenRejected("invalid")

    age = time.time() - issued_at
    if age < 0:
        raise PublicFormTokenRejected("invalid")
    if age < runtime.get("PRR_PUBLIC_INTAKE_MIN_FILL_SECONDS"):
        raise PublicFormTokenRejected("too_fast")
    return nonce


def public_form_token_digest(nonce):
    return hashlib.sha256(nonce.encode()).hexdigest()
