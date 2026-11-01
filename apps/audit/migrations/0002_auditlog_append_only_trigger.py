from django.db import migrations


def create_audit_log_append_only_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE FUNCTION audit_reject_audit_log_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'Audit logs are append-only.';
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER audit_reject_audit_log_mutation
            BEFORE UPDATE OR DELETE ON audit_auditlog
            FOR EACH ROW
            EXECUTE FUNCTION audit_reject_audit_log_mutation();
            """
        )
        return
    if vendor == "sqlite":
        # SQLite's test flush uses DELETE rather than TRUNCATE, so its update
        # trigger complements the application-layer delete guard. Production
        # PostgreSQL enforces both update and delete in the database.
        schema_editor.execute(
            """
            CREATE TRIGGER audit_reject_audit_log_update
            BEFORE UPDATE ON audit_auditlog
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Audit logs are append-only.');
            END;
            """
        )
        return
    raise RuntimeError(
        "Audit-log append-only enforcement is supported only on "
        "PostgreSQL and SQLite."
    )


def drop_audit_log_append_only_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS audit_reject_audit_log_mutation "
            "ON audit_auditlog;"
        )
        schema_editor.execute(
            "DROP FUNCTION IF EXISTS audit_reject_audit_log_mutation();"
        )
        return
    if vendor == "sqlite":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS audit_reject_audit_log_update;"
        )
        return
    raise RuntimeError(
        "Audit-log append-only enforcement is supported only on "
        "PostgreSQL and SQLite."
    )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            create_audit_log_append_only_trigger,
            reverse_code=drop_audit_log_append_only_trigger,
        ),
    ]
