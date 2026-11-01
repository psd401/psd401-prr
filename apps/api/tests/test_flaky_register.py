"""Repository contracts for the flaky-test register (`FLAKY.md`).

psd-dev-standards `standards/05-testing.md` rule 6 requires a quarantine
process with an owner, an issue, and a two-to-four-week window, tracked in
`FLAKY.md`. A register nobody checks becomes a list of tests that stopped
mattering, so these tests enforce both directions:

* no row may outlive its window, and
* no test may be quarantined without a row.
"""
import re
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase
from django.utils import timezone

#: The longest a quarantine may last (standards/05 rule 6: "2-4 week window").
MAX_QUARANTINE_DAYS = 28

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISSUE_REFERENCE = re.compile(r"#\d+")


def _parse_active_rows(text: str) -> list[dict[str, str]]:
    """Rows of the markdown table under `## Active quarantine`."""
    section = text.split("## Active quarantine", 1)
    if len(section) == 1:
        return []
    body = section[1].split("\n## ", 1)[0]

    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if header is None:
            header = [cell.lower() for cell in cells]
            continue
        if all(set(cell) <= {"-", ":"} for cell in cells if cell):
            continue
        rows.append(dict(zip(header, cells, strict=False)))
    return rows


class FlakyRegisterTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.register_path = self.root / "FLAKY.md"
        self.assertTrue(
            self.register_path.is_file(),
            "FLAKY.md is the required flaky register (standards/05 rule 6).",
        )
        self.register = self.register_path.read_text(encoding="utf-8")
        self.rows = _parse_active_rows(self.register)

    def test_the_register_documents_the_quarantine_mechanics(self):
        """Prose a reader cannot act on is not a process."""
        for required in (
            "@quarantine",             # the Playwright tag
            "--exclude-tag=quarantine",  # the Django lane
            "--quarantine",            # the non-gating launcher mode
            "2%",                      # the suite flake budget
            "four weeks",              # the window cap
            "one red and one green",   # the evidence the row must carry
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.register)

    def test_every_active_row_names_an_owner_an_issue_and_a_window(self):
        for row in self.rows:
            with self.subTest(test=row.get("test")):
                self.assertTrue(row.get("owner"), "A quarantine needs an owner.")
                self.assertRegex(
                    row.get("issue", ""),
                    ISSUE_REFERENCE,
                    "A quarantine needs an issue owning the fix.",
                )
                for column in ("quarantined", "expires"):
                    self.assertRegex(
                        row.get(column, ""),
                        ISO_DATE,
                        f"{column} must be an ISO date.",
                    )
                self.assertTrue(row.get("symptom"), "Record what it does when it fails.")
                # The control against quarantining a test that fails every
                # time: two runs on one commit, one red and one green.
                self.assertRegex(
                    row.get("evidence", ""),
                    r"https?://\S+.*https?://\S+",
                    "Link the two CI runs on the same commit that prove the "
                    "flake, or this is a bug report being silenced.",
                )

    def test_no_quarantine_outlives_its_window(self):
        today = timezone.localdate()
        for row in self.rows:
            with self.subTest(test=row.get("test")):
                started = timezone.datetime.fromisoformat(row["quarantined"]).date()
                expires = timezone.datetime.fromisoformat(row["expires"]).date()
                self.assertGreater(expires, started)
                self.assertLessEqual(
                    (expires - started).days,
                    MAX_QUARANTINE_DAYS,
                    "standards/05 rule 6 caps a quarantine at four weeks.",
                )
                self.assertGreaterEqual(
                    expires,
                    today,
                    f"The quarantine of {row.get('test')} expired on {expires}. "
                    "Fix it or re-triage it in the open with a new date.",
                )

    def test_a_quarantined_playwright_flow_is_tagged_and_registered(self):
        """Both directions: a row needs a tag, and a tag needs a row."""
        registered = {
            row["test"].strip("`")
            for row in self.rows
            if row.get("lane", "").lower().startswith("playwright")
        }
        tagged = {
            spec.stem
            for spec in sorted((self.root / "frontend" / "e2e").glob("*.spec.ts"))
            if "@quarantine" in spec.read_text(encoding="utf-8")
        }

        self.assertEqual(
            tagged,
            registered,
            "Every @quarantine-tagged flow needs a FLAKY.md row, and vice versa.",
        )

    def test_a_quarantined_backend_test_is_registered(self):
        registered = {
            row["test"].strip("`")
            for row in self.rows
            if row.get("lane", "").lower().startswith("django")
        }
        tagged = [
            path.relative_to(self.root).as_posix()
            for path in sorted(self.root.glob("apps/*/tests/*.py"))
            if re.search(r"""tag\(\s*["']quarantine["']""", path.read_text(encoding="utf-8"))
        ]

        if tagged:
            self.assertTrue(
                registered,
                f"{tagged} tag tests as quarantined with no FLAKY.md row.",
            )
        else:
            self.assertEqual(registered, set())

    def test_the_gating_lanes_exclude_quarantined_tests(self):
        launcher = (
            self.root / "scripts" / "run-casework-acceptance.sh"
        ).read_text(encoding="utf-8")
        gate = (self.root / "scripts" / "verify-fast.sh").read_text(encoding="utf-8")

        # Playwright: the gating modes skip the tag, and one mode runs only it.
        self.assertIn("--grep-invert", launcher)
        self.assertIn("--grep ", launcher)
        self.assertIn("--pass-with-no-tests", launcher)
        self.assertIn("--quarantine", launcher)
        # Django: the fast gate excludes the tag.
        self.assertIn("--exclude-tag=quarantine", gate)

    def test_ci_gates_exclude_quarantined_tests_and_still_run_them(self):
        workflow_path = self.root / ".github" / "workflows" / "psd-ci-jobs.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

        gating = next(
            step for step in workflow["jobs"]["backend"]["steps"]
            if step.get("name") == "Run backend tests"
        )
        self.assertIn("--exclude-tag=quarantine", gating["run"])

        # The non-gating lanes run the quarantined tests and cannot fail the
        # job: `continue-on-error` on the step, so the result is unambiguous.
        non_gating = [
            step
            for job in ("backend-audit", "acceptance")
            for step in workflow["jobs"][job]["steps"]
            if "quarantine" in (step.get("name") or "").lower()
        ]
        self.assertEqual(len(non_gating), 2)
        for step in non_gating:
            with self.subTest(step=step["name"]):
                self.assertTrue(step["continue-on-error"])
        self.assertIn("--tag=quarantine", non_gating[0]["run"])
        self.assertIn("--quarantine", non_gating[1]["run"])
