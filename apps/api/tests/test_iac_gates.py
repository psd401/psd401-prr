"""Repository contracts for the infrastructure-as-code gates (standards/08).

standards/08 builds its guardrails in layers, and the point of the layers is
that each one is enforced somewhere a prompt-injected agent cannot quietly
remove: the rule pack runs inside ``cdk synth``, a second engine re-checks the
synthesized templates in CI, and the Terraform root is formatted, validated,
linted and scanned before anything merges.

Every assertion here is about the wiring rather than the rules. A Checkov step
that drifted to an unpinned version, a repository-wide ``--skip-check`` that
disabled a rule for every future resource, a ``terraform`` job that stopped
being required, or a rule pack that was registered but never reached - each
would leave the gate reporting green while checking less.
"""
import json
import re
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase

CHECKOV_INVOCATION = 'uv tool run --from "checkov==${PRR_CHECKOV_VERSION}" checkov'


def _step(job, name):
    """One named step, or a failure that says which name went missing.

    A bare ``next()`` raises ``StopIteration`` here, which fails the test with
    a traceback that names neither the step nor the job - the renamed step is
    exactly what the reader needs to be told.
    """
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"No step named {name!r}. This job has: "
        + ", ".join(repr(step.get("name")) for step in job["steps"])
    )


def _step_names(job):
    return [step.get("name") for step in job["steps"]]


class IacGateWiringTests(SimpleTestCase):
    """The CI side: both engines run, pinned, and both are required."""

    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.ci = yaml.safe_load(
            (self.root / ".github/workflows/psd-ci-jobs.yml").read_text(
                encoding="utf-8"
            )
        )

    def test_the_combined_gate_requires_both_infrastructure_lanes(self):
        """A lane nobody waits on is not a gate."""
        required = self.ci["jobs"]["psd-ci"]["needs"]
        self.assertIn("infrastructure", required)
        self.assertIn("terraform", required)

    def test_checkov_runs_at_one_pinned_version_everywhere(self):
        """A scanner that updates itself fails on days nobody touched infra."""
        version = self.ci["env"]["PRR_CHECKOV_VERSION"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

        # Per job, not a global count: two invocations in one lane and none in
        # the other would satisfy a total of two while leaving a tree
        # unscanned.
        by_job = {
            name: [
                step["run"]
                for step in job.get("steps", [])
                if "checkov" in str(step.get("run", ""))
            ]
            for name, job in self.ci["jobs"].items()
        }
        self.assertEqual(
            {name: len(runs) for name, runs in by_job.items() if runs},
            {"infrastructure": 1, "terraform": 1},
            "Exactly one Checkov step in the CDK lane and one in the "
            "Terraform lane, and none anywhere else.",
        )
        for runs in by_job.values():
            for run in runs:
                self.assertIn(CHECKOV_INVOCATION, " ".join(run.split()))

    def test_no_lane_disables_a_checkov_rule_for_every_resource(self):
        """Suppressions are per resource, with evidence, or they are not reviewed.

        ``--skip-check`` on the command line, or ``skip-check`` in a Checkov
        config file, turns a reviewed exception for one resource into a hole
        every future resource falls through.
        """
        for job in self.ci["jobs"].values():
            for step in job.get("steps", []):
                self.assertNotIn("--skip-check", str(step.get("run", "")))

        for name in (".checkov.yaml", ".checkov.yml"):
            config = self.root / name
            if config.is_file():
                parsed = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
                self.assertNotIn("skip-check", parsed)

    def test_the_cdk_lane_scans_what_it_synthesized(self):
        """cdk-nag reads the CDK source; Checkov reads the template it produced."""
        job = self.ci["jobs"]["infrastructure"]
        names = _step_names(job)
        synth = "Synthesize both stacks without AWS credentials"
        scan = "Check the synthesized templates with Checkov"
        self.assertLess(
            names.index(synth),
            names.index(scan),
            "Checkov has nothing to read until synthesis has written cdk.out.",
        )
        self.assertIn("--directory infra/cdk/cdk.out", _step(job, scan)["run"])
        self.assertIn("--framework cloudformation", _step(job, scan)["run"])

    def test_the_terraform_lane_runs_the_standard_order(self):
        """standards/08 CI order: fmt, validate, tflint, Checkov."""
        job = self.ci["jobs"]["terraform"]
        self.assertEqual(
            job["defaults"]["run"]["working-directory"],
            "infra/terraform/google-identity",
        )
        ordered = [
            "Check formatting",
            "Validate the configuration",
            "Lint",
            "Check the configuration with Checkov",
        ]
        names = _step_names(job)
        positions = [names.index(step) for step in ordered]
        self.assertEqual(positions, sorted(positions))

    def test_validation_never_reaches_the_state_backend(self):
        """This lane holds no credentials, so it must not need any."""
        job = self.ci["jobs"]["terraform"]
        init = _step(job, "Initialize providers without the state backend")
        self.assertIn("-backend=false", init["run"])

    def test_no_workflow_applies_or_imports_terraform(self):
        """standards/08 PR flow: applies happen through an OIDC role, elsewhere."""
        forbidden = re.compile(r"terraform\s+(apply|import|destroy)")
        for workflow in sorted((self.root / ".github/workflows").glob("*.yml")):
            self.assertIsNone(
                forbidden.search(workflow.read_text(encoding="utf-8")),
                f"{workflow.name} runs a state-changing Terraform command.",
            )

    def test_the_python_setup_action_can_skip_the_project_environment(self):
        """The two Checkov lanes need uv, not the application's dependencies."""
        action = yaml.safe_load(
            (self.root / ".github/actions/setup-python-deps/action.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(action["inputs"]["sync"]["default"], "true")
        for step in action["runs"]["steps"]:
            if "uv sync" in str(step.get("run", "")):
                self.assertIn("inputs.sync == 'true'", step["if"])


class IacToolVersionTests(SimpleTestCase):
    """Versions pinned in more than one place have to agree."""

    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.ci = yaml.safe_load(
            (self.root / ".github/workflows/psd-ci-jobs.yml").read_text(
                encoding="utf-8"
            )
        )
        self.terraform_root = self.root / "infra/terraform/google-identity"

    def _ci_terraform_version(self):
        setup = next(
            step
            for step in self.ci["jobs"]["terraform"]["steps"]
            if "setup-terraform" in str(step.get("uses", ""))
        )
        return setup["with"]["terraform_version"]

    def test_ci_terraform_satisfies_the_roots_required_version(self):
        """standards/08: Terraform 1.16.x, `required_version = "~> 1.16.0"`."""
        versions = (self.terraform_root / "versions.tf").read_text(encoding="utf-8")
        self.assertIn('required_version = "~> 1.16.0"', versions)
        self.assertRegex(self._ci_terraform_version(), r"^1\.16\.\d+$")

    def test_every_infrastructure_action_is_pinned_to_a_commit(self):
        """standards/06: an action pinned to a tag is an action somebody can move."""
        for step in self.ci["jobs"]["terraform"]["steps"]:
            uses = step.get("uses")
            if uses and not uses.startswith("./"):
                self.assertRegex(uses, r"@[0-9a-f]{40}$", uses)

    def test_tflint_pins_its_ruleset(self):
        config = (self.terraform_root / ".tflint.hcl").read_text(encoding="utf-8")
        self.assertIn('plugin "google"', config)
        self.assertRegex(config, r'version\s*=\s*"\d+\.\d+\.\d+"')

    def test_tflint_is_installed_at_a_pinned_verified_version(self):
        """setup-tflint downloads a release archive, so pin what it downloads.

        Without an exact version the action takes ``latest``, and without
        ``checksums`` it extracts whatever the download returned onto the
        gate's PATH. The checksum is the SHA-256 of the release zip, which is
        what the action hashes before extracting.
        """
        setup = _step(self.ci["jobs"]["terraform"], "Set up TFLint")
        self.assertTrue(setup["uses"].startswith("terraform-linters/setup-tflint@"))
        self.assertRegex(setup["with"]["tflint_version"], r"^v\d+\.\d+\.\d+$")
        self.assertRegex(setup["with"]["checksums"].strip(), r"^[0-9a-f]{64}$")

    def test_the_provider_lockfile_is_committed(self):
        """standards/08 Terraform rules: `.terraform.lock.hcl` is committed."""
        self.assertTrue((self.terraform_root / ".terraform.lock.hcl").is_file())


class CdkNagWiringTests(SimpleTestCase):
    """The rule pack has to be registered where synthesis actually sees it."""

    def setUp(self):
        self.cdk = Path(settings.BASE_DIR) / "infra/cdk"

    def test_cdk_nag_is_an_exact_pinned_dependency(self):
        manifest = json.loads(
            (self.cdk / "package.json").read_text(encoding="utf-8")
        )
        self.assertRegex(
            manifest["devDependencies"]["cdk-nag"],
            r"^3\.\d+\.\d+$",
            "cdk-nag v3 exactly: v2 was an aspect and would not gate synthesis.",
        )

    def test_the_guard_validator_is_an_exact_pinned_dependency(self):
        """The Control Tower rule set ships inside the package version.

        A floating range would let a new release add rules that fail a
        synthesis nobody changed, so the version moves only by an edit.
        """
        manifest = json.loads(
            (self.cdk / "package.json").read_text(encoding="utf-8")
        )
        self.assertRegex(
            manifest["devDependencies"]["@cdklabs/cdk-validator-cfnguard"],
            r"^\d+\.\d+\.\d+$",
        )

    def test_the_entry_point_uses_the_same_app_the_tests_do(self):
        """A pack registered in a factory the CLI does not call gates nothing.

        That the pack gates synthesis is proven behaviourally, by adding an
        unacknowledged violation to the real app and asserting synthesis
        throws (``infra/cdk/test/app.test.ts``). Nothing in that suite can
        prove the CDK CLI runs the same factory, because ``cdk.json`` points
        at ``bin/prr.ts`` and a forked entry point would synthesize cleanly.
        So this one reads the file, deliberately.
        """
        entry = (self.cdk / "bin/prr.ts").read_text(encoding="utf-8")
        self.assertIn("createPrrApp", entry)
