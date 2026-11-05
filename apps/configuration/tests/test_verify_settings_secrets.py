"""``verify_settings_secrets``: the deploy gate that every stored secret opens.

A wrong key does not crash anything: ``runtime.get_secret`` logs and returns
"", and Google sign-in quietly stops working. Moving the signing key to AWS
Secrets Manager is exactly when that could happen, so the deployment runs
this and rolls back on a non-zero exit.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.configuration import secrets
from apps.configuration.models import AppSecret


def _store(key, plaintext):
    return AppSecret.objects.create(
        key=key,
        ciphertext=secrets.encrypt(plaintext) if plaintext else "",
        fingerprint=secrets.fingerprint(plaintext) if plaintext else "",
    )


class VerifySettingsSecretsTests(TestCase):
    def _run(self):
        out = StringIO()
        call_command("verify_settings_secrets", stdout=out)
        return out.getvalue()

    def test_reports_counts_when_every_stored_secret_opens(self):
        _store("PRR_GOOGLE_OIDC_CLIENT_SECRET", "GOCSPX-sensitive-value")
        _store("ALTA_API_PASSWORD", "alta-sensitive-value")
        _store("CLEARED_SECRET", "")

        output = self._run()

        self.assertIn("2 of 2 stored secrets decrypt", output)
        self.assertNotIn("sensitive-value", output)

    def test_no_stored_secrets_is_a_pass(self):
        self.assertIn("0 of 0 stored secrets decrypt", self._run())

    def test_fails_naming_keys_only_when_a_secret_does_not_open(self):
        _store("PRR_GOOGLE_OIDC_CLIENT_SECRET", "GOCSPX-sensitive-value")
        other_key = secrets.Fernet.generate_key().decode("ascii")

        with override_settings(PRR_SETTINGS_ENCRYPTION_KEY=other_key):
            with self.assertRaises(CommandError) as caught:
                self._run()

        message = str(caught.exception)
        self.assertIn("PRR_GOOGLE_OIDC_CLIENT_SECRET", message)
        self.assertIn("0 of 1", message)
        self.assertNotIn("sensitive-value", message)

    def test_a_retired_secret_is_reported_by_name_and_not_judged(self):
        """A key the registry no longer has (the old Alta token) cannot be
        cleared from the Settings page, so a row left under an old encryption
        key must not fail every later deploy."""
        _store("PRR_GOOGLE_OIDC_CLIENT_SECRET", "GOCSPX-sensitive-value")
        other_key = secrets.Fernet.generate_key().decode("ascii")
        with override_settings(PRR_SETTINGS_ENCRYPTION_KEY=other_key):
            _store("ALTA_API_TOKEN", "retired-sensitive-value")

        output = self._run()

        self.assertIn("1 of 1 stored secrets decrypt", output)
        self.assertIn("ALTA_API_TOKEN", output)
        self.assertIn("retired", output)
        self.assertNotIn("sensitive-value", output)
