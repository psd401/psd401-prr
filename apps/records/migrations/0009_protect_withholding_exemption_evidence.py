import django.db.models.deletion
from django.db import migrations, models


def copy_withholding_bases(apps, schema_editor):
    RecordItem = apps.get_model("records", "RecordItem")
    ProtectedBasis = apps.get_model(
        "records",
        "RecordItemWithholdingExemption",
    )
    legacy_through = RecordItem.withholding_exemptions.through
    ProtectedBasis.objects.bulk_create(
        [
            ProtectedBasis(
                record_item_id=record_item_id,
                exemption_id=exemption_id,
            )
            for record_item_id, exemption_id in legacy_through.objects.values_list(
                "recorditem_id",
                "exemptioncitation_id",
            ).iterator()
        ],
        ignore_conflicts=True,
    )


def restore_legacy_withholding_bases(apps, schema_editor):
    RecordItem = apps.get_model("records", "RecordItem")
    ProtectedBasis = apps.get_model(
        "records",
        "RecordItemWithholdingExemption",
    )
    legacy_through = RecordItem.withholding_exemptions.through
    legacy_through.objects.bulk_create(
        [
            legacy_through(
                recorditem_id=record_item_id,
                exemptioncitation_id=exemption_id,
            )
            for record_item_id, exemption_id in ProtectedBasis.objects.values_list(
                "record_item_id",
                "exemption_id",
            ).iterator()
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("records", "0008_statusevent_correspondence_effect_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecordItemWithholdingExemption",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "exemption",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="withheld_item_bases",
                        to="records.exemptioncitation",
                    ),
                ),
                (
                    "record_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="records.recorditem",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("record_item", "exemption"),
                        name=(
                            "records_unique_record_item_withholding_exemption"
                        ),
                    )
                ],
            },
        ),
        migrations.RunPython(
            copy_withholding_bases,
            reverse_code=restore_legacy_withholding_bases,
        ),
        migrations.RemoveField(
            model_name="recorditem",
            name="withholding_exemptions",
        ),
        migrations.AddField(
            model_name="recorditem",
            name="withholding_exemptions",
            field=models.ManyToManyField(
                blank=True,
                related_name="withheld_items",
                through=(
                    "records.RecordItemWithholdingExemption"
                ),
                to="records.exemptioncitation",
            ),
        ),
    ]
