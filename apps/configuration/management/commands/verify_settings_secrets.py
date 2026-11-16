# ABOUTME: Deploy gate: every stored settings secret must decrypt under the
# ABOUTME: running key. Prints counts and key names only, never a value.
"""Prove the deployment's settings-store key opens every stored secret.

``runtime.get_secret`` treats an unreadable secret as unset and only logs it,
so a wrong key (a mistyped PRR_SETTINGS_ENCRYPTION_KEY, or a DJANGO_SECRET_KEY
that differs from the one the secrets were written under) silently breaks
Google sign-in and every other stored credential. The production deployment
runs this after it starts the new release and rolls back on a non-zero exit.

A row with empty ciphertext is a secret an administrator cleared; it has
nothing to decrypt and is not counted. A row whose key the registry no longer
has (``ALTA_API_TOKEN``, from before Alta holds signed in with an account) is
read by nothing and cannot be cleared from the Settings page, so it is named
as retired and not judged: a stale row must not fail every later deploy.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.configuration import secrets
from apps.configuration.models import AppSecret
from apps.configuration.registry import SECRET_KEYS


class Command(BaseCommand):
    help = "Fail unless every stored settings secret decrypts under the running key."

    def handle(self, *args, **options):
        stored = AppSecret.objects.exclude(ciphertext="").values_list(
            "key", "ciphertext"
        )
        unreadable = []
        retired = set()
        total = 0
        for key, ciphertext in stored:
            if key not in SECRET_KEYS:
                retired.add(key)
                continue
            total += 1
            try:
                secrets.decrypt(ciphertext)
            except secrets.SecretUnreadable:
                unreadable.append(key)

        readable = total - len(unreadable)
        if unreadable:
            raise CommandError(
                f"{readable} of {total} stored secrets decrypt; unreadable: "
                f"{', '.join(sorted(unreadable))}."
            )
        summary = f"{readable} of {total} stored secrets decrypt."
        if retired:
            summary += f" Ignored retired secret rows: {', '.join(sorted(retired))}."
        self.stdout.write(summary)
