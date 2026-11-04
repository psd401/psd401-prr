"""Repository contracts for the casework UX research and browser-test gate."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless

import yaml
from django.conf import settings
from django.test import SimpleTestCase


class CaseworkVerificationFoundationTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(settings.BASE_DIR)

    def test_research_records_role_journeys_and_approved_decisions(self):
        path = self.root / "docs" / "ux" / "case-workspace-research.md"
        self.assertTrue(path.exists())
        research = path.read_text(encoding="utf-8")

        for role in (
            "Records Officer",
            "Department Liaison",
            "Reviewer",
            "Intake Clerk",
            "Application Admin",
        ):
            self.assertRegex(research, rf"(?s){re.escape(role)}.*Current state.*Target state")

        for decision in (
            "Case ownership versus work-item assignment",
            "Inbound disposition authority and categories",
            "Notification channels",
            "Global versus case queues",
            "Releases",
        ):
            self.assertIn(decision, research)
        self.assertIn("No action required", research)
        self.assertIn("Records Officer-only", research)
        self.assertIn("Cancelled", research)
        for ownership in (
            "Overview owns",
            "Messages owns",
            "Collection owns",
            "Records owns",
            "Releases owns",
            "Overview owns the Running log",
        ):
            self.assertIn(ownership, research)

    def test_verify_gate_is_blocking_and_covers_every_required_surface(self):
        config = json.loads((self.root / ".psd" / "verify.json").read_text())

        self.assertEqual(config["strictness"], "block")
        self.assertFalse(config["baseline"]["allow_preexisting_failures"])
        self.assertTrue(config["e2e_required"])
        self.assertEqual(
            config["e2e_flows"],
            [
                "staff-login-open-case",
                "officer-high-volume-messages",
                "officer-message-disposition",
                "new-arrival-stability",
                "role-aware-work-inbox",
                "admin-agent-configuration",
                "admin-district-settings",
                "intake-clerk-route-boundary",
                "officer-new-case",
                "officer-matter-kinds",
                "officer-clarification-response",
                "officer-scope-conversation",
                "officer-correct-requester-identity",
                "officer-email-to-existing-case",
                "officer-statutory-response-and-templates",
                "officer-case-table-sort",
                "officer-case-table-caught-up",
                "officer-case-table-row-opens-the-case",
                "officer-case-table-row-opens-from-the-keyboard",
                "officer-case-table-bulk-caught-up",
                "officer-case-table-fits-1440",
                "officer-case-table-fits-1280",
                "officer-case-notes",
                "officer-closure-letters-and-batch",
                "officer-fee-estimate-and-deposit",
                "liaison-assigned-case-work",
                "reviewer-record-work-no-pii",
                "review-throughput-queue",
                "correspondence-destination-parity",
                "keyboard-case-inbox",
                "accessible-live-updates",
                "high-volume-performance",
                "complete-officer-case-journey",
                "complete-liaison-handoff",
                "complete-reviewer-journey",
                "accessibility-audit",
                "officer-supersede-collection",
                "reviewer-bulk-review-by-filter",
                "officer-ai-evaluation-report",
                "officer-knowledge-pack-edit",
            ],
        )
        self.assertEqual(
            set(config["e2e_screenshots"]),
            set(config["e2e_flows"]),
        )
        self.assertTrue(all(config["e2e_screenshots"].values()))
        self.assertEqual(
            len({
                screenshot
                for screenshots in config["e2e_screenshots"].values()
                for screenshot in screenshots
            }),
            sum(len(value) for value in config["e2e_screenshots"].values()),
        )
        self.assertEqual(config["screenshot_dir"], ".verification")
        self.assertIn(
            "../.verification/staff-login-open-case.png",
            (self.root / "frontend/e2e/staff-login-open-case.spec.ts").read_text(),
        )

        commands = config["commands"]
        self.assertIn("bun run build", commands["build"])
        self.assertIn("bun run lint", commands["lint"])
        self.assertIn("bun run typecheck", commands["typecheck"])
        self.assertIn("scripts/verify-fast.sh", commands["test"])
        self.assertIn("run-casework-acceptance.sh", commands["e2e"])

        # The fast lane must cover backend, frontend, and -- with --full -- the
        # postgres contracts and the whole browser suite. Assert the coverage,
        # not the exact command text, so the runner can be retuned freely.
        gate = (self.root / "scripts" / "verify-fast.sh").read_text(encoding="utf-8")
        self.assertIn("config.test_settings", gate)
        self.assertIn("bun run test", gate)
        self.assertIn("bun run lint", gate)
        self.assertIn("bun run typecheck", gate)
        self.assertIn("bun run build", gate)
        self.assertIn("run-postgres-contract-tests.sh", gate)
        self.assertIn("run-casework-acceptance.sh", gate)
        for app_label in ("apps.records", "apps.api", "apps.correspondence", "apps.audit"):
            self.assertIn(app_label, gate)

        # The smoke subset lives in the acceptance script -- one copy. It must
        # never name a flow the full gate does not also run.
        launcher = (
            self.root / "scripts" / "run-casework-acceptance.sh"
        ).read_text(encoding="utf-8")
        smoke = re.findall(r"^\s+e2e/([\w-]+)\.spec\.ts$", launcher, re.MULTILINE)
        self.assertTrue(smoke, "The acceptance script names no smoke specs.")
        self.assertLess(len(smoke), len(config["e2e_flows"]))
        for flow in smoke:
            with self.subTest(smoke_flow=flow):
                self.assertIn(flow, config["e2e_flows"])
                self.assertTrue((self.root / f"frontend/e2e/{flow}.spec.ts").exists())

    def test_acceptance_artifacts_cover_accessibility_performance_uat_and_training(self):
        required_paths = (
            "frontend/e2e/accessibility-audit.spec.ts",
            "frontend/e2e/keyboard-case-inbox.spec.ts",
            "frontend/e2e/accessible-live-updates.spec.ts",
            "frontend/e2e/high-volume-performance.spec.ts",
            "frontend/e2e/complete-officer-case-journey.spec.ts",
            "frontend/e2e/complete-liaison-handoff.spec.ts",
            "frontend/e2e/complete-reviewer-journey.spec.ts",
            "docs/casework-staff-guide.md",
            "docs/ux/casework-uat.md",
            "docs/ux/casework-rollout-readiness.md",
        )
        for relative_path in required_paths:
            self.assertTrue(
                (self.root / relative_path).exists(),
                f"Missing issue #154 acceptance artifact: {relative_path}",
            )

        guide = (self.root / "docs" / "casework-staff-guide.md").read_text()
        for phrase in (
            "No action required",
            "Reopen team work",
            "At most 50 summaries",
            "Skip to main content",
            "Not Authorized",
        ):
            self.assertIn(phrase, guide)

        uat = (self.root / "docs" / "ux" / "casework-uat.md").read_text()
        for phrase in (
            "Records Officer",
            "Department Liaison",
            "Reviewer",
            "Automated acceptance status: **PASS**",
            "Manual UAT status: **PENDING**",
            "200% zoom",
            "Screen-reader structure",
        ):
            self.assertIn(phrase, uat)

        rollout = " ".join(
            (
                self.root / "docs" / "ux" / "casework-rollout-readiness.md"
            ).read_text().split()
        )
        for phrase in (
            "Issue #154 completed",
            "merged and live at `prr.psd401.net`",
            "bunx cdk deploy psd401-prr-prod --require-approval never",
            "Nothing runs between merge and that command",
            "CDK performs backup, migration, container replacement",
            "[docs/casework-staff-guide.md](../casework-staff-guide.md)",
            "[docs/ux/casework-uat.md](casework-uat.md)",
        ):
            self.assertIn(phrase, rollout)

    def test_playwright_is_pinned_and_runs_local_auth_case_flow(self):
        package = json.loads((self.root / "frontend" / "package.json").read_text())
        playwright_version = package["devDependencies"]["@playwright/test"]

        self.assertRegex(playwright_version, r"^\d+\.\d+\.\d+$")
        self.assertIn("playwright test", package["scripts"]["test:e2e"])
        self.assertIn("tsc", package["scripts"]["typecheck"])
        self.assertIn("deny-warnings", package["scripts"]["lint"])

        playwright_config = (
            self.root / "frontend" / "playwright.config.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("config.e2e_settings", playwright_config)
        self.assertIn("PRR_AUTH_MODE: 'local'", playwright_config)
        self.assertIn("PYTHON_DOTENV_DISABLED: '1'", playwright_config)
        self.assertIn(".verification", playwright_config)

        vite_config = (self.root / "frontend" / "vite.config.ts").read_text()
        self.assertIn("http://127.0.0.1:8000", vite_config)

        spec = (self.root / "frontend" / "e2e" / "staff-login-open-case.spec.ts").read_text()
        self.assertIn("staff-login-open-case", spec)
        self.assertIn("PRR-E2E-2000", spec)
        self.assertIn("staff-login-open-case.png", spec)

        for flow, evidence in {
            "role-aware-work-inbox": (
                "officer-shell.png",
                "liaison-shell.png",
                "reviewer-shell.png",
                "intake-shell.png",
                "admin-shell.png",
            ),
            "admin-agent-configuration": ("/configuration/agent-access",),
            "intake-clerk-route-boundary": ("Not authorized",),
            "officer-new-case": (
                "/overview",
                "officer-new-case-overview.png",
                "officer-case-collection.png",
                "officer-case-records.png",
                "officer-case-releases.png",
            ),
            "officer-clarification-response": (
                "/messages",
                "officer-clarification-response.png",
            ),
            "liaison-assigned-case-work": (
                "/collection",
                "liaison-assigned-case-work.png",
            ),
            "reviewer-record-work-no-pii": (
                "/records",
                "reviewer-record-work-no-pii.png",
            ),
            "correspondence-destination-parity": (
                "/correspondence",
                "/inbox",
                "/messages",
                "/configuration/correspondence",
                "correspondence-destination-parity-inbox.png",
            ),
        }.items():
            flow_spec = (self.root / "frontend" / "e2e" / f"{flow}.spec.ts")
            self.assertTrue(flow_spec.exists())
            flow_text = flow_spec.read_text(encoding="utf-8")
            self.assertIn(flow, flow_text)
            for expected in evidence:
                self.assertIn(expected, flow_text)

    def test_ci_runs_credential_free_playwright_and_uploads_failure_evidence(self):
        workflow_path = self.root / ".github" / "workflows" / "psd-ci-jobs.yml"
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        self.assertEqual(
            workflow["jobs"]["backend"]["timeout-minutes"],
            15,
            "One shard of the suite, plus dependency setup on a cold runner, "
            "without allowing an effectively unbounded CI job.",
        )
        acceptance_steps = workflow["jobs"]["acceptance"]["steps"]
        step_names = {step.get("name") for step in acceptance_steps}

        self.assertIn("Run Playwright acceptance flows", step_names)
        self.assertIn("Upload Playwright acceptance evidence", step_names)
        self.assertIn("Upload Playwright failure evidence", step_names)
        acceptance_step = next(
            step for step in acceptance_steps
            if step.get("name") == "Upload Playwright acceptance evidence"
        )
        self.assertEqual(acceptance_step["if"], "success()")
        self.assertIn(
            ".verification/playwright-results",
            acceptance_step["with"]["path"],
        )
        self.assertTrue(acceptance_step["with"]["include-hidden-files"])
        upload_step = next(
            step for step in acceptance_steps
            if step.get("name") == "Upload Playwright failure evidence"
        )
        self.assertEqual(upload_step["if"], "failure()")
        self.assertTrue(upload_step["with"]["include-hidden-files"])
        self.assertRegex(upload_step["uses"], r"actions/upload-artifact@[0-9a-f]{40}$")

        e2e_step = next(
            step for step in acceptance_steps
            if step.get("name") == "Run Playwright acceptance flows"
        )
        # The smoke subset gates the merge; nightly.yml runs every flow
        # (psd-dev-standards standards/05 rule 5).
        self.assertEqual(
            e2e_step["run"], "bash scripts/run-casework-acceptance.sh --smoke"
        )
        self.assertIn("measure_casework_performance --runs 30", workflow_text)
        self.assertIn("--output .verification/casework-performance.json", workflow_text)
        postgres_step_names = {
            step.get("name")
            for step in workflow["jobs"]["postgres-concurrency"]["steps"]
        }
        postgres_steps = workflow["jobs"]["postgres-concurrency"]["steps"]
        postgres_checkout = next(
            step for step in postgres_steps
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(
            postgres_checkout["with"]["ref"],
            "${{ github.event.pull_request.head.sha || github.sha }}",
        )
        postgres_contract = next(
            step for step in postgres_steps
            if step.get("name") == "Run PostgreSQL legal-workflow and search contracts"
        )
        self.assertIn("config.postgres_test_settings", postgres_contract["run"])
        self.assertIn("Upload PostgreSQL performance evidence", postgres_step_names)
        self.assertIn("PRR_CASEWORK_PERFORMANCE_DISPOSABLE", workflow_text)
        acceptance_launcher = (
            self.root / "scripts" / "run-casework-acceptance.sh"
        ).read_text()
        self.assertIn("bun run test:e2e", acceptance_launcher)
        self.assertIn("--smoke", acceptance_launcher)
        for job_name in (
            "backend-audit",
            "backend",
            "patch-coverage",
            "postgres-concurrency",
            "frontend",
            "acceptance",
            "infrastructure",
            "deployment-compose",
        ):
            job = workflow["jobs"][job_name]
            checkout = next(
                step for step in job["steps"]
                if str(step.get("uses", "")).startswith("actions/checkout@")
            )
            self.assertEqual(
                checkout["with"]["ref"],
                "${{ github.event.pull_request.head.sha || github.sha }}",
            )
        launcher = (self.root / "scripts" / "run-e2e-server.sh").read_text()
        playwright_config = (
            self.root / "frontend" / "playwright.config.ts"
        ).read_text()
        self.assertIn("failOnFlakyTests: true", playwright_config)
        postgres_launcher = (
            self.root / "scripts" / "run-postgres-contract-tests.sh"
        ).read_text()
        for script in (launcher, postgres_launcher):
            self.assertIn("scripts/prr-python.sh", script)
            self.assertIn("PRR_REPOSITORY_ROOT", script)
        self.assertIn(
            "apps.correspondence.tests.test_casework_fixture_command",
            postgres_launcher,
        )
        self.assertIn("measure_casework_performance", postgres_launcher)
        self.assertIn(
            'rm -f "$PRR_REPOSITORY_ROOT/.verification/e2e.sqlite3"',
            launcher,
        )
        for forbidden in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "PRR_GOOGLE_OIDC_CLIENT_SECRET",
            "PRR_GMAIL",
            "OBJECT_STORE",
        ):
            self.assertNotIn(
                forbidden,
                "\n".join(
                    (
                        str(workflow["jobs"]["frontend"]),
                        str(workflow["jobs"]["acceptance"]),
                        playwright_config,
                        launcher,
                    )
                ),
            )


class GateRunsFromAWorktreeTests(SimpleTestCase):
    """The Definition-of-Done gate must work unchanged from a git worktree."""

    def setUp(self):
        self.root = Path(settings.BASE_DIR)

    @skipUnless(shutil.which("git"), "git is not installed")
    def test_regenerated_evidence_is_not_tracked(self):
        # b663e80d untracked .verification: every acceptance run deletes and
        # regenerates it, so tracked screenshots make every gate run dirty the
        # tree and a committed gate run silently deletes the evidence.
        result = subprocess.run(
            ["git", "ls-files", "--", ".verification"],
            cwd=self.root, capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.skipTest("not a git checkout")
        self.assertEqual(result.stdout.split(), [".verification/.gitkeep"])

    def test_every_gate_lane_resolves_python_through_one_helper(self):
        # Anchor on the live assignment line, so a commented-out or dead
        # reference to the helper cannot satisfy the contract.
        for script, assignment in {
            "run-e2e-server.sh": r'^PRR_E2E_PYTHON="\$\(bash "\$PRR_REPOSITORY_ROOT/scripts/prr-python\.sh"\)"$',
            "run-postgres-contract-tests.sh": r'^PRR_CONTRACT_PYTHON="\$\(bash "\$PRR_REPOSITORY_ROOT/scripts/prr-python\.sh"\)"$',
            "verify-fast.sh": r'^PY="\$\{PY:-\$\(bash scripts/prr-python\.sh\)\}"$',
        }.items():
            with self.subTest(script=script):
                text = (self.root / "scripts" / script).read_text(encoding="utf-8")
                self.assertRegex(text, re.compile(assignment, re.MULTILINE))
                self.assertNotIn("PRR_E2E_PYTHON=python\n", text)
                self.assertNotIn(".venv/bin/python}", text)

    def _resolve(self, cwd, **env):
        return subprocess.run(
            ["bash", "scripts/prr-python.sh"],
            cwd=cwd, capture_output=True, text=True, check=True,
            env={"PATH": os.environ["PATH"], **env},
        ).stdout.strip()

    def test_explicit_interpreter_wins(self):
        self.assertEqual(
            self._resolve(self.root, PRR_PYTHON_BIN="/opt/custom/python"),
            "/opt/custom/python",
        )

    @skipUnless(shutil.which("git"), "git is not installed")
    def test_worktree_without_venv_uses_the_main_checkouts_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            (main / "scripts").mkdir(parents=True)
            shutil.copy(self.root / "scripts" / "prr-python.sh", main / "scripts")
            venv_python = main / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/bin/sh\n")
            venv_python.chmod(0o755)
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
            subprocess.run([*git, "init", "-q"], cwd=main, check=True)
            subprocess.run([*git, "add", "scripts"], cwd=main, check=True)
            subprocess.run([*git, "commit", "-qm", "x"], cwd=main, check=True)
            worktree = Path(tmp) / "wt"
            subprocess.run([*git, "worktree", "add", "-q", str(worktree)], cwd=main, check=True)

            self.assertFalse((worktree / ".venv").exists())
            self.assertEqual(
                Path(self._resolve(worktree)).resolve(), venv_python.resolve()
            )
            # The worktree's own .venv, when present, still wins.
            own = worktree / ".venv" / "bin" / "python"
            own.parent.mkdir(parents=True)
            own.write_text("#!/bin/sh\n")
            own.chmod(0o755)
            self.assertEqual(Path(self._resolve(worktree)).resolve(), own.resolve())
