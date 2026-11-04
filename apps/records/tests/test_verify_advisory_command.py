"""The Bedrock smoke test uses synthetic data and creates no legal record."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
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
    @patch("apps.records.management.commands.verify_advisory_ai.invoke_advisory_model")
    def test_verifies_synthetic_proposal_without_persisting_a_run(self, invoke):
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
