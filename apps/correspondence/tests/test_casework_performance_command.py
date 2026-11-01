import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from apps.correspondence.management.commands.measure_casework_performance import (
    PERFORMANCE_SEARCH_QUERY,
    Command,
)
from apps.correspondence.management.commands.seed_casework_test_fixture import (
    FIXTURE_SEARCHABLE_MESSAGE_COUNT,
)
from apps.correspondence.performance_safety import validate_disposable_database


class _SearchResponse:
    status_code = 200

    def __init__(self, payload, *, content=None):
        self._payload = payload
        self.content = content or json.dumps(payload).encode()

    def json(self):
        return self._payload


class _SearchClient:
    def __init__(self, payload, *, content=None):
        self.payload = payload
        self.content = content

    def post(self, path, body, format):
        del path, body, format
        return _SearchResponse(self.payload, content=self.content)


class _SearchTraversalClient:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.bodies = []

    def post(self, path, body, format):
        del path, format
        self.bodies.append(dict(body))
        return _SearchResponse(next(self.payloads))


class CaseworkPerformanceCommandTests(SimpleTestCase):
    def test_performance_query_exercises_the_accepted_complexity_boundary(self):
        from apps.api.resources.correspondence import (
            MESSAGE_SEARCH_MAX_ATOMS,
            MESSAGE_SEARCH_MAX_GROUPS,
            _websearch_groups,
        )

        groups = _websearch_groups(PERFORMANCE_SEARCH_QUERY)

        self.assertEqual(len(groups), MESSAGE_SEARCH_MAX_GROUPS)
        self.assertEqual(
            sum(len(group) for group in groups),
            MESSAGE_SEARCH_MAX_ATOMS,
        )

    def test_search_execution_deduplicates_repeated_clauses_and_groups(self):
        from apps.api.resources.correspondence import (
            _deduplicated_websearch_groups,
        )

        self.assertEqual(
            _deduplicated_websearch_groups(PERFORMANCE_SEARCH_QUERY),
            [[(False, "Synthetic")]],
        )
        self.assertEqual(
            _deduplicated_websearch_groups(
                "alpha alpha -missing -missing OR alpha alpha -missing"
            ),
            [[(False, "alpha"), (True, "missing")]],
        )

    @override_settings(PRR_TEST_FIXTURE_SEED_ENABLED=False)
    def test_command_is_restricted_to_the_test_fixture_environment(self):
        with self.assertRaisesMessage(
            CommandError,
            "test-only casework performance measurement is disabled",
        ):
            call_command("measure_casework_performance")

    @override_settings(PRR_TEST_FIXTURE_SEED_ENABLED=True)
    def test_command_requires_postgresql(self):
        with self.assertRaisesMessage(
            CommandError,
            "casework performance measurement requires PostgreSQL",
        ):
            call_command("measure_casework_performance")

    def test_search_measurement_rejects_empty_or_wrong_fixture_results(self):
        client = _SearchClient({"count": 0, "results": []})

        with self.assertRaisesMessage(
            CommandError,
            f"expected {FIXTURE_SEARCHABLE_MESSAGE_COUNT} searchable fixture messages",
        ):
            Command._search_request(
                client,
                "/api/staff/correspondence/search/",
                {"query": '"Synthetic casework message"'},
                expected_count=FIXTURE_SEARCHABLE_MESSAGE_COUNT,
            )

    def test_search_measurement_rejects_malformed_results(self):
        client = _SearchClient({
            "count": FIXTURE_SEARCHABLE_MESSAGE_COUNT,
            "results": "not-a-list",
        })

        with self.assertRaisesMessage(CommandError, "malformed results"):
            Command._search_request(
                client,
                "/api/staff/correspondence/search/",
                {"query": '"Synthetic casework message"'},
                expected_count=FIXTURE_SEARCHABLE_MESSAGE_COUNT,
            )

    def test_search_measurement_rejects_full_bodies_and_reports_payload_size(self):
        rows = [{"id": str(index)} for index in range(50)]
        duration, payload_bytes, row_count, ids = Command._search_request(
            _SearchClient({
                "count": FIXTURE_SEARCHABLE_MESSAGE_COUNT,
                "results": rows,
            }),
            "/api/staff/correspondence/search/",
            {"query": '"Synthetic casework message"'},
            expected_count=FIXTURE_SEARCHABLE_MESSAGE_COUNT,
        )
        self.assertGreaterEqual(duration, 0)
        self.assertGreater(payload_bytes, 0)
        self.assertEqual(row_count, 50)
        self.assertEqual(ids, [str(index) for index in range(50)])

        rows[0]["approved_body"] = "must stay out of summaries"
        with self.assertRaisesMessage(CommandError, "contains full body fields"):
            Command._search_request(
                _SearchClient({
                    "count": FIXTURE_SEARCHABLE_MESSAGE_COUNT,
                    "results": rows,
                }),
                "/api/staff/correspondence/search/",
                {"query": '"Synthetic casework message"'},
                expected_count=FIXTURE_SEARCHABLE_MESSAGE_COUNT,
            )

    def test_search_traversal_resubmits_cursor_and_requires_complete_unique_ids(self):
        client = _SearchTraversalClient([
            {
                "count": 51,
                "next": "opaque-page-2",
                "results": [{"id": f"row-{index}"} for index in range(50)],
            },
            {
                "count": 51,
                "next": None,
                "results": [{"id": "row-50"}],
            },
        ])

        evidence = Command._traverse_search(
            client,
            "/api/staff/correspondence/search/",
            {"request": "PRR-TEST", "query": "bounded query"},
            expected_count=51,
        )

        self.assertEqual(client.bodies, [
            {"request": "PRR-TEST", "query": "bounded query"},
            {
                "request": "PRR-TEST",
                "query": "bounded query",
                "cursor": "opaque-page-2",
            },
        ])
        self.assertEqual(evidence["pages"], 2)
        self.assertEqual(evidence["unique_ids"], 51)

        duplicate_client = _SearchTraversalClient([
            {
                "count": 2,
                "next": None,
                "results": [{"id": "same"}, {"id": "same"}],
            },
        ])
        with self.assertRaisesMessage(CommandError, "duplicate IDs"):
            Command._traverse_search(
                duplicate_client,
                "/api/staff/correspondence/search/",
                {"request": "PRR-TEST", "query": "bounded query"},
                expected_count=2,
            )

    def test_external_contract_database_requires_explicit_disposable_acknowledgment(self):
        script = (
            Path(settings.BASE_DIR) / "scripts" / "run-postgres-contract-tests.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'elif [[ "${PRR_CASEWORK_PERFORMANCE_DISPOSABLE:-}" '
            '!= "casework-performance-disposable" ]]',
            script,
        )
        self.assertIn(
            "A caller-supplied PostgreSQL contract database requires explicit disposable acknowledgment.",
            script,
        )
        self.assertIn('PRR_EXIT_STATUS="$?"', script)
        self.assertIn('exit "$PRR_EXIT_STATUS"', script)
        self.assertNotIn("PRR_PERFORMANCE_OUTPUT_ARGS", script)
        self.assertIn('PRR_PERFORMANCE_SOURCE_SHA="$(git rev-parse HEAD)"', script)
        self.assertIn('--source-sha "$PRR_PERFORMANCE_SOURCE_SHA"', script)

    def test_disposable_database_gate_rejects_production_like_targets(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_disposable_database(
                "postgresql://prr:secret@production.example/prr",
                sentinel="casework-performance-disposable",
            )
        with self.assertRaises(ImproperlyConfigured):
            validate_disposable_database(
                "postgresql://prr_ci:secret@127.0.0.1/prr_ci",
                sentinel="",
            )

    def test_disposable_database_gate_accepts_only_named_loopback_fixture_target(self):
        parsed = validate_disposable_database(
            "postgresql://prr_contract:secret@127.0.0.1/prr_contract",
            sentinel="casework-performance-disposable",
        )

        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertEqual(parsed.path, "/prr_contract")
