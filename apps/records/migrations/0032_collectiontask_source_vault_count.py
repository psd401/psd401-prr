from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("records", "0031_vaultcountrun_search_scope"),
    ]

    operations = [
        migrations.AddField(
            model_name="collectiontask",
            name="source_vault_count",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=models.PROTECT,
                related_name="collection_task",
                to="records.vaultcountrun",
            ),
        ),
        migrations.AddField(
            model_name="recordsystem",
            name="default_assignee",
            field=models.ForeignKey(
                blank=True,
                help_text="Staff owner automatically assigned to new collection tasks.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="default_record_systems",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
