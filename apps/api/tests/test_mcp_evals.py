# ABOUTME: Runs integrations/mcp/evals.py cases over a real hosted MCP session.
# ABOUTME: Each case checks keys an agent reads and keys a tool must withhold.
"""The eval-suite runner psd-dev-standards standards/07-mcp.md §6 asks for.

This drives every case in ``integrations.mcp.evals`` through the mounted
streamable-HTTP transport with an attributed personal token -- the same path a
staff member's Claude Code session takes -- rather than calling ``PRRClient``
directly. That is the point: an eval has to fail when the wire contract breaks,
including a renamed tool, a dropped annotation, a changed argument name, or a
serializer that quietly stops returning a field an agent was told to read.

The cases themselves, and the reasoning behind each one, live in
``integrations/mcp/evals.py``.
"""
import hashlib
import json
from datetime import timedelta

import httpx
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from apps.api.models import PersonalAccessToken
from apps.audit.models import audit
from apps.records.models import (
    CollectionTask,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)
from config.asgi import create_application
from integrations.mcp import evals
from integrations.mcp.tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES

_MISSING = object()


def rendered(result):
    """Flatten an MCP tool result to the text an agent would actually read."""
    return json.dumps(
        [item.model_dump(mode="json") for item in result.content],
    )


def parsed(result):
    """Parse the tool result's text blocks into Python data.

    A tool returning a dict comes back as one text block holding JSON. Blocks
    that are not JSON (an error message, for instance) are kept as strings so
    a caller can still assert on them.
    """
    documents = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text is None:
            continue
        try:
            documents.append(json.loads(text))
        except (TypeError, ValueError):
            documents.append(text)
    return documents


def keys_at_any_depth(document):
    """Every JSON object key in a nested structure.

    Recursive on purpose: `forbidden_keys` is the PII assertion, and a
    requester address nested inside a row is still disclosed.
    """
    found = set()
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.update(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def value_at_path(documents, dotted_path):
    """Resolve a dotted path against the first document that has it."""
    for document in documents:
        node = document
        for segment in dotted_path.split("."):
            if not isinstance(node, dict) or segment not in node:
                node = _MISSING
                break
            node = node[segment]
        if node is not _MISSING:
            return node
    return _MISSING


class MCPEvalSuiteTests(TransactionTestCase):
    """Every eval case, against seeded data, over the real transport."""

    def setUp(self):
        call_command("seed_exemptions", verbosity=0)
        # `create`, not `create_user`: this account authenticates only by
        # personal access token, so it needs no usable password, and the
        # manager method is one django-stubs actually knows about.
        self.officer = get_user_model().objects.create(
            username="mcp-eval-officer",
            email="mcp-eval-officer@psd401.net",
            is_staff=True,
        )
        suffix = "mcp-eval-token"
        prefix = hashlib.sha256(suffix.encode()).hexdigest()[:12]
        self.presented_token = f"prr_pat_{prefix}_{suffix}"
        PersonalAccessToken.objects.create(
            user=self.officer,
            name="MCP eval suite",
            prefix=prefix,
            token_hash=hashlib.sha256(
                self.presented_token.encode(),
            ).hexdigest(),
            expires_at=timezone.now() + timedelta(days=30),
        )
        self.requester = Requester.objects.create(
            name="Eval Requester",
            email="mcp-eval-requester@example.org",
        )
        # Overdue on purpose: several cases are about the statutory overdue
        # view, and an empty result set would let a broken projection pass.
        self.prr = PublicRecordsRequest.objects.create(
            requester=self.requester,
            description="Records for the MCP eval suite.",
            status=RequestStatus.ACKNOWLEDGED,
            statutory_response_due=timezone.localdate() - timedelta(days=4),
        )
        self.task = CollectionTask.objects.create(
            request=self.prr,
            kind=CollectionTask.Kind.MANUAL,
            instructions="Search the original date range.",
            assigned_to=self.officer,
        )
        StatusEvent.objects.create(
            request=self.prr,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="Acknowledged for the eval suite.",
            visible_to_requester=True,
        )
        audit(
            "request.status_changed",
            target=self.prr.number,
            actor=self.officer,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
        )
        self.substitutions = {
            evals.REQUEST_NUMBER: self.prr.number,
            evals.TASK_ID: self.task.pk,
            evals.FUTURE_DATE: (
                timezone.localdate() + timedelta(days=14)
            ).isoformat(),
        }

    def resolved_arguments(self, case):
        return {
            key: self.substitutions.get(value, value)
            if isinstance(value, str)
            else value
            for key, value in case.arguments.items()
        }

    async def run_cases(self, cases):
        application = create_application()
        transport = httpx.ASGITransport(app=application)
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://localhost",
                headers={
                    "Authorization": f"Token {self.presented_token}",
                },
            ) as http_client:
                async with streamable_http_client(
                    "https://localhost/mcp",
                    http_client=http_client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                    ) as session:
                        await session.initialize()
                        results = {}
                        for case in cases:
                            results[case.name] = await session.call_tool(
                                case.tool,
                                self.resolved_arguments(case),
                            )
                        return results

    def test_every_eval_case_holds(self):
        self.assertTrue(evals.CASES, "the eval suite may not be empty")
        results = async_to_sync(self.run_cases)(evals.CASES)

        for case in evals.CASES:
            with self.subTest(case=case.name, tool=case.tool):
                result = results[case.name]
                text = rendered(result)
                detail = f"{case.rationale}\nresponse: {text[:800]}"

                if case.expect_error:
                    self.assertTrue(result.isError, detail)
                    for fragment in case.error_contains:
                        self.assertIn(fragment.lower(), text.lower(), detail)
                    continue

                self.assertFalse(result.isError, detail)
                documents = parsed(result)
                present = set()
                for document in documents:
                    present |= keys_at_any_depth(document)
                for key in case.required_keys:
                    self.assertIn(key, present, f"missing {key!r}. {detail}")
                for key in case.forbidden_keys:
                    self.assertNotIn(key, present, f"leaked {key!r}. {detail}")
                for path, expected in case.required_values.items():
                    self.assertEqual(
                        value_at_path(documents, path),
                        expected,
                        f"{path} is not {expected!r}. {detail}",
                    )

    def test_every_eval_case_names_a_registered_tool_and_explains_itself(self):
        # An eval naming a tool that no longer exists would pass as an
        # "expected error" forever, which is the quietest way for a suite
        # like this to stop meaning anything.
        registered = set(READ_TOOL_NAMES).union(WRITE_TOOL_NAMES)
        for case in evals.CASES:
            with self.subTest(case=case.name):
                self.assertIn(case.tool, registered)
                self.assertTrue(case.rationale.strip(), "state why it matters")
                if case.expect_error:
                    self.assertTrue(
                        case.error_contains,
                        "an expected error must assert what it teaches",
                    )
                else:
                    self.assertTrue(
                        case.required_keys
                        or case.forbidden_keys
                        or case.required_values,
                        "a success case must assert something about the body",
                    )

    def test_the_eval_suite_covers_every_read_tool(self):
        covered = {case.tool for case in evals.CASES}
        self.assertSetEqual(
            set(READ_TOOL_NAMES) - covered,
            set(),
            "every read tool needs at least one eval case",
        )
