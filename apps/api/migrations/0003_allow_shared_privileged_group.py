from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0002_separate_privileged_google_groups"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="googlegrouprolemapping",
            name="api_unique_active_privileged_google_group",
        ),
    ]
