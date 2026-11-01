from django.db import migrations, models


def populate_collection_task_activity(apps, schema_editor):
    CollectionTask = apps.get_model("records", "CollectionTask")
    tasks = list(CollectionTask.objects.all().iterator(chunk_size=500))
    for task in tasks:
        task.updated_at = task.completed_at or task.created_at
    CollectionTask.objects.bulk_update(
        tasks,
        ["updated_at"],
        batch_size=500,
    )


class Migration(migrations.Migration):
    dependencies = [
        (
            "records",
            "0013_redaction_records_redaction_page_number_positive",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="collectiontask",
            name="updated_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunPython(
            populate_collection_task_activity,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="collectiontask",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
    ]
