from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apps.integrations.gmail import client


class VerifyGmailCommandTests(SimpleTestCase):
    @mock.patch("apps.integrations.gmail.client.verify_configuration")
    def test_reports_credentials_subject_scopes_and_profile_without_sending(
        self,
        verify,
    ):
        verify.return_value = client.VerificationReport(
            credentials_ok=True,
            subject="publicrecords@psd401.net",
            granted_scopes=tuple(client.SCOPES),
            profile_email="publicrecords@psd401.net",
        )
        stdout = StringIO()

        call_command("verify_gmail", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("Credentials: OK", output)
        self.assertIn("Subject: OK — publicrecords@psd401.net", output)
        for scope in client.SCOPES:
            self.assertIn(f"Scope: OK — {scope}", output)
        self.assertIn("Profile: OK — publicrecords@psd401.net", output)
        self.assertIn("Gmail verification passed; no message was sent.", output)
        verify.assert_called_once_with()

    @mock.patch("apps.integrations.gmail.client.verify_configuration")
    def test_failure_includes_actionable_remediation(self, verify):
        verify.side_effect = client.GmailUnavailable(
            "Gmail credentials could not be resolved.",
            category="authentication_failed",
            remediation=(
                "Check the workload identity binding or the local debug key path."
            ),
        )

        with self.assertRaises(CommandError) as caught:
            call_command("verify_gmail")

        self.assertIn("credentials could not be resolved", str(caught.exception))
        self.assertIn("Remediation:", str(caught.exception))
        self.assertIn("workload identity binding", str(caught.exception))
