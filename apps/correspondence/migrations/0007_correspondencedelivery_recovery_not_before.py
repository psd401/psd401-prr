from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("correspondence", "0006_inbound_quarantine_write_reservation"),
    ]

    operations = [
        migrations.AddField(
            model_name="correspondencedelivery",
            name="recovery_not_before",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
