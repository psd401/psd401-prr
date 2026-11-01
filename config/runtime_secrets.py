"""Resolve deployment secrets without persisting them in application env files."""

import json

from django.core.exceptions import ImproperlyConfigured


def resolve_runtime_secret(
    *,
    inline_value,
    secret_id,
    region=None,
    client_factory=None,
):
    """Return an inline secret or load its AWSCURRENT value from AWS.

    Local and explicitly isolated non-AWS tests may use the inline environment
    variable. Production sets only the non-secret Secrets Manager id and
    authenticates through the AWS instance role.
    """
    if inline_value and secret_id:
        raise ImproperlyConfigured(
            "Configure either PRR_GOOGLE_OIDC_CLIENT_SECRET or "
            "PRR_GOOGLE_OIDC_CLIENT_SECRET_ID, not both."
        )
    if not secret_id:
        return inline_value

    if client_factory is None:
        import boto3

        client_factory = boto3.client

    client_options = {}
    if region:
        client_options["region_name"] = region

    try:
        client = client_factory("secretsmanager", **client_options)
        response = client.get_secret_value(
            SecretId=secret_id,
            VersionStage="AWSCURRENT",
        )
    except Exception:  # noqa: BLE001 - the provider error is not chained into startup
        # Do not chain the provider error: an upstream message can contain
        # request material and would otherwise appear in startup tracebacks.
        raise ImproperlyConfigured(
            "Unable to load the Google OIDC client secret from AWS Secrets Manager."
        ) from None

    value = response.get("SecretString")
    if not isinstance(value, str) or not value:
        raise ImproperlyConfigured(
            "AWS Secrets Manager returned an empty Google OIDC client secret."
        )
    return value


# The deployed instance's signing key and settings-store key. Both are read
# from one Secrets Manager secret at process start (psd-dev-standards
# standards/04 rule 15a) so neither lives in a host env file or in a
# container's stored configuration. The key set is exact: an unknown key is
# refused rather than exported, so the secret can never smuggle in config.
RUNTIME_SECRET_KEYS = ("DJANGO_SECRET_KEY", "PRR_SETTINGS_ENCRYPTION_KEY")


def load_runtime_environment(*, secret_id, region, client_factory=None):
    """Return the runtime secret's AWSCURRENT key/value pairs.

    ``PRR_SETTINGS_ENCRYPTION_KEY`` may be ``""``: blank derives the
    settings-store key from the signing key, and a value generated here would
    strand every stored secret. Nothing in this function ever generates one.
    No error message carries a value.
    """
    if client_factory is None:
        import boto3

        client_factory = boto3.client

    try:
        client = client_factory("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_id, VersionStage="AWSCURRENT")
    except Exception:  # noqa: BLE001 - the provider error is not chained into startup
        # Not chained: an upstream message can carry request material.
        raise ImproperlyConfigured(
            f"Unable to load runtime secret {secret_id} from AWS Secrets Manager."
        ) from None

    try:
        values = json.loads(response.get("SecretString") or "")
    except (TypeError, ValueError):
        values = None
    if not isinstance(values, dict):
        raise ImproperlyConfigured(
            f"Runtime secret {secret_id} must be a JSON object."
        )

    if sorted(values) != sorted(RUNTIME_SECRET_KEYS):
        raise ImproperlyConfigured(
            f"Runtime secret {secret_id} must hold exactly "
            f"{', '.join(RUNTIME_SECRET_KEYS)}."
        )
    if not all(isinstance(value, str) for value in values.values()):
        raise ImproperlyConfigured(
            f"Every value in runtime secret {secret_id} must be a string."
        )
    if not values["DJANGO_SECRET_KEY"]:
        raise ImproperlyConfigured(
            f"Runtime secret {secret_id} has an empty DJANGO_SECRET_KEY."
        )
    return values


def read_runtime_secret(environ, *, client_factory=None):
    """Return the runtime secret's keys when PRR_RUNTIME_SECRET_ID is set.

    Without the id this returns ``{}``, so local, CI, and test runs read their
    environment as before. With it, neither key may also be set in the
    environment: an inline copy, equal or not, is a secret back in a host file
    (production's .env.app was scrubbed on 2026-10-14), so the process refuses
    to start, naming the key only.

    The values are returned, never written into ``environ``. A child process
    -- a media tool, a nested ``manage.py`` -- therefore inherits no secret and
    cannot trip its own inline-key refusal.
    """
    secret_id = environ.get("PRR_RUNTIME_SECRET_ID", "").strip()
    if not secret_id:
        return {}
    region = environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise ImproperlyConfigured(
            "PRR_RUNTIME_SECRET_ID needs AWS_REGION or AWS_DEFAULT_REGION."
        )

    values = load_runtime_environment(
        secret_id=secret_id, region=region, client_factory=client_factory
    )
    for key in values:
        if environ.get(key, ""):
            raise ImproperlyConfigured(
                f"{key} must not be set in the environment; it comes from "
                f"runtime secret {secret_id}."
            )
    return values
