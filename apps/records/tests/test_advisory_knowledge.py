# ABOUTME: The request-intent advisory reads the district knowledge pack and
# ABOUTME: the registry's custodian context, within the input ceiling (#35).
import datetime as dt
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.advisory_ai import _advisory_system_message, _build_advisory_input
from apps.records.models import (
    AdvisoryRun,
    KnowledgeNoiseSender,
    KnowledgeRoleAccount,
    KnowledgeSystemNote,
    PublicRecordsRequest,
    RecordSystem,
    Requester,
)


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class RequestIntentKnowledgeInputTests(TestCase):
    def setUp(self):
        officer = get_user_model().objects.create(username="kp-ai-officer", email="officer@psd401.net", is_staff=True)
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Emails from the principal about the playground project.",
        )
        self.system = RecordSystem.objects.create(
            slug="google-workspace",
            name="Google Workspace",
            custodian_title="Director of Technology",
            custodian_email="tech.director@psd401.net",
            retention_days=365,
            retention_note="Mail is kept for one year unless held.",
            triage_keywords=["email"],
            search_checklist="1. Search named accounts.\n2. Search shared drives.",
        )
        RecordSystem.objects.create(slug="retired", name="Retired system", active=False)
        KnowledgeSystemNote.objects.create(
            record_system=self.system,
            notes="Exclude district newsletters before counting.",
        )
        KnowledgeRoleAccount.objects.create(
            role="Principal, Example Elementary",
            account_email="principal.example@psd401.net",
            starts_on=dt.date(2024, 7, 1),
            ends_on=dt.date(2026, 6, 30),
            note="Interim year.",
        )
        KnowledgeNoiseSender.objects.create(kind="domain", value="news.example.org")
        KnowledgeNoiseSender.objects.create(
            kind="list", value="all-staff.example.org", active=False
        )
        self.advisory_run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            request=self.request,
            requested_by=officer,
            model_identifier="district-approved-model-v1",
            input_sha256="placeholder",
        )

    def test_systems_carry_custodian_checklist_retention_and_search_notes(self):
        payload = _build_advisory_input(self.advisory_run).payload

        self.assertEqual(
            payload["available_record_systems"],
            [
                {
                    "slug": "google-workspace",
                    "name": "Google Workspace",
                    "triage_keywords": ["email"],
                    "custodian_title": "Director of Technology",
                    "custodian_email": "tech.director@psd401.net",
                    "retention_days": 365,
                    "retention_note": "Mail is kept for one year unless held.",
                    "search_checklist": "1. Search named accounts.\n2. Search shared drives.",
                    "search_notes": "Exclude district newsletters before counting.",
                }
            ],
        )

    def test_the_pack_carries_dated_role_accounts_and_active_noise_senders(self):
        payload = _build_advisory_input(self.advisory_run).payload

        self.assertEqual(
            payload["district_knowledge"],
            {
                "role_accounts": [
                    {
                        "role": "Principal, Example Elementary",
                        "account_email": "principal.example@psd401.net",
                        "starts_on": "2024-07-01",
                        "ends_on": "2026-06-30",
                        "note": "Interim year.",
                    }
                ],
                "noise_senders": [{"kind": "domain", "value": "news.example.org"}],
            },
        )
        self.assertFalse(_build_advisory_input(self.advisory_run).truncated)

    def test_an_edit_to_the_pack_changes_the_input_fingerprint(self):
        from apps.records.advisory_ai import advisory_input_sha256

        before = advisory_input_sha256(self.advisory_run)
        KnowledgeNoiseSender.objects.create(kind="address", value="no-reply@example.org")
        self.assertNotEqual(before, advisory_input_sha256(self.advisory_run))

    @override_settings(PRR_AI_MAX_INPUT_CHARACTERS=1_000)
    def test_the_context_stays_under_the_input_ceiling_and_reports_truncation(self):
        for index in range(60):
            KnowledgeRoleAccount.objects.create(
                role=f"Principal, Example School {index:02d}",
                account_email=f"principal{index:02d}@psd401.net",
            )

        advisory_input = _build_advisory_input(self.advisory_run)

        self.assertTrue(advisory_input.truncated)
        payload = advisory_input.payload
        context = {
            "available_record_systems": payload["available_record_systems"],
            "district_knowledge": payload["district_knowledge"],
        }
        self.assertLessEqual(
            len(payload["request_description"]) + len(json.dumps(context)),
            1_000,
        )
        # The system slugs are the advisory's routing vocabulary; they are
        # kept even when the rest of the context must be cut.
        self.assertEqual(
            [row["slug"] for row in payload["available_record_systems"]],
            ["google-workspace"],
        )

    def test_the_instruction_tells_the_model_how_to_use_the_pack(self):
        message = _advisory_system_message(AdvisoryRun.Kind.REQUEST_INTENT)

        self.assertIn("district_knowledge", message)
        self.assertIn("noise_senders", message)
