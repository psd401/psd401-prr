# ABOUTME: Live-server coverage for the token-authenticated PRR command-line client.
# ABOUTME: Tests exercise JSON reads, attributed writes, handoffs, and safe config.
import contextlib
import datetime as dt
import hashlib
import io
import json
from unittest import mock
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase, SimpleTestCase
from django.utils import timezone

from apps.api.capabilities import CAPABILITIES
from apps.api.models import PersonalAccessToken
from apps.audit.models import AuditLog
from apps.correspondence.models import Correspondence
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    Requester,
    TimeEntry,
)
from integrations.cli import prr


class CLIConfigurationTests(SimpleTestCase):
    def run_cli(self, *arguments, environ=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = prr.main(list(arguments), environ=environ or {})
        return status, stdout.getvalue(), stderr.getvalue()

    def test_help_lists_the_supported_command_groups(self):
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            prr.build_parser().parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        for command in (
            "requests list|get|transition",
            "tasks list|update",
            "systems list",
            "exemptions list",
            "audit search",
            "time log",
            "correspondence draft",
        ):
            with self.subTest(command=command):
                self.assertIn(command, help_text)

    def test_each_command_maps_to_a_non_human_only_capability(self):
        capability_ids = {
            capability["id"]: capability["execution"]
            for capability in CAPABILITIES
        }

        self.assertTrue(prr.COMMAND_CAPABILITIES)
        for command, capability_id in prr.COMMAND_CAPABILITIES.items():
            with self.subTest(command=command):
                self.assertIn(capability_id, capability_ids)
                self.assertNotEqual(
                    capability_ids[capability_id],
                    "human_only",
                )

    def test_audit_search_rejects_obsolete_page_option(self):
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            prr.build_parser().parse_args(
                ["audit", "search", "--page", "2"],
            )

        self.assertEqual(raised.exception.code, 2)

    def test_missing_configuration_exits_one_without_network_call(self):
        with mock.patch.object(
            prr.requests.Session,
            "request",
        ) as request:
            status, _stdout, stderr = self.run_cli(
                "requests",
                "list",
                environ={},
            )

        self.assertEqual(status, 1)
        self.assertIn("PRR_BASE_URL", stderr)
        self.assertIn("PRR_API_TOKEN", stderr)
        request.assert_not_called()

    def test_non_loopback_plain_http_is_refused_without_network_call(self):
        with mock.patch.object(
            prr.requests.Session,
            "request",
        ) as request:
            status, _stdout, stderr = self.run_cli(
                "requests",
                "list",
                environ={
                    "PRR_BASE_URL": "http://prr.example.org",
                    "PRR_API_TOKEN": "not-a-real-token",
                },
            )

        self.assertEqual(status, 1)
        self.assertIn("HTTP is allowed only for localhost", stderr)
        request.assert_not_called()


class PRRCLILiveServerTests(LiveServerTestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_superuser(
            "cli-officer",
            "cli-officer@psd401.net",
            "pw",
        )
        self.presented_token = (
            "prr_pat_abc123abc123_"
            "cli-test-secret-with-more-than-enough-entropy"
        )
        self.personal_token = PersonalAccessToken.objects.create(
            user=self.officer,
            name="CLI live-server test",
            prefix="abc123abc123",
            token_hash=hashlib.sha256(
                self.presented_token.encode(),
            ).hexdigest(),
            expires_at=timezone.now() + dt.timedelta(days=1),
        )
        self.requester = Requester.objects.create(
            email="cli-requester@example.org",
            name="CLI Requester",
        )
        self.public_request = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Records used by the CLI live-server acceptance flow.",
        )

    def run_cli(self, *arguments, token=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        environ = {
            "PRR_BASE_URL": self.live_server_url,
            "PRR_API_TOKEN": token or self.presented_token,
        }
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = prr.main(list(arguments), environ=environ)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_requests_list_and_get_emit_parseable_json(self):
        status, stdout, stderr = self.run_cli(
            "requests",
            "list",
            "--json",
        )

        self.assertEqual(status, 0, stderr)
        listed = json.loads(stdout)
        self.assertIn(
            self.public_request.number,
            {row["number"] for row in listed["results"]},
        )

        status, stdout, stderr = self.run_cli(
            "requests",
            "get",
            self.public_request.number,
            "--json",
        )

        self.assertEqual(status, 0, stderr)
        detail = json.loads(stdout)
        self.assertEqual(detail["number"], self.public_request.number)
        self.assertEqual(detail["description"], self.public_request.description)

    def test_audit_search_follows_opaque_cursor_without_repeating_page_one(self):
        target = "CLI-PAGINATION"
        originals = [
            AuditLog.objects.create(
                action="cli.pagination.test",
                target=target,
                detail={"sequence": sequence},
            )
            for sequence in range(51)
        ]

        status, stdout, stderr = self.run_cli(
            "audit",
            "search",
            "--target",
            target,
            "--json",
        )
        self.assertEqual(status, 0, stderr)
        first = json.loads(stdout)
        first_ids = [row["id"] for row in first["results"]]
        cursor = next(
            part.removeprefix("cursor=")
            for part in urlsplit(first["next"]).query.split("&")
            if part.startswith("cursor=")
        )
        self.assertIn("%", cursor)

        status, stdout, stderr = self.run_cli(
            "audit",
            "search",
            "--target",
            target,
            "--cursor",
            cursor,
            "--json",
        )
        self.assertEqual(status, 0, stderr)
        second = json.loads(stdout)
        second_ids = [row["id"] for row in second["results"]]

        combined = first_ids + second_ids
        self.assertEqual(set(combined), {entry.id for entry in originals})
        self.assertEqual(len(combined), len(originals))
        self.assertIsNone(second["next"])

    def test_attributed_write_commands_update_task_log_time_and_draft(self):
        task = CollectionTask.objects.create(
            request=self.public_request,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Search the shared drive.",
        )

        status, stdout, stderr = self.run_cli(
            "tasks",
            "update",
            str(task.pk),
            "--state",
            CollectionTask.State.RUNNING,
            "--json",
        )
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["state"], CollectionTask.State.RUNNING)
        task.refresh_from_db()
        self.assertEqual(task.state, CollectionTask.State.RUNNING)

        status, stdout, stderr = self.run_cli(
            "time",
            "log",
            "--request-id",
            str(self.public_request.pk),
            "--date",
            "2026-07-30",
            "--minutes",
            "25",
            "--activity",
            "Reviewed CLI acceptance records.",
            "--json",
        )
        self.assertEqual(status, 0, stderr)
        entry = TimeEntry.objects.get(pk=json.loads(stdout)["id"])
        self.assertEqual(entry.staff, self.officer)
        self.assertEqual(entry.minutes, 25)

        status, stdout, stderr = self.run_cli(
            "correspondence",
            "draft",
            "general",
            self.public_request.number,
            "--subject",
            "CLI draft",
            "--body",
            "This remains an editable draft for human review.",
            "--json",
        )
        self.assertEqual(status, 0, stderr)
        draft = Correspondence.objects.get(pk=json.loads(stdout)["id"])
        self.assertEqual(draft.created_by, self.officer)
        self.assertEqual(draft.status, Correspondence.Status.DRAFT)

    def test_human_only_response_prints_complete_handoff_and_exits_nonzero(self):
        status, _stdout, stderr = self.run_cli(
            "requests",
            "transition",
            self.public_request.number,
            "denied",
            "--note",
            "This action must remain with an accountable human.",
        )

        self.assertEqual(status, 2)
        self.assertIn("Human approval required", stderr)
        self.assertIn("requests.transition", stderr)
        self.assertIn("Deny a public records request", stderr)
        self.assertIn("records_officer", stderr)
        self.assertIn(
            f"/requests/{self.public_request.number}",
            stderr,
        )

    def test_expired_and_revoked_tokens_name_the_agent_access_fix(self):
        self.personal_token.expires_at = timezone.now() - dt.timedelta(
            seconds=1,
        )
        self.personal_token.save(update_fields=["expires_at"])

        status, _stdout, stderr = self.run_cli("requests", "list")

        self.assertEqual(status, 1)
        self.assertIn("expired", stderr.lower())
        self.assertIn("mint", stderr.lower())
        self.assertIn("Agent Access", stderr)

        self.personal_token.expires_at = timezone.now() + dt.timedelta(
            days=1,
        )
        self.personal_token.revoked_at = timezone.now()
        self.personal_token.save(
            update_fields=["expires_at", "revoked_at"],
        )

        status, _stdout, stderr = self.run_cli("requests", "list")

        self.assertEqual(status, 1)
        self.assertIn("revoked", stderr.lower())
        self.assertIn("rotate", stderr.lower())
        self.assertIn("Agent Access", stderr)
