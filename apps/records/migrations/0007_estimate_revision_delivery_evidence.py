import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("correspondence", "0002_correspondence_metadata"),
        ("records", "0006_requester_city_requester_company_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="estimaterevision",
            name="communicated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="estimaterevision",
            name="correspondence",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="communicated_estimate_revision",
                to="correspondence.correspondence",
            ),
        ),
    ]
