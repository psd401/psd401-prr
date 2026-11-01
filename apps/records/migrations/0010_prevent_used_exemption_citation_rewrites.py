from django.db import migrations


def create_immutability_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE FUNCTION records_prevent_used_exemption_citation_rewrite()
            RETURNS trigger AS $$
            BEGIN
                IF (
                    OLD.citation IS DISTINCT FROM NEW.citation
                    OR OLD.short_name IS DISTINCT FROM NEW.short_name
                    OR OLD.description IS DISTINCT FROM NEW.description
                    OR OLD.requires_second_review
                       IS DISTINCT FROM NEW.requires_second_review
                ) AND (
                    EXISTS (
                        SELECT 1
                        FROM records_recorditemwithholdingexemption
                        WHERE exemption_id = OLD.id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM records_redaction
                        WHERE exemption_id = OLD.id
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM correspondence_correspondencedenialcitation
                        WHERE exemption_id = OLD.id
                    )
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'A citation already used as legal evidence cannot be rewritten.';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER records_prevent_used_exemption_citation_rewrite
            BEFORE UPDATE OF
                citation,
                short_name,
                description,
                requires_second_review
            ON records_exemptioncitation
            FOR EACH ROW
            EXECUTE FUNCTION
                records_prevent_used_exemption_citation_rewrite();
            """
        )
        return
    if vendor == "sqlite":
        schema_editor.execute(
            """
            CREATE TRIGGER records_prevent_used_exemption_citation_rewrite
            BEFORE UPDATE OF
                citation,
                short_name,
                description,
                requires_second_review
            ON records_exemptioncitation
            FOR EACH ROW
            WHEN (
                OLD.citation IS NOT NEW.citation
                OR OLD.short_name IS NOT NEW.short_name
                OR OLD.description IS NOT NEW.description
                OR OLD.requires_second_review
                   IS NOT NEW.requires_second_review
            ) AND (
                EXISTS (
                    SELECT 1
                    FROM records_recorditemwithholdingexemption
                    WHERE exemption_id = OLD.id
                )
                OR EXISTS (
                    SELECT 1
                    FROM records_redaction
                    WHERE exemption_id = OLD.id
                )
                OR EXISTS (
                    SELECT 1
                    FROM correspondence_correspondencedenialcitation
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
        return
    raise RuntimeError(
        "Used exemption-citation immutability is supported only on "
        "PostgreSQL and SQLite."
    )


def drop_immutability_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS "
            "records_prevent_used_exemption_citation_rewrite "
            "ON records_exemptioncitation;"
        )
        schema_editor.execute(
            "DROP FUNCTION IF EXISTS "
            "records_prevent_used_exemption_citation_rewrite();"
        )
        return
    if vendor == "sqlite":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS "
            "records_prevent_used_exemption_citation_rewrite;"
        )
        return
    raise RuntimeError(
        "Used exemption-citation immutability is supported only on "
        "PostgreSQL and SQLite."
    )


class Migration(migrations.Migration):
    dependencies = [
        ("records", "0009_protect_withholding_exemption_evidence"),
        ("correspondence", "0012_resulting_status_event_integrity"),
    ]

    operations = [
        migrations.RunPython(
            create_immutability_trigger,
            reverse_code=drop_immutability_trigger,
        ),
    ]
