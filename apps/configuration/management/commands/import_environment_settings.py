# ABOUTME: One-time, idempotent import of each deployed environment switch
# ABOUTME: value into the in-app settings store, attributed to a system import.
"""Write down what this deployment was already doing.

The production deployment runs this as its own step right after it migrates;
the container entrypoint runs it wherever the entrypoint migrates. Every key
that already has a stored row is left alone, so this is safe on every
redeploy and an administrator's decision is never overwritten by a stale
environment value.

An imported value is preserved even when the Settings page would have
refused it (a dependent on without its parent, or a switch on without its
still-environment-managed prerequisite). Each one is logged as a warning so
the drift is noticed on the deploy that created it.

The value imported is whatever the deployment is *running*, resolved by
``runtime.bootstrap_value``: a Django settings attribute where the key still
has one, and otherwise the raw environment variable. Phase B keys are gone
from ``config/settings.py``, so reading them from Django settings would have
imported the registry default over the district's configured value and
silently discarded it.

A deployed value the registry refuses (out of bounds, malformed) fails the
command before anything is written, naming every such key. Storing the
default in its place would be permanent -- the environment is never read
again once a row exists -- so the deploy stops and the running release stays
up until the value is fixed. A key that already has a row is never judged,
since its environment value no longer means anything.

After the import, a deployment that would come up with nobody able to sign in
is logged as an error. The system checks that used to fail such a deploy are
gone, because the values they judged now live in the store. This does not
fail the command: once a row exists the environment is never read again, so
a refused deploy could not be repaired by redeploying, only stranded.
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.configuration import runtime, signin, values
from apps.configuration.models import AppSecret, AppSetting
from apps.configuration.registry import SETTINGS
from apps.configuration.runtime import _bootstrap_secret, bootstrap_value
from apps.configuration.services import (
    enable_blocker,
    import_environment_secret,
    import_environment_value,
)

logger = logging.getLogger("apps.configuration.import")
signin_logger = logging.getLogger("apps.configuration.signin")


class Command(BaseCommand):
    help = (
        "Import the currently deployed value of each application-managed "
        "setting into the settings store, once."
    )

    def handle(self, *args, **options):
        verbosity = int(options.get("verbosity", 1))
        stored = set(AppSetting.objects.values_list("key", flat=True))
        stored_secrets = set(AppSecret.objects.values_list("key", flat=True))
        pending = {}
        pending_secrets = {}
        refused = []
        for spec in SETTINGS:
            if spec.type == values.SECRET:
                # A secret is never echoed: what is carried forward is the
                # value itself, and what is reported is only its key.
                if spec.key not in stored_secrets:
                    pending_secrets[spec.key] = _bootstrap_secret(spec)
                continue
            if spec.key in stored:
                continue
            try:
                pending[spec.key] = bootstrap_value(spec, strict=True)
            except ValueError as exc:
                refused.append(str(exc))
        if refused:
            raise CommandError(
                "Deployed setting values the registry refuses; fix them in "
                "the environment and redeploy. Nothing was imported.\n"
                + "\n".join(refused)
            )
        imported_on = []
        imported = 0
        for key, value in pending.items():
            _row, created = import_environment_value(key, value)
            if created:
                imported += 1
                if value is True:
                    imported_on.append(key)
                if verbosity >= 2:
                    self.stdout.write(f"Imported {key}={value}")
        for key, plaintext in pending_secrets.items():
            _row, created = import_environment_secret(key, plaintext)
            if created:
                imported += 1
                if verbosity >= 2:
                    self.stdout.write(f"Imported {key} (value not shown)")
        # Checked after every key is stored, so a dependency is judged against
        # the imported parent rather than the pre-import bootstrap read.
        for key in imported_on:
            blocker = enable_blocker(key)
            if blocker:
                logger.warning(
                    "Imported %s=True, which the Settings page would refuse: %s",
                    key,
                    blocker,
                )
        if not settings.DEBUG:
            lockout = signin.lockout_blocker(
                runtime.get_uncached, runtime.secret_is_set
            )
            if lockout:
                signin_logger.error("Deployed sign-in configuration: %s", lockout)
        if verbosity >= 1:
            # A secret the deployment never configured is neither imported
            # nor already stored, so the two numbers are reported against
            # what was actually considered rather than against the registry.
            considered = len(pending) + len(pending_secrets) + len(stored) + len(
                stored_secrets
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Settings store current: {imported} imported, "
                    f"{considered - imported} already stored or not "
                    "configured."
                )
            )
