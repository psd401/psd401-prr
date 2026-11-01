# ABOUTME: End-to-end contract for the application-mounted streamable HTTP MCP
# ABOUTME: transport, including auth pass-through, attribution, and ASGI parity.
import ast
import hashlib
import inspect
import json
import textwrap
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.wsgi import get_wsgi_application
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from apps.api.models import PersonalAccessToken, StaffIdentity
from apps.audit.models import audit
from apps.records.models import (
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)
from config.asgi import create_application
from integrations.mcp.client import PRRClient
from integrations.mcp.hosted import DjangoPRRClient
from integrations.mcp.server import mcp as stdio_mcp
from integrations.mcp.tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES


class MCPDocumentationContractTests(SimpleTestCase):
    def _source_doc(self, relative_path):
        text = (Path(settings.BASE_DIR) / relative_path).read_text()
        return " ".join(text.split())

    def test_write_tool_registry_is_the_draft_proposal_operations(self):
        self.assertSetEqual(
            set(WRITE_TOOL_NAMES),
            {
                "psd_prr_acknowledgment_draft",
                "psd_prr_clarification_letter_draft",
                "psd_prr_collection_task_notes_propose",
                "psd_prr_exemption_citations_suggest",
            },
        )

    def test_security_doc_allows_hosted_reads_for_readonly_service_identity(self):
        security = self._source_doc("docs/security.md")

        self.assertIn(
            "| Hosted attributed MCP at `/mcp` | Personal staff token or dedicated "
            "nonstaff service identity |",
            security,
        )
        self.assertIn(
            "The dedicated nonstaff service identity may use hosted `/mcp` for the "
            "eight read tools",
            security,
        )
        self.assertIn(
            "the four draft/proposal tools require an attributed personal token",
            security,
        )

    def test_source_docs_do_not_describe_the_whole_mcp_surface_as_readonly(self):
        current_claims = {
            "AGENTS.md": (
                "hosted MCP uses attributed personal staff tokens",
                "never that shared identity for its gated tools",
            ),
            "docs/compliance-rules.md": (
                "hosted MCP uses attributed personal staff tokens",
                "never that shared identity for its gated tools",
                (
                    "Hosted `/mcp` exposes exactly four attributed personal-token "
                    "draft/proposal tools"
                ),
            ),
            "README.md": (
                "Hosted `/mcp` adds exactly four personal-token draft/proposal tools",
                "standalone stdio and the dedicated nonstaff MCP identity remain read-only",
            ),
            "docs/security.md": (
                "both identities may use the eight reads",
                "four gated draft/proposal tools require a personal token",
            ),
            "docs/project-status.md": (
                "MCP transports",
                "attributed personal-token draft/proposal tools",
            ),
            "docs/agentic-integration.md": (
                "no MCP transport exposes request intake",
                "shared read tools remain read-only on both MCP transports",
            ),
        }
        stale_claims = {
            "AGENTS.md": (
                "MCP uses its own read-only nonstaff token;",
                "Never reuse a staff token for MCP.",
                "`integrations/mcp/` — read-only MCP facade over the staff REST API",
                "OpenAPI docs, read-only MCP server,",
            ),
            "docs/compliance-rules.md": (
                "MCP uses its own read-only nonstaff token;",
                "Never reuse a staff token for MCP.",
                "`integrations/mcp/` — read-only MCP facade over the staff REST API",
                "OpenAPI docs, read-only MCP server,",
            ),
            "README.md": (
                "read-only MCP surface are implemented",
                "and read-only MCP; generated OpenAPI",
                "and a read-only MCP server over the staff API",
                "read-only MCP server provide additional agent surfaces",
                "| `integrations/mcp/` | Read-only MCP server over the staff REST API |",
                "and a read-only MCP server for agents",
                "MCP server (hosted `/mcp` plus stdio, both read-only)",
            ),
            "docs/security.md": ("| Hosted read-only MCP at `/mcp` |",),
            "docs/project-status.md": ("OpenAPI docs, a read-only MCP server,",),
            "docs/agentic-integration.md": (
                "the MCP surface is intentionally read-only and cannot submit requests",
                "the entire MCP surface in (c) is read-only for exactly this reason",
            ),
        }

        for relative_path, forbidden in stale_claims.items():
            with self.subTest(path=relative_path):
                text = self._source_doc(relative_path)
                for claim in current_claims[relative_path]:
                    self.assertIn(claim, text)
                for claim in forbidden:
                    self.assertNotIn(claim, text)


def issue_personal_token(user, *, suffix, expires_at=None, revoked_at=None):
    prefix = hashlib.sha256(suffix.encode()).hexdigest()[:12]
    presented = f"prr_pat_{prefix}_{suffix}"
    row = PersonalAccessToken.objects.create(
        user=user,
        name=f"MCP {suffix}",
        prefix=prefix,
        token_hash=hashlib.sha256(presented.encode()).hexdigest(),
        expires_at=expires_at or timezone.now() + timedelta(days=30),
        revoked_at=revoked_at,
    )
    return row, presented


async def raw_mcp_initialize(
    application,
    authorization=None,
    *,
    base_url="https://localhost",
    extra_headers=None,
):
    headers = {"Accept": "application/json, text/event-stream"}
    if authorization is not None:
        headers["Authorization"] = authorization
    headers.update(extra_headers or {})
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=base_url,
        headers=headers,
    ) as client:
        return await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "hosted-mcp-test", "version": "1"},
                },
            },
        )


class HostedMCPTransportTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "hosted-mcp-user",
            "hosted-mcp-user@psd401.net",
            is_staff=True,
        )
        self.token_row, self.presented_token = issue_personal_token(
            self.user,
            suffix="valid-hosted-token",
        )
        self.other_user = get_user_model().objects.create_user(
            "other-hosted-mcp-user",
            "other-hosted-mcp-user@psd401.net",
            is_staff=True,
        )
        self.other_token_row, _ = issue_personal_token(
            self.other_user,
            suffix="other-hosted-token",
        )
        requester = Requester.objects.create(
            email="hosted-requester@example.org",
            name="Hosted Requester",
        )
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Hosted MCP acceptance request",
            status=RequestStatus.ACKNOWLEDGED,
        )
        occurred_at = timezone.now()
        StatusEvent.objects.create(
            request=self.prr,
            actor=self.user,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            note="Acknowledged for hosted MCP.",
            visible_to_requester=True,
            occurred_at=occurred_at,
        )
        audit(
            "request.status_changed",
            target=self.prr.number,
            actor=self.user,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.ACKNOWLEDGED,
            occurred_at=occurred_at.isoformat(),
        )

    async def exercise_all_tools(self):
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
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.assertSetEqual(
                            {tool.name for tool in listed.tools},
                            set(READ_TOOL_NAMES).union(WRITE_TOOL_NAMES),
                        )
                        calls = {
                            "psd_prr_requests_list": {},
                            "psd_prr_request_get": {
                                "number": self.prr.number,
                            },
                            "psd_prr_overdue_requests_list": {},
                            "psd_prr_collection_tasks_list": {
                                "request_number": self.prr.number,
                            },
                            "psd_prr_record_systems_list": {},
                            "psd_prr_exemptions_list": {},
                            "psd_prr_audit_log_search": {
                                "target": self.prr.number,
                            },
                            "psd_prr_capabilities_list": {},
                        }
                        results = {}
                        for name, arguments in calls.items():
                            result = await session.call_tool(name, arguments)
                            self.assertFalse(result.isError, name)
                            results[name] = result
                        return results

    def test_valid_personal_token_calls_every_shared_read_tool_and_is_attributed(self):
        results = async_to_sync(self.exercise_all_tools)()

        request_result = results["psd_prr_request_get"]
        self.assertIn(
            self.prr.number,
            json.dumps(
                [item.model_dump(mode="json") for item in request_result.content],
            ),
        )
        self.token_row.refresh_from_db()
        self.other_token_row.refresh_from_db()
        self.assertIsNotNone(self.token_row.last_used_at)
        self.assertIsNone(self.other_token_row.last_used_at)

    def test_stdio_stays_readonly_while_hosted_adds_gated_write_tools(self):
        application = create_application()
        stdio_names = {
            tool.name for tool in async_to_sync(stdio_mcp.list_tools)()
        }
        hosted_names = {
            tool.name
            for tool in async_to_sync(application.state.mcp.list_tools)()
        }

        self.assertSetEqual(stdio_names, set(READ_TOOL_NAMES))
        self.assertSetEqual(
            hosted_names,
            set(READ_TOOL_NAMES).union(WRITE_TOOL_NAMES),
        )
        # The registry's shape -- namespacing, and which REST method each
        # tool reaches -- is asserted in test_mcp_tool_contract.py, which
        # owns that concern. What only this test can show is the live split:
        # the same registry module serves two transports, and the stdio one
        # has to stay write-free no matter how it is credentialed.
        for client_class in (PRRClient, DjangoPRRClient):
            tree = ast.parse(textwrap.dedent(inspect.getsource(client_class)))
            invoked_writes = {
                node.func.attr
                for node in ast.walk(tree)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"post", "put", "patch", "delete"}
                )
            }
            self.assertSetEqual(
                invoked_writes,
                {"post"},
                client_class.__name__,
            )

    def test_missing_invalid_expired_and_revoked_tokens_fail_before_a_tool_runs(self):
        expired_row, expired = issue_personal_token(
            self.user,
            suffix="expired-hosted-token",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        revoked_row, revoked = issue_personal_token(
            self.user,
            suffix="revoked-hosted-token",
            revoked_at=timezone.now(),
        )
        self.assertIsNotNone(expired_row.pk)
        self.assertIsNotNone(revoked_row.pk)
        cases = (
            ("missing", None),
            ("invalid", "Token prr_pat_000000000000_invalid"),
            ("expired", f"Token {expired}"),
            ("revoked", f"Token {revoked}"),
        )

        with patch.object(
            PRRClient,
            "list_requests",
            side_effect=AssertionError("a tool executed before authentication"),
        ) as list_requests:
            for label, authorization in cases:
                with self.subTest(label=label):
                    response = async_to_sync(raw_mcp_initialize)(
                        create_application(),
                        authorization,
                    )
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(
                        response.json()["error"]["message"],
                        "Authentication required.",
                    )
            list_requests.assert_not_called()

    def test_rejected_token_is_never_written_to_mcp_or_application_logs(self):
        secret = "prr_pat_000000000000_DO-NOT-LOG-THIS-TOKEN"
        with self.assertLogs(level="WARNING") as captured:
            response = async_to_sync(raw_mcp_initialize)(
                create_application(),
                f"Token {secret}",
            )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(secret, "\n".join(captured.output))

    async def authenticated_initialize(self):
        application = create_application()
        async with application.router.lifespan_context(application):
            return await raw_mcp_initialize(
                application,
                f"Token {self.presented_token}",
            )

    def test_successful_mcp_responses_are_sensitive_and_noncacheable(self):
        response = async_to_sync(self.authenticated_initialize)()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")

    @override_settings(
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SECURE_SSL_REDIRECT=True,
    )
    def test_trusted_proxy_https_header_keeps_internal_auth_probe_secure(self):
        async def initialize_behind_proxy():
            application = create_application()
            async with application.router.lifespan_context(application):
                return await raw_mcp_initialize(
                    application,
                    f"Token {self.presented_token}",
                    base_url="http://localhost",
                    extra_headers={"X-Forwarded-Proto": "https"},
                )

        with patch(
            "integrations.mcp.hosted._django_wsgi_application",
            get_wsgi_application(),
        ):
            response = async_to_sync(initialize_behind_proxy)()

        self.assertEqual(response.status_code, 200, response.text)

    @override_settings(
        SECURE_PROXY_SSL_HEADER=None,
        SECURE_SSL_REDIRECT=True,
    )
    def test_unconfigured_forwarded_proto_header_is_not_trusted(self):
        async def initialize_with_spoofed_header():
            application = create_application()
            async with application.router.lifespan_context(application):
                return await raw_mcp_initialize(
                    application,
                    f"Token {self.presented_token}",
                    base_url="http://localhost",
                    extra_headers={"X-Forwarded-Proto": "https"},
                )

        with patch(
            "integrations.mcp.hosted._django_wsgi_application",
            get_wsgi_application(),
        ):
            response = async_to_sync(initialize_with_spoofed_header)()

        self.assertEqual(response.status_code, 401, response.text)


class ASGIApplicationParityTests(TransactionTestCase):
    reset_sequences = True

    async def request(self, method, path, **kwargs):
        application = create_application()
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://localhost",
        ) as client:
            return await client.request(method, path, **kwargs)

    def test_existing_api_spa_security_headers_and_mcp_route_share_the_asgi_app(self):
        user = get_user_model().objects.create_user(
            "asgi-officer",
            "asgi-officer@psd401.net",
            is_staff=True,
        )
        _row, presented = issue_personal_token(
            user,
            suffix="asgi-api-token",
        )

        api_response = async_to_sync(self.request)(
            "GET",
            "/api/staff/capabilities/",
            headers={"Authorization": f"Token {presented}"},
        )
        spa_response = async_to_sync(self.request)("GET", "/agent-access")
        mcp_response = async_to_sync(self.request)("POST", "/mcp")
        static_response = async_to_sync(self.request)(
            "GET",
            "/static/definitely-missing.css",
        )

        self.assertEqual(api_response.status_code, 200)
        self.assertEqual(api_response.headers["Cache-Control"], "no-store")
        self.assertEqual(api_response.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(spa_response.status_code, 200)
        self.assertIn('data-auth-mode="local"', spa_response.text)
        self.assertIn("Content-Security-Policy", spa_response.headers)
        self.assertEqual(mcp_response.status_code, 401)
        self.assertEqual(static_response.status_code, 404)
        self.assertNotIn("Frontend build not found", static_response.text)

    def test_google_session_revalidation_middleware_still_fails_closed_under_asgi(self):
        user = get_user_model().objects.create_user(
            "stale-google-asgi-user",
            "stale-google-asgi-user@psd401.net",
            is_staff=True,
        )
        StaffIdentity.objects.create(
            user=user,
            source=StaffIdentity.Source.GOOGLE,
            google_subject="stale-google-asgi-subject",
            last_group_check_at=timezone.now() - timedelta(hours=1),
        )
        session_client = Client()
        session_client.force_login(user)
        session_cookie = session_client.cookies[
            settings.SESSION_COOKIE_NAME
        ].value

        with patch(
            "apps.api.middleware.refresh_google_roles",
            return_value=set(),
        ):
            response = async_to_sync(self.request)(
                "GET",
                "/agent-access",
                cookies={settings.SESSION_COOKIE_NAME: session_cookie},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "Your PRR staff access has been removed.",
        )

    def test_deployment_uses_asgi_worker_and_declares_hosted_dependencies(self):
        dockerfile = (Path(settings.BASE_DIR) / "Dockerfile").read_text()
        pyproject = (
            Path(settings.BASE_DIR) / "pyproject.toml"
        ).read_text()
        stdio_entrypoint = (
            Path(settings.BASE_DIR) / "integrations" / "mcp" / "server.py"
        ).read_text()

        self.assertIn("uvicorn.workers.UvicornWorker", dockerfile)
        self.assertIn("config.asgi:application", dockerfile)
        self.assertRegex(pyproject, r'(?m)^\s*"mcp>=1\.29\.1,<2",$')
        self.assertIn('"mcp>=1.29.1,<2"', stdio_entrypoint)
        self.assertRegex(pyproject, r'(?m)^\s*"uvicorn\[standard\]>=')
