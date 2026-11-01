"""Measure bounded message-list and search performance on the test fixture."""

from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.resources.correspondence import (
    MESSAGE_SEARCH_MAX_ATOMS,
    MESSAGE_SEARCH_MAX_GROUPS,
)
from apps.correspondence.management.commands.seed_casework_test_fixture import (
    FIXTURE_MESSAGE_COUNT,
    FIXTURE_REQUEST_NUMBER,
    FIXTURE_SEARCHABLE_MESSAGE_COUNT,
    FIXTURE_USERNAME,
)
from apps.correspondence.models import Correspondence, CorrespondenceSearchChunk

DEFAULT_RUNS = 30
MAX_P95_MS = 500.0
MAX_SUMMARY_PAYLOAD_BYTES = 256 * 1024
MAX_SUMMARY_ROWS = 50
PERFORMANCE_SEARCH_QUERY = " OR ".join(
    "Synthetic Synthetic" for _ in range(MESSAGE_SEARCH_MAX_GROUPS)
)


def _p95(values: list[float]) -> float:
    """Return the nearest-rank 95th percentile used by the release gate."""
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]


class Command(BaseCommand):
    help = (
        "Measure the deterministic 2,000-message summary and full-text search "
        "endpoints on local PostgreSQL."
    )

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
        parser.add_argument("--warmup-runs", type=int, default=5)
        parser.add_argument("--output", default="")
        parser.add_argument("--source-sha", default="")

    def handle(self, *args, **options):
        if not getattr(settings, "PRR_TEST_FIXTURE_SEED_ENABLED", False):
            raise CommandError(
                "test-only casework performance measurement is disabled"
            )
        if connection.vendor != "postgresql":
            raise CommandError("casework performance measurement requires PostgreSQL")

        runs = options["runs"]
        warmup_runs = options["warmup_runs"]
        if runs < DEFAULT_RUNS:
            raise CommandError(f"performance measurement requires at least {DEFAULT_RUNS} runs")
        if warmup_runs < 0:
            raise CommandError("warmup runs cannot be negative")

        request_count = Correspondence.objects.filter(
            request__number=FIXTURE_REQUEST_NUMBER
        ).count()
        if request_count != FIXTURE_MESSAGE_COUNT:
            raise CommandError(
                "seed_casework_test_fixture must provide exactly "
                f"{FIXTURE_MESSAGE_COUNT} messages before measurement"
            )
        indexed_count = (
            CorrespondenceSearchChunk.objects.filter(
                correspondence__request__number=FIXTURE_REQUEST_NUMBER,
            )
            .values("correspondence_id")
            .distinct()
            .count()
        )
        if indexed_count != FIXTURE_SEARCHABLE_MESSAGE_COUNT:
            raise CommandError(
                "search projection must contain exactly "
                f"{FIXTURE_SEARCHABLE_MESSAGE_COUNT} searchable fixture messages"
            )
        try:
            officer = get_user_model().objects.get(username=FIXTURE_USERNAME)
        except get_user_model().DoesNotExist as exc:
            raise CommandError("casework fixture officer is missing") from exc

        client = APIClient()
        client.force_authenticate(officer)
        summary_path = (
            "/api/staff/correspondence/"
            f"?request={FIXTURE_REQUEST_NUMBER}"
        )
        search_path = "/api/staff/correspondence/search/"
        search_body = {
            "request": FIXTURE_REQUEST_NUMBER,
            "query": PERFORMANCE_SEARCH_QUERY,
        }
        pagination = self._traverse_summary(client, summary_path)
        search_pagination = self._traverse_search(
            client,
            search_path,
            search_body,
            expected_count=FIXTURE_SEARCHABLE_MESSAGE_COUNT,
        )

        expected_search_ids = None
        for _ in range(warmup_runs):
            self._summary_request(client, summary_path)
            _, _, _, sample_ids = self._search_request(
                client,
                search_path,
                search_body,
                expected_count=FIXTURE_SEARCHABLE_MESSAGE_COUNT,
                expected_ids=expected_search_ids,
            )
            expected_search_ids = expected_search_ids or sample_ids

        summary_durations = []
        search_durations = []
        summary_payload_sizes = []
        search_payload_sizes = []
        summary_rows = []
        search_rows = []
        for _ in range(runs):
            duration, payload_size, row_count = self._summary_request(
                client,
                summary_path,
            )
            summary_durations.append(duration)
            summary_payload_sizes.append(payload_size)
            summary_rows.append(row_count)

            duration, payload_size, row_count, sample_ids = self._search_request(
                client,
                search_path,
                search_body,
                expected_count=FIXTURE_SEARCHABLE_MESSAGE_COUNT,
                expected_ids=expected_search_ids,
            )
            search_durations.append(duration)
            search_payload_sizes.append(payload_size)
            search_rows.append(row_count)
            expected_search_ids = expected_search_ids or sample_ids

        source_sha = options["source_sha"] or os.environ.get("GITHUB_SHA", "")
        if source_sha and not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise CommandError("source SHA must be a full lowercase 40-character SHA")

        report = {
            "schema_version": 1,
            "source_sha": source_sha or "local-uncommitted",
            "recorded_at": timezone.now().isoformat(),
            "database": {
                "engine": "PostgreSQL",
                "version": self._postgres_version(),
            },
            "fixture": {
                "request_number": FIXTURE_REQUEST_NUMBER,
                "message_count": request_count,
            },
            "runs": runs,
            "warmup_runs": warmup_runs,
            "pagination": pagination,
            "search_pagination": search_pagination,
            "thresholds": {
                "p95_ms": MAX_P95_MS,
                "summary_payload_bytes": MAX_SUMMARY_PAYLOAD_BYTES,
                "summary_rows": MAX_SUMMARY_ROWS,
            },
            "message_summary": self._duration_report(summary_durations),
            "full_text_search": self._duration_report(search_durations),
        }
        report["message_summary"].update(
            {
                "max_payload_bytes": max(summary_payload_sizes),
                "max_rows": max(summary_rows),
                "full_body_fields_present": False,
            }
        )
        report["full_text_search"].update(
            {
                "max_payload_bytes": max(search_payload_sizes),
                "max_rows": max(search_rows),
                "full_body_fields_present": False,
                "expected_matches": FIXTURE_SEARCHABLE_MESSAGE_COUNT,
                "stable_first_page": True,
                "query_shape": {
                    "measured_atoms": MESSAGE_SEARCH_MAX_ATOMS,
                    "measured_or_groups": MESSAGE_SEARCH_MAX_GROUPS,
                    "maximum_atoms": MESSAGE_SEARCH_MAX_ATOMS,
                    "maximum_or_groups": MESSAGE_SEARCH_MAX_GROUPS,
                },
            }
        )
        report["passed"] = all(
            (
                report["message_summary"]["p95_ms"] <= MAX_P95_MS,
                report["full_text_search"]["p95_ms"] <= MAX_P95_MS,
                report["message_summary"]["max_payload_bytes"]
                <= MAX_SUMMARY_PAYLOAD_BYTES,
                report["full_text_search"]["max_payload_bytes"]
                <= MAX_SUMMARY_PAYLOAD_BYTES,
                report["pagination"]["max_payload_bytes"]
                <= MAX_SUMMARY_PAYLOAD_BYTES,
                report["search_pagination"]["max_payload_bytes"]
                <= MAX_SUMMARY_PAYLOAD_BYTES,
                report["message_summary"]["max_rows"] <= MAX_SUMMARY_ROWS,
                report["full_text_search"]["max_rows"] <= MAX_SUMMARY_ROWS,
            )
        )

        encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
        output_path = options["output"]
        if output_path:
            destination = Path(output_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(encoded, encoding="utf-8")
        self.stdout.write(encoded.rstrip())
        if not report["passed"]:
            raise CommandError("casework performance budget exceeded")

    @staticmethod
    def _duration_report(values: list[float]) -> dict[str, float | list[float]]:
        return {
            "samples_ms": [round(value, 3) for value in values],
            "min_ms": round(min(values), 3),
            "median_ms": round(sorted(values)[len(values) // 2], 3),
            "p95_ms": round(_p95(values), 3),
            "max_ms": round(max(values), 3),
        }

    @staticmethod
    def _postgres_version() -> str:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('server_version')")
            return cursor.fetchone()[0]

    @classmethod
    def _traverse_summary(cls, client, path):
        """Follow every opaque cursor and prove the fixture is neither lost nor repeated."""
        current_path = path
        message_ids = []
        pages = 0
        max_rows = 0
        max_payload_bytes = 0
        while current_path:
            response = client.get(current_path)
            if response.status_code != 200:
                raise CommandError(
                    "message summary cursor traversal returned HTTP "
                    f"{response.status_code}"
                )
            payload = response.json()
            rows = payload.get("results", [])
            cls._assert_bounded_summaries(rows)
            message_ids.extend(row["id"] for row in rows)
            pages += 1
            max_rows = max(max_rows, len(rows))
            max_payload_bytes = max(max_payload_bytes, len(response.content))
            if pages > math.ceil(FIXTURE_MESSAGE_COUNT / MAX_SUMMARY_ROWS):
                raise CommandError("message summary cursor traversal did not terminate")
            next_url = payload.get("next")
            if next_url:
                split = urlsplit(next_url)
                current_path = split.path
                if split.query:
                    current_path = f"{current_path}?{split.query}"
            else:
                current_path = ""

        if len(message_ids) != FIXTURE_MESSAGE_COUNT:
            raise CommandError(
                "message summary cursor traversal returned "
                f"{len(message_ids)} of {FIXTURE_MESSAGE_COUNT} fixture IDs"
            )
        unique_ids = len(set(message_ids))
        if unique_ids != FIXTURE_MESSAGE_COUNT:
            raise CommandError(
                "message summary cursor traversal returned duplicate fixture IDs"
            )
        return {
            "pages": pages,
            "traversed_ids": len(message_ids),
            "unique_ids": unique_ids,
            "max_rows": max_rows,
            "max_payload_bytes": max_payload_bytes,
        }

    @staticmethod
    def _assert_bounded_summaries(rows):
        forbidden = {
            "draft_body",
            "approved_body",
            "received_body",
            "attachments",
        }
        if len(rows) > MAX_SUMMARY_ROWS:
            raise CommandError("message summary payload exceeds the 50-row bound")
        if any(forbidden.intersection(row) for row in rows):
            raise CommandError("message summary payload contains full body fields")

    @classmethod
    def _traverse_search(cls, client, path, body, *, expected_count):
        """Follow every body cursor and prove all search matches exactly once."""
        cursor = None
        message_ids = []
        pages = 0
        max_rows = 0
        max_payload_bytes = 0
        max_pages = math.ceil(expected_count / MAX_SUMMARY_ROWS)
        while True:
            request_body = dict(body)
            if cursor is not None:
                request_body["cursor"] = cursor
            response = client.post(path, request_body, format="json")
            if response.status_code != 200:
                raise CommandError(
                    "full-text search cursor traversal returned HTTP "
                    f"{response.status_code}"
                )
            payload = response.json()
            rows = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise CommandError("full-text search cursor traversal was malformed")
            if payload.get("count") != expected_count:
                raise CommandError(
                    f"expected {expected_count} searchable fixture messages"
                )
            cls._assert_bounded_summaries(rows)
            ids = [
                row.get("id")
                for row in rows
                if isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and row["id"]
            ]
            if len(ids) != len(rows):
                raise CommandError(
                    "full-text search cursor traversal returned missing IDs"
                )
            message_ids.extend(ids)
            pages += 1
            max_rows = max(max_rows, len(rows))
            max_payload_bytes = max(max_payload_bytes, len(response.content))
            if max_payload_bytes > MAX_SUMMARY_PAYLOAD_BYTES:
                raise CommandError(
                    "full-text search cursor traversal exceeded the payload bound"
                )
            cursor = payload.get("next")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor:
                raise CommandError(
                    "full-text search cursor traversal returned an invalid cursor"
                )
            if pages >= max_pages:
                raise CommandError(
                    "full-text search cursor traversal did not terminate"
                )

        if len(message_ids) != expected_count:
            raise CommandError(
                "full-text search cursor traversal returned "
                f"{len(message_ids)} of {expected_count} fixture IDs"
            )
        unique_ids = len(set(message_ids))
        if unique_ids != expected_count:
            raise CommandError(
                "full-text search cursor traversal returned duplicate IDs"
            )
        return {
            "pages": pages,
            "traversed_ids": len(message_ids),
            "unique_ids": unique_ids,
            "max_rows": max_rows,
            "max_payload_bytes": max_payload_bytes,
        }

    @staticmethod
    def _summary_request(client, path):
        started = time.perf_counter_ns()
        response = client.get(path)
        payload_bytes = len(response.content)
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        if response.status_code != 200:
            raise CommandError(
                f"message summary endpoint returned HTTP {response.status_code}"
            )
        payload = response.json()
        rows = payload.get("results", [])
        Command._assert_bounded_summaries(rows)
        return duration_ms, payload_bytes, len(rows)

    @staticmethod
    def _search_request(
        client,
        path,
        body,
        *,
        expected_count,
        expected_ids=None,
    ):
        started = time.perf_counter_ns()
        response = client.post(path, body, format="json")
        _ = response.content
        duration_ms = (time.perf_counter_ns() - started) / 1_000_000
        if response.status_code != 200:
            raise CommandError(
                f"full-text search endpoint returned HTTP {response.status_code}"
            )
        payload = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CommandError("full-text search returned malformed results")
        if payload.get("count") != expected_count:
            raise CommandError(
                f"expected {expected_count} searchable fixture messages"
            )
        Command._assert_bounded_summaries(rows)
        ids = [row.get("id") for row in rows if isinstance(row, dict)]
        if len(rows) != MAX_SUMMARY_ROWS or len(ids) != len(rows):
            raise CommandError("full-text search returned an incomplete first page")
        if len(set(ids)) != len(ids):
            raise CommandError("full-text search returned duplicate first-page IDs")
        if expected_ids is not None and ids != expected_ids:
            raise CommandError("full-text search first-page IDs were unstable")
        return duration_ms, len(response.content), len(rows), ids
