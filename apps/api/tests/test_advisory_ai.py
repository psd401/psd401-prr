"""Staff REST surfaces expose advisory proposals without applying them."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import AdvisoryRun, PublicRecordsRequest, RecordItem, Requester


@override_settings(
    PRR_AI_ADVISORY_ENABLED=True,
    PRR_AI_AWS_REGION="us-west-2",
    PRR_AI_MODEL="district-approved-model-v1",
    PRR_AI_MAX_INPUT_CHARACTERS=50_000,
)
class AdvisoryAIAPITests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "advisory-api-officer", "officer@example.org", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = User.objects.create_user(
            "advisory-api-reviewer", "reviewer@example.org", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        requester = Requester.objects.create(email="requester@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Contracts for the field project.",
        )
        self.item = RecordItem.objects.create(
            request=self.request,
            storage_key="records/item.pdf",
            content_length=12,
            content_type="application/pdf",
            sha256="b" * 64,
            ocr_text="Field project contract",
        )

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_request_intent_get_and_post_are_officer_only(self, delay):
        url = f"/api/staff/requests/{self.request.number}/advisory-intent/"
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)

        self.client.force_authenticate(self.officer)
        empty = self.client.get(url)
        self.assertEqual(empty.status_code, 200)
        self.assertTrue(empty.data["enabled"])
        self.assertIsNone(empty.data["latest"])

        queued = self.client.post(url, {})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.data["kind"], "request_intent")
        self.assertEqual(queued.data["state"], "pending")
        self.assertFalse(queued.data["applied"])
        self.assertNotIn("request_description", queued.data)
        delay.assert_called_once()

    @patch("apps.records.tasks.execute_advisory_analysis.delay")
    def test_record_responsiveness_surface_is_reviewer_scoped(self, delay):
        url = f"/api/staff/record-items/{self.item.pk}/advisory-responsiveness/"
        self.client.force_authenticate(self.reviewer)
        queued = self.client.post(url, {})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.data["kind"], "record_responsiveness")
        self.assertEqual(str(queued.data["record_item"]), str(self.item.pk))

        latest = self.client.get(url)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.data["latest"]["id"], queued.data["id"])
        self.item.refresh_from_db()
        self.assertEqual(self.item.review_state, RecordItem.ReviewState.UNREVIEWED)

    @override_settings(PRR_AI_ADVISORY_ENABLED=False)
    def test_disabled_get_is_legible_and_post_fails_closed(self):
        self.client.force_authenticate(self.officer)
        url = f"/api/staff/requests/{self.request.number}/advisory-intent/"
        response = self.client.get(url)
        self.assertFalse(response.data["enabled"])
        self.assertEqual(self.client.post(url, {}).status_code, 400)

    def test_serializer_exposes_bounded_proposal_and_provenance_not_source_text(self):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.REQUEST_INTENT,
            state=AdvisoryRun.State.COMPLETE,
            request=self.request,
            requested_by=self.officer,
            model_identifier="district-approved-model-v1",
            input_sha256="c" * 64,
            proposal={
                "summary": "Field-contract request",
                "required_search_terms": ["field project"],
                "search_terms": ["field contract"],
                "record_system_slugs": [],
                "clarification_questions": [],
                "complexity_flags": [],
            },
        )
        self.client.force_authenticate(self.officer)
        response = self.client.get(
            f"/api/staff/requests/{self.request.number}/advisory-intent/"
        )
        latest = response.data["latest"]
        self.assertEqual(latest["id"], str(run.pk))
        self.assertEqual(latest["input_sha256"], "c" * 64)
        self.assertEqual(latest["proposal"]["summary"], "Field-contract request")
        self.assertEqual(
            latest["proposal"]["required_search_terms"],
            ["field project"],
        )
        self.assertNotIn(self.request.description, str(latest))

    def test_serializer_exposes_system_trigger_without_inventing_an_actor(self):
        run = AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            state=AdvisoryRun.State.PENDING,
            trigger=AdvisoryRun.Trigger.AUTOMATIC,
            request=self.request,
            record_item=self.item,
            requested_by=None,
            model_identifier="district-approved-model-v1",
            input_sha256="d" * 64,
        )
        self.client.force_authenticate(self.reviewer)

        response = self.client.get(
            f"/api/staff/record-items/{self.item.pk}/advisory-responsiveness/"
        )

        latest = response.data["latest"]
        self.assertEqual(latest["id"], str(run.pk))
        self.assertEqual(latest["trigger"], AdvisoryRun.Trigger.AUTOMATIC)
        self.assertIsNone(latest["requested_by_username"])
