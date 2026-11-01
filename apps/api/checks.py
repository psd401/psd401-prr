from django.conf import settings
from django.core.checks import Error, Tags, register

INSECURE_SECRET_KEYS = {"", "dev-only", "change-me", "build-only"}


# The Gmail transport's startup check is gone, in three steps.
#
# Phase A took the enable switch, so startup could no longer ask "is Gmail
# on?" -- an administrator may turn it on minutes later without a redeploy --
# and the keyless-impersonation requirement became a prerequisite the settings
# store enforces before it will enable the transport.
#
# Phase B took the mailbox, so the address checks (api.E017: a valid address,
# at most the 100 characters the correspondence provider-account column holds)
# became part of its registry type, enforced on the save path.
#
# Phase C took the credentials file (api.E018). "No private key outside
# development" is not something a startup check can decide about a value that
# changes without a restart, and it never needed to: it is enforced where it
# always mattered, in ``apps.integrations.gmail.client._credentials``, which
# refuses to build a credential from a key file while DEBUG is false no
# matter what is configured.


@register(Tags.security, deploy=True)
def production_security_checks(app_configs, **kwargs):
    if settings.PRR_DEPLOYMENT_MODE != "production":
        return []

    errors = []
    if settings.DEBUG:
        errors.append(
            Error(
                "DJANGO_DEBUG must be false outside the explicit development environment.",
                id="api.E015",
            )
        )
    if settings.SECRET_KEY in INSECURE_SECRET_KEYS or len(settings.SECRET_KEY) < 50:
        errors.append(
            Error(
                "DJANGO_SECRET_KEY must be a unique high-entropy production secret.",
                id="api.E001",
            )
        )
    if not settings.SECURE_SSL_REDIRECT:
        errors.append(
            Error(
                "SECURE_SSL_REDIRECT must be enabled outside local development.",
                id="api.E002",
            )
        )
    if not settings.SESSION_COOKIE_SECURE:
        errors.append(
            Error(
                "SESSION_COOKIE_SECURE must be enabled outside local development.",
                id="api.E003",
            )
        )
    if not settings.CSRF_COOKIE_SECURE:
        errors.append(
            Error(
                "CSRF_COOKIE_SECURE must be enabled outside local development.",
                id="api.E004",
            )
        )
    if "*" in settings.ALLOWED_HOSTS:
        errors.append(
            Error(
                "ALLOWED_HOSTS must list exact production hostnames; '*' is forbidden.",
                id="api.E005",
            )
        )
    # The sign-in checks (api.E006 through api.E010) moved with their
    # subjects in Phase C. A system check runs once, at process start,
    # against the environment; these values are now settings an Application
    # Admin changes in seconds, so a startup verdict about them would be
    # stale before it was useful and could not be re-run without a redeploy.
    # They are enforced where the change happens instead: the sign-in mode
    # and the OAuth client are checked by the lockout rule on the save path
    # (apps.configuration.signin), which refuses any change that would leave
    # nobody able to sign in, and the break-glass-needs-the-admin-route rule
    # (api.E010) is a registry ``requires`` on the switch itself. The Alta
    # HTTPS rule (api.E016) is the stored setting's own pattern.
    return errors
