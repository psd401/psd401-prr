# ABOUTME: Failing regression contracts for defects found by the feature audit.
# ABOUTME: These tests describe the intended behavior before production fixes.
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models, transaction
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from drf_spectacular.drainage import GENERATOR_STATS

from apps.api.identity import StaffRole
from apps.api.models import GoogleGroupRoleMapping
from apps.api.resources.identity_config import GoogleGroupRoleMappingViewSet
from apps.audit.models import AuditLog
from apps.integrations.avigilon import client as alta
from apps.integrations.vault import client as vault
from apps.records import tasks as record_tasks
from apps.records.models import (
    CollectionTask,
    ExemptionCitation,
    PublicRecordsRequest,
    Requester,
)
from integrations.mcp.hosted import (
    DjangoPRRClient,
    StaffAPIError,
    StaffTokenAuthenticationMiddleware,
)


class StaffApiRegressionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "inventory-regression-officer",
            "inventory-regression-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        self.client.login(
            username="inventory-regression-officer",
            password="pw",
        )

    def test_d01_exemption_search_filters_nonmatching_rows(self):
        matching = ExemptionCitation.objects.create(
            citation="34 CFR Part 99",
            short_name="FERPA student records",
        )
        ExemptionCitation.objects.create(
            citation="RCW 42.56.250(3)",
            short_name="Employee personal information",
        )

        response = self.client.get("/api/staff/exemptions/?search=FERPA")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        self.assertEqual([row["id"] for row in rows], [matching.pk])

    def test_d02_reopening_complete_collection_task_clears_completed_at(self):
        request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="reopen@example.org"),
            description="Records collected by a task that must be reopened.",
        )
        task = CollectionTask.objects.create(
            request=request,
            kind=CollectionTask.Kind.MANUAL,
            state=CollectionTask.State.COMPLETE,
            instructions="Recheck the shared drive.",
            completed_at=timezone.now(),
        )

        response = self.client.patch(
            f"/api/staff/collection-tasks/{task.pk}/",
            {"state": CollectionTask.State.RUNNING},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        task.refresh_from_db()
        self.assertEqual(task.state, CollectionTask.State.RUNNING)
        self.assertIsNone(task.completed_at)


class HostedMCPRegressionTests(SimpleTestCase):
    def test_d03_staff_api_503_is_reported_as_service_unavailable(self):
        async def downstream(_scope, _receive, _send):
            raise AssertionError("MCP application ran after failed auth probe")

        fake_client = MagicMock()
        fake_client.list_capabilities.side_effect = StaffAPIError(503)
        application = StaffTokenAuthenticationMiddleware(downstream)

        async def request():
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
            ) as client:
                return await client.post(
                    "/mcp",
                    headers={"Authorization": "Token attributed-staff-token"},
                )

        with patch.object(
            DjangoPRRClient,
            "from_scope",
            return_value=fake_client,
        ):
            response = async_to_sync(request)()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["message"],
            "Authentication service unavailable.",
        )
        fake_client.close.assert_called_once_with()


class VerifyAltaCommandRegressionTests(SimpleTestCase):
    def test_d04_configuration_and_api_failures_exit_nonzero(self):
        failures = (
            alta.AltaNotConfigured("Alta is not configured."),
            alta.AltaError("Alta rejected the API request."),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch(
                "apps.records.management.commands.verify_alta.alta.checks",
                side_effect=failure,
            ), self.assertRaises(CommandError):
                call_command("verify_alta", stdout=StringIO())


class LastAdminMappingDeletionRegressionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "mapping-regression-admin",
            is_staff=True,
        )
        self.target = GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="admin-a@psd401.net",
            active=True,
        )
        self.survivor = GoogleGroupRoleMapping.objects.create(
            role=StaffRole.ADMIN,
            google_group="admin-b@psd401.net",
            active=True,
        )

    def viewset(self):
        view = GoogleGroupRoleMappingViewSet()
        view.request = SimpleNamespace(user=self.user)
        return view

    def test_d05_mapping_delete_executes_inside_an_atomic_transaction(self):
        observed_atomic_state = []
        original_delete = GoogleGroupRoleMapping.delete

        def checked_delete(instance, *args, **kwargs):
            observed_atomic_state.append(
                transaction.get_connection().in_atomic_block
            )
            return original_delete(instance, *args, **kwargs)

        with patch.object(
            GoogleGroupRoleMapping,
            "delete",
            autospec=True,
            side_effect=checked_delete,
        ), patch(
            "apps.api.resources.identity_config._expire_google_authorizations"
        ):
            self.viewset().perform_destroy(self.target)

        self.assertEqual(observed_atomic_state, [True])

    def test_d05_mapping_delete_locks_the_admin_cardinality_set(self):
        locked_row_sets = []
        locked_atomic_state = []
        original_select_for_update = models.QuerySet.select_for_update

        def tracked_select_for_update(queryset, *args, **kwargs):
            if queryset.model is GoogleGroupRoleMapping:
                locked_row_sets.append(
                    set(queryset.values_list("pk", flat=True))
                )
                locked_atomic_state.append(
                    transaction.get_connection().in_atomic_block
                )
            return original_select_for_update(queryset, *args, **kwargs)

        with patch.object(
            models.QuerySet,
            "select_for_update",
            autospec=True,
            side_effect=tracked_select_for_update,
        ), patch(
            "apps.api.resources.identity_config._expire_google_authorizations"
        ):
            self.viewset().perform_destroy(self.target)

        self.assertTrue(locked_row_sets)
        self.assertTrue(all(locked_atomic_state))
        self.assertTrue(
            any(
                {self.target.pk, self.survivor.pk}.issubset(row_set)
                for row_set in locked_row_sets
            )
        )


class VaultClientRegressionTests(TestCase):
    matter_name = "PRR-2026-0999 — Public Records Request"

    def service_with_matter_listing(self, matters):
        service = MagicMock()
        service.matters.return_value.list.return_value.execute.return_value = {
            "matters": matters,
        }
        return service

    def assert_single_vault_audit(self, before_count, external_id):
        self.assertEqual(AuditLog.objects.count(), before_count + 1)
        log = AuditLog.objects.order_by("pk").last()
        self.assertTrue(log.action.startswith("vault."), log.action)
        evidence = f"{log.target} {json.dumps(log.detail, sort_keys=True)}"
        self.assertIn(external_id, evidence)

    def test_d06_ensure_matter_reuses_existing_prr_matter(self):
        service = self.service_with_matter_listing([
            {
                "matterId": "matter-existing",
                "name": self.matter_name,
            }
        ])
        service.matters.return_value.create.return_value.execute.return_value = {
            "matterId": "matter-duplicate",
        }

        with patch.object(vault, "_vault_service", return_value=service):
            matter_id = vault.ensure_matter(
                "PRR-2026-0999",
                "Existing request matter should be reused.",
            )

        self.assertEqual(matter_id, "matter-existing")
        service.matters.return_value.create.assert_not_called()

    def test_d06_matter_creation_is_audited(self):
        service = self.service_with_matter_listing([])
        service.matters.return_value.create.return_value.execute.return_value = {
            "matterId": "matter-new",
        }
        before_count = AuditLog.objects.count()

        with patch.object(vault, "_vault_service", return_value=service):
            vault.ensure_matter("PRR-2026-1000", "Create a new matter.")

        self.assert_single_vault_audit(before_count, "matter-new")

    def test_d06_mail_hold_creation_is_audited(self):
        service = MagicMock()
        service.matters.return_value.holds.return_value.create.return_value.execute.return_value = {
            "holdId": "hold-new",
        }
        before_count = AuditLog.objects.count()

        with patch.object(vault, "_vault_service", return_value=service):
            vault.create_mail_hold(
                "matter-1",
                ["custodian@psd401.net"],
                "PRR mail hold",
            )

        self.assert_single_vault_audit(before_count, "hold-new")

    def test_d06_mail_export_creation_is_audited(self):
        service = MagicMock()
        service.matters.return_value.exports.return_value.create.return_value.execute.return_value = {
            "id": "mail-export-new",
        }
        before_count = AuditLog.objects.count()

        with patch.object(vault, "_vault_service", return_value=service):
            vault.create_mail_export(
                "matter-1",
                ["custodian@psd401.net"],
                "board minutes",
            )

        self.assert_single_vault_audit(before_count, "mail-export-new")

    def test_d06_drive_export_creation_is_audited(self):
        service = MagicMock()
        service.matters.return_value.exports.return_value.create.return_value.execute.return_value = {
            "id": "drive-export-new",
        }
        before_count = AuditLog.objects.count()

        with patch.object(vault, "_vault_service", return_value=service):
            vault.create_source_export(
                "drive",
                "matter-1",
                ["custodian@psd401.net"],
                "board minutes",
                name="PRR drive export",
            )

        service.matters.return_value.exports.return_value.create.assert_called_once_with(
            matterId="matter-1",
            body={
                "name": "PRR drive export",
                "query": {
                    "corpus": "DRIVE",
                    "dataScope": "ALL_DATA",
                    "method": "ACCOUNT",
                    "accountInfo": {"emails": ["custodian@psd401.net"]},
                    "terms": "board minutes",
                    "driveOptions": {"sharedDrivesOption": "INCLUDED"},
                },
                "exportOptions": {"driveOptions": {"includeAccessInfo": False}},
            },
        )
        self.assert_single_vault_audit(before_count, "drive-export-new")


class VaultPollingRegressionTests(TestCase):
    def make_task(self, *, state=CollectionTask.State.RUNNING):
        request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email=f"vault-poll-{PublicRecordsRequest.objects.count()}@example.org"
            ),
            description="Vault export polling regression.",
        )
        return CollectionTask.objects.create(
            request=request,
            kind=CollectionTask.Kind.VAULT_MAIL,
            state=state,
            instructions="Export responsive mail.",
            vault_matter_id="matter-1",
            vault_export_id="export-1",
        )

    def test_d06_completed_export_is_not_reenqueued_on_every_poll(self):
        task = self.make_task()

        with patch(
            "apps.integrations.vault.client.get_export",
            return_value={"status": "COMPLETED"},
        ), patch.object(record_tasks.ingest_vault_export, "delay") as enqueue:
            record_tasks.poll_vault_exports()
            record_tasks.poll_vault_exports()

        enqueue.assert_called_once_with(task.pk)
        task.refresh_from_db()
        self.assertEqual(task.state, CollectionTask.State.INGESTING)
        self.assertIsNone(task.completed_at)
        self.assertTrue(
            AuditLog.objects.filter(
                action="vault.ingest.enqueued",
                target=str(task.pk),
            ).exists()
        )

    @override_settings(PRR_VAULT_INGEST_ENQUEUE_LEASE_SECONDS=300)
    def test_d06_fresh_ingesting_claim_is_not_reenqueued(self):
        task = self.make_task(state=CollectionTask.State.INGESTING)
        fresh_claim = timezone.now() - timezone.timedelta(seconds=299)
        CollectionTask.objects.filter(pk=task.pk).update(
            updated_at=fresh_claim,
        )

        with patch.object(record_tasks.ingest_vault_export, "delay") as enqueue:
            record_tasks.poll_vault_exports()

        enqueue.assert_not_called()

    @override_settings(PRR_VAULT_INGEST_ENQUEUE_LEASE_SECONDS=300)
    def test_d06_stale_unpublished_ingesting_claim_is_reconciled_once(self):
        task = self.make_task(state=CollectionTask.State.INGESTING)
        stale_claim = timezone.now() - timezone.timedelta(seconds=301)
        CollectionTask.objects.filter(pk=task.pk).update(
            updated_at=stale_claim,
        )

        with patch.object(record_tasks.ingest_vault_export, "delay") as enqueue:
            record_tasks.poll_vault_exports()
            task.refresh_from_db()
            self.assertGreater(task.updated_at, stale_claim)
            self.assertTrue(
                AuditLog.objects.filter(
                    action="vault.ingest.enqueued",
                    target=str(task.pk),
                ).exists()
            )

            # A successfully published handoff stays visibly INGESTING; age
            # alone must not create a duplicate enqueue loop.
            CollectionTask.objects.filter(pk=task.pk).update(
                updated_at=stale_claim,
            )
            record_tasks.poll_vault_exports()

        enqueue.assert_called_once_with(task.pk)
        task.refresh_from_db()
        self.assertEqual(task.state, CollectionTask.State.INGESTING)

    @override_settings(PRR_VAULT_INGEST_ENQUEUE_LEASE_SECONDS=300)
    def test_d06_stale_published_claims_do_not_add_one_query_per_task(self):
        tasks = [
            self.make_task(state=CollectionTask.State.INGESTING)
            for _ in range(12)
        ]
        task_ids = [task.pk for task in tasks]
        CollectionTask.objects.filter(pk__in=task_ids).update(
            updated_at=timezone.now() - timezone.timedelta(seconds=301),
        )
        AuditLog.objects.bulk_create(
            [
                AuditLog(
                    action="vault.ingest.enqueued",
                    target=str(task.pk),
                )
                for task in tasks
            ]
        )

        with patch.object(record_tasks.ingest_vault_export, "delay") as enqueue:
            # One query checks RUNNING exports, one reads the enqueue lease
            # from the settings store, and one SQL anti-join selects only
            # stale claims without durable enqueue evidence, and one finds
            # Vault counts waiting on a Drive or Chat count-only export.
            # Historical published claims must not add an AuditLog.exists()
            # per row -- twelve tasks here, and the count must not move with it.
            with self.assertNumQueries(4):
                record_tasks.poll_vault_exports()

        enqueue.assert_not_called()

    @override_settings(PRR_VAULT_INGEST_ENQUEUE_LEASE_SECONDS=300)
    def test_d06_overlapping_reconciler_cannot_enqueue_same_claim_twice(self):
        task = self.make_task(state=CollectionTask.State.INGESTING)
        CollectionTask.objects.filter(pk=task.pk).update(
            updated_at=timezone.now() - timezone.timedelta(seconds=301),
        )

        def overlap(_task_id):
            record_tasks.poll_vault_exports()

        with patch.object(
            record_tasks.ingest_vault_export,
            "delay",
            side_effect=overlap,
        ) as enqueue:
            record_tasks.poll_vault_exports()

        enqueue.assert_called_once_with(task.pk)


class OpenAPISchemaRegressionTests(SimpleTestCase):
    def test_schema_generation_has_no_untyped_views_fields_or_enum_collisions(self):  # no-assert: fail_on_warn=True is the assertion
        GENERATOR_STATS.reset()

        call_command(
            "spectacular",
            validate=True,
            fail_on_warn=True,
            stdout=StringIO(),
        )
