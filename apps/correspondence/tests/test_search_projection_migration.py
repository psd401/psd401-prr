from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone


class CorrespondenceSearchProjectionMigrationTests(TransactionTestCase):
    migrate_from = ("correspondence", "0019_automaticmessagetemplate")
    migrate_to = ("correspondence", "0022_correspondence_search_index")

    def tearDown(self):
        # Rolling `correspondence` back to 0019 also unapplies every migration
        # in another app that depends on a later one -- `records` 0044 and
        # everything after it. Migrating forward to this app's target alone
        # would leave those unapplied for the rest of the process, so the next
        # TransactionTestCase would run against a records schema missing its
        # newest columns. Restore the full leaf set instead.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    @skipUnlessDBFeature("has_native_uuid_field")
    def test_postgres_backfills_projects_new_rows_and_creates_gin_index(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Correspondence = old_apps.get_model(
            "correspondence",
            "Correspondence",
        )
        oversized_body = "Backfill phrase redwood-orbit " + " ".join(
            f"migrationtoken{index}" for index in range(200_000)
        ) + " terminal-aurora"
        existing = Correspondence.objects.create(
            direction="inbound",
            kind="general",
            status="unmatched",
            received_subject="Historic custodial calendar",
            received_body=oversized_body,
            received_at=timezone.now(),
            provider_message_id="search-backfill",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        apps = executor.loader.project_state([self.migrate_to]).apps
        Correspondence = apps.get_model("correspondence", "Correspondence")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM correspondence_correspondencesearchchunk
                    WHERE correspondence_id = %s
                      AND search_vector
                          @@ websearch_to_tsquery('english', %s)
                )
                """,
                [existing.pk, "redwood orbit"],
            )
            self.assertTrue(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM correspondence_correspondencesearchchunk
                    WHERE correspondence_id = %s
                      AND search_vector
                          @@ websearch_to_tsquery('english', %s)
                )
                """,
                [existing.pk, "terminal aurora"],
            )
            self.assertTrue(cursor.fetchone()[0])

        inserted = Correspondence.objects.create(
            direction="inbound",
            kind="general",
            status="unmatched",
            received_subject="New board packet",
            received_body=oversized_body.replace(
                "Backfill phrase redwood-orbit",
                "Trigger phrase amber-vessel",
                1,
            ),
            received_at=timezone.now(),
            provider_message_id="search-trigger",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM correspondence_correspondencesearchchunk
                    WHERE correspondence_id = %s
                      AND search_vector
                          @@ websearch_to_tsquery('english', %s)
                )
                """,
                [inserted.pk, "amber vessel"],
            )
            self.assertTrue(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'correspondence_correspondencesearchchunk'
                  AND indexname = 'corr_search_chunk_gin'
                """
            )
            self.assertIn("USING gin", cursor.fetchone()[0])
