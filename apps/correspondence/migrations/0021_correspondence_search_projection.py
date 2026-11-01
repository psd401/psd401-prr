from django.db import migrations, transaction


SOURCE_TABLE = "correspondence_correspondence"
CHUNK_TABLE = "correspondence_correspondencesearchchunk"
TRIGGER = "correspondence_search_chunks_trigger"
FUNCTION = "correspondence_search_chunks_refresh"
CHUNK_SIZE = 40_000
CHUNK_STEP = 39_744
BACKFILL_BATCH_SIZE = 500

# Dynamic SQL below interpolates only these migration-owned identifiers after
# Django quotes them; all row IDs and batch sizes remain bound parameters.


def _projection_select(*, row_prefix, id_expression, from_clause, id_filter=""):
    projection_sql = """
        SELECT
            {id_expression},
            content.source,
            ((positions.start_at - 1) / {chunk_step})::integer,
            setweight(
                to_tsvector(
                    'english',
                    substring(
                        content.body
                        FROM positions.start_at
                        FOR {chunk_size}
                    )
                ),
                content.weight::"char"
            )
        FROM {from_clause}
        CROSS JOIN LATERAL (
            VALUES
                ('received_subject', coalesce({row_prefix}.received_subject, ''), 'A'),
                ('approved_subject', coalesce({row_prefix}.approved_subject, ''), 'A'),
                ('received_body', coalesce({row_prefix}.received_body, ''), 'B'),
                ('approved_body', coalesce({row_prefix}.approved_body, ''), 'B')
        ) AS content(source, body, weight)
        CROSS JOIN LATERAL generate_series(
            1,
            length(content.body),
            {chunk_step}
        ) AS positions(start_at)
        WHERE content.body <> ''
        {id_filter}
    """.format(  # nosec B608
        id_expression=id_expression,
        chunk_step=CHUNK_STEP,
        chunk_size=CHUNK_SIZE,
        from_clause=from_clause,
        row_prefix=row_prefix,
        id_filter=id_filter,
    )
    return projection_sql


def install_search_projection(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    quoted_source = schema_editor.quote_name(SOURCE_TABLE)
    quoted_chunks = schema_editor.quote_name(CHUNK_TABLE)
    quoted_trigger = schema_editor.quote_name(TRIGGER)
    quoted_function = schema_editor.quote_name(FUNCTION)
    trigger_projection = _projection_select(
        row_prefix="source_row",
        id_expression="source_row.id",
        from_clause="""
            (SELECT
                NEW.id AS id,
                NEW.received_subject AS received_subject,
                NEW.approved_subject AS approved_subject,
                NEW.received_body AS received_body,
                NEW.approved_body AS approved_body
            ) AS source_row
        """,
    )
    function_sql = """
        CREATE OR REPLACE FUNCTION {quoted_function}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            DELETE FROM {quoted_chunks} WHERE correspondence_id = NEW.id;
            INSERT INTO {quoted_chunks} (
                correspondence_id,
                source,
                chunk_index,
                search_vector
            )
            {trigger_projection};
            RETURN NEW;
        END
        $function$
        """.format(  # nosec B608
        quoted_function=quoted_function,
        quoted_chunks=quoted_chunks,
        trigger_projection=trigger_projection,
    )
    schema_editor.execute(function_sql)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {quoted_trigger} ON {quoted_source}"
    )
    schema_editor.execute(
        f"""
        CREATE TRIGGER {quoted_trigger}
        AFTER INSERT OR UPDATE OF
            received_subject, received_body, approved_subject, approved_body
        ON {quoted_source}
        FOR EACH ROW EXECUTE FUNCTION {quoted_function}()
        """
    )

    select_first_batch_sql = (
        "SELECT id FROM {quoted_source} ORDER BY id LIMIT %s".format(  # nosec B608
            quoted_source=quoted_source,
        )
    )
    select_next_batch_sql = """
        SELECT id FROM {quoted_source}
        WHERE id > %s
        ORDER BY id
        LIMIT %s
        """.format(  # nosec B608
        quoted_source=quoted_source,
    )
    lock_batch_sql = """
        SELECT id FROM {quoted_source}
        WHERE id = ANY(%s)
        ORDER BY id
        FOR UPDATE
        """.format(  # nosec B608
        quoted_source=quoted_source,
    )
    delete_batch_sql = """
        DELETE FROM {quoted_chunks}
        WHERE correspondence_id = ANY(%s)
        """.format(  # nosec B608
        quoted_chunks=quoted_chunks,
    )

    last_id = None
    while True:
        with schema_editor.connection.cursor() as cursor:
            if last_id is None:
                cursor.execute(
                    select_first_batch_sql,
                    [BACKFILL_BATCH_SIZE],
                )
            else:
                cursor.execute(
                    select_next_batch_sql,
                    [last_id, BACKFILL_BATCH_SIZE],
                )
            ids = [row[0] for row in cursor.fetchall()]
        if not ids:
            break
        with transaction.atomic(using=schema_editor.connection.alias):
            with schema_editor.connection.cursor() as cursor:
                cursor.execute(
                    lock_batch_sql,
                    [ids],
                )
                locked_ids = [row[0] for row in cursor.fetchall()]
                cursor.execute(
                    delete_batch_sql,
                    [locked_ids],
                )
                backfill_projection = _projection_select(
                    row_prefix="source_row",
                    id_expression="source_row.id",
                    from_clause=f"{quoted_source} AS source_row",
                    id_filter="AND source_row.id = ANY(%s)",
                )
                cursor.execute(
                    f"""
                    INSERT INTO {quoted_chunks} (
                        correspondence_id,
                        source,
                        chunk_index,
                        search_vector
                    )
                    {backfill_projection}
                    """,
                    [locked_ids],
                )
        last_id = ids[-1]


def uninstall_search_projection(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    quoted_source = schema_editor.quote_name(SOURCE_TABLE)
    quoted_trigger = schema_editor.quote_name(TRIGGER)
    quoted_function = schema_editor.quote_name(FUNCTION)
    schema_editor.execute(
        f"DROP TRIGGER IF EXISTS {quoted_trigger} ON {quoted_source}"
    )
    schema_editor.execute(f"DROP FUNCTION IF EXISTS {quoted_function}()")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("correspondence", "0020_correspondence_search_vector")]

    operations = [
        migrations.RunPython(
            install_search_projection,
            uninstall_search_projection,
        )
    ]
