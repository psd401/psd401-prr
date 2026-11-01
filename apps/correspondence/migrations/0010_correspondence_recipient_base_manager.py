from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("correspondence", "0009_correspondencedenialcitation"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="correspondencerecipient",
            options={
                "base_manager_name": "objects",
                "ordering": ["created_at", "id"],
            },
        ),
    ]
