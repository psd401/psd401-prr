import ast
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class WorkQueueArchitectureDecisionTests(SimpleTestCase):
    def test_adr_defines_one_next_action_without_a_second_legal_lifecycle(self):
        adr = (
            Path(settings.BASE_DIR)
            / "docs"
            / "decisions"
            / "0003-work-queue-domain.md"
        ).read_text()

        for required in (
            "PublicRecordsRequest.assigned_officer",
            "case owner",
            "one next action",
            "WorkItem",
            "WorkItemEvent",
            "WorkItemReceipt",
            "not a legal lifecycle",
            "StatusEvent",
            "50",
            "forward fix",
        ):
            with self.subTest(required=required):
                self.assertIn(required, adr)

    def test_production_domain_cannot_write_the_legal_lifecycle(self):
        work_queue_root = Path(settings.BASE_DIR) / "apps" / "work_queue"
        violations = []
        for path in sorted(work_queue_root.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in {
                    "apps.records.models",
                    "apps.records.services",
                }:
                    forbidden = {
                        alias.name
                        for alias in node.names
                        if alias.name in {"StatusEvent", "transition"}
                    }
                    if forbidden:
                        violations.append(
                            f"{path.name}:{node.lineno} imports "
                            f"{', '.join(sorted(forbidden))}"
                        )
                targets = []
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    if isinstance(node, ast.Assign):
                        raw_targets = node.targets
                    else:
                        raw_targets = [node.target]
                    targets.extend(raw_targets)
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "status":
                        violations.append(  # noqa: PERF401 - reads clearer as a loop
                            f"{path.name}:{node.lineno} assigns a status field"
                        )

        self.assertEqual(
            violations,
            [],
            "Work queue must not import or write the request legal lifecycle.",
        )
