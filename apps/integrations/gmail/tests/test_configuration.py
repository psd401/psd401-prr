import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from apps.configuration import runtime
from apps.integrations.gmail import client


class GmailSettingsTests(TestCase):
    """What the environment still decides about the Gmail transport.

    Phase B moved the mailbox, the transport timeout, the processed label,
    the poll lookback, the poll stale threshold, the blocked intake sender
    domains, and the reply address into the settings store. Their defaults
    and their bounds are asserted against the registry and the save path in
    ``apps/configuration/tests/test_phase_b_settings.py``; what is left here
    is the environment-managed remainder: the strict booleans that are still
    bootstrap seeds. Phase C took the service identity, the key file, and the
    CloudWatch region as well, so those are asserted through the store below.
    """

    def test_safe_defaults(self):
        self.assertFalse(settings.PRR_GMAIL_ENABLED)
        self.assertEqual(runtime.get("PRR_GMAIL_IMPERSONATION_TARGET"), "")
        self.assertEqual(runtime.get("PRR_GMAIL_CREDENTIALS_FILE"), "")
        self.assertFalse(settings.PRR_GMAIL_INBOUND_POLL_ENABLED)
        self.assertFalse(settings.PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED)
        self.assertFalse(settings.PRR_CLOUDWATCH_METRICS_ENABLED)
        self.assertFalse(
            settings.PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED
        )

    def test_application_managed_defaults_resolve_to_the_shipped_values(self):
        """The values a district ran before Phase B are the ones it still runs.

        Read through the store, which with no row and no environment
        resolves to the registry default -- so this is the assertion that
        moving them changed nothing for a deployment that changes nothing.
        """
        self.assertEqual(
            runtime.get("PRR_GMAIL_MAILBOX"), "publicrecords@psd401.net"
        )
        self.assertEqual(runtime.get("PRR_GMAIL_TIMEOUT_SECONDS"), 30)
        self.assertEqual(
            runtime.get("PRR_GMAIL_INBOUND_PROCESSED_LABEL"), "PRR/Processed"
        )
        self.assertEqual(runtime.get("PRR_GMAIL_INBOUND_LOOKBACK_DAYS"), 7)
        self.assertEqual(runtime.get("PRR_GMAIL_INBOUND_STALE_SECONDS"), 300)
        self.assertEqual(
            runtime.get("PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS"),
            ("nextrequest.com",),
        )
        self.assertEqual(
            runtime.get("PRR_CORRESPONDENCE_REPLY_LOCAL_PART"), "publicrecords"
        )
        self.assertEqual(
            runtime.get("PRR_CORRESPONDENCE_REPLY_DOMAIN"), "psd401.net"
        )

    def _import_settings(self, **environment):
        process_environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(settings.BASE_DIR)),
            "PYTHON_DOTENV_DISABLED": "1",
            "DJANGO_DEBUG": "true",
            **environment,
        }
        return subprocess.run(
            [sys.executable, "-c", "import config.settings"],
            cwd=settings.BASE_DIR,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _print_setting(self, name, **environment):
        process_environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(settings.BASE_DIR)),
            "PYTHON_DOTENV_DISABLED": "1",
            "DJANGO_DEBUG": "true",
            **environment,
        }
        return subprocess.run(
            [
                sys.executable,
                "-c",
                f"import config.settings as configured; print(configured.{name})",
            ],
            cwd=settings.BASE_DIR,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    @override_settings(
        PRR_GMAIL_IMPERSONATION_TARGET="prr-gmail@example.iam.gserviceaccount.com"
    )
    def test_the_deployed_impersonation_target_still_answers_before_the_import(
        self,
    ):
        """Phase C moved identity, and the move is a no-op for a live deploy.

        The service account the application impersonates is a district
        decision after all: re-issuing it is something a district does, and
        doing it by hand on the host is the operation this work exists to
        end. What must not move is the *key*, and none is involved --
        deployed access is keyless impersonation and a deployed private key
        is still refused outright.

        Until the value is imported, the store resolves it from where the
        deployment already keeps it, which is what makes the first deploy
        after this change change nothing.
        """
        self.assertEqual(
            runtime.get("PRR_GMAIL_IMPERSONATION_TARGET"),
            "prr-gmail@example.iam.gserviceaccount.com",
        )

    def test_enabled_setting_rejects_non_boolean_values(self):
        result = self._import_settings(PRR_GMAIL_ENABLED="yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PRR_GMAIL_ENABLED must be exactly true or false.",
            result.stderr,
        )

    def test_inbound_poll_switches_are_strictly_validated(self):
        # Only the two booleans are left: the label, the lookback, and the
        # stale threshold are bounded by their registry types now.
        invalid = (
            (
                {"PRR_GMAIL_INBOUND_POLL_ENABLED": "yes"},
                "PRR_GMAIL_INBOUND_POLL_ENABLED must be exactly true or false.",
            ),
            (
                {"PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED": "yes"},
                (
                    "PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED must be exactly true or "
                    "false."
                ),
            ),
        )
        for environment, expected in invalid:
            with self.subTest(environment=environment):
                result = self._import_settings(**environment)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_the_metrics_switch_is_a_strict_boolean(self):
        result = self._import_settings(PRR_CLOUDWATCH_METRICS_ENABLED="yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PRR_CLOUDWATCH_METRICS_ENABLED must be exactly true or false.",
            result.stderr,
        )

    @override_settings(PRR_CLOUDWATCH_REGION="unsafe region")
    def test_an_unusable_region_resolves_to_blank_rather_than_crash_looping(self):
        """The region's shape moved from startup to the registry.

        A malformed region used to raise ``ImproperlyConfigured`` and
        crash-loop the container, where nobody without host access could see
        why. It is a setting now: a malformed one is refused on the save path
        with a sentence an administrator reads, and a malformed *deployed*
        value resolves to blank -- which the metrics prerequisite then
        refuses to enable metrics without.
        """
        self.assertEqual(runtime.get("PRR_CLOUDWATCH_REGION"), "")

    @override_settings(PRR_CLOUDWATCH_REGION="us-west-2")
    def test_a_usable_region_resolves_as_configured(self):
        self.assertEqual(runtime.get("PRR_CLOUDWATCH_REGION"), "us-west-2")

    def test_automated_outbound_is_a_strict_boolean(self):
        # The blocked sender domains moved; their exact-DNS-domain rule is
        # asserted against the save path in test_phase_b_settings.
        result = self._import_settings(
            PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED="yes",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED must be exactly "
            "true or false.",
            result.stderr,
        )


class GmailKeyFileIsRefusedOutsideDevelopmentTests(TestCase):
    """The api.E018 rule, enforced where the credential is actually built.

    It used to be a startup check over an environment variable. The key file
    is a setting now, so a verdict taken once at process start could be wrong
    minutes later; the refusal lives in ``_credentials`` instead, which is
    the only place a key file could ever be turned into a credential. That is
    also a stronger guarantee than the check was: it holds for a value that
    arrived after the container booted.
    """

    @override_settings(
        DEBUG=False,
        PRR_GMAIL_IMPERSONATION_TARGET="",
        PRR_GMAIL_CREDENTIALS_FILE="/run/secrets/gmail-key.json",
    )
    def test_a_deployed_key_file_is_refused_rather_than_used(self):
        with self.assertRaises(client.GmailUnavailable) as raised:
            client._credentials()

        self.assertIn("forbidden outside debug mode", str(raised.exception))
        self.assertEqual(raised.exception.category, "authentication_failed")

    @override_settings(
        DEBUG=False,
        PRR_GMAIL_IMPERSONATION_TARGET="",
        PRR_GMAIL_CREDENTIALS_FILE="",
    )
    def test_a_deployed_instance_without_an_identity_refuses_rather_than_guesses(
        self,
    ):
        with self.assertRaises(client.GmailUnavailable) as raised:
            client._credentials()

        self.assertIn("PRR_GMAIL_IMPERSONATION_TARGET", str(raised.exception))
