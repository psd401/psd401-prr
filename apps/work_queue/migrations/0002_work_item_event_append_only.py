from django.db import migrations


def create_work_item_event_append_only_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE FUNCTION work_queue_reject_event_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'Work-item events are append-only.';
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER work_queue_reject_event_mutation
            BEFORE UPDATE OR DELETE ON work_queue_workitemevent
            FOR EACH ROW
            EXECUTE FUNCTION work_queue_reject_event_mutation();
            """
        )
        return
    if vendor == "sqlite":
        schema_editor.execute(
            """
            CREATE TRIGGER work_queue_reject_event_delete
            BEFORE DELETE ON work_queue_workitemevent
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Work-item events are append-only.');
            END;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER work_queue_reject_event_update
            BEFORE UPDATE ON work_queue_workitemevent
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Work-item events are append-only.');
            END;
            """
        )
        return
    raise RuntimeError(
        "Work-item append-only enforcement supports PostgreSQL and SQLite only."
    )


def drop_work_item_event_append_only_trigger(apps, schema_editor):
    del apps
    with schema_editor.connection.cursor() as cursor:
        for query in (
            "SELECT 1 FROM work_queue_workitemevent LIMIT 1",
            "SELECT 1 FROM work_queue_workitemreceipt LIMIT 1",
            "SELECT 1 FROM work_queue_workitem LIMIT 1",
        ):
            cursor.execute(query)
            if cursor.fetchone() is not None:
                raise RuntimeError(
                    "Work-queue migrations cannot be reversed after durable "
                    "work evidence exists; deploy a forward fix."
                )
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS work_queue_reject_event_mutation "
            "ON work_queue_workitemevent;"
        )
        schema_editor.execute(
            "DROP FUNCTION IF EXISTS work_queue_reject_event_mutation();"
        )
        return
    if vendor == "sqlite":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS work_queue_reject_event_delete;"
        )
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS work_queue_reject_event_update;"
        )
        return
    raise RuntimeError(
        "Work-item append-only enforcement supports PostgreSQL and SQLite only."
    )


class Migration(migrations.Migration):
    dependencies = [("work_queue", "0001_initial")]

    operations = [
        migrations.RunPython(
            create_work_item_event_append_only_trigger,
            reverse_code=drop_work_item_event_append_only_trigger,
        )
    ]
