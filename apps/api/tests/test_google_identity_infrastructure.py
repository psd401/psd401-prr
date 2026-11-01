# ABOUTME: Static contract for the deliberately small Google identity
# ABOUTME: Terraform root and its documented one-time console bootstrap.
from django.conf import settings
from django.test import SimpleTestCase


class GoogleIdentityTerraformTests(SimpleTestCase):
    def setUp(self):
        self.root = settings.BASE_DIR / "infra" / "terraform" / "google-identity"

    def test_single_root_manages_identity_foundation_without_secret_values(self):
        main = (self.root / "main.tf").read_text()

        self.assertIn('resource "google_project_service" "cloud_identity"', main)
        self.assertIn('resource "google_project_service" "iam"', main)
        self.assertIn('resource "google_service_account" "group_reader"', main)
        self.assertIn('resource "google_project_service" "resource_manager"', main)
        self.assertIn('resource "google_secret_manager_secret" "oidc_client_secret"', main)

        # The portal address is a stored setting now; its default is the
        # registry's, and no example environment file names it.
        from apps.configuration.registry import SETTINGS_BY_KEY

        settings_source = (settings.BASE_DIR / "config" / "settings.py").read_text()
        env_example = (settings.BASE_DIR / ".env.example").read_text()
        self.assertEqual(
            SETTINGS_BY_KEY["PORTAL_BASE_URL"].default, "https://prr.psd401.net"
        )
        self.assertNotIn("records.psd401.net", settings_source)
        self.assertNotIn("records.psd401.net", env_example)
        self.assertIn('resource "google_iam_workload_identity_pool" "aws_runtime"', main)
        self.assertIn('resource "google_iam_workload_identity_pool_provider" "aws_runtime"', main)
        self.assertIn("attribute.aws_role", main)
        self.assertIn("roles/iam.workloadIdentityUser", main)
        self.assertNotIn("google_secret_manager_secret_version", main)
        self.assertNotIn("roles/owner", main)
        self.assertNotIn("github", main.lower())

        ignore = (settings.BASE_DIR / ".gitignore").read_text()
        self.assertIn(".terraform/", ignore)
        self.assertIn("*.tfstate", ignore)
        docker_ignore = (settings.BASE_DIR / ".dockerignore").read_text()
        self.assertIn(".terraform", docker_ignore)

    def test_identity_foundation_federates_only_production(self):
        main = (self.root / "main.tf").read_text()
        variables = (self.root / "variables.tf").read_text()
        outputs = (self.root / "outputs.tf").read_text()

        self.assertIn("application_hosts = {", main)
        self.assertIn('prod = "prr.psd401.net"', main)
        self.assertNotIn("dev-prr.psd401.net", main)
        self.assertIn('variable "aws_runtimes"', variables)
        self.assertIn('contains(["prod"], name)', variables)
        self.assertIn("oidc_callback_urls", outputs)
        self.assertIn("local.application_hosts", outputs)
        self.assertIn("for_each = var.aws_runtimes", main)

        # Production is the only environment (ADR 0002); the retired dev box's
        # host and federated role were removed on 2026-10-13.
        self.assertNotIn("for_each = var.environments", main)
        self.assertIn('resource "google_project" "identity"', main)
        self.assertIn('resource "google_service_account" "group_reader"', main)
        self.assertIn('resource "google_secret_manager_secret" "oidc_client_secret"', main)

    def test_identity_state_is_shared_encrypted_versioned_and_locked(self):
        versions = (self.root / "versions.tf").read_text()
        readme = (self.root / "README.md").read_text()

        self.assertIn('backend "s3"', versions)
        self.assertIn('bucket       = "psd401-prr-terraform-state-338414773271"', versions)
        self.assertIn('key          = "google-identity/terraform.tfstate"', versions)
        self.assertIn('region       = "us-west-2"', versions)
        self.assertIn("encrypt      = true", versions)
        self.assertIn("use_lockfile = true", versions)
        self.assertNotIn("dynamodb_table", versions)
        self.assertIn("AWS_PROFILE=psd401-prr-prod terraform init", readme)
        self.assertIn(
            "terraform plan -out=production-identity.tfplan",
            readme,
        )
        # The production identity root has no routine reason to delete or
        # replace anything; the gate makes every destroy a deliberate act.
        self.assertIn(
            """jq -e '[.resource_changes[]? | select(.change.actions | index("delete"))] | length == 0'""",
            readme,
        )
        self.assertIn(
            "terraform apply production-identity.tfplan",
            readme,
        )
        self.assertNotIn(
            "AWS_PROFILE=psd401-prr-prod terraform apply\n",
            readme,
        )

    def test_identity_root_runs_on_terraform_not_opentofu(self):
        """psd-dev-standards standards/08: Terraform on GCP (1.16.x, psd-dev-standards#81).

        The district standardizes on Terraform. OpenTofu crept in on
        2026-07-29 (36f36374) without a recorded decision; nothing in the
        operator docs or the provider lock may point back at it.
        """
        versions = (self.root / "versions.tf").read_text()
        lock = (self.root / ".terraform.lock.hcl").read_text()

        self.assertIn('required_version = "~> 1.16.0"', versions)
        self.assertIn('provider "registry.terraform.io/hashicorp/google"', lock)
        self.assertNotIn("opentofu", lock.lower())
        for doc in (
            self.root / "README.md",
            settings.BASE_DIR / "docs/google-identity-bootstrap.md",
            settings.BASE_DIR / "docs/gmail-transport.md",
            settings.BASE_DIR / "infra/cdk/README.md",
        ):
            text = doc.read_text()
            with self.subTest(doc=doc.name):
                self.assertNotRegex(text, r"\btofu (init|plan|apply|show|output|force-unlock)\b")
                self.assertNotIn("OpenTofu", text)

        # The state stack and bucket were renamed off the OpenTofu name on
        # 2026-10-13; the CDK app and the backend must not drift back to it.
        cdk = settings.BASE_DIR / "infra/cdk"
        for source in (
            self.root / "versions.tf",
            cdk / "bin/prr.ts",
            *sorted((cdk / "lib").glob("*.ts")),
            *sorted((cdk / "test").glob("*.ts")),
        ):
            with self.subTest(source=source.name):
                self.assertNotRegex(source.read_text().lower(), r"\b(open)?tofu")

    def test_nonsecret_identity_inputs_are_versioned_for_every_operator(self):
        shared_inputs = (self.root / "identity.auto.tfvars").read_text()
        ignore = (settings.BASE_DIR / ".gitignore").read_text()

        self.assertIn('project_id      = "psd401-prr-identity"', shared_inputs)
        self.assertIn('organization_id = "482073499306"', shared_inputs)
        # The billing link is environment record (standard 14: it belongs to
        # psd-gcp-infra), not this public repo. Terraform leaves the live link
        # alone rather than carrying the district's billing account ID.
        self.assertNotIn("billing_account", shared_inputs)
        self.assertNotIn("billing_account", (self.root / "variables.tf").read_text())
        main = (self.root / "main.tf").read_text()
        self.assertNotRegex(main, r"billing_account\s*=")
        self.assertIn("ignore_changes = [billing_account]", main)
        self.assertNotIn("psd401-prr-dev-runtime", shared_inputs)
        self.assertIn('role_name  = "psd401-prr-prod-runtime"', shared_inputs)
        self.assertIn(
            "!infra/terraform/google-identity/identity.auto.tfvars",
            ignore,
        )

    def test_bootstrap_runbook_documents_the_only_manual_google_setup(self):
        runbook = (settings.BASE_DIR / "docs" / "google-identity-bootstrap.md").read_text()
        normalized_runbook = " ".join(runbook.lower().split())

        self.assertIn("Google Auth Platform", runbook)
        self.assertIn("Internal", runbook)
        self.assertIn("never place the client secret in terraform", runbook.lower())
        self.assertIn("Developers do not need", runbook)
        self.assertIn(
            "https://prr.psd401.net/api/staff/auth/oidc/callback/",
            runbook,
        )
        self.assertIn("Groups Reader", runbook)
        self.assertIn("Assign service accounts", runbook)
        self.assertNotIn("Add it as a direct group member", runbook)
        self.assertIn("tsd-engineering@psd401.net", runbook)
        self.assertIn("roles in production", runbook.lower())
        self.assertIn("does not enforce group separation", runbook.lower())
        self.assertIn("explicitly mapped to all five roles", runbook.lower())
        self.assertIn("production is the only environment", normalized_runbook)
        self.assertIn("never add it back", normalized_runbook)

    def test_identity_decision_records_direct_google_and_developer_constraints(self):
        decision = (
            settings.BASE_DIR / "docs" / "decisions" / "0001-google-workspace-identity.md"
        ).read_text()
        normalized_decision = " ".join(decision.lower().split())

        self.assertIn("Direct Google Workspace OIDC", decision)
        self.assertIn("Cognito", decision)
        self.assertIn("Developers", decision)
        self.assertIn(
            "Status: Accepted; deployment topology superseded by ADR 0002",
            decision,
        )
        self.assertIn("legacy inventory candidate", normalized_decision)
