"""Minimal Phase 1 settings. Secrets via env; never commit .env."""
import os
from pathlib import Path
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent

# On the deployed instance the signing and settings-store keys come from AWS
# Secrets Manager through the instance role, before anything below reads them.
# No PRR_RUNTIME_SECRET_ID: the environment is used as-is (local, CI, tests).
# Lower-case on purpose: Django copies every upper-case name here into
# django.conf.settings, and this dict must not become a setting.
from config.runtime_secrets import read_runtime_secret  # noqa: E402

_runtime_secret = read_runtime_secret(os.environ)


def _parse_strict_boolean(name, value):
    normalized = str(value).strip().lower()
    if normalized not in {"true", "false"}:
        raise ImproperlyConfigured(f"{name} must be exactly true or false.")
    return normalized == "true"


# The throttle-rate, email-list, and domain-list parsers that used to live
# here went with the settings they parsed. They are now typed entries in
# apps.configuration.registry, where the same rules are enforced on an
# administrator's save instead of at process start.

SECRET_KEY = _runtime_secret.get(
    "DJANGO_SECRET_KEY", os.environ.get("DJANGO_SECRET_KEY", "dev-only")
)
# Encrypts settings-store secrets (apps.configuration.secrets). Blank derives
# the key from SECRET_KEY, so rotating that would strand every stored secret.
PRR_SETTINGS_ENCRYPTION_KEY = _runtime_secret.get(
    "PRR_SETTINGS_ENCRYPTION_KEY", os.environ.get("PRR_SETTINGS_ENCRYPTION_KEY", "")
).strip()
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
PRR_DEPLOYMENT_MODE = os.environ.get(
    "PRR_DEPLOYMENT_MODE",
    "development",
).strip()
if PRR_DEPLOYMENT_MODE not in {"development", "production"}:
    raise ImproperlyConfigured(
        "PRR_DEPLOYMENT_MODE must be exactly development or production."
    )
if not DEBUG and (SECRET_KEY in {"", "dev-only", "change-me", "build-only"} or len(SECRET_KEY) < 50):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a unique high-entropy value outside local development."
    )
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.postgres",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "django_celery_beat",
    "mozilla_django_oidc",
    "apps.configuration.apps.ConfigurationConfig",
    "apps.records",
    "apps.correspondence.apps.CorrespondenceConfig",
    "apps.audit",
    "apps.work_queue.apps.WorkQueueConfig",
    "apps.api.apps.ApiConfig",
    "apps.integrations.gmail.apps.GmailConfig",
]

# Application-managed (apps.configuration), Phase B: the reply local part,
# reply domain, and Message-ID domain are read through
# ``runtime.get(...)``. They are absent here on purpose -- a key that still
# had a Django settings attribute would keep resolving through it for any
# deployment that had not yet run ``manage.py import_environment_settings``,
# which is a second source of truth with a different lifetime.

PRR_GMAIL_ENABLED = _parse_strict_boolean(
    "PRR_GMAIL_ENABLED",
    os.environ.get("PRR_GMAIL_ENABLED", "false"),
)
# Application-managed (apps.configuration), Phase C: the Gmail service
# identity and the development-only key file. They are identity rather than
# key material -- deployed Gmail access is keyless impersonation and a
# deployed private key is still refused -- and the district rotates the
# identity in the application, with a reason and a history, rather than by
# asking whoever holds the host.
# Application-managed (apps.configuration), Phase B: the mailbox, the request
# timeout, the processed label, the poll lookback, and the poll stale
# threshold. The mailbox's address form and its 100-character
# provider-account bound moved with it, from the api.E017 system check to the
# registry's EMAIL type, and the keyless-impersonation requirement is a
# prerequisite the store checks before it will enable the transport.
#
# Application-managed (apps.configuration): read through
# ``runtime.get("PRR_GMAIL_INBOUND_POLL_ENABLED")``.
PRR_GMAIL_INBOUND_POLL_ENABLED = _parse_strict_boolean(
    "PRR_GMAIL_INBOUND_POLL_ENABLED",
    os.environ.get("PRR_GMAIL_INBOUND_POLL_ENABLED", "false"),
)
# Archiving is a mailbox-hygiene convenience, never a processing gate: the
# processed label is applied only after the message is durably received, and
# this flag only decides whether that same modify call also drops INBOX.
PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED = _parse_strict_boolean(
    "PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED",
    os.environ.get("PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED", "false"),
)
# Application-managed (apps.configuration): read through
# ``runtime.get("PRR_CLOUDWATCH_METRICS_ENABLED")``. The value here is the
# one-time bootstrap seed the settings store imports on first start, and is
# ignored afterwards.
PRR_CLOUDWATCH_METRICS_ENABLED = _parse_strict_boolean(
    "PRR_CLOUDWATCH_METRICS_ENABLED",
    os.environ.get("PRR_CLOUDWATCH_METRICS_ENABLED", "false"),
)
# Application-managed (apps.configuration), Phase C: the CloudWatch region.
# Its shape (at most 64 characters of lower-case letters, digits, and
# hyphens) moved with it, from this startup check to the registry's pattern,
# and is enforced on the save path where an administrator can read the
# refusal. ``AWS_DEFAULT_REGION``/``AWS_REGION`` remain the bootstrap
# fallback the key used to default from.
# Application-managed (apps.configuration), Phase B: the blocked intake
# sender domains, the district's own mail domains, and the narrower set of
# domains whose accounts may be written as a record's custodian. The
# custodian list is deliberately a separate setting from the district mail
# domains so widening the external-sender review tag can never silently
# widen what gets written as custody evidence, and it must never include a
# student domain.
#
# The reply local part and domain used to be validated here as a pair,
# because one is only meaningful with the other. That pairing is now the
# ``reply_address_valid`` rule, checked on the save path of either one.

PRR_CORRESPONDENCE_OUTBOUND_ENABLED = _parse_strict_boolean(
    "PRR_CORRESPONDENCE_OUTBOUND_ENABLED",
    os.environ.get("PRR_CORRESPONDENCE_OUTBOUND_ENABLED", "false"),
)
PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED = _parse_strict_boolean(
    "PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED",
    os.environ.get(
        "PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED",
        "false",
    ),
)
# Application-managed (apps.configuration), Phase B: the non-production
# recipient allowlist, the stuck and lease windows, and the seven inbound
# MIME limits. The version 1 security contract those limits express is now
# the registry's per-key bounds, refused on the save path with the limit in
# the message instead of crash-looping a container at import.
PRR_ENVIRONMENT = os.environ.get(
    "PRR_ENVIRONMENT", "development" if DEBUG else "production"
).strip()
if (
    not PRR_ENVIRONMENT
    or len(PRR_ENVIRONMENT) > 64
    or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in PRR_ENVIRONMENT
    )
):
    raise ImproperlyConfigured(
        "PRR_ENVIRONMENT must be 1-64 letters, numbers, dots, underscores, or hyphens."
    )

# Staff API defaults. Any non-staff integration must declare a narrower
# authenticator and permission explicitly. Public intake is the sole reviewed
# anonymous exception and overrides both explicitly on its write-only view.
# SessionAuthentication stays first so anonymous staff requests keep getting
# 403 (no WWW-Authenticate challenge) instead of 401 — DRF's
# APIView.get_authenticate_header() only consults authenticators[0].
# TokenAuthentication is for agents/scripts; DEFAULT_PERMISSION_CLASSES below
# permits staff normally and explicitly scoped nonstaff identities on safe
# methods only.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.authentication.StaffSessionAuthentication",
        "apps.api.authentication.PersonalAccessTokenAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.api.permissions.IsStaffOrReadOnlyAgent",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Production has exactly one trusted Caddy proxy. Using the rightmost
    # forwarded address prevents a caller-supplied prefix from evading per-IP
    # public-intake throttling.
    "NUM_PROXIES": 1,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "PSD401 PRR Staff API",
    "VERSION": "1.0.0",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
    "ENUM_NAME_OVERRIDES": {
        "InboundReceiptOutcomeEnum": [
            "matched",
            "unmatched",
            "candidate",
            "ambiguous",
            "quarantined",
            "duplicate",
        ],
        # Pinned so the redaction-decision enum keeps the name it had before
        # record uploads introduced a second field called ``outcome``.
        "OutcomeEnum": (
            "apps.records.models.RedactionProposalDecision.Outcome"
        ),
        # Mirrors the OUTCOME_* constants in apps/records/record_uploads.py.
        "RecordUploadOutcomeEnum": [
            "added",
            "duplicate",
            "empty",
            "infected",
            "scanner_unavailable",
            "case_closed",
            "not_authorized",
        ],
        "CollectionTaskKindEnum": (
            "apps.records.models.CollectionTask.Kind"
        ),
        "DecisionBatchKindEnum": (
            "apps.records.models.DecisionBatch.Kind"
        ),
        "CorrespondenceKindEnum": (
            "apps.correspondence.models.Correspondence.Kind"
        ),
        "CollectionTaskStateEnum": (
            "apps.records.models.CollectionTask.State"
        ),
        "FootagePreservationStateEnum": (
            "apps.records.models.FootagePreservation.State"
        ),
        "CameraHoldStateEnum": (
            "apps.records.models.CameraHold.State"
        ),
        "BackgroundRunStateEnum": (
            "apps.records.models.AdvisoryRun.State"
        ),
        "CorrespondenceStatusEnum": (
            "apps.correspondence.models.Correspondence.Status"
        ),
        "CorrespondenceDeliveryStatusEnum": (
            "apps.correspondence.models.CorrespondenceDelivery.Status"
        ),
        "PublicRecordsRequestStatusEnum": (
            "apps.records.models.RequestStatus"
        ),
        "InboundReceiptParsingStatusEnum": (
            "apps.correspondence.models.InboundCorrespondenceReceipt.ParsingStatus"
        ),
        "InboundReceiptMatchingStatusEnum": (
            "apps.correspondence.models.InboundCorrespondenceReceipt.MatchingStatus"
        ),
        "InboundReceiptMatchMethodEnum": (
            "apps.correspondence.models.InboundCorrespondenceReceipt.MatchMethod"
        ),
        "WorkItemDispositionEnum": (
            "apps.work_queue.models.WorkItem.Disposition"
        ),
        "WorkItemMutableDispositionEnum": ["waiting", "handled"],
        "WorkItemDispositionFilterEnum": [
            "all",
            "needs_attention",
            "waiting",
            "handled",
            "no_action_required",
            "source_excluded",
        ],
        "WorkItemNoActionCategoryEnum": (
            "apps.work_queue.models.WorkItem.NoActionCategory"
        ),
    },
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "apps.api.middleware.ApplicationSecurityHeadersMiddleware",
    "apps.api.middleware.BreakGlassAdminRouteMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.api.middleware.StaffIdentityRevalidationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

_db = urlparse(os.environ.get("DATABASE_URL", "postgres://prr:prr-dev-only@localhost:5432/prr"))
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": _db.path.lstrip("/"),
    "USER": _db.username,
    "PASSWORD": _db.password,
    "HOST": _db.hostname,
    "PORT": _db.port or 5432,
}}

TIME_ZONE = "America/Los_Angeles"   # deadline math depends on this
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Built SPA assets (vite base "/static/app/") served from frontend/dist.
_spa_dist = BASE_DIR / "frontend" / "dist"
STATICFILES_DIRS = [("app", _spa_dist)] if _spa_dist.exists() else []
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TIMEZONE = TIME_ZONE
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": CELERY_BROKER_URL,
    },
}

# Secure-by-default outside local development. A TLS-terminating reverse proxy
# may be trusted only when the operator opts in and strips client-supplied
# X-Forwarded-Proto before setting its own value.
SECURE_SSL_REDIRECT = os.environ.get(
    "DJANGO_SECURE_SSL_REDIRECT", "false" if DEBUG else "true"
).lower() == "true"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(os.environ.get("DJANGO_SESSION_COOKIE_AGE", str(8 * 60 * 60)))
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_HSTS_SECONDS = int(
    os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000")
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_REFERRER_POLICY = "same-origin"
if os.environ.get("DJANGO_TRUST_X_FORWARDED_PROTO", "false").lower() == "true":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Synthetic high-volume browser-test data is never available to a normal
# application runtime. The dedicated E2E settings module opts in explicitly.
PRR_TEST_FIXTURE_SEED_ENABLED = False

# Development seeds for two Phase C keys whose old defaults were derived from
# DEBUG. They are bootstrap values, not a second source of truth: the store
# reads them only for a key that has no stored row, and only when the
# environment has not answered first, so a developer can still override
# either. A deployed instance has DEBUG false and never reaches them, which
# is what keeps "google sign-in, no admin route" the safe deployed default.
if DEBUG:
    if not os.environ.get("PRR_AUTH_MODE"):
        PRR_AUTH_MODE = "local"
    if not os.environ.get("PRR_ENABLE_ADMIN"):
        PRR_ENABLE_ADMIN = True

# Staff identity. Developers use local Django identities with no Google or AWS
# credentials. Deployed instances use Google OIDC; the password backend is
# absent unless an operator deliberately enables the audited break-glass path.
# Application-managed (apps.configuration), Phase C: the sign-in mode, the
# break-glass password door, the Workspace domain, the group reader's
# credential file, the OAuth client ID, its secret, and the request timeout.
# The one rule they all answer to is new and lives on the save path: a change
# that would leave no Application Admin able to sign in is refused, because
# that is the one settings mistake the Settings page cannot undo.
PRR_LOCAL_IMPLICIT_STAFF_ROLES = tuple(
    role.strip()
    for role in os.environ.get(
        "PRR_LOCAL_IMPLICIT_STAFF_ROLES",
        "records_officer,intake_clerk,liaison,reviewer,admin" if DEBUG else "",
    ).split(",")
    if role.strip()
)
# Application-managed (apps.configuration), Phase B: the allowed Workspace
# domains, the membership recheck interval, and the maximum staleness. The
# rule that staleness may not be shorter than the recheck -- which would end
# every session on its first recheck -- is now a registry RELATION, checked
# from either side on the save path.

# The password backend is always installed and always gated: it refuses
# unless the settings store says local sign-in or break-glass is on. The
# conditional list this replaces was decided at process start, which is the
# one moment a break-glass door is no use.
AUTHENTICATION_BACKENDS = [
    "apps.api.oidc.StaffOIDCBackend",
    "apps.api.auth_backends.GatedPasswordBackend",
]

# Google publishes these stable OpenID Connect endpoints. Only basic identity
# scopes are requested; the login client never asks for Workspace data or an
# offline refresh token.
#
# The client ID, its secret, and the request timeout are application-managed
# (apps.configuration, Phase C). They are absent here on purpose: the OIDC
# backend and views read them through ``apps.api.oidc.runtime_oidc_setting``
# on the request, so a rotation takes effect without a redeploy.
OIDC_OP_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
OIDC_OP_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
OIDC_OP_USER_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
OIDC_OP_JWKS_ENDPOINT = "https://www.googleapis.com/oauth2/v3/certs"
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_RP_SCOPES = "openid email profile"
OIDC_USE_NONCE = True
OIDC_USE_PKCE = True
OIDC_VERIFY_SSL = True
OIDC_STORE_ACCESS_TOKEN = False
OIDC_STORE_ID_TOKEN = False
OIDC_AUTHENTICATION_CALLBACK_URL = "oidc_authentication_callback"
LOGIN_REDIRECT_URL = "/"
LOGIN_REDIRECT_URL_FAILURE = "/?login=denied"
LOGOUT_REDIRECT_URL = "/"

# PRA-specific
# Application-managed (apps.configuration), Phase C: the Vault service
# identity, the Workspace account it acts as, and the development-only key
# file. The two legacy variables they used to default from are still read,
# once, by the bootstrap import.
# Application-managed (apps.configuration), Phase B: the two ingest leases,
# the download timeout, and the export, archive, message, and OCR limits.
# The "combined export size must be at least the per-object size" rule is now
# a registry RELATION.
# Application-managed (apps.configuration), Phase C: the supervisor,
# records-officer, and facilities addresses, the portal address, the district
# closure days, the default estimate, the stall threshold, ClamAV, and Alta.
#
# Django's own from address. No application mail uses it: requester and staff
# mail go through the mailbox-pinned Gmail clients, and production refuses
# every generic Django send below. So it is a constant, not a setting.
DEFAULT_FROM_EMAIL = "publicrecords@psd401.net"
# Application mail uses the mailbox-pinned Gmail clients and their explicit
# gates. Keep Django's parallel backend visible in local logs and make every
# production send attempt fail closed, even if a host tries to select SMTP.
EMAIL_BACKEND = (
    "django.core.mail.backends.console.EmailBackend"
    if PRR_DEPLOYMENT_MODE == "development"
    else "config.mail_backends.ProductionDenyEmailBackend"
)

# --- Compliance automation ---
# Application-managed (apps.configuration), Phase B: the installment download
# lifetime, the per-installment record ceiling, and the access-code minimum
# length.
# Application-managed (apps.configuration): read through
# ``runtime.get("PRR_STAFF_NOTIFICATIONS_ENABLED")``. The value here is the
# one-time bootstrap seed the settings store imports, and is ignored afterwards.
PRR_STAFF_NOTIFICATIONS_ENABLED = _parse_strict_boolean(
    "PRR_STAFF_NOTIFICATIONS_ENABLED",
    os.environ.get("PRR_STAFF_NOTIFICATIONS_ENABLED", "false"),
)
# Application-managed (apps.configuration), Phase B: the records-officer
# recipient list and the repeat-alert suppression window. The "at least one
# recipient" rule is a prerequisite the store checks before it will let
# anyone enable staff alerts, asked of the store rather than of this file.
# Safety net: when explicitly enabled, queue the acknowledgment for durable
# delivery if no human has responded by the last RCW 42.56.520 business day.
# Application-managed (apps.configuration): read through
# ``runtime.get("AUTO_ACK_SAFETY_NET", raise_on_store_error=True)``. The value
# here is the one-time bootstrap seed the settings store imports, and is
# ignored afterwards.
AUTO_ACK_SAFETY_NET = _parse_strict_boolean(
    "AUTO_ACK_SAFETY_NET",
    os.environ.get("AUTO_ACK_SAFETY_NET", "false"),
)

# --- Anonymous public intake ---
# Application-managed (apps.configuration), Phase B: the two public-intake
# rates, the minimum fill time, the form token lifetime, and the processing
# stale window. So are the staff login rate, the three installment rates,
# and the personal-token rate, lifetime, active ceiling, and last-used
# window.
#
# Every DRF throttle already overrode get_rate(), and DRF builds a throttle
# per request, so a rate change now takes effect without a restart.

# Advisory model calls are disabled unless the district has approved the
# provider for the request/record data involved. This boundary proposes only;
# database constraints and domain services prevent it from applying outcomes.
PRR_AI_ADVISORY_ENABLED = _parse_strict_boolean(
    "PRR_AI_ADVISORY_ENABLED",
    os.environ.get("PRR_AI_ADVISORY_ENABLED", "false"),
)
PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED = _parse_strict_boolean(
    "PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED",
    os.environ.get("PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED", "false"),
)
# Auto-running the intent proposal (and the count it enables) at intake adds
# no requester-facing effect: it prepares the officer's starting point. It
# stays opt-in so a district turns it on with the same approval evidence the
# rest of advisory AI requires.
PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED = _parse_strict_boolean(
    "PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
    os.environ.get("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED", "false"),
)
# Application-managed (apps.configuration), Phase B: the provider, region,
# model, timeout, input ceiling, and response ceiling. "Does this deployment
# have an approved model to call at all?" used to be answered here, as
# PRR_AI_ADVISORY_MODEL_CONFIGURED; it is now the advisory_model_configured
# prerequisite, which asks the store the same question before it will let an
# administrator enable advisory AI.

# --- Camera footage (Avigilon Alta / Ava Aware) ---
# CAMERA_RETENTION_DAYS is application-managed (apps.configuration), Phase B.
# Shortening it below the true retention means footage that still exists is
# never held, so the Settings page spells that consequence out; outside the
# window the request still gets a human search, and there is still no
# automatic denial anywhere.

# Object storage (S3 or MinIO — identical API)
OBJECT_STORE = {
    "endpoint": os.environ.get("OBJECT_STORE_ENDPOINT", ""),
    "bucket": os.environ.get("OBJECT_STORE_BUCKET", "prr-records"),
    "access_key": os.environ.get("OBJECT_STORE_ACCESS_KEY", ""),
    "secret_key": os.environ.get("OBJECT_STORE_SECRET_KEY", ""),
    "region": os.environ.get("OBJECT_STORE_REGION", ""),
}
# Application-managed (apps.configuration), Phase B: the object-store connect
# and read timeouts, the retry attempts, the digest-verification memory
# ceiling, the three preview limits, and the redaction burn-in resolution.

# Public intake files are untrusted. Keep the accepted surface deliberately
# document-shaped, bound the work before hashing/scanning, and quarantine every
# byte until the private ClamAV service reports it clean.
#
# Application-managed (apps.configuration), Phase B: the intake attachment
# count, per-file, and combined limits, with the "combined must be at least
# per-file" rule now a registry RELATION. So are the office-converter
# timeout, the derivative source ceiling, the three media limits, and the
# migrated legacy-file ceiling -- legacy collections routinely exceed what
# the public form allows, which is why they have a ceiling of their own
# rather than borrowing one whose meaning is "what a member of the public may
# upload through a form".
#
# The accepted content types below are not a district decision: widening them
# widens the untrusted surface the malware scanner and the content sniffers
# were written against, so they stay in code.
# Application-managed (apps.configuration), Phase C: the Office converter
# path. It is a deployment fact rather than a district decision, but it is
# the difference between a Word record being reviewable and not, so it is
# visible and changeable where the person who notices can act.
PRR_INTAKE_ATTACHMENT_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/rtf",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "message/rfc822",
    "text/csv",
    "text/plain",
}
# Application-managed (apps.configuration), Phase B: the correspondence
# attachment count, per-file, and combined limits -- the version 1 contract
# (10 files, 25 MiB each, 50 MiB combined) is now the registry's bounds and
# a RELATION -- plus the scan stale window and the inbound reservation stale
# window.
