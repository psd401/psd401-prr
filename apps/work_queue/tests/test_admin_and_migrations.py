from importlib import import_module
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, TestCase

from apps.records.models import PublicRecordsRequest, Requester
from apps.work_queue.models import WorkItem, WorkItemEvent, WorkItemReceipt
from apps.work_queue.services import materialize_work_item


class WorkQueueAdminAndMigrationTests(TestCase):
    def setUp(self):
        self.request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Work queue migration guard.",
        )
        self.item = materialize_work_item(
            source_type=WorkItem.SourceType.REQUEST,
            source_id=self.request.pk,
            work_kind=WorkItem.WorkKind.REQUEST_ACTION,
            request=self.request,
        ).work_item

    def test_all_work_queue_admin_surfaces_are_read_only(self):
        request = RequestFactory().get("/admin/")
        request.user = self._make_superuser()
        for model in (WorkItem, WorkItemEvent, WorkItemReceipt):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertTrue(model_admin.has_view_permission(request, self.item))
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request, self.item))
                self.assertFalse(model_admin.has_delete_permission(request, self.item))
                self.assertTrue(model_admin.get_readonly_fields(request, self.item))

    @staticmethod
    def _make_superuser():
        return get_user_model().objects.create_superuser(
            "work-queue-admin",
            "work-queue-admin@psd401.net",
            "pw",
        )

    def test_database_trigger_rejects_event_update(self):
        event = self.item.events.get(event_type=WorkItemEvent.EventType.CREATED)

        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE work_queue_workitemevent "
                    "SET resulting_version = resulting_version + 1 WHERE id = %s",
                    [event.pk.hex],
                )

    def test_database_trigger_rejects_event_delete(self):
        event = self.item.events.get(event_type=WorkItemEvent.EventType.CREATED)

        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM work_queue_workitemevent WHERE id = %s",
                    [event.pk.hex],
                )

    def test_reverse_guard_blocks_schema_removal_after_evidence_exists(self):
        migration = import_module(
            "apps.work_queue.migrations.0002_work_item_event_append_only"
        )
        schema_editor = SimpleNamespace(connection=connection)

        with self.assertRaisesRegex(RuntimeError, "forward fix"):
            migration.drop_work_item_event_append_only_trigger(
                None,
                schema_editor,
            )
