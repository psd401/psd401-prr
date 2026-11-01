# ABOUTME: Tests for the settings needed to serve the SPA/static assets
# ABOUTME: under the multi-user Docker deployment (whitenoise wiring).
import json
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase, TestCase


class WhitenoiseSettingsTests(TestCase):
    def test_whitenoise_middleware_immediately_after_security_middleware(self):
        security_index = settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
        self.assertEqual(
            settings.MIDDLEWARE[security_index + 1],
            "whitenoise.middleware.WhiteNoiseMiddleware",
        )

    def test_staticfiles_storage_is_whitenoise_compressed(self):
        self.assertEqual(
            settings.STORAGES["staticfiles"]["BACKEND"],
            "whitenoise.storage.CompressedStaticFilesStorage",
        )


class DeploymentReadinessTests(SimpleTestCase):
    @staticmethod
    def _service_block(compose_text, service_name):
        lines = compose_text.splitlines()
        start = lines.index(f"  {service_name}:") + 1
        end = next(
            (
                index
                for index in range(start, len(lines))
                if lines[index].startswith("  ")
                and not lines[index].startswith("    ")
                and lines[index].strip()
            ),
            len(lines),
        )
        return "\n".join(lines[start:end])

    def test_every_django_process_gets_the_advisory_model_with_the_switch(self):
        """A service given the AI switch without the model cannot start.

        ``PRR_AI_ADVISORY_ENABLED`` lives in the operator's env file and so
        reaches every service, while the model identifier is set per service
        in the overlays. A service with the switch and no model raises
        ImproperlyConfigured at settings import and crash-loops.

        The service list is DERIVED, not written down. Naming the services by
        hand is what let this ship twice: first ``beat`` was missed and took
        the statutory scheduler down, then ``migrate`` was missed and broke
        every deployment at the migration step. Anything running the release
        image loads Django settings, so that is the question this asks.
        """
        import yaml

        deploy = yaml.safe_load(
            (Path(settings.BASE_DIR) / "docker-compose.deploy.yml").read_text(
                encoding="utf-8"
            )
        )
        django_services = {
            name
            for name, service in deploy["services"].items()
            if "PRR_RELEASE_IMAGE" in str(service.get("image", ""))
        }
        self.assertTrue(
            django_services,
            "No service was found running the release image; this test would "
            "silently assert nothing.",
        )

        for overlay_name in ("docker-compose.aws.yml",):
            overlay = yaml.safe_load(
                (Path(settings.BASE_DIR) / overlay_name).read_text(encoding="utf-8")
            )
            for service_name in sorted(django_services):
                service = overlay["services"].get(service_name)
                if service is None:
                    # An overlay need not define every service. What it must
                    # not do is define one and omit the model.
                    continue
                environment = service.get("environment") or {}
                with self.subTest(overlay=overlay_name, service=service_name):
                    for required in (
                        "PRR_AI_MODEL",
                        "PRR_AI_PROVIDER",
                        "PRR_AI_AWS_REGION",
                    ):
                        self.assertIn(
                            required,
                            environment,
                            f"{service_name} runs the release image and so loads "
                            f"Django settings; without {required} the advisory "
                            "switch crashes it at import.",
                        )

    def test_worker_and_beat_wait_for_post_entrypoint_web_readiness(self):
        compose_text = (
            Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        ).read_text()
        web = self._service_block(compose_text, "web")

        self.assertIn("socket.create_connection", web)
        self.assertIn("127.0.0.1", web)
        self.assertIn("8000", web)
        self.assertNotIn("connection.ensure_connection", web)
        for service_name in ("worker", "beat"):
            service = self._service_block(compose_text, service_name)
            self.assertIn("web:", service)
            self.assertIn("condition: service_healthy", service)

    def test_deploy_services_pin_production_security_mode(self):
        compose_text = (
            Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        ).read_text()

        for service_name in ("web", "worker", "beat"):
            service = self._service_block(compose_text, service_name)
            self.assertIn('PRR_DEPLOYMENT_MODE: "production"', service)
            self.assertIn('DJANGO_DEBUG: "false"', service)

    def test_every_application_process_requires_one_immutable_release_image(self):
        compose_path = Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        compose = yaml.safe_load(compose_path.read_text())

        for service_name in ("migrate", "web", "worker", "beat"):
            service = compose["services"][service_name]
            self.assertEqual(
                service["image"],
                "${PRR_RELEASE_IMAGE:?Set PRR_RELEASE_IMAGE to the exact reviewed release tag}",
                service_name,
            )
            self.assertNotIn("build", service, service_name)

        migrate_environment = compose["services"]["migrate"]["environment"]
        self.assertEqual(
            migrate_environment["RUN_MIGRATIONS"],
            "${PRR_RUN_MIGRATIONS:-false}",
        )
        self.assertEqual(
            migrate_environment["AUTO_SEED"],
            "${PRR_AUTO_SEED:-false}",
        )

    def test_production_runtime_example_is_mutation_free_by_default(self):
        runtime_example = (
            Path(settings.BASE_DIR) / ".env.app.example"
        ).read_text(encoding="utf-8")

        self.assertIn("RUN_MIGRATIONS=false\n", runtime_example)
        self.assertIn("AUTO_SEED=false\n", runtime_example)
        for bootstrap_name in (
            "DJANGO_SUPERUSER_USERNAME",
            "DJANGO_SUPERUSER_EMAIL",
            "DJANGO_SUPERUSER_PASSWORD",
        ):
            self.assertNotIn(bootstrap_name, runtime_example)

    def test_release_image_records_the_exact_merged_commit(self):
        dockerfile = (Path(settings.BASE_DIR) / "Dockerfile").read_text()

        self.assertIn("ARG PRR_RELEASE_SHA", dockerfile)
        self.assertIn(
            "LABEL org.opencontainers.image.revision=$PRR_RELEASE_SHA",
            dockerfile,
        )

        workflow = yaml.safe_load(
            (Path(settings.BASE_DIR) / ".github" / "workflows" / "psd-ci-jobs.yml").read_text()
        )
        self.assertEqual(
            workflow["jobs"]["deployment-compose"]["env"]["PRR_RELEASE_IMAGE"],
            "psd401-prr:ci",
        )

    def test_production_pinned_compose_is_not_advertised_as_local_runtime(self):
        root = Path(settings.BASE_DIR)
        unsafe_local_command = (
            "docker compose -f docker-compose.deploy.yml "
            "-f docker-compose.minio.yml up -d --build"
        )

        for path in (
            root / "README.md",
            root / "AGENTS.md",
            root / "docker-compose.minio.yml",
        ):
            self.assertNotIn(unsafe_local_command, path.read_text(), str(path))

        self.assertIn(
            "PRR_ENVIRONMENT=production",
            (root / ".env.app.example").read_text(),
        )
        self.assertIn(
            "configuration validation only",
            (root / "docker-compose.minio.yml").read_text(),
        )

    def test_deployed_services_receive_the_versioned_public_oidc_client_id(self):
        compose_text = (
            Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        ).read_text()

        self.assertIn(
            "PRR_GOOGLE_OIDC_CLIENT_ID: "
            "996556698486-2bo54rh7c0r2go178fssfmagcgpqnnvr"
            ".apps.googleusercontent.com",
            compose_text,
        )
        for service_name in ("migrate", "web", "worker", "beat"):
            service = self._service_block(compose_text, service_name)
            self.assertIn("*google-identity", service, service_name)

    def test_vault_operator_is_not_named_in_versioned_deployment_config(self):
        # The Vault administrator is an in-app setting; a named person's
        # account in a tracked file would be the fallback whenever the
        # store is empty, and it is public once the repository is.
        compose_text = (
            Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        ).read_text()

        self.assertNotIn("PRR_VAULT_ADMIN_USER", compose_text)
        self.assertNotRegex(compose_text, r"[\w.+-]+@psd401\.net")

    def test_vault_subject_accounts_are_described_by_role_in_docs(self):
        # The live value is visible in Configuration > District settings;
        # the public runbook names the role, not the account.
        handoffs = (
            Path(settings.BASE_DIR) / "docs" / "phase-2-production-handoffs.md"
        ).read_text()

        self.assertIn("Vault administrator account", handoffs)
        self.assertNotRegex(handoffs, r"serv_prr|hagelk")
        self.assertNotRegex(handoffs, r"[\w.+-]+@psd401\.net")

    def test_gmail_keyless_runtime_configuration_reaches_every_process(self):
        root = Path(settings.BASE_DIR)
        compose_text = (root / "docker-compose.deploy.yml").read_text()
        app_example = (root / ".env.app.example").read_text()

        # The container's own keyless identity is the one Google fact that
        # still has to be on the host: it is what the application
        # authenticates AS before it can impersonate anything, so it cannot
        # be read out of a database the application has not connected to yet.
        # Which service accounts it impersonates -- the Gmail and Vault
        # identities -- are in-app settings since Phase C, and a line for
        # either here would read as a control that silently does nothing
        # once the deployment has imported its values.
        self.assertIn(
            "GOOGLE_APPLICATION_CREDENTIALS: "
            "/run/secrets/prr-google-runtime-credential.json",
            compose_text,
        )
        self.assertIn(
            "GOOGLE_APPLICATION_CREDENTIALS="
            "/run/secrets/prr-google-runtime-credential.json",
            app_example,
        )
        for variable in (
            "PRR_GMAIL_MAILBOX",
            "PRR_GMAIL_TIMEOUT_SECONDS",
            "PRR_GMAIL_IMPERSONATION_TARGET",
            "PRR_GMAIL_CREDENTIALS_FILE",
        ):
            self.assertNotIn(f"{variable}=", app_example)
        for service_name in ("web", "worker", "beat"):
            service = self._service_block(compose_text, service_name)
            self.assertIn("env_file: .env.app", service, service_name)

        aws_overlay = (root / "docker-compose.aws.yml").read_text()
        for service_name in ("web", "worker", "beat"):
            service = self._service_block(aws_overlay, service_name)
            self.assertIn("env_file: .env.app", service, service_name)
            self.assertIn("volumes: *secrets-mount", service, service_name)
        # Every application-managed setting, DERIVED from the registry rather
        # than listed here, because a list is what lets the next phase's keys
        # quietly stay behind. An operator's runtime file no longer decides
        # any of them, so neither example may carry a line for one: it would
        # read as a control that silently does nothing once the deployment
        # has imported its values.
        from apps.configuration.registry import SETTING_KEYS

        local_example = (root / ".env.example").read_text()
        for variable in SETTING_KEYS:
            self.assertNotIn(f"{variable}=", app_example, variable)
            self.assertNotIn(f"{variable}=", local_example, variable)
        for compose_name in (
            "docker-compose.deploy.yml",
            "docker-compose.aws.yml",
        ):
            parsed_compose = yaml.safe_load((root / compose_name).read_text())
            for service_name in ("web", "worker", "beat"):
                service_environment = parsed_compose["services"][service_name][
                    "environment"
                ]
                self.assertNotIn(
                    "PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS",
                    service_environment,
                    f"{compose_name}:{service_name}",
                )

    def test_deployed_advisory_uses_keyless_us_bedrock_inference(self):
        root = Path(settings.BASE_DIR)
        app_example = (root / ".env.app.example").read_text()
        local_example = (root / ".env.example").read_text()
        advisory_docs = (root / "docs/advisory-ai.md").read_text()
        for overlay_name in ("docker-compose.aws.yml",):
            overlay = (root / overlay_name).read_text()
            parsed_overlay = yaml.safe_load(overlay)
            self.assertIn('PRR_AI_PROVIDER: "bedrock"', overlay)
            self.assertIn('PRR_AI_AWS_REGION: "us-west-2"', overlay)
            self.assertIn(
                'PRR_AI_MODEL: "us.amazon.nova-2-lite-v1:0"',
                overlay,
            )
            for service_name in ("web", "worker"):
                service = self._service_block(overlay, service_name)
                self.assertIn("*advisory-ai", service, service_name)
                service_environment = parsed_overlay["services"][service_name][
                    "environment"
                ]
                self.assertNotIn(
                    "PRR_AI_ADVISORY_ENABLED", service_environment, service_name
                )
                self.assertNotIn(
                    "PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED",
                    service_environment,
                    service_name,
                )
        # The three advisory switches are application-managed, and since
        # Phase B so are the provider, region, and model. Neither example
        # file lists any of them: a line there would read as a control that
        # silently does nothing once the deployment has imported. The
        # overlays keep them as the one-time bootstrap seed, which is what
        # the assertions above check.
        for variable in (
            "PRR_AI_ADVISORY_ENABLED",
            "PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED",
            "PRR_AI_PROVIDER",
            "PRR_AI_AWS_REGION",
            "PRR_AI_MODEL",
        ):
            self.assertNotIn(f"{variable}=", app_example, variable)
            self.assertNotIn(f"{variable}=", local_example, variable)
        self.assertIn("us.amazon.nova-2-lite-v1:0", advisory_docs)
        for content in (app_example, local_example, advisory_docs):
            self.assertNotIn("PRR_AI_API_KEY", content)
            self.assertNotIn("PRR_AI_BASE_URL", content)
        self.assertIn("PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED", advisory_docs)
        normalized_advisory_docs = " ".join(advisory_docs.lower().split())
        self.assertNotIn(
            "enabled explicitly by the two deployed-environment overlays",
            normalized_advisory_docs,
        )
        self.assertIn(
            "compose overlays deliberately do not override these switches",
            normalized_advisory_docs,
        )

    def test_retired_dev_host_has_no_deployment_or_identity_artifacts(self):
        """Production is the only environment (ADR 0002).

        The district-hosted dev box was retired but kept a live AWS IAM user,
        a role Google federated to the production Gmail and Vault service
        accounts, and its deployment files. None of it may come back.
        """
        root = Path(settings.BASE_DIR)
        for retired in (
            "infra/onprem",
            "docs/onprem-dev.md",
            "docker-compose.onprem.yml",
            "docker/Caddyfile.onprem",
            "infra/cdk/lib/onprem-identity-stack.ts",
        ):
            self.assertFalse((root / retired).exists(), retired)

        cdk_app = (root / "infra/cdk/bin/prr.ts").read_text()
        self.assertNotIn("dev-identity", cdk_app)
        self.assertNotIn("OnPrem", cdk_app)

        google_identity = root / "infra/terraform/google-identity"
        for name in ("identity.auto.tfvars", "main.tf", "outputs.tf"):
            text = (google_identity / name).read_text()
            self.assertNotIn("psd401-prr-dev-runtime", text, name)
            self.assertNotIn("dev-prr.psd401.net", text, name)

    def test_google_identity_terraform_provisions_keyless_transport_identities(self):
        root = Path(settings.BASE_DIR) / "infra" / "terraform" / "google-identity"
        main = (root / "main.tf").read_text()
        outputs = (root / "outputs.tf").read_text()
        combined = f"{main}\n{outputs}"

        self.assertIn('resource "google_service_account" "gmail"', main)
        self.assertIn('account_id   = "prr-gmail"', main)
        self.assertIn(
            'resource "google_service_account_iam_member" "aws_runtime_gmail"',
            main,
        )
        self.assertIn(
            "service_account_id = google_service_account.gmail.name",
            main,
        )
        self.assertIn('role               = "roles/iam.workloadIdentityUser"', main)
        self.assertIn(
            'resource "google_service_account_iam_member" '
            '"aws_runtime_gmail_token_creator"',
            main,
        )
        self.assertIn(
            'role               = "roles/iam.serviceAccountTokenCreator"',
            main,
        )
        self.assertIn('output "gmail_service_account"', outputs)
        self.assertIn('output "gmail_service_account_unique_id"', outputs)
        self.assertIn('resource "google_service_account" "vault"', main)
        self.assertIn('account_id   = "prr-vault"', main)
        self.assertIn(
            'resource "google_service_account_iam_member" "aws_runtime_vault"',
            main,
        )
        self.assertIn(
            'resource "google_service_account_iam_member" '
            '"aws_runtime_vault_token_creator"',
            main,
        )
        self.assertIn('output "vault_service_account"', outputs)
        self.assertIn('output "vault_service_account_unique_id"', outputs)
        self.assertEqual(main.count("roles/iam.serviceAccountTokenCreator"), 2)
        self.assertNotIn("google_service_account_key", combined)

    def test_gmail_operator_docs_pin_scopes_and_independent_kill_switches(self):
        root = Path(settings.BASE_DIR)
        bootstrap = (root / "docs" / "google-identity-bootstrap.md").read_text()
        transport = (root / "docs" / "gmail-transport.md").read_text()

        for scope in (
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ):
            self.assertIn(scope, bootstrap)
        self.assertIn("domain-wide", bootstrap.lower())
        self.assertIn("PRR_CORRESPONDENCE_OUTBOUND_ENABLED", transport)
        self.assertIn("PRR_GMAIL_INBOUND_POLL_ENABLED", transport)
        self.assertIn("PRR_GMAIL_ENABLED", transport)
        self.assertIn("Staff-alert acceptance and disabled coverage", transport)
        self.assertIn("staff_notification.sent", transport)
        self.assertIn("named Records Officer", transport)
        self.assertIn("manual coverage", transport)
        self.assertIn("district-controlled synthetic request", transport)

    def test_application_services_never_load_the_bootstrap_env_file(self):
        compose_text = (
            Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        ).read_text()

        for service_name in ("web", "worker", "beat"):
            service = self._service_block(compose_text, service_name)
            self.assertIn("env_file: .env.app", service)
            self.assertNotIn("env_file: .env\n", service)
            self.assertNotIn("MINIO_ROOT_", service)
            self.assertNotIn("POSTGRES_PASSWORD:", service)
            self.assertIn("prr_app", service)

    def test_store_overlay_supplies_the_scoped_object_store_identity(self):
        # The object-store settings live in the store overlay, not the base
        # file, so each deployment gets the store it actually has. The scoped
        # identity is still the only one an application service ever receives.
        overlay = (
            Path(settings.BASE_DIR) / "docker-compose.minio.yml"
        ).read_text()

        self.assertIn("OBJECT_STORE_ACCESS_KEY: ${MINIO_APP_USER:-prr-app}", overlay)
        self.assertIn("OBJECT_STORE_SECRET_KEY: ${MINIO_APP_PASSWORD", overlay)
        self.assertNotIn("OBJECT_STORE_SECRET_KEY: ${MINIO_ROOT_PASSWORD", overlay)

        for service_name in ("migrate", "web", "worker", "beat"):
            service = self._service_block(overlay, service_name)
            self.assertIn("environment: *object-store", service)

    def test_minio_identity_can_create_but_not_delete_retained_artifacts(self):
        policy = json.loads(
            (Path(settings.BASE_DIR) / "docker" / "minio-prr-records-policy.json")
            .read_text()
        )
        statements = policy["Statement"]
        readable_writable = next(
            statement
            for statement in statements
            if "s3:GetObject" in statement["Action"]
        )
        retained = {
            "arn:aws:s3:::prr-records/records/*",
            "arn:aws:s3:::prr-records/productions/*",
            "arn:aws:s3:::prr-records/exemption-logs/*",
            "arn:aws:s3:::prr-records/vault-exports/*",
            "arn:aws:s3:::prr-records/vault-messages/*",
        }
        self.assertTrue(retained.issubset(set(readable_writable["Resource"])))
        deletable = {
            resource
            for statement in statements
            if "s3:DeleteObject" in statement["Action"]
            for resource in statement["Resource"]
        }
        self.assertTrue(retained.isdisjoint(deletable))
        overlay = (Path(settings.BASE_DIR) / "docker-compose.minio.yml").read_text()
        self.assertIn("mc version enable local/prr-records", overlay)

    def test_base_stack_requires_no_object_store_credential(self):
        # Compose interpolates the base file before merging an overlay, so a
        # required MinIO variable here would stop the AWS host, which has no
        # MinIO at all, from starting.
        compose_text = (
            Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        ).read_text()
        # Comments explain the rule; only what Compose interpolates breaks it.
        directives = "\n".join(
            line
            for line in compose_text.splitlines()
            if not line.lstrip().startswith("#")
        )

        self.assertNotIn("MINIO_", directives)
        self.assertNotIn("OBJECT_STORE_", directives)

    def test_aws_overlay_reaches_s3_through_the_instance_role(self):
        overlay = (Path(settings.BASE_DIR) / "docker-compose.aws.yml").read_text()

        # Empty endpoint and empty keys are what send boto3 to S3 with the
        # instance role. A populated key here would be a credential on disk.
        self.assertIn('OBJECT_STORE_ENDPOINT: ""', overlay)
        self.assertIn('OBJECT_STORE_ACCESS_KEY: ""', overlay)
        self.assertIn('OBJECT_STORE_SECRET_KEY: ""', overlay)
        self.assertNotIn("MINIO_", overlay)

        for service_name in ("migrate", "web", "worker", "beat"):
            service = self._service_block(overlay, service_name)
            self.assertIn("app-runtime", service)

    def test_aws_overlay_no_longer_names_a_secrets_manager_secret(self):
        """The sign-in secret is a stored setting; the overlay only still
        blanks any legacy inline value so nothing stale can be imported."""
        overlay = (Path(settings.BASE_DIR) / "docker-compose.aws.yml").read_text()

        self.assertNotIn("PRR_GOOGLE_OIDC_CLIENT_SECRET_ID", overlay)
        self.assertNotIn("google-oidc-client-secret", overlay)
        self.assertIn('PRR_GOOGLE_OIDC_CLIENT_SECRET: ""', overlay)
        for service_name in ("migrate", "web", "worker", "beat"):
            service = self._service_block(overlay, service_name)
            self.assertIn("*app-runtime", service, service_name)

    def test_aws_runtime_reads_its_signing_keys_from_secrets_manager(self):
        """standards/04 rule 15a: every Django process on the AWS host loads
        DJANGO_SECRET_KEY and PRR_SETTINGS_ENCRYPTION_KEY from the runtime
        secret through the instance role (config/runtime_secrets.py)."""
        root = Path(settings.BASE_DIR)
        overlay = yaml.safe_load((root / "docker-compose.aws.yml").read_text())

        self.assertEqual(
            overlay["x-app-runtime"]["PRR_RUNTIME_SECRET_ID"],
            "psd401-prr/prod/runtime",
        )
        for service_name in ("migrate", "web", "worker", "beat"):
            environment = overlay["services"][service_name]["environment"]
            with self.subTest(service=service_name):
                self.assertEqual(
                    environment["PRR_RUNTIME_SECRET_ID"], "psd401-prr/prod/runtime"
                )
        # Only the AWS host reads it; local and MinIO stacks keep their env.
        self.assertNotIn(
            "PRR_RUNTIME_SECRET_ID", (root / "docker-compose.deploy.yml").read_text()
        )

    def test_aws_runbook_never_prints_the_oidc_secret(self):
        runbook = (Path(settings.BASE_DIR) / "docs" / "aws-deploy.md").read_text()

        self.assertNotIn(
            "aws secretsmanager get-secret-value "
            "--secret-id psd401-prr/prod/google-oidc-client-secret "
            "--query SecretString --output text",
            runbook,
        )

    def test_app_env_template_carries_no_signing_or_settings_key(self):
        """The AWS host reads both keys from Secrets Manager and refuses an
        inline copy, so the template an operator copies must not offer one."""
        example = (Path(settings.BASE_DIR) / ".env.app.example").read_text()
        active = [
            line.split("=", 1)[0]
            for line in example.splitlines()
            if line and not line.lstrip().startswith("#") and "=" in line
        ]

        self.assertNotIn("DJANGO_SECRET_KEY", active)
        self.assertNotIn("PRR_SETTINGS_ENCRYPTION_KEY", active)
        self.assertIn("psd401-prr/prod/runtime", example)

    def test_runtime_secret_runbook_never_reads_the_secret_into_a_terminal(self):
        """The seed writes the runtime secret; nothing in the runbook reads it
        back, and the stack must never generate one (it would replace the key
        the settings store is encrypted under)."""
        runbook = (Path(settings.BASE_DIR) / "docs" / "aws-deploy.md").read_text()

        self.assertIn("psd401-prr/prod/runtime", runbook)
        self.assertNotIn("get-secret-value", runbook)
        self.assertNotIn("--generate-secret", runbook)
        self.assertIn("verify_settings_secrets", runbook)
        # Operator credentials pasted on the host must never reach root's
        # shell history (review of #337).
        self.assertIn("set +o history", runbook)
        self.assertLess(
            runbook.index("set +o history"), runbook.index("export AWS_DEFAULT_REGION")
        )

    def test_recurring_deployment_is_one_direct_cdk_command(self):
        root = Path(settings.BASE_DIR)
        runbook = (root / "docs" / "aws-deploy.md").read_text()
        command = (
            "bunx cdk deploy psd401-prr-prod --require-approval never "
            "--profile psd401-prr-prod"
        )

        self.assertEqual(runbook.count(command), 1)
        obsolete_names = (
            "production_deploy_cli.py",
            "production_release_gate.py",
            "production_release_artifact.py",
            "production_release_seal.py",
            "PRR-CLAUDE-RELEASE-SEAL",
            "PRR-RELEASE-VERDICT",
            "adopt-baseline",
            "release publisher",
            "release seal",
        )
        for obsolete in obsolete_names:
            self.assertNotIn(obsolete, runbook)

        # Hand-written instructions only. openwiki/ is regenerated by a
        # scheduled LLM workflow, so asserting on its wording makes an
        # unrelated regeneration fail the backend suite.
        for relative_path in (
            "AGENTS.md",
            "README.md",
            "docs/decisions/0002-production-only-deployments.md",
            "docs/ux/casework-rollout-readiness.md",
        ):
            source = (root / relative_path).read_text()
            with self.subTest(instruction=relative_path):
                self.assertIn(command, source)
                for obsolete in obsolete_names:
                    self.assertNotIn(obsolete, source)

        for relative_path in (
            ".github/workflows/claude-release-seal.yml",
            ".github/workflows/publish-production-release.yml",
            "infra/aws/production_deploy.py",
            "infra/aws/production_deploy_cli.py",
            "infra/aws/production_release_artifact.py",
            "infra/aws/production_release_gate.py",
            "infra/aws/production_release_seal.py",
            "infra/cdk/lib/production-release-transport.ts",
            "apps/api/tests/test_production_deploy_cli.py",
            "apps/api/tests/test_production_deployer.py",
            "apps/api/tests/test_production_release_artifact.py",
            "apps/api/tests/test_production_release_gate.py",
            "apps/api/tests/test_production_release_seal.py",
        ):
            with self.subTest(obsolete_path=relative_path):
                self.assertFalse((root / relative_path).exists())

        stack = (root / "infra/cdk/lib/prr-hosting-stack.ts").read_text()
        self.assertNotIn("productionReleaseCommand", stack)
        self.assertNotIn("production-release-transport", stack)

    def test_schema_migration_isolated_to_one_shot_owner_service(self):
        compose_text = (
            Path(settings.BASE_DIR) / "docker-compose.deploy.yml"
        ).read_text()
        database = self._service_block(compose_text, "db")
        migration = self._service_block(compose_text, "migrate")
        web = self._service_block(compose_text, "web")

        # Preserve the original database owner so existing deployment volumes
        # remain upgradeable; only long-running services switch to prr_app.
        self.assertIn("POSTGRES_USER: prr", database)
        self.assertIn('"postgres://prr:', migration)
        self.assertIn('"true"', migration)
        self.assertIn('command: ["true"]', migration)
        self.assertIn("migrate:", web)
        self.assertIn("condition: service_completed_successfully", web)
        self.assertNotIn('"postgres://prr:', web)
        self.assertNotIn('RUN_MIGRATIONS: "true"', web)

    def test_infrastructure_bootstrap_creates_scoped_app_identities(self):
        root = Path(settings.BASE_DIR)
        compose_text = (root / "docker-compose.deploy.yml").read_text()
        database = self._service_block(compose_text, "db")
        # The store's bootstrap lives with the store it bootstraps.
        minio_init = self._service_block(
            (root / "docker-compose.minio.yml").read_text(), "minio-init"
        )
        app_example = (root / ".env.app.example").read_text()
        database_init = (
            root / "docker" / "postgres-init" / "001-create-app-user.sh"
        ).read_text()
        object_policy = (
            root / "docker" / "minio-prr-records-policy.json"
        ).read_text()

        self.assertIn("POSTGRES_APP_USER", database)
        self.assertIn("POSTGRES_APP_PASSWORD", database)
        self.assertIn("docker-entrypoint-initdb.d", database)
        self.assertIn("ALTER DEFAULT PRIVILEGES", database_init)
        self.assertIn(
            r"\getenv owner_user POSTGRES_USER",
            database_init,
        )
        self.assertIn("POSTGRES_APP_USER", database_init)
        self.assertIn(
            r"\getenv app_password POSTGRES_APP_PASSWORD",
            database_init,
        )
        self.assertNotIn("--set=app_password", database_init)
        self.assertIn("mc admin user add", minio_init)
        self.assertIn("mc admin policy attach", minio_init)
        self.assertIn("prr-records", object_policy)
        self.assertNotIn("MINIO_ROOT_", app_example)
        self.assertNotIn("POSTGRES_PASSWORD", app_example)

    def test_minio_policy_matches_private_object_operations(self):
        policy = json.loads(
            (
                Path(settings.BASE_DIR)
                / "docker"
                / "minio-prr-records-policy.json"
            ).read_text()
        )
        expected_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetBucketVersioning"],
                    "Resource": ["arn:aws:s3:::prr-records"],
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucketVersions"],
                    "Resource": ["arn:aws:s3:::prr-records"],
                    "Condition": {
                        "StringLike": {
                            "s3:prefix": [
                                "quarantine/intake/*",
                                "quarantine/correspondence/raw/*",
                                "quarantine/correspondence/attachments/*",
                                "quarantine/vault-messages/*",
                            ]
                        }
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject"],
                    "Resource": [
                        "arn:aws:s3:::prr-records/quarantine/intake/*",
                        (
                            "arn:aws:s3:::prr-records/"
                            "quarantine/correspondence/raw/*"
                        ),
                        (
                            "arn:aws:s3:::prr-records/"
                            "quarantine/correspondence/attachments/*"
                        ),
                        (
                            "arn:aws:s3:::prr-records/"
                            "quarantine/vault-messages/*"
                        ),
                        "arn:aws:s3:::prr-records/intake/*",
                        (
                            "arn:aws:s3:::prr-records/"
                            "correspondence/attachments/*"
                        ),
                        "arn:aws:s3:::prr-records/records/*",
                        "arn:aws:s3:::prr-records/productions/*",
                        "arn:aws:s3:::prr-records/exemption-logs/*",
                        "arn:aws:s3:::prr-records/vault-exports/*",
                        "arn:aws:s3:::prr-records/vault-messages/*",
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:DeleteObject",
                        "s3:DeleteObjectVersion",
                    ],
                    "Resource": [
                        "arn:aws:s3:::prr-records/quarantine/intake/*",
                        (
                            "arn:aws:s3:::prr-records/"
                            "quarantine/correspondence/raw/*"
                        ),
                        (
                            "arn:aws:s3:::prr-records/"
                            "quarantine/correspondence/attachments/*"
                        ),
                        (
                            "arn:aws:s3:::prr-records/"
                            "quarantine/vault-messages/*"
                        ),
                    ],
                },
            ],
        }

        def normalize(value):
            if isinstance(value, dict):
                return {
                    key: normalize(item)
                    for key, item in sorted(value.items())
                }
            if isinstance(value, list):
                normalized = [normalize(item) for item in value]
                return sorted(
                    normalized,
                    key=lambda item: json.dumps(item, sort_keys=True),
                )
            return value

        self.assertEqual(normalize(policy), normalize(expected_policy))

    def test_minio_init_reconciles_the_named_policy_before_attachment(self):
        overlay = (
            Path(settings.BASE_DIR) / "docker-compose.minio.yml"
        ).read_text()
        minio_init = self._service_block(overlay, "minio-init")
        replace_policy = (
            "mc admin policy create local prr-records-app "
            "/policies/prr-records.json"
        )
        attach_policy = (
            "mc admin policy attach local prr-records-app "
            '--user "$$MINIO_APP_USER"'
        )

        self.assertIn(replace_policy, minio_init)
        self.assertNotIn("mc admin policy info", minio_init)
        replace_policy_end = minio_init.index(replace_policy) + len(
            replace_policy
        )
        attach_policy_start = minio_init.index(attach_policy)
        self.assertEqual(
            minio_init[replace_policy_end:attach_policy_start].strip(),
            "&&",
        )
