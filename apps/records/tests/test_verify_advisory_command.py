"""The Bedrock smoke test uses synthetic data and creates no legal record."""

from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.audit.models import AuditLog
from apps.records.models import AdvisoryRun


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_PROVIDER="bedrock",
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
)
class VerifyAdvisoryCommandTests(TestCase):
    @patch(
        "apps.records.management.commands.verify_advisory_ai.account_data_retention_mode",
        return_value="none",
    )
    @patch("apps.records.management.commands.verify_advisory_ai.invoke_advisory_model")
    def test_verifies_synthetic_proposal_without_persisting_a_run(self, invoke, _mode):
        invoke.return_value = {
            "summary": "A synthetic request for meeting minutes.",
            "required_search_terms": [],
            "search_terms": ["meeting minutes"],
            "record_system_slugs": [],
            "clarification_questions": [],
            "complexity_flags": [],
            "date_range": {"start": "2026-01-01", "end": None},
            "narrowing_options": [
                {
                    "terms": ["meeting minutes", "regular session"],
                    "rationale": "Only the regular-session minutes.",
                }
            ],
        }
        stdout = StringIO()

        call_command("verify_advisory_ai", stdout=stdout)

        kind, payload = invoke.call_args.args
        self.assertEqual(kind, AdvisoryRun.Kind.REQUEST_INTENT)
        self.assertIn("synthetic", payload["request_description"].lower())
        self.assertEqual(AdvisoryRun.objects.count(), 0)
        self.assertEqual(AuditLog.objects.count(), 0)
        output = stdout.getvalue()
        self.assertIn("Bedrock advisory verification passed", output)
        self.assertIn("no legal record was created", output)
        self.assertIn("data retention none", output)

    @patch("apps.records.management.commands.verify_advisory_ai.invoke_advisory_model")
    def test_refuses_unless_bedrock_retains_nothing(self, invoke):
        # Zero data retention (district decision, 2026-11-05) is an account
        # setting CDK cannot hold, so verification is where it is checked.
        for mode in ("default", "aws_review", "inherit"):
            with self.subTest(mode=mode), patch(
                "apps.records.management.commands.verify_advisory_ai.account_data_retention_mode",
                return_value=mode,
            ):
                with self.assertRaisesMessage(CommandError, f"retention mode is {mode}"):
                    call_command("verify_advisory_ai", stdout=StringIO())
        invoke.assert_not_called()
