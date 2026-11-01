from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("correspondence", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="correspondence",
            name="approved_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="correspondence",
            name="draft_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
