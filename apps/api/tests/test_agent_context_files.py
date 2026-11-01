# ABOUTME: Contract for the agent context files: AGENTS.md stays a short map and
# ABOUTME: docs/compliance-rules.md keeps the full text of every numbered rule.
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

# psd-dev-standards standards/02-agentic-development.md: "One root AGENTS.md,
# <= ~150 lines, written as a map not a manual". The ceiling is enforced here so
# the file cannot drift back into a manual.
AGENTS_MD_MAX_LINES = 150

RULE_COUNT = 13

RULE_TITLES = {
    1: "Django owns compliance and its own transport edges",
    2: "All status changes go through `services.transition()`",
    3: "Every redaction or withholding requires an ExemptionCitation",
    4: "AuditLog and StatusEvent are append-only",
    5: "Requester PII is minimized",
    6: "Automated requester-facing messages are limited to two paths",
    7: "Footage: auto-hold always, auto-deny never",
    8: "The system registry routes collection",
    9: "`request.description` is never edited after intake",
    10: "GUI/agent parity is mandatory",
    11: "Every integration fails closed and uses least privilege",
    12: "Google group authorization stays keyless and read-only",
    13: "A case's matter kind decides which law it answers to",
}

# Every enforcement anchor the rules name. Slimming AGENTS.md must not drop the
# symbol, guard, or command an agent needs to find the rule in code.
RULE_ANCHORS = (
    "apps/records/services.transition()",
    "ExemptionCitation",
    "tasks.auto_acknowledge_safety_net",
    "AUTO_ACK_SAFETY_NET",
    "apps.configuration.runtime.get",
    "test_compliance.test_no_automated_path_exists",
    "receipt-ast-guard",
    "apps/records/letters.revise_estimate()",
    "scope-ast-guard",
    "PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
    "services.record_external_initial_response()",
    "InitialResponseAttestation",
    "attest_external_initial_responses",
    "apps/records/triage.py",
    "CAMERA_RETENTION_DAYS",
    "apps/integrations/avigilon/client.py",
    "manage.py verify_alta",
    "triage.exclude_camera()",
    "manage.py seed_systems",
    "triage.route_to_systems",
    "services.merge_requests()",
    "CLOSED_DUPLICATE",
    "work_queue.services.handle_case_inbox_items",
    "apps/api/capabilities.py",
    "test_agent_capabilities",
    "POST /api/public/requests/",
    "records.read_staff_api",
    "IntakeAttachment",
    "DirectStaffRoleGrant",
    "PublicRecordsRequest.matter_kind",
    "docs/decisions/0004-matter-kinds.md",
    "ferpa_access_due",
)

# The clauses in AGENTS.md's one-liners that forbid a specific, plausible
# change. Compared after whitespace normalization so reflowing a line is safe.
AGENTS_MD_PROHIBITIONS = (
    "never set `PublicRecordsRequest.status` directly",
    "no update/delete paths",
    "don't add required intake fields",
    "Only two automated requester-facing paths",
    "There is no auto-denial code path and none may be added",
    "holds degrade to URGENT manual instructions to facilities — never to silence",
    "Parity never weakens rules 6 or 7",
    "Never put tokens in source or `.env.example`",
    "it never holds, exports, collects, changes status, or reaches the requester",
    "do not widen it",
    "never `RecordItem`s; no unscanned file is downloadable",
    "exactly three gated draft/proposal tools",
    "No private key, domain-wide delegation, Vault-identity reuse, or Groups Editor/Admin role",
    "it never bypasses the last-admin guard",
    "never `settings.PRR_*`",
    "The public form and intake mailbox open only `public_records`; kinds never merge",
    "litigation sends no requester mail, FERPA none asserting RCW 42.56",
)


class AgentContextFileTests(SimpleTestCase):
    def _read(self, relative_path):
        return (Path(settings.BASE_DIR) / relative_path).read_text()

    def test_agents_md_is_a_map_not_a_manual(self):
        text = self._read("AGENTS.md")

        self.assertLessEqual(
            len(text.splitlines()),
            AGENTS_MD_MAX_LINES,
            "AGENTS.md must stay a ~150-line map; move depth into docs/.",
        )
        self.assertIn("docs/compliance-rules.md", text)

    def test_agents_md_answers_the_three_context_questions(self):
        text = self._read("AGENTS.md")

        # Build/test/lint commands, an architecture map, and the PR evidence bar.
        self.assertIn("python manage.py test apps --settings=config.test_settings", text)
        self.assertIn(
            "bunx cdk deploy psd401-prr-prod --require-approval never "
            "--profile psd401-prr-prod",
            text,
        )
        self.assertIn("## Layout", text)
        self.assertIn("## Evidence a change must carry", text)

    def test_every_numbered_rule_survives_in_agents_md(self):
        text = self._read("AGENTS.md")

        rules = text[text.index("## Architecture rules") : text.index("## Layout")]
        numbers = [
            int(match)
            for match in re.findall(r"^\s*(\d+)\. ", rules, flags=re.MULTILINE)
        ]
        self.assertEqual(
            numbers,
            list(range(1, RULE_COUNT + 1)),
            "AGENTS.md must list rules 1-13 once each, in order.",
        )

    def test_agents_md_keeps_each_rules_operative_prohibition(self):
        # An agent may read only AGENTS.md. Each one-liner must still carry the
        # clause whose absence would let a plausible change violate the rule.
        text = " ".join(self._read("AGENTS.md").split())

        for clause in AGENTS_MD_PROHIBITIONS:
            with self.subTest(clause=clause):
                self.assertIn(clause, text)

    def test_compliance_rules_doc_holds_the_full_text_of_every_rule(self):
        text = self._read("docs/compliance-rules.md")

        for number, title in RULE_TITLES.items():
            with self.subTest(rule=number):
                self.assertIn(f"## Rule {number} — {title}", text)

        for anchor in RULE_ANCHORS:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, text)

    def test_moved_sections_landed_in_their_owning_docs(self):
        # The sections AGENTS.md used to carry inline now live where they are
        # maintained; each destination must actually hold them.
        self.assertIn(
            "Do not bypass a readiness check",
            self._read("docs/phase-2-production-handoffs.md"),
        )
        self.assertIn(
            "Vault service account w/ domain-wide delegation",
            self._read("docs/phase-2-production-handoffs.md"),
        )
        self.assertIn(
            "Juneteenth",
            self._read("docs/development.md"),
        )
        self.assertIn(
            "## 10. Repository layout",
            self._read("docs/architecture.md"),
        )
        self.assertIn(
            "## Implemented capability snapshot (verified 2026-08-02)",
            self._read("docs/project-status.md"),
        )

    def test_claude_md_only_imports_agents_md(self):
        text = self._read("CLAUDE.md")

        self.assertIn("@AGENTS.md", text)
        self.assertLessEqual(len(text.splitlines()), 5)

    def test_no_context_file_publishes_a_dev_superuser_password(self):
        # A documented default password gets reused. `createsuperuser` prompts
        # for one; no hand-written doc may suggest a value.
        for relative_path in ("AGENTS.md", "README.md", "docs/development.md"):
            with self.subTest(path=relative_path):
                self.assertNotIn("prr-dev-admin", self._read(relative_path))
