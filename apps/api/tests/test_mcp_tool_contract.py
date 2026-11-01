# ABOUTME: Static gate for the MCP tool contract and the server.json manifest.
# ABOUTME: Namespacing, annotations, the tool ceiling, and tool-definition pins.
"""The parts of psd-dev-standards standards/07-mcp.md a test can enforce.

Three of the standard's build rules are properties of the tool list itself,
so they belong in a gate rather than in a reviewer's memory:

* build standard 3 -- ``psd_<system>_<resource>_<verb>`` names, and no more
  than about fifteen tools on one server;
* build standard 4 -- ``readOnlyHint`` and ``destructiveHint`` on every tool;
* build standard 6 -- a ``server.json`` whose declared inventory is the
  inventory the server actually serves.

The last test here is the standard's **rug-pull defense** ("tool-definition
hashes pinned; any description/name change alerts"). A tool description is a
prompt: an attacker or a careless edit that rewrites one changes what every
connected agent is instructed to do, and nothing else in CI would notice. The
pin makes that edit fail the suite, with the new digest printed so a
deliberate change is one line to update and a reviewer sees the diff.
"""
import hashlib
import json
import re
from pathlib import Path

from asgiref.sync import async_to_sync
from django.conf import settings
from django.test import SimpleTestCase
from mcp.types import ToolAnnotations

from apps.api.capabilities import CAPABILITIES
from integrations.mcp.client import PRRClient
from integrations.mcp.tools import (
    MAX_TOOLS,
    READ_TOOL_NAMES,
    TOOL_CLIENT_METHODS,
    TOOL_NAMESPACE,
    WRITE_TOOL_NAMES,
    create_mcp_server,
)

#: SHA-256 over the canonical tool definitions (name, title, description and
#: every annotation, for all 12 tools). Regenerate deliberately -- the failure
#: message prints the new value -- and only in a commit whose diff shows the
#: description or annotation change that earned it.
TOOL_DEFINITION_DIGEST = (
    "587c18b6cd619ce6650d00cb74cb436d8c19e558fe447f724274ffca3d590699"
)


def unreachable_client(_ctx) -> PRRClient:
    """A client provider for a registry that is only ever introspected.

    These tests read tool definitions; they never execute a tool, so there is
    nothing for a provider to return. Raising keeps that honest -- if a test
    ever does call a tool, it fails loudly here instead of against a half-set-up
    client.
    """
    raise AssertionError("the tool-contract gate never executes a tool")


def annotations_for(tool) -> ToolAnnotations:
    """The tool's annotations, refusing to continue if it has none.

    ``Tool.annotations`` is optional in the MCP schema, and "absent" is the
    case these tests exist to forbid, so every reader goes through here.
    """
    annotations = tool.annotations
    if annotations is None:
        raise AssertionError(f"{tool.name} declares no annotations at all")
    return annotations


#: MCP tools that execute no single catalog row, and why. The capability
#: catalog's ``mcp_tool`` field names the tool that *executes* an operation,
#: so a tool that derives its answer from another capability, or that reads
#: one row and proposes a change to a different one, has nowhere honest to be
#: cited.
TOOLS_THAT_EXECUTE_NO_SINGLE_CAPABILITY = {
    "psd_prr_overdue_requests_list": (
        "A client-side statutory filter layered over requests.list; it "
        "executes no REST operation of its own."
    ),
    "psd_prr_collection_task_notes_propose": (
        "Reads one collection task and returns a proposal. It deliberately "
        "does not execute the task PATCH, so citing it on that row would "
        "claim an execution path it refuses to take."
    ),
    "psd_prr_exemption_citations_suggest": (
        "Validates citations and returns a proposal. Attaching a citation "
        "stays human-only under rule 3, so it executes no row."
    ),
}


def cited_tools():
    """Every MCP tool name the capability catalog points agents at."""
    return {
        capability["mcp_tool"]
        for capability in CAPABILITIES
        if capability.get("mcp_tool")
    }


def hosted_tools():
    mcp = create_mcp_server(unreachable_client, include_write_tools=True)
    return async_to_sync(mcp.list_tools)()


def stdio_tools():
    mcp = create_mcp_server(unreachable_client)
    return async_to_sync(mcp.list_tools)()


class ToolNamingTests(SimpleTestCase):
    def test_every_tool_is_namespaced_for_the_district_gateway(self):
        for tool in hosted_tools():
            with self.subTest(tool=tool.name):
                self.assertTrue(
                    tool.name.startswith(TOOL_NAMESPACE),
                    f"{tool.name} must start with {TOOL_NAMESPACE}",
                )
                # Underscores only: the registry mixed snake_case reads with
                # kebab-case writes before this, which is exactly the kind of
                # inconsistency a shared gateway makes expensive.
                self.assertRegex(tool.name, r"\A[a-z][a-z0-9_]*\Z")

    def test_the_server_stays_under_the_tool_ceiling(self):
        tools = hosted_tools()
        self.assertLessEqual(len(tools), MAX_TOOLS, [t.name for t in tools])

    def test_the_name_registry_matches_what_the_server_serves(self):
        self.assertSetEqual(
            {tool.name for tool in hosted_tools()},
            set(READ_TOOL_NAMES) | set(WRITE_TOOL_NAMES),
        )
        self.assertSetEqual(
            {tool.name for tool in stdio_tools()},
            set(READ_TOOL_NAMES),
        )

    def test_read_and_write_registries_do_not_overlap(self):
        self.assertSetEqual(
            set(READ_TOOL_NAMES) & set(WRITE_TOOL_NAMES),
            set(),
        )

    def test_every_tool_maps_to_a_real_client_method(self):
        self.assertSetEqual(
            set(TOOL_CLIENT_METHODS),
            set(READ_TOOL_NAMES) | set(WRITE_TOOL_NAMES),
        )
        for tool_name, method in TOOL_CLIENT_METHODS.items():
            with self.subTest(tool=tool_name):
                self.assertTrue(
                    callable(getattr(PRRClient, method, None)),
                    f"PRRClient has no {method}()",
                )

    def test_the_map_accounts_for_every_public_client_method(self):
        # The other direction: a REST method added to PRRClient without a
        # tool, or a tool quietly dropped, shows up here. This assertion used
        # to live in test_hosted_mcp, which is about the live transport; the
        # registry's shape belongs with the rest of the registry contract.
        public_client_methods = {
            name
            for name, value in vars(PRRClient).items()
            if callable(value) and not name.startswith("_")
        }
        self.assertSetEqual(
            public_client_methods,
            set(TOOL_CLIENT_METHODS.values()),
        )

    def test_each_tool_dispatches_to_its_own_mapped_client_method(self):
        """The map is the dispatch, so prove it by actually dispatching.

        Checking that the map is internally coherent is not the same claim as
        checking that calling a tool reaches the method the map names. This
        invokes every read tool against a recording client and compares what
        was called, which is the assertion that would catch a tool wired to
        another tool's REST method.
        """

        class RecordingClient(PRRClient):
            """Records which REST method a tool reached, and returns a page.

            Every method the map names is replaced with a recorder; nothing
            else about PRRClient is touched, and the base __init__ is skipped
            because this stub opens no session.
            """

            def __init__(self):
                self.called = []
                for method_name in TOOL_CLIENT_METHODS.values():
                    setattr(self, method_name, self._recorder(method_name))

            def _recorder(self, method_name):
                def record(**kwargs):
                    self.called.append(method_name)
                    return {"results": []}

                return record

        # Only the arguments the schema makes mandatory; the recording client
        # never validates them, so the values just have to satisfy pydantic.
        required_arguments = {"psd_prr_request_get": {"number": "PRR-2026-0001"}}

        client = RecordingClient()
        mcp = create_mcp_server(lambda ctx: client)
        for tool_name in READ_TOOL_NAMES:
            with self.subTest(tool=tool_name):
                client.called.clear()
                async_to_sync(mcp.call_tool)(
                    tool_name,
                    required_arguments.get(tool_name, {}),
                )
                self.assertEqual(
                    client.called,
                    [TOOL_CLIENT_METHODS[tool_name]],
                )

    def test_an_unregistered_tool_name_cannot_reach_a_client_method(self):
        # TOOL_CLIENT_METHODS is a closed set, so a name that is not in it
        # has nowhere to dispatch. This is what makes the lookup a guard
        # rather than a convenience.
        self.assertNotIn("psd_prr_requests_delete", TOOL_CLIENT_METHODS)

    def test_the_capability_catalog_only_cites_tools_that_exist(self):
        # Rule 10's contract tells agents which MCP tool reaches a staff-UI
        # outcome. A stale name there sends an agent to a tool that 404s on
        # the wire, which is worse than no pointer at all.
        registered = set(READ_TOOL_NAMES) | set(WRITE_TOOL_NAMES)
        self.assertTrue(cited_tools(), "the catalog should cite some MCP tools")
        self.assertSetEqual(cited_tools() - registered, set())

    def test_the_capability_catalog_names_every_executing_tool(self):
        """The other direction, which is the one issue #12 was filed about.

        A tool that executes exactly one catalog operation has to be named on
        that row, or an agent reading the catalog is told the outcome is
        REST-only and drafts the letter by hand. The exemptions below are
        the tools that execute no single row, each with the reason; adding a
        tool means either citing it or adding it here deliberately.
        """
        registered = set(READ_TOOL_NAMES) | set(WRITE_TOOL_NAMES)
        self.assertSetEqual(
            registered - cited_tools(),
            set(TOOLS_THAT_EXECUTE_NO_SINGLE_CAPABILITY),
            "An MCP tool executes a catalog capability without being named "
            "on its row (or stopped doing so). Set mcp_tool on that row, or "
            "record why the tool executes no single operation in "
            "TOOLS_THAT_EXECUTE_NO_SINGLE_CAPABILITY.",
        )

    def test_no_tool_is_cited_by_more_than_one_capability(self):
        # mcp_tool is "the tool that executes this operation", so two rows
        # naming one tool means at least one of them is wrong about what the
        # tool does.
        names = [
            capability["mcp_tool"]
            for capability in CAPABILITIES
            if capability.get("mcp_tool")
        ]
        self.assertCountEqual(names, set(names))

    def test_a_cited_tool_is_never_on_a_human_only_row(self):
        # Naming a tool on a human_only row would promise an agent an
        # execution path the handoff then refuses, which is the parity
        # failure rule 10 exists to prevent.
        for capability in CAPABILITIES:
            if capability.get("mcp_tool"):
                with self.subTest(capability=capability["id"]):
                    self.assertNotEqual(capability["execution"], "human_only")


class ToolAnnotationTests(SimpleTestCase):
    def test_every_tool_declares_both_required_hints(self):
        for tool in hosted_tools():
            with self.subTest(tool=tool.name):
                annotations = annotations_for(tool)
                # Neither may be left to the schema default: destructiveHint
                # defaults to true, so silence reads as "dangerous".
                self.assertIsNotNone(annotations.readOnlyHint)
                self.assertIsNotNone(annotations.destructiveHint)
                self.assertIsNotNone(annotations.idempotentHint)
                self.assertIsNotNone(annotations.openWorldHint)
                self.assertTrue(annotations.title)

    def test_reads_are_read_only_and_drafts_are_not(self):
        hints = {
            tool.name: annotations_for(tool).readOnlyHint
            for tool in hosted_tools()
        }
        for name in READ_TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertIs(hints[name], True)
        for name in WRITE_TOOL_NAMES:
            with self.subTest(tool=name):
                # Two of the three only read, but all three are the tools a
                # gateway must put behind human approval, so none may claim
                # the hint that would let it auto-approve them.
                self.assertIs(hints[name], False)

    def test_no_tool_is_destructive_or_reaches_an_outside_system(self):
        for tool in hosted_tools():
            with self.subTest(tool=tool.name):
                annotations = annotations_for(tool)
                self.assertIs(annotations.destructiveHint, False)
                self.assertIs(annotations.openWorldHint, False)

    def test_every_tool_has_a_description(self):
        for tool in hosted_tools():
            with self.subTest(tool=tool.name):
                self.assertTrue((tool.description or "").strip())


class ToolDefinitionPinTests(SimpleTestCase):
    """Rug-pull defense: a tool's wording cannot change unnoticed."""

    @staticmethod
    def digest():
        canonical = json.dumps(
            [
                {
                    "name": tool.name,
                    "title": annotations_for(tool).title,
                    "description": tool.description,
                    "readOnlyHint": annotations_for(tool).readOnlyHint,
                    "destructiveHint": annotations_for(tool).destructiveHint,
                    "idempotentHint": annotations_for(tool).idempotentHint,
                    "openWorldHint": annotations_for(tool).openWorldHint,
                }
                for tool in sorted(hosted_tools(), key=lambda t: t.name)
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def test_tool_definitions_match_the_pinned_digest(self):
        actual = self.digest()
        self.assertEqual(
            actual,
            TOOL_DEFINITION_DIGEST,
            "A tool name, title, description or annotation changed. If that "
            "was deliberate, update TOOL_DEFINITION_DIGEST in this file to "
            f"{actual!r} in the same commit, so the review sees the wording "
            "diff. If it was not deliberate, you have found a rug pull.",
        )


class ServerManifestTests(SimpleTestCase):
    """build standard 6: the manifest has to describe this server."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manifest = json.loads(
            (Path(settings.BASE_DIR) / "server.json").read_text(),
        )
        cls.catalog = cls.manifest["_meta"]["net.psd401.registry/catalog"]

    def test_the_manifest_declares_the_registry_schema_and_identity(self):
        self.assertTrue(
            self.manifest["$schema"].startswith(
                "https://static.modelcontextprotocol.io/schemas/",
            ),
        )
        self.assertEqual(self.manifest["name"], "io.github.psd401/prr-staff-api")
        # The official registry caps `description` at 100 characters, and a
        # manifest that cannot be published is not a manifest.
        self.assertLessEqual(len(self.manifest["description"]), 100)

    def test_the_manifest_version_tracks_the_project_version(self):
        pyproject = (Path(settings.BASE_DIR) / "pyproject.toml").read_text()
        self.assertIn(
            f'version = "{self.manifest["version"]}"',
            pyproject,
            "server.json and pyproject.toml disagree about the version",
        )

    def test_the_manifest_points_at_the_hosted_endpoint(self):
        remote = self.manifest["remotes"][0]
        self.assertEqual(remote["type"], "streamable-http")
        self.assertEqual(remote["url"], "https://prr.psd401.net/mcp")

    def test_the_manifest_inventory_is_the_served_inventory(self):
        declared = {
            entry["name"]: entry for entry in self.catalog["tools"]
        }
        served = {tool.name: tool for tool in hosted_tools()}

        self.assertSetEqual(set(declared), set(served))
        for name, tool in served.items():
            with self.subTest(tool=name):
                annotations = annotations_for(tool)
                self.assertIs(
                    declared[name]["readOnlyHint"],
                    annotations.readOnlyHint,
                )
                self.assertIs(
                    declared[name]["destructiveHint"],
                    annotations.destructiveHint,
                )

    def test_the_manifest_marks_the_hosted_only_tools(self):
        hosted_only = {
            entry["name"]
            for entry in self.catalog["tools"]
            if entry.get("hostedOnly")
        }
        self.assertSetEqual(hosted_only, set(WRITE_TOOL_NAMES))

    def test_the_manifest_carries_the_catalog_fields_the_directory_needs(self):
        # standards/07 "Catalog": what it does, who can use it, data touched,
        # owner. A directory generated from these files is only as good as
        # the weakest manifest in the org.
        for key in (
            "owner",
            "riskTier",
            "riskTierReason",
            "whatItDoes",
            "whoCanUseIt",
            "dataTouched",
            "humanOnly",
        ):
            with self.subTest(key=key):
                self.assertTrue(self.catalog[key])

    def test_the_readme_states_the_same_risk_tier_and_oauth_gap(self):
        # The manifest is what a generated directory reads; the README is
        # what a person reads. Both carry the risk tier and the OAuth gap,
        # and nothing but this assertion stops one from being updated
        # without the other -- at which point the district would have two
        # answers to "is this server compliant" and no way to tell which is
        # current.
        # Whitespace-normalized, as the other doc-contract tests in this
        # suite do it: a phrase that happens to wrap across two lines in
        # Markdown is still the same claim.
        readme = " ".join(
            (
                Path(settings.BASE_DIR) / "integrations" / "mcp" / "README.md"
            ).read_text().split()
        )

        self.assertIn(self.catalog["riskTier"].upper(), readme)
        # The whole claim in one string, not three phrases that could each
        # survive in an unrelated sentence: which standard is unmet, what it
        # requires, and the concrete reason it is unmet.
        self.assertIn(
            "the one build standard this server does not meet: "
            "**build standard 2, OAuth 2.1 with no token passthrough.** "
            "Hosted `/mcp` still accepts a static attributed staff token "
            "and forwards it to the staff REST API.",
            readme,
        )

    def test_the_scanner_version_is_pinned_identically_in_both_lanes(self):
        # The token-free `inspect` lane and the token-gated `scan` lane run
        # the same tool from two workflow files. Bumping one and forgetting
        # the other would leave the gating lane on a stale scanner with
        # nothing failing, so the pins are asserted equal rather than
        # trusted to be noticed.
        workflows = Path(settings.BASE_DIR) / ".github" / "workflows"
        pins = set()
        for name in ("mcp-scan.yml", "psd-ci-jobs.yml"):
            text = (workflows / name).read_text()
            found = re.findall(r'PRR_MCP_SCAN_VERSION:\s*"([^"]+)"', text)
            self.assertEqual(
                len(found),
                1,
                f"{name} should pin the scanner exactly once",
            )
            pins.add(found[0])
        self.assertEqual(len(pins), 1, f"scanner pins disagree: {pins}")

    def test_the_manifest_is_honest_about_the_unmet_oauth_requirement(self):
        # The one build standard this server does not meet is build standard
        # 2, and the manifest is the artifact an org-wide audit reads. A
        # manifest that quietly omitted the gap would make the directory
        # report compliance the server does not have.
        standard = self.catalog["standard"]
        self.assertTrue(
            any(item.startswith("2 - OAuth") for item in standard["notMet"]),
            standard["notMet"],
        )
        self.assertFalse(
            any(item.startswith("2 ") for item in standard["met"]),
            "build standard 2 may not be claimed as met while hosted /mcp "
            "still accepts a static token",
        )
        self.assertFalse(self.catalog["publishToPublicRegistry"])
        self.assertTrue(self.catalog["publishToPublicRegistryReason"])
