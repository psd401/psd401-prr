# ABOUTME: Contract tests for GUI/agent capability parity and the discoverable
# ABOUTME: agent-native capability catalog exposed by the staff API and MCP.
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.api.capabilities import (
    AGENT_NATIVE_GUIDE_URL,
    CAPABILITIES,
    capability_catalog,
)

_AUTH_PATHS_WITH_AGENT_ALTERNATIVES = {
    ("GET", "/api/staff/auth/me"),
    ("POST", "/api/staff/auth/login"),
    ("POST", "/api/staff/auth/logout"),
    ("GET", "/api/staff/auth/oidc/authenticate/"),
}

# Capabilities that intentionally outlive their staff UI. Rule 10 requires every
# GUI outcome to have an agent primitive; it does not require the converse, so a
# retired screen may leave its REST primitive in place. Keep this list short and
# justified -- it is the only escape from the parity guard, and every entry must
# name a screen that was removed, not one that was never built.
#
# requesters.*: the /requesters directory had no navigation entry and no link
# anywhere in the shell, so no staff member could reach it. The screen was
# removed; the read/update primitives remain available to agents.
#
# correspondence.approve: approving and releasing were two buttons requiring the
# identical Records Officer role -- the same person clicking twice, with no
# workflow in which a different person did each half. The staff workspace now
# does both in one action (correspondence.approve_and_send). Approve-without-
# release keeps its REST primitive for agents and for recovery from a partially
# advanced message, but it is no longer a screen.
#
# installments.release: the same shape one layer down. Releasing a package and
# telling the requester it exists were two Records Officer clicks that no
# workflow ever separated, and a release nobody was told about is a release
# that did not happen. The Releases tab now does both in one action
# (installments.release_and_send); release-without-notice remains addressable
# for agents and for recovering a partially advanced release.
AGENT_ONLY_CAPABILITIES = frozenset({
    "requesters.list",
    "requesters.update",
    "correspondence.approve",
    "installments.release",
    # Agents propose decision batches and read one by id (#43); people
    # decide them from the /decisions list, which already carries the batch.
    "decision_batches.propose",
    "decision_batches.retrieve",
})

def _normalize_frontend_path(raw_path: str) -> str:
    """Normalize a literal/template staff API path to the registry format."""
    path = re.split(r"\$\{[^}]*\?", raw_path, maxsplit=1)[0]
    path = path.split("?", 1)[0]

    def replace_interpolation(match):
        expression = match.group(1)
        if "number" in expression.lower():
            return "{number}"
        return "{id}"

    return re.sub(r"\$\{([^}]+)\}", replace_interpolation, path)


def _frontend_router_routes() -> set[tuple[str, ...]]:
    """Every route pattern the staff router can actually match.

    Returned as segment tuples so a ``:param`` segment in the router compares
    equal to any concrete segment a capability advertises. Path literals are
    read flat: every staff route sits directly under one pathless layout
    route, so no parent path is composed with a child. Nesting routes under a
    parent that has its own ``path`` would need this to compose segments.
    """
    source = (Path(settings.BASE_DIR) / "frontend" / "src" / "App.tsx").read_text(
        encoding="utf-8"
    )
    routes = set()
    for raw in re.findall(r'path="([^"]+)"', source):
        if raw == "*" or raw.endswith("/*"):
            continue
        routes.add(tuple(raw.strip("/").split("/")))
    return routes


def _route_is_served(ui_route: str, router_routes: set[tuple[str, ...]]) -> bool:
    segments = tuple(ui_route.strip("/").split("/"))
    for pattern in router_routes:
        if len(pattern) != len(segments):
            continue
        if all(
            expected.startswith(":") or expected == actual
            for expected, actual in zip(pattern, segments, strict=False)
        ):
            return True
    return False


def _frontend_api_operation_from_line(
    line: str,
    following_source: str,
) -> tuple[str, str] | None:
    """Extract one quoted staff API operation without negative indexing."""
    marker = line.find("/api/staff/")
    if marker <= 0:
        return None

    quote = line[marker - 1]
    if quote not in {"'", '"', "`"}:
        return None

    remainder = line[marker:]
    raw_path = remainder.split(quote, 1)[0]
    method_match = re.search(r"method:\s*['\"]([A-Z]+)['\"]", following_source)
    method = method_match.group(1) if method_match else "GET"
    return method, _normalize_frontend_path(raw_path)


def _frontend_api_operations() -> set[tuple[str, str]]:
    """Extract every API operation invoked by production frontend code.

    The staff SPA keeps each api() URL literal on one line. Mutation methods
    appear either on that line or in the following five lines. This deliberately
    scans the application instead of maintaining a second hand-written list, so
    a new GUI action fails parity until its agent contract is registered.
    """
    frontend_root = Path(settings.BASE_DIR) / "frontend" / "src"
    operations = set()

    for source_path in sorted(frontend_root.rglob("*.ts*")):
        if ".test." in source_path.name:
            continue
        lines = source_path.read_text().splitlines()
        for index, line in enumerate(lines):
            window = "\n".join(lines[index:index + 6])
            operation = _frontend_api_operation_from_line(line, window)
            if operation:
                operations.add(operation)

    return operations - _AUTH_PATHS_WITH_AGENT_ALTERNATIVES


class AgentCapabilityRegistryTests(SimpleTestCase):
    def test_registry_cites_the_agent_native_guide(self):
        self.assertEqual(AGENT_NATIVE_GUIDE_URL, "https://every.to/guides/agent-native")
        catalog = capability_catalog()
        self.assertEqual(catalog["source"], AGENT_NATIVE_GUIDE_URL)
        self.assertIn("parity", catalog["principles"])

    def test_capability_ids_and_operations_are_unique(self):
        ids = [capability["id"] for capability in CAPABILITIES]
        operations = [
            (capability["method"], capability["path"])
            for capability in CAPABILITIES
        ]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(operations), len(set(operations)))

    def test_every_gui_api_operation_has_an_agent_capability(self):
        registered = {
            (capability["method"], capability["path"])
            for capability in CAPABILITIES
            if capability["ui_routes"]
        }
        self.assertSetEqual(_frontend_api_operations(), registered)

    def test_every_capability_has_policy_and_transport_metadata(self):
        for capability in CAPABILITIES:
            with self.subTest(capability=capability["id"]):
                self.assertIn(
                    capability["execution"],
                    {"anonymous", "direct", "human_confirmation", "human_only"},
                )
                self.assertIn(capability["risk"], {"read", "low", "moderate", "high"})
                self.assertEqual(capability["agent_transport"], "rest")
                self.assertTrue(capability["outcome"])
                self.assertIsInstance(capability["ui_routes"], list)
                if not capability["ui_routes"]:
                    self.assertTrue(
                        capability["path"].startswith("/api/staff/auth/")
                        or capability["execution"] == "anonymous"
                        or capability["id"] in AGENT_ONLY_CAPABILITIES,
                        "Only authentication, anonymous public, or explicitly "
                        "listed agent-only capabilities may have no UI route.",
                    )

    def test_every_advertised_ui_route_is_served_by_the_staff_router(self):
        """A catalog route no router serves is a lie an agent will act on.

        The catalog is the machine-readable handoff surface: when an action
        needs an accountable human, the agent hands the officer a route. A
        route that 404s sends them nowhere.
        """
        router_routes = _frontend_router_routes()
        self.assertIn(("inbox",), router_routes)
        unserved = sorted(
            {
                route
                for capability in CAPABILITIES
                for route in capability["ui_routes"]
                if not _route_is_served(route, router_routes)
            }
        )
        self.assertEqual(unserved, [])

    def test_case_ui_routes_name_a_stage_of_the_rail(self):
        """Parity for the stage rail (#45): every case action is reachable.

        The case workspace serves the seven stages plus Messages and
        Activity. The pre-rail tab names still resolve, but only as
        redirects, so the catalog must hand a person the stage where the
        action now lives. Each stage segment must be one the frontend's
        route registry authorizes, or the handoff lands on "not found".
        """
        nav = (Path(settings.BASE_DIR) / "frontend" / "src" / "nav.ts").read_text(
            encoding="utf-8"
        )
        stage_block = nav[nav.index("const STAGE_ROLES = {"):nav.index("} satisfies")]
        stages = set(re.findall(r"^  ([a-z]+):", stage_block, re.MULTILINE))
        self.assertEqual(
            stages,
            {"intake", "scope", "collect", "review", "redact", "release", "close"},
        )
        served = stages | {"messages", "activity"}
        advertised = {
            route.split("/")[3]
            for capability in CAPABILITIES
            for route in capability["ui_routes"]
            if route.startswith("/requests/:number/")
        }
        self.assertEqual(advertised - served, set())
        # Every stage hands off at least one action, so none is a dead end.
        self.assertEqual(served - advertised, set())

    def test_agent_only_capabilities_are_registered_and_have_no_ui_route(self):
        """Named exceptions must stay real, so the list cannot rot silently."""
        by_id = {capability["id"]: capability for capability in CAPABILITIES}
        for capability_id in AGENT_ONLY_CAPABILITIES:
            with self.subTest(capability=capability_id):
                self.assertIn(capability_id, by_id)
                self.assertEqual(by_id[capability_id]["ui_routes"], [])

    def test_every_constraint_is_defined_for_catalog_consumers(self):
        catalog = capability_catalog()
        definitions = catalog["constraint_definitions"]
        constraints = {
            constraint
            for capability in catalog["capabilities"]
            for constraint in capability["constraints"]
        }
        self.assertSetEqual(constraints, set(definitions))
        self.assertIn(
            "confirmed delivery",
            definitions["denial_delivery_required"].lower(),
        )
        self.assertIn(
            "records officer",
            definitions["terminal_reason_required"].lower(),
        )
        self.assertNotIn("ui_deferred", definitions)

    def test_path_normalizer_handles_any_ternary_query_filter(self):
        raw_path = "/api/staff/widgets/${kindFilter ? `?kind=${kindFilter}` : ''}"
        self.assertEqual(_normalize_frontend_path(raw_path), "/api/staff/widgets/")

    def test_source_line_parser_ignores_an_unquoted_path_at_column_zero(self):
        self.assertIsNone(
            _frontend_api_operation_from_line("/api/staff/requests/", ""),
        )

    def test_statutory_human_handoffs_remain_explicit(self):
        by_id = {capability["id"]: capability for capability in CAPABILITIES}
        self.assertNotIn("requests.revise_estimate", by_id)
        self.assertEqual(
            by_id["correspondence.draft_estimate_revision"]["execution"],
            "direct",
        )
        self.assertIn(
            "substantive_reason",
            by_id["correspondence.draft_estimate_revision"]["constraints"],
        )
        self.assertIn(
            "accountable_human_actor",
            by_id["correspondence.draft_estimate_revision"]["constraints"],
        )
        self.assertEqual(by_id["preservations.exclude_camera"]["execution"], "human_only")
        self.assertEqual(
            by_id["correspondence.approve"]["execution"],
            "human_only",
        )
        self.assertEqual(
            by_id["correspondence.queue"]["execution"],
            "human_only",
        )
        self.assertEqual(
            by_id["clarifications.resolve"]["execution"],
            "human_only",
        )
        self.assertEqual(
            by_id["inbound_correspondence.match"]["execution"],
            "human_only",
        )
        self.assertEqual(
            by_id["inbound_correspondence.rematch"]["execution"],
            "human_only",
        )
        self.assertEqual(
            by_id[
                "inbound_correspondence.associate_clarification_response"
            ]["execution"],
            "human_only",
        )
        self.assertEqual(
            by_id[
                "inbound_correspondence.associate_clarification_response"
            ]["risk"],
            "high",
        )
        self.assertIn(
            "substantive_reason",
            by_id["inbound_correspondence.match"]["constraints"],
        )
        self.assertIn(
            "accountable_human_actor",
            by_id["inbound_correspondence.rematch"]["constraints"],
        )
        self.assertIn(
            "delivery-status",
            by_id["inbound_correspondence.match"]["outcome"],
        )
        self.assertIn(
            "delivery-status",
            by_id["inbound_correspondence.rematch"]["outcome"],
        )
        self.assertIn(
            "denial_delivery_required",
            by_id["requests.transition"]["constraints"],
        )
        closure = by_id["requests.close_non_request"]
        self.assertEqual(closure["execution"], "human_confirmation")
        self.assertEqual(
            closure["path"],
            "/api/staff/requests/{number}/close-non-request/",
        )
        self.assertIn("accountable_human_actor", closure["constraints"])
        self.assertIn("substantive_reason", closure["constraints"])
        self.assertIn("email_intake_only", closure["constraints"])
        self.assertIn("append_only_audit", closure["constraints"])

    def test_personal_token_lifecycle_is_an_explicit_session_only_handoff(self):
        by_id = {capability["id"]: capability for capability in CAPABILITIES}
        expected = {
            "api_tokens.list": (
                "GET",
                "/api/staff/auth/api-tokens/",
            ),
            "api_tokens.create": (
                "POST",
                "/api/staff/auth/api-tokens/",
            ),
            "api_tokens.rotate": (
                "POST",
                "/api/staff/auth/api-tokens/{id}/rotate",
            ),
            "api_tokens.revoke": (
                "DELETE",
                "/api/staff/auth/api-tokens/{id}",
            ),
        }

        for capability_id, (method, path) in expected.items():
            with self.subTest(capability_id=capability_id):
                capability = by_id[capability_id]
                self.assertEqual(capability["method"], method)
                self.assertEqual(capability["path"], path)
                self.assertEqual(capability["execution"], "human_only")
                self.assertIn(
                    "interactive_staff_session",
                    capability["constraints"],
                )
                self.assertEqual(
                    capability["ui_routes"],
                    ["/configuration/agent-access"],
                )

    def test_work_item_operations_are_explicit_and_no_action_is_human_only(self):
        by_id = {capability["id"]: capability for capability in CAPABILITIES}
        expected = {
            "work_items.list": ("GET", "/api/staff/work-items/"),
            "work_items.retrieve": ("GET", "/api/staff/work-items/{id}/"),
            "work_items.counts": ("GET", "/api/staff/work-items/counts/"),
            "work_items.assignment_targets": (
                "GET",
                "/api/staff/work-items/{id}/assignment-targets/",
            ),
            "work_items.read": ("POST", "/api/staff/work-items/{id}/read/"),
            "work_items.unread": ("POST", "/api/staff/work-items/{id}/unread/"),
            "work_items.assignment": (
                "POST",
                "/api/staff/work-items/{id}/assignment/",
            ),
            "work_items.disposition": (
                "POST",
                "/api/staff/work-items/{id}/disposition/",
            ),
            "work_items.reopen": ("POST", "/api/staff/work-items/{id}/reopen/"),
            "work_items.no_action_required": (
                "POST",
                "/api/staff/work-items/{id}/no-action-required/",
            ),
            "work_items.bulk_metadata": (
                "POST",
                "/api/staff/work-items/bulk-metadata/",
            ),
        }
        for capability_id, operation in expected.items():
            with self.subTest(capability_id=capability_id):
                capability = by_id[capability_id]
                self.assertEqual(
                    (capability["method"], capability["path"]),
                    operation,
                )
        shared_work_actions = {
            "work_items.read": ["/requests/:number/messages", "/work/:id"],
            "work_items.unread": ["/requests/:number/messages", "/work/:id"],
            # Triaged straight from an Inbox row as well as inside a case.
            "work_items.disposition": [
                "/inbox",
                "/requests/:number/messages",
                "/work/:id",
            ],
            "work_items.reopen": ["/requests/:number/messages", "/work/:id"],
            "work_items.no_action_required": [
                "/inbox",
                "/requests/:number/messages",
                "/work/:id",
            ],
        }
        for capability_id, ui_routes in shared_work_actions.items():
            capability = by_id[capability_id]
            self.assertEqual(capability["ui_routes"], ui_routes)
            self.assertNotIn("ui_deferred", capability["constraints"])
        self.assertEqual(
            by_id["work_items.list"]["ui_routes"],
            ["/inbox", "/review"],
        )
        self.assertEqual(
            by_id["work_items.retrieve"]["ui_routes"],
            ["/work/:id"],
        )
        self.assertEqual(
            by_id["work_items.counts"]["ui_routes"],
            ["/inbox", "/review"],
        )
        self.assertEqual(
            by_id["work_items.assignment_targets"]["ui_routes"],
            ["/work/:id"],
        )
        self.assertEqual(
            by_id["work_items.assignment"]["ui_routes"],
            ["/work/:id"],
        )
        self.assertEqual(
            by_id["work_items.bulk_metadata"]["ui_routes"],
            ["/requests/:number/messages"],
        )
        for capability_id in expected:
            self.assertNotIn("ui_deferred", by_id[capability_id]["constraints"])
        no_action = by_id["work_items.no_action_required"]
        self.assertEqual(no_action["execution"], "human_only")
        self.assertIn("records_officer_authority", no_action["constraints"])
        self.assertIn("substantive_reason", no_action["constraints"])

    def test_shell_capabilities_advertise_only_canonical_routes(self):
        by_id = {
            capability["id"]: capability
            for capability in capability_catalog()["capabilities"]
        }
        self.assertEqual(
            by_id["requests.list"]["ui_routes"],
            ["/today", "/cases"],
        )
        self.assertEqual(
            by_id["requests.submit_staff"]["ui_routes"],
            ["/staff-intake"],
        )
        self.assertEqual(
            by_id["collection_tasks.list"]["ui_routes"],
            ["/requests/:number/scope", "/requests/:number/collect"],
        )
        for capability_id in (
            "requests.list",
            "requests.submit_staff",
            "collection_tasks.list",
        ):
            self.assertNotIn("/", by_id[capability_id]["ui_routes"])
            self.assertNotIn("/requests", by_id[capability_id]["ui_routes"])
            self.assertNotIn("/intake", by_id[capability_id]["ui_routes"])

    def test_scoped_work_and_correspondence_capabilities_use_canonical_routes(self):
        by_id = {
            capability["id"]: capability
            for capability in capability_catalog()["capabilities"]
        }
        self.assertEqual(
            by_id["time_entries.list"]["ui_routes"],
            ["/requests/:number/intake"],
        )
        self.assertEqual(
            by_id["time_entries.create"]["ui_routes"],
            ["/requests/:number/intake"],
        )
        self.assertEqual(
            by_id["time_entries.delete"]["ui_routes"],
            ["/requests/:number/intake"],
        )
        self.assertEqual(
            by_id["inbound_correspondence.list"]["ui_routes"],
            ["/inbox"],
        )
        self.assertEqual(
            by_id["inbound_correspondence.match"]["ui_routes"],
            ["/inbox"],
        )
        self.assertIn(
            "/configuration/correspondence",
            by_id["correspondence.list"]["ui_routes"],
        )
        self.assertIn(
            "/requests/:number/redact",
            by_id["redaction_proposals.accept"]["ui_routes"],
        )
        self.assertIn(
            "/requests/:number/redact",
            by_id["redaction_proposals.reject"]["ui_routes"],
        )
        for capability in by_id.values():
            self.assertNotIn("/correspondence", capability["ui_routes"])

    def test_case_workspace_read_models_and_assignment_are_registered(self):
        by_id = {capability["id"]: capability for capability in CAPABILITIES}
        expected = {
            "requests.assign_owner": ("PATCH", "/api/staff/requests/{number}/"),
            "requests.workspace_summary": (
                "GET",
                "/api/staff/requests/{number}/workspace-summary/",
            ),
            "requests.activity": (
                "GET",
                "/api/staff/requests/{number}/activity/",
            ),
            "requests.closure_readiness": (
                "GET",
                "/api/staff/requests/{number}/closure-readiness/",
            ),
        }
        for capability_id, operation in expected.items():
            with self.subTest(capability_id=capability_id):
                capability = by_id[capability_id]
                self.assertEqual(
                    (capability["method"], capability["path"]),
                    operation,
                )
                self.assertTrue(
                    all(
                        route.startswith("/requests/:number/")
                        for route in capability["ui_routes"]
                    )
                )
        self.assertIn(
            "append_only_audit",
            by_id["requests.assign_owner"]["constraints"],
        )

        self.assertIn(
            "/requests/:number/messages",
            by_id["staff_users.list"]["ui_routes"],
        )
        self.assertIn(
            "/requests/:number/review",
            by_id["exemptions.list"]["ui_routes"],
        )
        self.assertIn(
            "/requests/:number/messages",
            by_id["exemptions.list"]["ui_routes"],
        )
        self.assertIn(
            "/requests/:number/release",
            by_id["fee_schedules.list"]["ui_routes"],
        )

    def test_message_workspace_operations_are_explicit_and_separate(self):
        by_id = {capability["id"]: capability for capability in CAPABILITIES}

        self.assertEqual(
            by_id["correspondence.list"]["path"],
            "/api/staff/correspondence/",
        )
        self.assertEqual(
            by_id["correspondence.retrieve"]["path"],
            "/api/staff/correspondence/{id}/",
        )
        self.assertEqual(
            by_id["correspondence.search"]["method"],
            "POST",
        )
        self.assertEqual(
            by_id["correspondence.threads"]["path"],
            "/api/staff/correspondence/threads/",
        )
        self.assertEqual(
            by_id["correspondence.thread_messages"]["path"],
            "/api/staff/correspondence/thread-messages/",
        )
    def test_installment_preparation_is_an_integrity_checked_officer_action(self):
        capability = {
            row["id"]: row for row in CAPABILITIES
        }["installments.prepare"]

        self.assertEqual(capability["method"], "POST")
        self.assertEqual(
            capability["path"],
            "/api/staff/installments/{id}/prepare/",
        )
        self.assertEqual(capability["execution"], "human_confirmation")
        self.assertEqual(capability["risk"], "high")
        self.assertIn("records_officer_authority", capability["constraints"])
        self.assertIn("integrity_verified", capability["constraints"])
        self.assertIn("immutable_artifact", capability["constraints"])
        self.assertIn("append_only_audit", capability["constraints"])

    def test_installment_release_is_an_expiring_officer_action(self):
        capability = {
            row["id"]: row for row in CAPABILITIES
        }["installments.release"]

        self.assertEqual(capability["method"], "POST")
        self.assertEqual(
            capability["path"],
            "/api/staff/installments/{id}/release/",
        )
        self.assertEqual(capability["execution"], "human_confirmation")
        self.assertEqual(capability["risk"], "high")
        self.assertIn("records_officer_authority", capability["constraints"])
        self.assertIn("integrity_verified", capability["constraints"])
        self.assertIn("expiring_possession_link", capability["constraints"])
        self.assertIn("requester_notice_required", capability["constraints"])
        self.assertIn("append_only_audit", capability["constraints"])

    def test_integration_identity_console_is_an_explicit_secret_handoff(self):
        by_id = {capability["id"]: capability for capability in CAPABILITIES}
        expected = {
            "integration_identities.list": (
                "GET",
                "/api/staff/integration-identities/",
            ),
            "integration_identities.provision": (
                "POST",
                "/api/staff/integration-identities/{id}/provision/",
            ),
            "integration_identities.rotate": (
                "POST",
                "/api/staff/integration-identities/{id}/rotate/",
            ),
        }

        for capability_id, (method, path) in expected.items():
            with self.subTest(capability_id=capability_id):
                capability = by_id[capability_id]
                self.assertEqual(capability["method"], method)
                self.assertEqual(capability["path"], path)
                self.assertEqual(capability["execution"], "human_only")
                self.assertIn(
                    "interactive_staff_session",
                    capability["constraints"],
                )
        for capability_id in (
            "integration_identities.provision",
            "integration_identities.rotate",
        ):
            with self.subTest(capability_id=capability_id):
                constraints = by_id[capability_id]["constraints"]
                self.assertIn("secret_write_only", constraints)
                self.assertIn("secret_returned_once", constraints)
                self.assertIn("append_only_audit", constraints)

    def test_mcp_server_exposes_capability_discovery(self):
        tools_path = Path(settings.BASE_DIR) / "integrations" / "mcp" / "tools.py"
        self.assertIn("def list_capabilities(", tools_path.read_text())


class AgentCapabilityEndpointTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            "capability-agent", "capability-agent@psd401.net", "pw", is_staff=True,
        )

    def test_catalog_requires_staff_authentication(self):
        response = self.client.get("/api/staff/capabilities/")
        self.assertIn(response.status_code, (401, 403))

    def test_staff_can_discover_the_catalog(self):
        self.client.force_login(self.staff)
        response = self.client.get("/api/staff/capabilities/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], AGENT_NATIVE_GUIDE_URL)
        self.assertEqual(len(payload["capabilities"]), len(CAPABILITIES))
        self.assertTrue(
            any(capability["id"] == "requests.list" for capability in payload["capabilities"])
        )
