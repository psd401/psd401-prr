# ABOUTME: Email metadata on RecordItem, the per-record custodian set that
# ABOUTME: replaces the comma-joined string, and the counsel-domain list.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def drop_sqlite_exemption_trigger(apps, schema_editor):
    """SQLite remakes a table to add a column, which breaks the trigger.

    The guard trigger references ``records_recorditem``; during a table remake
    that name does not exist. Dropped for the length of this migration and
    recreated verbatim at the end, exactly as migration 0022 does.
    """
    if schema_editor.connection.vendor == "sqlite":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS "
            "records_prevent_used_exemption_citation_rewrite;"
        )


def create_sqlite_exemption_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    schema_editor.execute(
        """
        CREATE TRIGGER records_prevent_used_exemption_citation_rewrite
        BEFORE UPDATE OF citation, short_name, description, requires_second_review
        ON records_exemptioncitation
        FOR EACH ROW
        WHEN (
            OLD.citation IS NOT NEW.citation
            OR OLD.short_name IS NOT NEW.short_name
            OR OLD.description IS NOT NEW.description
            OR OLD.requires_second_review IS NOT NEW.requires_second_review
        ) AND (
            EXISTS (
                SELECT 1 FROM records_recorditemwithholdingexemption
                WHERE exemption_id = OLD.id
            )
            OR EXISTS (
                SELECT 1 FROM records_recorditem
                WHERE withholding_exemption_id = OLD.id
            )
            OR EXISTS (
                SELECT 1 FROM records_redaction
                WHERE exemption_id = OLD.id
            )
            OR EXISTS (
                SELECT 1 FROM correspondence_correspondencedenialcitation
                WHERE exemption_id = OLD.id
            )
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'A citation already used as legal evidence cannot be rewritten.'
            );
        END;
        """
    )


def split_joined_custodians(apps, schema_editor):
    """Turn the old comma-joined custodian string into custodian rows.

    Existing records carry ``"a@x, b@x"`` in one column, which no filter can
    read and no reviewer can group by. Each address becomes a row, and the
    column keeps the first one so a single-custodian display still works. No
    information is discarded: the rows are the same addresses in the same
    order.
    """
    RecordItem = apps.get_model("records", "RecordItem")
    RecordItemCustodian = apps.get_model("records", "RecordItemCustodian")
    rows = []
    updates = []
    for item in RecordItem.objects.exclude(custodian="").only(
        "pk", "custodian", "collection_task",
    ).iterator():
        names = [
            part.strip()[:255]
            for part in dict.fromkeys(item.custodian.split(","))
            if part.strip()
        ]
        if not names:
            continue
        rows.extend(
            RecordItemCustodian(
                record_item_id=item.pk,
                custodian=name,
                collection_task_id=item.collection_task_id,
            )
            for name in names
        )
        if item.custodian != names[0]:
            item.custodian = names[0]
            updates.append(item)
    RecordItemCustodian.objects.bulk_create(rows, ignore_conflicts=True)
    RecordItem.objects.bulk_update(updates, ["custodian"], batch_size=500)


def drop_custodian_rows(apps, schema_editor):
    apps.get_model("records", "RecordItemCustodian").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('records', '0041_installment_access_code_hash_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            drop_sqlite_exemption_trigger,
            reverse_code=create_sqlite_exemption_trigger,
        ),
        migrations.CreateModel(
            name='CounselDomain',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('domain', models.CharField(max_length=253, unique=True)),
                ('note', models.CharField(blank=True, max_length=500)),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['domain'],
            },
        ),
        migrations.CreateModel(
            name='RecordItemCustodian',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('custodian', models.CharField(max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['custodian'],
            },
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_auto_submitted',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_body_sha256',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_cc',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_from',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_has_calendar_part',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_in_reply_to',
            field=models.CharField(blank=True, max_length=998),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_list_id',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_message_id',
            field=models.CharField(blank=True, max_length=998),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_precedence',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_sender_domain',
            field=models.CharField(blank=True, db_index=True, max_length=253),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_sent_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='recorditem',
            name='email_to',
            field=models.TextField(blank=True),
        ),
        migrations.AddConstraint(
            model_name='recorditem',
            constraint=models.UniqueConstraint(condition=models.Q(models.Q(('email_message_id', ''), _negated=True), models.Q(('email_body_sha256', ''), _negated=True)), fields=('request', 'email_message_id', 'email_body_sha256'), name='records_item_unique_message_per_request'),
        ),
        migrations.AddField(
            model_name='recorditemcustodian',
            name='collection_task',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='record_item_custodians', to='records.collectiontask'),
        ),
        migrations.AddField(
            model_name='recorditemcustodian',
            name='record_item',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='custodians', to='records.recorditem'),
        ),
        migrations.AddConstraint(
            model_name='recorditemcustodian',
            constraint=models.UniqueConstraint(fields=('record_item', 'custodian'), name='records_unique_record_item_custodian'),
        ),
        migrations.RunPython(
            split_joined_custodians,
            reverse_code=drop_custodian_rows,
        ),
        migrations.RunPython(
            create_sqlite_exemption_trigger,
            reverse_code=drop_sqlite_exemption_trigger,
        ),
    ]
