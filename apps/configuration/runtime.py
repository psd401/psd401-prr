# ABOUTME: The single read path for application-managed settings — cached,
# ABOUTME: restart-free across web/worker/beat, and failing closed on error.
"""``runtime.get(key)`` instead of ``settings.PRR_*``.

Three properties this module has to hold at once.

**Restart-free.** An Application Admin changes a value in the staff UI and
the next Celery task honours it. Values are cached in the shared Django cache
(Redis in production) for a few seconds rather than in process memory, so
every web worker, every Celery worker, and beat converge without a redeploy.
The TTL is the propagation bound: short enough that "off" means off within
seconds, long enough that a hot path does not become a database query.

This is also why no consumer may keep a managed value in a module-level
constant or a default argument. Both are evaluated once at import, which is
exactly the restart-bound behaviour this replaces.

**Fail closed.** If the store cannot be read at all -- database down, table
not yet migrated, cache misbehaving -- a read returns the registry's safe
default rather than the convenient one. A switch whose failure mode reaches a
requester defaults off, so a broken store silences the system instead of
letting it speak unsupervised. A limit's default is the shipped value, which
is the one the application has always been able to work at.

**Bootstrap once, then ignore the environment.** Until the deployed value has
been imported (``manage.py import_environment_settings``, run by the
production deployment right after it migrates, and by the entrypoint wherever
the entrypoint migrates), a key with no stored row resolves to what the
deployment is already running: the Django settings attribute if the key still
has one, and otherwise the raw environment variable, parsed by the same
registry rules a save uses. That is what makes the first deploy after this
change a no-op -- the running configuration is read from where it already
lives, including for the Phase B keys that `config/settings.py` no longer
defines at all. Once the row exists it wins outright, and the environment is
ignored for good.
"""

import logging
import os

from django.conf import settings
from django.core.cache import cache
from django.core.signals import setting_changed
from django.db import DatabaseError
from django.dispatch import receiver

from . import secrets, values
from .registry import SECRET_KEYS, SETTING_KEYS, spec_for

logger = logging.getLogger(__name__)

#: Seconds a resolved value may be reused before the store is consulted again.
#: This is the worst-case delay between an admin saving a change and a Celery
#: worker acting on it.
CACHE_TTL_SECONDS = 5

_CACHE_PREFIX = "prr:appsetting:"


def _cache_key(key: str) -> str:
    return f"{_CACHE_PREFIX}{key}"


def _coerce(spec, raw):
    """Turn a stored or bootstrap value into the canonical Python value.

    A read never refuses: a value the registry would not accept today (an
    old row whose bound has since tightened, or a malformed environment
    variable) resolves to the safe default and is logged, rather than
    raising somewhere a consumer cannot do anything about it.
    """
    try:
        return values.coerce(spec, raw)
    except ValueError:
        logger.warning(
            "Stored value for %s is not valid under the current registry; "
            "using the safe default.",
            spec.key,
        )
        return values.coerce(spec, spec.default)


def _cache_get(key):
    """Read one cached value, treating an unusable cache as a miss.

    The cache is an optimisation, never the source of truth: if Redis is
    unreachable the store is still authoritative, so a failure here costs a
    query rather than a wrong answer.
    """
    try:
        return cache.get(_cache_key(key))
    except Exception:  # noqa: BLE001 - an unreadable cache reads the store
        logger.warning("Settings cache unreadable for %s; reading the store.", key)
        return None


def _cache_set(key, value) -> None:
    try:
        cache.set(_cache_key(key), {"value": value}, CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - an unwritable cache is not a failed write
        logger.warning("Settings cache unwritable for %s.", key)


def _stored_value(key):
    """The stored value, or ``None`` when no row exists.

    Raises only for genuine store failures, which the caller turns into the
    safe default.
    """
    from .models import AppSetting

    return AppSetting.objects.filter(key=key).values_list("value", flat=True).first()


_UNSET = object()


def bootstrap_value(spec, *, strict=False):
    """What this deployment is running for ``spec`` before anything is stored.

    Django settings first, so an ``override_settings`` in a test and the
    Phase A keys `config/settings.py` still defines both keep working. Then
    the raw environment, which is where a Phase B key's deployed value lives
    now that the key is gone from `config/settings.py`. Then, for a key that
    used to inherit its default from another variable, that variable
    (``spec.bootstrap_from``), so a deployment that only ever set the parent
    keeps its value. Then the registry default.

    A deployed value the registry refuses resolves to the safe default and is
    logged -- unless ``strict``, which raises ``ValueError`` instead. The
    import is strict because what it stores is permanent: a silently
    substituted default would outlive the deploy that produced it.
    """
    configured = getattr(settings, spec.key, _UNSET)
    if configured is not _UNSET:
        return _deployed(spec, configured, spec.key, strict)
    raw = os.environ.get(spec.key)
    if raw is not None and raw.strip():
        return _deployed(spec, raw, spec.key, strict)
    for source in spec.bootstrap_from:
        inherited = _inherited_value(source, strict)
        if inherited is not None:
            return _deployed(spec, inherited, source, strict)
    return values.coerce(spec, spec.default)


def _deployed(spec, raw, source, strict):
    try:
        return values.coerce(spec, raw)
    except ValueError as exc:
        if strict:
            raise ValueError(f"{spec.key} (from {source}): {exc}") from exc
        logger.warning(
            "Deployed value for %s (from %s) is not valid; using the safe "
            "default until it is set in the application.",
            spec.key,
            source,
        )
        return values.coerce(spec, spec.default)


def _inherited_value(source, strict):
    """The deployed value of a parent a key used to default from, if any."""
    if source in SETTING_KEYS:
        return bootstrap_value(spec_for(source), strict=strict)
    configured = getattr(settings, source, None)
    if configured is None:
        configured = os.environ.get(source)
    if isinstance(configured, str) and not configured.strip():
        return None
    return configured


def _bootstrap_secret(spec) -> str:
    """What this deployment is running for a secret before one is stored.

    The inline environment variable, or the AWS Secrets Manager id the
    deployment used instead of one. Both are read through the same resolver
    ``config/settings.py`` used before the secret moved into the
    application, so a running deployment keeps signing people in between the
    deploy and the import.
    """
    from config.runtime_secrets import resolve_runtime_secret

    # A Django settings attribute answers first, as it does for every other
    # key. ``config/settings.py`` defines none for a secret, so on a real
    # deployment this is always the environment.
    inline = str(getattr(settings, spec.key, "") or "").strip() or (
        os.environ.get(spec.key) or ""
    ).strip()
    secret_id = (os.environ.get(f"{spec.key}_ID") or "").strip()
    if not inline and not secret_id:
        return ""
    try:
        return resolve_runtime_secret(
            inline_value=inline,
            secret_id=secret_id,
            region=(
                os.environ.get("AWS_DEFAULT_REGION")
                or os.environ.get("AWS_REGION")
                or None
            ),
        )
    except Exception:
        logger.exception(
            "The deployed value of %s could not be resolved; treating it as "
            "unset until it is set in the application.",
            spec.key,
        )
        return ""


def get_secret(key: str) -> str:
    """Resolve one write-only setting's plaintext, or ``""``.

    Never cached. A secret is read on a sign-in, not in a hot loop, and the
    five-second window that makes a limit change propagate cheaply is five
    seconds of a revoked credential still working.

    Fails closed in every direction an operator can get wrong: no row, an
    unreadable store, or a ciphertext this deployment's key cannot open all
    resolve to the empty string, which is exactly what every consumer
    already treats as "not configured".

    Before anything is stored, the deployed environment answers, as it does
    for every other key — that is what makes the first deploy after this
    change a no-op rather than a lockout.
    """
    spec = spec_for(key)
    if spec.type != values.SECRET:
        raise ValueError(f"{key} is not a secret setting.")
    from .models import AppSecret

    try:
        ciphertext = (
            AppSecret.objects.filter(key=key)
            .values_list("ciphertext", flat=True)
            .first()
        )
    except DatabaseError:
        logger.exception("Secret store unreadable for %s; treating as unset.", key)
        return ""
    if ciphertext is None:
        return _bootstrap_secret(spec)
    if not ciphertext:
        # A row with no ciphertext is a secret an administrator cleared. That
        # is a decision, and it outranks whatever the environment still holds
        # -- only the *absence* of a row means "nothing has been decided yet".
        return ""
    try:
        return secrets.decrypt(ciphertext)
    except secrets.SecretUnreadable:
        logger.error(
            "Stored secret %s cannot be decrypted with this deployment's key; "
            "treating it as unset.",
            key,
        )
        return ""


def secret_is_set(key: str) -> bool:
    """Whether a usable secret exists, without producing one."""
    return bool(get_secret(key))


def get(key: str, *, raise_on_store_error: bool = False):
    """Resolve one application-managed setting.

    ``raise_on_store_error`` is for a caller for whom "off" is itself a
    failure: the statutory safety net must fail loudly when it cannot read its
    switch, not skip a run looking exactly as if it were turned off.
    """
    spec = spec_for(key)
    if spec.type == values.SECRET:
        # The one caller that wants this value has to say so by name. A
        # secret reached through the ordinary read would be cached, and
        # would land in every place an ordinary value is allowed to go.
        raise ValueError(f"{key} is a secret; use runtime.get_secret().")
    cached = _cache_get(key)
    if cached is not None:
        return _coerce(spec, cached["value"])

    try:
        stored = _stored_value(key)
    except DatabaseError:
        if raise_on_store_error:
            raise
        # Fail closed: a store we cannot read is not evidence that a switch is
        # on. Only a genuine database failure is swallowed -- a programming
        # error here should surface, not silently disable correspondence.
        logger.exception("Settings store unreadable for %s; using safe default.", key)
        return values.coerce(spec, spec.default)

    if stored is None:
        # Pre-import bootstrap. Deliberately not cached -- this branch
        # resolves against a mutable Django setting or the process
        # environment rather than a durable row, and a cached copy of it
        # would outlive an override. It stops once the import has run.
        return bootstrap_value(spec)

    value = _coerce(spec, stored)
    _cache_set(key, values.to_json(spec, value))
    return value


def get_uncached(key: str):
    """Resolve one setting without consulting the cache.

    The write path uses this. A cached read is up to ``CACHE_TTL_SECONDS``
    behind, which is harmless for a consumer deciding whether to act and not
    harmless for a decision *about* the settings themselves: a five-second-old
    copy of a parent switch could let a dependent be enabled just after the
    parent was turned off.
    """
    invalidate(key)
    return get(key)


def all_values() -> dict:
    """Every managed setting resolved at once, for the staff Settings page.

    One query for the whole store rather than one per key: the registry is
    approaching a hundred entries, and a page that costs a hundred queries
    is a page nobody opens during an incident.
    """
    from .models import AppSetting

    try:
        stored = dict(AppSetting.objects.values_list("key", "value"))
    except DatabaseError:
        logger.exception("Settings store unreadable; showing safe defaults.")
        stored = {}
    resolved = {}
    for key in SETTING_KEYS:
        spec = spec_for(key)
        if spec.type == values.SECRET:
            # Not "resolved to a blank": absent. The page that calls this
            # asks the secret store for its own, value-free description.
            continue
        if key in stored:
            resolved[key] = _coerce(spec, stored[key])
        else:
            resolved[key] = bootstrap_value(spec)
    return resolved


def invalidate(key: str) -> None:
    """Drop one cached value so the next read goes to the store."""
    try:
        cache.delete(_cache_key(key))
    except Exception:  # noqa: BLE001 - a stale cache is corrected on the next write
        logger.warning("Settings cache could not be invalidated for %s.", key)


def invalidate_all() -> None:
    for key in SETTING_KEYS:
        if key not in SECRET_KEYS:
            invalidate(key)


@receiver(setting_changed)
def _drop_cache_on_setting_override(sender, setting, **kwargs):
    """Keep ``override_settings`` honest about the bootstrap fallback.

    Tests and management shells change Django settings in place. A key with
    no stored row resolves through ``settings``, so a stale cache entry would
    outlive the override. This never fires in a running deployment: nothing
    in production emits ``setting_changed``.
    """
    if setting in SETTING_KEYS and setting not in SECRET_KEYS:
        invalidate(setting)
