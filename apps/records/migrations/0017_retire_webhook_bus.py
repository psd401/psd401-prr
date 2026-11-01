"""Drop the retired outbound-webhook tables.

Fresh installations never create these tables because the events application
is no longer installed. Existing installations may still have the historical
tables and migration records, so this migration removes them without importing
the deleted application. Reversing recreates the empty legacy schema; webhook
delivery history is intentionally not recoverable after the forward drop.
"""

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.apps.registry import Apps
from django.db import migrations, models


def _legacy_event_models():
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
            on_delete=django.db.models.deletion.PROTECT,
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
        next_attempt_at = models.DateTimeField(
            default=django.utils.timezone.now,
            db_index=True,
        )
        delivered_at = models.DateTimeField(null=True, blank=True)

        class Meta:
            apps = historical_apps
            app_label = "events"
            db_table = "events_outboundevent"

    return WebhookEndpoint, OutboundEvent


def drop_webhook_tables(apps, schema_editor):
    WebhookEndpoint, OutboundEvent = _legacy_event_models()
    tables = set(schema_editor.connection.introspection.table_names())
    if OutboundEvent._meta.db_table in tables:
        schema_editor.delete_model(OutboundEvent)
    if WebhookEndpoint._meta.db_table in tables:
        schema_editor.delete_model(WebhookEndpoint)


def recreate_webhook_tables(apps, schema_editor):
    WebhookEndpoint, OutboundEvent = _legacy_event_models()
    tables = set(schema_editor.connection.introspection.table_names())
    if WebhookEndpoint._meta.db_table not in tables:
        schema_editor.create_model(WebhookEndpoint)
    if OutboundEvent._meta.db_table not in tables:
        schema_editor.create_model(OutboundEvent)


class Migration(migrations.Migration):
    dependencies = [
        ("records", "0016_retire_legacy_integration"),
    ]

    operations = [
        migrations.RunPython(
            drop_webhook_tables,
            reverse_code=recreate_webhook_tables,
        ),
    ]
