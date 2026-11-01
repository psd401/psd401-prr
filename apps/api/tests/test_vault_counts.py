"""Records officers can request and inspect bounded Vault message counts."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    Requester,
    VaultCountRun,
)


@override_settings(
    PRR_VAULT_CREDENTIALS_FILE="",
    PRR_VAULT_ADMIN_USER="vault-admin@example.org",
    PRR_VAULT_IMPERSONATION_TARGET=(
        "prr-vault@psd401-prr-identity.iam.gserviceaccount.com"
    ),
    PRR_GOOGLE_HOSTED_DOMAIN="psd401.net",
)
class VaultCountApiTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "vault-api-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = User.objects.create_user(
            "vault-api-reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        requester = Requester.objects.create(email="requester@example.org")
        self.request = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Emails about a district construction project.",
        )
        self.url = f"/api/staff/requests/{self.request.number}/vault-counts/"
        self.collection_url = (
            f"/api/staff/requests/{self.request.number}/vault-collection/"
        )

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_officer_can_queue_and_read_latest_count(self, delay):
        self.client.force_authenticate(self.officer)

        empty = self.client.get(self.url)
        self.assertEqual(empty.status_code, 200)
        self.assertTrue(empty.data["enabled"])
        self.assertEqual(empty.data["corpus"], "MAIL")
        self.assertIsNone(empty.data["latest"])

        queued = self.client.post(
            self.url,
            {
                "terms": '"field project" OR contractor',
                "custodian_emails": ["superintendent@psd401.net"],
                "date_range_start": "2026-01-01",
                "date_range_end": "2026-06-30",
            },
            format="json",
        )
        self.assertEqual(queued.status_code, 202, queued.data)
        self.assertEqual(queued.data["state"], "pending")
        self.assertEqual(queued.data["corpus"], "MAIL")
        self.assertEqual(queued.data["search_scope"], "accounts")
        self.assertIsNone(queued.data["total_count"])
        self.assertNotIn("operation_name", queued.data)
        self.assertNotIn("vault_matter_id", queued.data)

        latest = self.client.get(self.url)
        self.assertEqual(latest.data["latest"]["id"], queued.data["id"])
        delay.assert_called_once()

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_officer_can_explicitly_count_all_district_accounts(self, delay):
        self.client.force_authenticate(self.officer)

        queued = self.client.post(
            self.url,
            {
                "terms": '"Jordan Sample"',
                "search_scope": "entire_org",
                "custodian_emails": [],
                "date_range_start": "2026-01-01",
                "date_range_end": "2026-08-01",
            },
            format="json",
        )

        self.assertEqual(queued.status_code, 202, queued.data)
        self.assertEqual(queued.data["search_scope"], "entire_org")
        self.assertEqual(queued.data["custodian_emails"], [])
        delay.assert_called_once()

    def test_reviewer_cannot_read_or_request_counts(self):
        self.client.force_authenticate(self.reviewer)

        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.post(
                self.url,
                {"terms": "project", "custodian_emails": ["a@psd401.net"]},
                format="json",
            ).status_code,
            403,
        )

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_post_rejects_empty_terms_accounts_and_reversed_dates(self, delay):
        self.client.force_authenticate(self.officer)
        invalid_payloads = [
            {"terms": "", "custodian_emails": ["a@psd401.net"]},
            {"terms": "project", "custodian_emails": []},
            {
                "terms": "project",
                "search_scope": "entire_org",
                "custodian_emails": ["a@psd401.net"],
            },
            {
                "terms": "project",
                "custodian_emails": ["a@psd401.net"],
                "date_range_start": "2026-08-01",
                "date_range_end": "2026-01-01",
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.client.post(self.url, payload, format="json").status_code,
                    400,
                )
        delay.assert_not_called()
        self.assertFalse(VaultCountRun.objects.exists())

    def test_an_automatic_count_reads_back_with_no_requester(self):
        """The intake-time count has no officer; the endpoint must say so.

        A dotted serializer source over a null relation raises unless it is
        declared nullable, which would turn the Collection tab into a 500.
        """
        self.client.force_authenticate(self.officer)
        VaultCountRun.objects.create(
            request=self.request,
            requested_by=None,
            trigger=VaultCountRun.Trigger.AUTOMATIC,
            search_scope=VaultCountRun.SearchScope.ENTIRE_ORG,
            state=VaultCountRun.State.PENDING,
            terms='"field project"',
            custodian_emails=[],
            input_sha256="e" * 64,
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["latest"]["trigger"], "automatic")
        self.assertIsNone(response.data["latest"]["requested_by_username"])

    @patch("apps.records.tasks.execute_vault_count.delay")
    def test_active_count_prevents_duplicate_provider_work(self, delay):
        self.client.force_authenticate(self.officer)
        VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            state=VaultCountRun.State.RUNNING,
            terms="first query",
            custodian_emails=["a@psd401.net"],
            input_sha256="d" * 64,
        )

        response = self.client.post(
            self.url,
            {"terms": "second query", "custodian_emails": ["a@psd401.net"]},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        delay.assert_not_called()

    @override_settings(
        PRR_VAULT_CREDENTIALS_FILE="",
        PRR_VAULT_ADMIN_USER="",
        PRR_VAULT_IMPERSONATION_TARGET="",
    )
    def test_unconfigured_vault_is_legible_and_fails_closed(self):
        self.client.force_authenticate(self.officer)

        self.assertFalse(self.client.get(self.url).data["enabled"])
        response = self.client.post(
            self.url,
            {"terms": "project", "custodian_emails": ["a@psd401.net"]},
            format="json",
        )
        self.assertEqual(response.status_code, 503)

    @patch("apps.records.tasks.execute_vault_collection.delay")
    def test_officer_confirms_completed_count_for_collection(self, delay):
        self.client.force_authenticate(self.officer)
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            state=VaultCountRun.State.COMPLETE,
            terms='"field project"',
            custodian_emails=["superintendent@psd401.net"],
            input_sha256="c" * 64,
            vault_matter_id="matter-1",
            total_count=42,
            completed_at="2026-08-11T16:00:00Z",
        )

        response = self.client.post(
            self.collection_url,
            {
                "count_run_id": str(run.pk),
                "expected_input_sha256": run.input_sha256,
                "confirmed": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["kind"], "vault_mail")
        self.assertEqual(response.data["state"], "pending")
        self.assertEqual(response.data["source_vault_count_id"], str(run.pk))
        task = CollectionTask.objects.get(pk=response.data["id"])
        delay.assert_called_once_with(task.pk)

        latest = self.client.get(self.url)
        self.assertEqual(latest.data["latest"]["collection_task_id"], task.pk)
        self.assertEqual(latest.data["latest"]["collection_task_state"], "pending")
        self.assertFalse(latest.data["latest"]["collection_task_retryable"])

        task.state = CollectionTask.State.FAILED
        task.save(update_fields=["state"])
        retryable = self.client.get(self.url)
        self.assertTrue(
            retryable.data["latest"]["collection_task_retryable"]
        )

    def test_collection_requires_confirmation_and_records_officer(self):
        run = VaultCountRun.objects.create(
            request=self.request,
            requested_by=self.officer,
            state=VaultCountRun.State.COMPLETE,
            terms="project",
            custodian_emails=["superintendent@psd401.net"],
            input_sha256="d" * 64,
            vault_matter_id="matter-1",
            total_count=1,
            completed_at="2026-08-11T16:00:00Z",
        )

        self.client.force_authenticate(self.officer)
        unconfirmed = self.client.post(
            self.collection_url,
            {
                "count_run_id": str(run.pk),
                "expected_input_sha256": run.input_sha256,
                "confirmed": False,
            },
            format="json",
        )
        self.assertEqual(unconfirmed.status_code, 400)

        self.client.force_authenticate(self.reviewer)
        forbidden = self.client.post(
            self.collection_url,
            {
                "count_run_id": str(run.pk),
                "expected_input_sha256": run.input_sha256,
                "confirmed": True,
            },
            format="json",
        )
        self.assertEqual(forbidden.status_code, 403)
