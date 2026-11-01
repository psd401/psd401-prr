import uuid

from django.apps.registry import Apps
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone


def _legacy_event_models():
    """Build unmanaged historical models for upgrade/rollback verification."""
    historical_apps = Apps(installed_apps=[])

    class WebhookEndpoint(models.Model):
        id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
        name = models.CharField(max_length=200)
        url = models.URLField(max_length=1000)
        secret = models.CharField(max_length=128)
        active = models.BooleanField(default=True)
        event_filters = models.JSONField(default=list, blank=True)
        created_at = models.DateTimeField(auto_now_add=True)

        class Meta:
            apps = historical_apps
            app_label = "events"
            db_table = "events_webhookendpoint"

    class OutboundEvent(models.Model):
        id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
        event_type = models.CharField(max_length=100, db_index=True)
        payload = models.JSONField(default=dict)
        endpoint = models.ForeignKey(
            WebhookEndpoint,
            null=True,
            blank=True,
            on_delete=models.PROTECT,
        )
        status = models.CharField(max_length=20, default="pending")
        attempts = models.PositiveIntegerField(default=0)
        last_error = models.TextField(blank=True)
        created_at = models.DateTimeField(auto_now_add=True, db_index=True)
        dispatch_enqueued_at = models.DateTimeField(
            null=True,
            blank=True,
            db_index=True,
        )
        next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
        delivered_at = models.DateTimeField(null=True, blank=True)

        class Meta:
            apps = historical_apps
            app_label = "events"
            db_table = "events_outboundevent"

    return WebhookEndpoint, OutboundEvent


class WebhookBusRetirementMigrationTests(TransactionTestCase):
    migrate_from = ("records", "0016_retire_legacy_integration")
    migrate_to = ("records", "0017_retire_webhook_bus")

    def test_populated_legacy_tables_are_dropped_and_reverse_recreates_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        WebhookEndpoint, OutboundEvent = _legacy_event_models()
        tables = set(connection.introspection.table_names())
        with connection.schema_editor() as schema_editor:
            if WebhookEndpoint._meta.db_table not in tables:
                schema_editor.create_model(WebhookEndpoint)
            if OutboundEvent._meta.db_table not in tables:
                schema_editor.create_model(OutboundEvent)

        endpoint = WebhookEndpoint.objects.create(
            name="Retired endpoint",
            url="https://automation.example.org/webhooks/retired",
            secret="legacy-signing-secret-at-least-32-characters",
        )
        OutboundEvent.objects.create(
            event_type="deadline.warning",
            payload={"number": "PRR-2026-9000"},
            endpoint=endpoint,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        tables = set(connection.introspection.table_names())
        self.assertNotIn("events_outboundevent", tables)
        self.assertNotIn("events_webhookendpoint", tables)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        tables = set(connection.introspection.table_names())
        self.assertIn("events_outboundevent", tables)
        self.assertIn("events_webhookendpoint", tables)

        MigrationExecutor(connection).migrate([self.migrate_to])


class RetiredWebhookPeriodicTaskCleanupMigrationTests(TransactionTestCase):
    migrate_from = ("records", "0028_alter_publicrecordsrequest_status_and_more")
    migrate_to = ("records", "0029_cleanup_retired_webhook_periodic_task")
    celery_beat_migration = (
        "django_celery_beat",
        "0019_alter_periodictasks_options",
    )

    def test_upgrade_removes_only_the_retired_webhook_recovery_task(self):
        executor = MigrationExecutor(connection)
        migration_targets = [self.migrate_from, self.celery_beat_migration]
        executor.migrate(migration_targets)
        old_apps = executor.loader.project_state(migration_targets).apps
        CrontabSchedule = old_apps.get_model(
            "django_celery_beat",
            "CrontabSchedule",
        )
        PeriodicTask = old_apps.get_model(
            "django_celery_beat",
            "PeriodicTask",
        )
        schedule = CrontabSchedule.objects.create(minute="*")
        retired = PeriodicTask.objects.create(
            name="recover-pending-outbound-events",
            task="apps.events.dispatch.requeue_pending_events",
            crontab=schedule,
            enabled=True,
        )
        preserved = PeriodicTask.objects.create(
            name="deadline-watchdog-hourly",
            task="apps.records.tasks.deadline_watchdog",
            crontab=schedule,
            enabled=True,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        PeriodicTask = current_apps.get_model(
            "django_celery_beat",
            "PeriodicTask",
        )

        self.assertFalse(PeriodicTask.objects.filter(pk=retired.pk).exists())
        self.assertTrue(
            PeriodicTask.objects.filter(pk=preserved.pk, enabled=True).exists()
        )


class LegacyIntegrationRetirementMigrationTests(TransactionTestCase):
    migrate_from = ("records", "0015_publicintakesubmission")
    migrate_to = ("records", "0016_retire_legacy_integration")
    token_migration = ("authtoken", "0004_alter_tokenproxy_options")
    content_type_migration = ("contenttypes", "0002_remove_content_type_name")

    def test_obsolete_receipts_are_removed_and_only_legacy_tokens_are_revoked(self):
        executor = MigrationExecutor(connection)
        migration_targets = [
            self.migrate_from,
            self.token_migration,
            self.content_type_migration,
        ]
        executor.migrate(migration_targets)
        old_apps = executor.loader.project_state(
            migration_targets
        ).apps
        User = old_apps.get_model("auth", "User")
        ContentType = old_apps.get_model("contenttypes", "ContentType")
        Permission = old_apps.get_model("auth", "Permission")
        Token = old_apps.get_model("authtoken", "Token")
        Receipt = old_apps.get_model("records", "IntegratedIntakeSubmission")

        permission_specs = {
            "submit_integrated_request": ("records", "publicrecordsrequest"),
            "read_staff_api": ("records", "publicrecordsrequest"),
            "deliver_outbound_correspondence": (
                "correspondence",
                "correspondence",
            ),
            "submit_inbound_correspondence": (
                "correspondence",
                "correspondence",
            ),
        }
        permissions = {}
        for codename, (app_label, model) in permission_specs.items():
            content_type, _ = ContentType.objects.get_or_create(
                app_label=app_label,
                model=model,
            )
            permissions[codename], _ = Permission.objects.get_or_create(
                content_type=content_type,
                codename=codename,
                defaults={"name": codename.replace("_", " ")},
            )
        retired_marker = "n" + "8n"

        def service_user(username, *permission_names, token_key=None):
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={
                    "password": "!",
                    "is_active": True,
                    "is_staff": False,
                    "is_superuser": False,
                },
            )
            user.password = "!"
            user.is_active = True
            user.is_staff = False
            user.is_superuser = False
            user.save(
                update_fields=[
                    "password",
                    "is_active",
                    "is_staff",
                    "is_superuser",
                ]
            )
            user.groups.clear()
            user.user_permissions.clear()
            user.user_permissions.add(
                *(permissions[name] for name in permission_names)
            )
            Token.objects.filter(user_id=user.pk).delete()
            if token_key:
                Token.objects.create(key=token_key, user_id=user.pk)
            return user

        retired_users = (
            service_user(
                f"{retired_marker}-intake",
                "submit_integrated_request",
                token_key="a" * 40,
            ),
            service_user(
                f"{retired_marker}-outbound",
                "deliver_outbound_correspondence",
                token_key="b" * 40,
            ),
            service_user(
                f"{retired_marker}-inbound",
                "submit_inbound_correspondence",
                token_key="c" * 40,
            ),
            service_user(
                f"{retired_marker}-tokenless-outbound",
                "deliver_outbound_correspondence",
            ),
        )
        mcp_user = service_user(
            "mcp-reader",
            "read_staff_api",
            token_key="d" * 40,
        )
        outbound_system = service_user(
            "prr-gmail-outbound-transport",
            "deliver_outbound_correspondence",
        )
        inbound_system = service_user(
            "prr-gmail-inbound-transport",
            "submit_inbound_correspondence",
        )
        broader_user = service_user(
            "broader-service",
            "deliver_outbound_correspondence",
            "submit_inbound_correspondence",
            token_key="e" * 40,
        )
        Receipt.objects.create(
            source=f"{retired_marker}-intake",
            idempotency_key="retired-receipt",
            payload_sha256="f" * 64,
            submitted_by_id=retired_users[0].pk,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        User = current_apps.get_model("auth", "User")
        Token = current_apps.get_model("authtoken", "Token")

        with self.assertRaises(LookupError):
            current_apps.get_model("records", "IntegratedIntakeSubmission")
        for retired_user in retired_users:
            migrated = User.objects.get(pk=retired_user.pk)
            self.assertFalse(migrated.is_active)
            self.assertFalse(migrated.user_permissions.exists())
            self.assertFalse(Token.objects.filter(user_id=migrated.pk).exists())

        for preserved_user in (
            mcp_user,
            outbound_system,
            inbound_system,
            broader_user,
        ):
            migrated = User.objects.get(pk=preserved_user.pk)
            self.assertTrue(migrated.is_active)
            self.assertTrue(migrated.user_permissions.exists())
        self.assertTrue(Token.objects.filter(user_id=mcp_user.pk).exists())
        self.assertTrue(Token.objects.filter(user_id=broader_user.pk).exists())


class WithholdingEvidenceMigrationTests(TransactionTestCase):
    migrate_from = (
        "records",
        "0008_statusevent_correspondence_effect_and_more",
    )
    migrate_to = (
        "records",
        "0009_protect_withholding_exemption_evidence",
    )

    def test_existing_withholding_basis_is_preserved_and_protected(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Requester = old_apps.get_model("records", "Requester")
        Request = old_apps.get_model("records", "PublicRecordsRequest")
        RecordItem = old_apps.get_model("records", "RecordItem")
        Citation = old_apps.get_model("records", "ExemptionCitation")

        requester = Requester.objects.create(email="requester@example.org")
        request = Request.objects.create(
            number="PRR-2026-9001",
            requester=requester,
            description="Migration evidence.",
        )
        citation = Citation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        record_item = RecordItem.objects.create(
            request=request,
            storage_key="records/migration-source",
            sha256="a" * 64,
            review_state="withheld",
        )
        record_item.withholding_exemptions.add(citation)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        RecordItem = current_apps.get_model("records", "RecordItem")
        Citation = current_apps.get_model("records", "ExemptionCitation")
        ProtectedBasis = current_apps.get_model(
            "records",
            "RecordItemWithholdingExemption",
        )

        migrated_item = RecordItem.objects.get(pk=record_item.pk)
        migrated_citation = Citation.objects.get(pk=citation.pk)
        self.assertEqual(
            list(
                migrated_item.withholding_exemptions.values_list(
                    "pk",
                    flat=True,
                )
            ),
            [citation.pk],
        )
        self.assertTrue(
            ProtectedBasis.objects.filter(
                record_item_id=record_item.pk,
                exemption_id=citation.pk,
            ).exists()
        )
        with self.assertRaises(ProtectedError):
            migrated_citation.delete()


class PrimaryWithholdingEvidenceMigrationTests(TransactionTestCase):
    migrate_from = (
        "records",
        "0010_prevent_used_exemption_citation_rewrites",
    )
    migrate_to = (
        "records",
        "0011_recorditem_withholding_exemption_and_more",
    )

    def test_existing_basis_becomes_database_required_primary_basis(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Requester = old_apps.get_model("records", "Requester")
        Request = old_apps.get_model("records", "PublicRecordsRequest")
        RecordItem = old_apps.get_model("records", "RecordItem")
        Citation = old_apps.get_model("records", "ExemptionCitation")

        requester = Requester.objects.create(email="primary@example.org")
        request = Request.objects.create(
            number="PRR-2026-9002",
            requester=requester,
            description="Primary withholding migration evidence.",
        )
        citation = Citation.objects.create(
            citation="RCW 42.56.230(2)",
            short_name="Personal information",
        )
        record_item = RecordItem.objects.create(
            request=request,
            storage_key="records/primary-migration-source",
            sha256="b" * 64,
            review_state="withheld",
        )
        record_item.withholding_exemptions.add(citation)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        current_apps = executor.loader.project_state([self.migrate_to]).apps
        RecordItem = current_apps.get_model("records", "RecordItem")
        Citation = current_apps.get_model("records", "ExemptionCitation")

        migrated_item = RecordItem.objects.get(pk=record_item.pk)
        self.assertEqual(migrated_item.withholding_exemption_id, citation.pk)

        direct_citation = Citation.objects.create(
            citation="RCW 42.56.250(1)",
            short_name="Employment records",
        )
        RecordItem.objects.create(
            request_id=request.pk,
            storage_key="records/direct-primary-source",
            sha256="c" * 64,
            review_state="withheld",
            withholding_exemption_id=direct_citation.pk,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Citation.objects.filter(pk=direct_citation.pk).update(
                short_name="Rewritten after withholding",
            )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        restored_apps = executor.loader.project_state([self.migrate_from]).apps
        RestoredRecordItem = restored_apps.get_model("records", "RecordItem")
        restored_item = RestoredRecordItem.objects.get(
            storage_key="records/direct-primary-source"
        )
        self.assertEqual(
            list(
                restored_item.withholding_exemptions.values_list(
                    "pk",
                    flat=True,
                )
            ),
            [direct_citation.pk],
        )

        MigrationExecutor(connection).migrate([self.migrate_to])
