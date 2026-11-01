# ABOUTME: ADR 0004 matter kinds — litigation/discovery and FERPA parent matters.
# ABOUTME: Additive; every existing row stays public_records.

import django.db.models.deletion
import django.utils.timezone
import uuid

import apps.append_only
from django.conf import settings
from django.db import migrations, models


def _drop(schema_editor, name):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {name} ON records_publicrecordsrequest;"
        )
        schema_editor.execute(f"DROP FUNCTION IF EXISTS {name}();")
        return
    if vendor == "sqlite":
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {name};")
        return
    raise RuntimeError(
        "Request immutability triggers are supported only on PostgreSQL and "
        "SQLite."
    )


def _create(schema_editor, *, name, column, message):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            f"""
            CREATE FUNCTION {name}()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.{column} IS DISTINCT FROM NEW.{column} THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = '{message}';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            f"""
            CREATE TRIGGER {name}
            BEFORE UPDATE OF {column}
            ON records_publicrecordsrequest
            FOR EACH ROW
            EXECUTE FUNCTION {name}();
            """
        )
        return
    if vendor == "sqlite":
        schema_editor.execute(
            f"""
            CREATE TRIGGER {name}
            BEFORE UPDATE OF {column}
            ON records_publicrecordsrequest
            FOR EACH ROW
            WHEN OLD.{column} IS NOT NEW.{column}
            BEGIN
                SELECT RAISE(ABORT, '{message}');
            END;
            """
        )
        return
    raise RuntimeError(
        "Request immutability triggers are supported only on PostgreSQL and "
        "SQLite."
    )


DESCRIPTION_TRIGGER = "records_prevent_request_description_rewrite"
MATTER_KIND_TRIGGER = "records_prevent_request_matter_kind_rewrite"
MATTER_KIND_MESSAGE = "A matter kind is immutable after intake."


def restore_immutability_triggers(apps, schema_editor):
    """Re-arm the description trigger and add the matter-kind one.

    Adding a column on SQLite rebuilds the table, and the rebuild silently
    drops every trigger attached to it — including the one that keeps the
    legal request wording from being rewritten behind the ORM. Dropping first
    is safe on both vendors (IF EXISTS) and is what makes this idempotent on
    PostgreSQL, where nothing was lost.
    """
    _drop(schema_editor, DESCRIPTION_TRIGGER)
    _create(
        schema_editor,
        name=DESCRIPTION_TRIGGER,
        column="description",
        message="Request descriptions are immutable after intake.",
    )
    _drop(schema_editor, MATTER_KIND_TRIGGER)
    _create(
        schema_editor,
        name=MATTER_KIND_TRIGGER,
        column="matter_kind",
        message=MATTER_KIND_MESSAGE,
    )


def drop_matter_kind_trigger(apps, schema_editor):
    _drop(schema_editor, MATTER_KIND_TRIGGER)


FERPA_VERIFICATION_TABLE = "records_ferpaidentityverification"
FERPA_VERIFICATION_FUNCTION = "records_reject_ferpa_verification_mutation"
FERPA_VERIFICATION_MESSAGE = "A FERPA identity verification is append-only."


def create_ferpa_verification_triggers(apps, schema_editor):
    """Nothing below the ORM may rewrite or erase who verified a parent."""
    table = FERPA_VERIFICATION_TABLE
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            f"""
            CREATE FUNCTION {FERPA_VERIFICATION_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = '{FERPA_VERIFICATION_MESSAGE}';
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION {FERPA_VERIFICATION_FUNCTION}();
            """
        )
        return
    if vendor == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            schema_editor.execute(
                f"""
                CREATE TRIGGER {table}_reject_{operation.lower()}
                BEFORE {operation} ON {table}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, '{FERPA_VERIFICATION_MESSAGE}');
                END;
                """
            )
        return
    raise RuntimeError(
        "FERPA verification append-only enforcement is supported only on "
        "PostgreSQL and SQLite."
    )


def drop_ferpa_verification_triggers(apps, schema_editor):
    table = FERPA_VERIFICATION_TABLE
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"
        )
        schema_editor.execute(
            f"DROP FUNCTION IF EXISTS {FERPA_VERIFICATION_FUNCTION}();"
        )
        return
    if vendor == "sqlite":
        for operation in ("update", "delete"):
            schema_editor.execute(
                f"DROP TRIGGER IF EXISTS {table}_reject_{operation};"
            )
        return
    raise RuntimeError(
        "FERPA verification append-only enforcement is supported only on "
        "PostgreSQL and SQLite."
    )


class Migration(migrations.Migration):

    dependencies = [
        ('records', '0045_feedepositgate_feewaiverfloor'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='publicrecordsrequest',
            name='ferpa_access_due',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='publicrecordsrequest',
            name='matter_kind',
            field=models.CharField(choices=[('public_records', 'Public records request (RCW 42.56)'), ('litigation_discovery', 'Litigation / discovery pull'), ('ferpa_parent', 'FERPA parent or eligible student (34 CFR 99.10)')], default='public_records', max_length=30),
        ),
        migrations.AddIndex(
            model_name='publicrecordsrequest',
            index=models.Index(fields=['matter_kind'], name='records_pub_matter__18670c_idx'),
        ),
        migrations.RunPython(
            restore_immutability_triggers,
            reverse_code=drop_matter_kind_trigger,
        ),
        migrations.CreateModel(
            name='FerpaIdentityVerification',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('method', models.CharField(choices=[('in_person_photo_id', 'In-person photo identification'), ('notarized_statement', 'Notarized written statement'), ('district_records_match', 'Match against district student records'), ('court_order', 'Court order or custody decree')], max_length=40)),
                ('note', models.TextField()),
                ('verified_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('request', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='ferpa_identity_verification', to='records.publicrecordsrequest')),
                ('verified_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ferpa_identity_verifications', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['verified_at'],
                'constraints': [models.CheckConstraint(condition=models.Q(('method__in', ['in_person_photo_id', 'notarized_statement', 'district_records_match', 'court_order'])), name='records_ferpa_identity_method_valid'), models.CheckConstraint(condition=models.Q(('note', ''), _negated=True), name='records_ferpa_identity_note_present')],
            },
            bases=(apps.append_only.AppendOnlyModelMixin, models.Model),
        ),
        migrations.RunPython(
            create_ferpa_verification_triggers,
            reverse_code=drop_ferpa_verification_triggers,
        ),
    ]
