"""Remove the database-scheduler entry for the retired webhook event bus."""

from django.db import migrations
from django.utils import timezone


RETIRED_TASK = "apps" + ".events.dispatch.requeue_pending_events"


def remove_retired_webhook_periodic_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTasks = apps.get_model("django_celery_beat", "PeriodicTasks")
    deleted, _ = PeriodicTask.objects.filter(task=RETIRED_TASK).delete()
    if deleted:
        PeriodicTasks.objects.update_or_create(
            ident=1,
            defaults={"last_update": timezone.now()},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("django_celery_beat", "0019_alter_periodictasks_options"),
        ("records", "0028_alter_publicrecordsrequest_status_and_more"),
    ]

    operations = [
        migrations.RunPython(
            remove_retired_webhook_periodic_task,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
