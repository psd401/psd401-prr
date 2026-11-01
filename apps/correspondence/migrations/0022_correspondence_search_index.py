from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


TABLE = "correspondence_correspondencesearchchunk"
INDEX = "corr_search_chunk_gin"


def create_search_index(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    quoted_index = schema_editor.quote_name(INDEX)
    quoted_table = schema_editor.quote_name(TABLE)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT index_definition.indisvalid
            FROM pg_class AS index_name
            JOIN pg_index AS index_definition
              ON index_definition.indexrelid = index_name.oid
            WHERE index_name.relname = %s
            """,
            [INDEX],
        )
        existing = cursor.fetchone()
    if existing and existing[0]:
        return
    if existing:
        schema_editor.execute(f"DROP INDEX CONCURRENTLY {quoted_index}")
    schema_editor.execute(
        f"""
        CREATE INDEX CONCURRENTLY {quoted_index}
        ON {quoted_table} USING GIN (search_vector)
        """
    )


def drop_search_index(apps, schema_editor):
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        f"DROP INDEX CONCURRENTLY IF EXISTS {schema_editor.quote_name(INDEX)}"
    )


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("correspondence", "0021_correspondence_search_projection")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(create_search_index, drop_search_index),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="correspondencesearchchunk",
                    index=GinIndex(
                        fields=["search_vector"],
                        name=INDEX,
                    ),
                ),
            ],
        )
    ]
