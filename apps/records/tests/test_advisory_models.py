"""Each advisory kind can run on its own model, and every call is made with
the model its run recorded -- never whatever the setting says by then."""

import hashlib
import json
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.advisory_ai import (
    MAX_OUTPUT_TOKENS,
    AdvisoryProviderError,
    execute_advisory_run,
    invoke_advisory_model,
    model_for_kind,
    request_record_responsiveness_advisory,
    request_request_intent_advisory,
)
from apps.records.models import (
    AdvisoryRun,
    PublicRecordsRequest,
    RecordItem,
    Requester,
)

INTENT = {
    "summary": "Board minutes.",
    "required_search_terms": [],
    "search_terms": ["board minutes"],
    "record_system_slugs": [],
    "clarification_questions": [],
    "complexity_flags": [],
    "date_range": {"start": None, "end": None},
    "narrowing_options": [],
}


def _converse_reply(payload):
    return {"output": {"message": {"content": [{"text": json.dumps(payload)}]}}}


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class PerKindModelTests(TestCase):
    def setUp(self):
        # Runs stay queued: no test here may reach a real provider.
        queue = patch("apps.records.tasks.execute_advisory_analysis.delay")
        queue.start()
        self.addCleanup(queue.stop)
        self.officer = get_user_model().objects.create(
            username="model-officer", email="model-officer@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Board minutes from March.",
        )

    def test_a_blank_kind_setting_falls_back_to_the_shared_model(self):
        for kind in AdvisoryRun.Kind.values:
            with self.subTest(kind=kind):
                self.assertEqual(model_for_kind(kind), "us.amazon.nova-2-lite-v1:0")

    @override_settings(PRR_AI_MODEL_INTENT="us.anthropic.claude-opus-5-5")
    def test_an_intent_run_records_the_intent_model(self):
        run = request_request_intent_advisory(self.prr.pk, actor=self.officer)
        self.assertEqual(run.model_identifier, "us.anthropic.claude-opus-5-5")

    @override_settings(PRR_AI_MODEL_RESPONSIVENESS="us.anthropic.claude-haiku-5-5")
    def test_a_responsiveness_run_records_the_review_model(self):
        item = RecordItem.objects.create(
            request=self.prr,
            original_filename="minutes.pdf",
            storage_key=f"records/{self.prr.pk}/minutes.pdf",
            content_length=10,
            content_type="application/pdf",
            sha256=hashlib.sha256(b"minutes").hexdigest(),
            ocr_text="Board minutes for March.",
        )
        run = request_record_responsiveness_advisory(item.pk, actor=self.officer)
        self.assertEqual(run.model_identifier, "us.anthropic.claude-haiku-5-5")

    def test_the_call_uses_the_model_the_run_recorded(self):
        # The run waits in the queue while the setting changes.
        with override_settings(PRR_AI_MODEL_INTENT="us.anthropic.claude-opus-5-5"):
            run = request_request_intent_advisory(self.prr.pk, actor=self.officer)
        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.PENDING)
        # The setting changes after the run was recorded; the call must not.
        with override_settings(PRR_AI_MODEL_INTENT="us.openai.gpt-6.1-sol"), patch(
            "apps.records.advisory_ai.boto3.client",
        ) as client_factory:
            client_factory.return_value.get_account_data_retention.return_value = {"mode": "none"}
            client_factory.return_value.converse.return_value = _converse_reply(INTENT)
            execute_advisory_run(run.pk)

        run.refresh_from_db()
        call = client_factory.return_value.converse.call_args.kwargs
        self.assertEqual(call["modelId"], "us.anthropic.claude-opus-5-5")
        self.assertEqual(
            call["inferenceConfig"]["maxTokens"],
            MAX_OUTPUT_TOKENS[AdvisoryRun.Kind.REQUEST_INTENT],
        )
        run.refresh_from_db()
        self.assertEqual(run.state, AdvisoryRun.State.COMPLETE)

    def test_every_kind_has_an_output_budget(self):
        # Scope refinement (#36) is a model call with no AdvisoryRun row of
        # its own; it is the one budget that is not an advisory kind.
        from apps.records.advisory_ai import SCOPE_REFINEMENT

        self.assertEqual(set(MAX_OUTPUT_TOKENS), set(AdvisoryRun.Kind.values) | {SCOPE_REFINEMENT})

    def test_each_kind_output_budget_is_an_in_app_setting(self):
        # #31: intake, intent, responsiveness and redaction each have their
        # own administrator-set output budget, defaulting to the code budget.
        from apps.configuration.registry import SETTINGS_BY_KEY
        from apps.records.advisory_ai import max_output_tokens

        for kind, key in (
            (AdvisoryRun.Kind.INTAKE_CLASSIFICATION, "PRR_AI_MAX_OUTPUT_TOKENS_INTAKE"),
            (AdvisoryRun.Kind.REQUEST_INTENT, "PRR_AI_MAX_OUTPUT_TOKENS_INTENT"),
            (AdvisoryRun.Kind.RECORD_RESPONSIVENESS, "PRR_AI_MAX_OUTPUT_TOKENS_RESPONSIVENESS"),
            (AdvisoryRun.Kind.RECORD_REDACTIONS, "PRR_AI_MAX_OUTPUT_TOKENS_REDACTION"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(SETTINGS_BY_KEY[key].default, MAX_OUTPUT_TOKENS[kind])
                self.assertEqual(max_output_tokens(kind), MAX_OUTPUT_TOKENS[kind])
                with override_settings(**{key: 3000}):
                    self.assertEqual(max_output_tokens(kind), 3000)

    def test_the_call_uses_the_configured_output_budget(self):
        with override_settings(PRR_AI_MODEL_INTENT="us.anthropic.claude-opus-5-5"):
            run = request_request_intent_advisory(self.prr.pk, actor=self.officer)
        with override_settings(PRR_AI_MAX_OUTPUT_TOKENS_INTENT=2500), patch(
            "apps.records.advisory_ai.boto3.client",
        ) as client_factory:
            client_factory.return_value.get_account_data_retention.return_value = {"mode": "none"}
            client_factory.return_value.converse.return_value = _converse_reply(INTENT)
            execute_advisory_run(run.pk)
        call = client_factory.return_value.converse.call_args.kwargs
        self.assertEqual(call["inferenceConfig"]["maxTokens"], 2500)

    def test_only_nova_is_sent_a_temperature(self):
        # Production, 2026-11-05: the Claude 5.x and GPT-6.1 models reject
        # the field outright ("temperature is deprecated for this model" /
        # "doesn't support the temperature field"); Nova keeps temperature 0.
        from apps.records.advisory_ai import inference_config

        self.assertEqual(
            inference_config(AdvisoryRun.Kind.REQUEST_INTENT, "us.amazon.nova-2-lite-v1:0"),
            {"maxTokens": 2000, "temperature": 0},
        )
        for model in (
            "us.anthropic.claude-opus-5-5",
            "us.anthropic.claude-haiku-5-5",
            "us.openai.gpt-6.1-sol",
            "us.openai.gpt-6.1-terra",
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    inference_config(AdvisoryRun.Kind.REQUEST_INTENT, model),
                    {"maxTokens": 2000},
                )

    def test_an_explicit_model_overrides_the_kind_setting(self):
        with patch("apps.records.advisory_ai.boto3.client") as client_factory:
            client_factory.return_value.get_account_data_retention.return_value = {"mode": "none"}
            client_factory.return_value.converse.return_value = _converse_reply(INTENT)
            invoke_advisory_model(
                AdvisoryRun.Kind.REQUEST_INTENT,
                {"request_description": "Synthetic"},
                model_identifier="us.openai.gpt-6.1-terra",
            )
        call = client_factory.return_value.converse.call_args.kwargs
        self.assertEqual(call["modelId"], "us.openai.gpt-6.1-terra")
        # The wire call itself never carries a temperature for GPT-6.1.
        self.assertNotIn("temperature", call["inferenceConfig"])

    def test_no_request_text_is_sent_unless_bedrock_retains_nothing(self):
        # Zero data retention is checked before every call, not only at
        # verification: a mode changed later stops the calls (fails closed).
        for mode in ("default", "aws_review", "inherit", ""):
            with self.subTest(mode=mode), patch(
                "apps.records.advisory_ai.boto3.client",
            ) as client_factory:
                client_factory.return_value.get_account_data_retention.return_value = {"mode": mode}
                with self.assertRaises(AdvisoryProviderError) as raised:
                    invoke_advisory_model(
                        AdvisoryRun.Kind.REQUEST_INTENT,
                        {"request_description": "Synthetic"},
                        model_identifier="us.amazon.nova-2-lite-v1:0",
                    )
                self.assertEqual(raised.exception.code, "provider_rejected")
                client_factory.return_value.converse.assert_not_called()

    def test_the_verify_command_can_name_a_model(self):
        out = StringIO()
        with patch("apps.records.advisory_ai.boto3.client") as client_factory:
            client_factory.return_value.converse.return_value = _converse_reply(INTENT)
            client_factory.return_value.get_account_data_retention.return_value = {"mode": "none"}
            call_command(
                "verify_advisory_ai", "--model", "us.anthropic.claude-haiku-5-5",
                stdout=out,
            )
        self.assertEqual(
            client_factory.return_value.converse.call_args.kwargs["modelId"],
            "us.anthropic.claude-haiku-5-5",
        )
        self.assertIn("us.anthropic.claude-haiku-5-5", out.getvalue())


class BedrockGrantMatchesDocumentedModelsTests(TestCase):
    """The CDK grant is the real allow-list: a model it does not name is an
    AccessDenied in production. The approved list in the docs must match it."""

    def test_docs_and_grant_name_the_same_inference_profiles(self):
        root = Path(settings.BASE_DIR)
        grant = (root / "infra/cdk/lib/advisory-bedrock.ts").read_text()
        granted = set(re.findall(r'profileId: "([^"]+)"', grant))
        self.assertIn("us.amazon.nova-2-lite-v1:0", granted)
        self.assertIn("us.anthropic.claude-opus-5-5", granted)
        self.assertIn("us.openai.gpt-6.1-terra", granted)
        docs = (root / "docs/advisory-ai.md").read_text()
        for profile in granted:
            with self.subTest(profile=profile):
                self.assertIn(f"`{profile}`", docs)
        # The Settings pick list offers exactly the granted profiles.
        from apps.configuration.registry import BEDROCK_PROFILES, SETTINGS_BY_KEY

        self.assertEqual(set(BEDROCK_PROFILES), granted)
        self.assertEqual(SETTINGS_BY_KEY["PRR_AI_MODEL_INTAKE"].suggestions, BEDROCK_PROFILES)
        # And the reverse: the docs' allow-list names nothing the grant lacks.
        documented = set(re.findall(r"^- `(us\.[^`]+)`$", docs, flags=re.MULTILINE))
        self.assertEqual(documented, granted)
