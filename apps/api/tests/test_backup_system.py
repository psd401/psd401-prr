# ABOUTME: Contract tests for app-layer backups, integrity evidence, and restore drills.
# ABOUTME: Guards append-only S3 writes and production-safe operational wiring.
import copy
import gzip
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

IMAGE_ID = f"sha256:{'a' * 64}"


def valid_manifest():
    return {
        "audit_export": {"row_count": 12, "max_id": 19},
        "backup_id": "11111111-2222-4333-8444-555555555555",
        "bucket": "psd401-prr-backups-prod",
        "created_at": "2026-08-06T03:00:00Z",
        "environment": "prod",
        "files": {
            "database.dump": {
                "bytes": 1,
                "content_type": "application/vnd.postgresql.custom",
                "sha256": "1" * 64,
            },
            "audit.jsonl.gz": {
                "bytes": 1,
                "content_type": "application/gzip",
                "sha256": "2" * 64,
            },
        },
        "image_id": IMAGE_ID,
        "s3_prefix": (
            "app-backups/2026/08/06/11111111-2222-4333-8444-555555555555"
        ),
        "schema_version": 1,
    }


def load_manifest_module():
    path = Path(settings.BASE_DIR) / "infra" / "aws" / "backup_manifest.py"
    spec = importlib.util.spec_from_file_location("prr_backup_manifest", path)
    if spec is None or spec.loader is None:
        raise AssertionError("backup manifest module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackupManifestTests(SimpleTestCase):
    def _artifacts(self, directory: Path):
        database = directory / "database.dump"
        database.write_bytes(b"postgres-custom-format")
        audit = directory / "audit.jsonl.gz"
        with gzip.open(audit, "wt", encoding="utf-8", newline="\n") as stream:
            stream.write('{"id":4,"action":"request.created"}\n')
            stream.write('{"id":9,"action":"request.received"}\n')
        return database, audit

    def test_creates_and_verifies_an_integrity_manifest(self):
        module = load_manifest_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database, audit = self._artifacts(directory)
            manifest_path = directory / "manifest.json"

            manifest = module.create_manifest(
                database_path=database,
                audit_path=audit,
                output_path=manifest_path,
                backup_id="11111111-2222-4333-8444-555555555555",
                created_at="2026-08-06T03:00:00Z",
                environment="prod",
                image_id=IMAGE_ID,
                bucket="psd401-prr-backups-prod",
            )

            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["audit_export"]["row_count"], 2)
            self.assertEqual(manifest["audit_export"]["max_id"], 9)
            self.assertEqual(
                manifest["s3_prefix"],
                "app-backups/2026/08/06/11111111-2222-4333-8444-555555555555",
            )
            self.assertEqual(
                module.verify_manifest(manifest_path, directory), manifest
            )

    def test_verification_rejects_tampered_artifacts(self):
        module = load_manifest_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database, audit = self._artifacts(directory)
            manifest_path = directory / "manifest.json"
            module.create_manifest(
                database_path=database,
                audit_path=audit,
                output_path=manifest_path,
                backup_id="11111111-2222-4333-8444-555555555555",
                created_at="2026-08-06T03:00:00Z",
                environment="prod",
                image_id=IMAGE_ID,
                bucket="psd401-prr-backups-prod",
            )
            database.write_bytes(b"postgres-custom-formax")

            with self.assertRaisesRegex(ValueError, "database.dump SHA-256"):
                module.verify_manifest(manifest_path, directory)

    def test_image_id_must_be_a_canonical_lowercase_sha256(self):
        module = load_manifest_module()
        invalid_image_ids = (
            "sha256:abc123",
            f"sha256:{'A' * 64}",
            f"sha256:{'g' * 64}",
            "a" * 64,
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database, audit = self._artifacts(directory)
            for image_id in invalid_image_ids:
                with self.subTest(image_id=image_id), self.assertRaisesRegex(
                    ValueError, "canonical SHA-256"
                ):
                    module.create_manifest(
                        database_path=database,
                        audit_path=audit,
                        output_path=directory / "manifest.json",
                        backup_id="11111111-2222-4333-8444-555555555555",
                        created_at="2026-08-06T03:00:00Z",
                        environment="prod",
                        image_id=image_id,
                        bucket="psd401-prr-backups-prod",
                    )

    def test_verifier_accepts_only_the_installed_legacy_bare_image_digest(self):
        module = load_manifest_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database, audit = self._artifacts(directory)
            manifest_path = directory / "manifest.json"
            manifest = module.create_manifest(
                database_path=database,
                audit_path=audit,
                output_path=manifest_path,
                backup_id="11111111-2222-4333-8444-555555555555",
                created_at="2026-08-06T03:00:00Z",
                environment="prod",
                image_id=IMAGE_ID,
                bucket="psd401-prr-backups-prod",
            )
            manifest["image_id"] = IMAGE_ID.removeprefix("sha256:")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            verified = module.verify_manifest(manifest_path, directory)

            self.assertEqual(verified["image_id"], manifest["image_id"])
            with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
                module.create_manifest(
                    database_path=database,
                    audit_path=audit,
                    output_path=manifest_path,
                    backup_id="11111111-2222-4333-8444-555555555555",
                    created_at="2026-08-06T03:00:00Z",
                    environment="prod",
                    image_id=manifest["image_id"],
                    bucket="psd401-prr-backups-prod",
                )

    def test_verification_requires_the_exact_manifest_schema(self):
        module = load_manifest_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database, audit = self._artifacts(directory)
            manifest_path = directory / "manifest.json"
            original = module.create_manifest(
                database_path=database,
                audit_path=audit,
                output_path=manifest_path,
                backup_id="11111111-2222-4333-8444-555555555555",
                created_at="2026-08-06T03:00:00Z",
                environment="prod",
                image_id=IMAGE_ID,
                bucket="psd401-prr-backups-prod",
            )

            def add_top_level_field(manifest):
                manifest["unexpected"] = "must be rejected"

            def remove_top_level_field(manifest):
                manifest.pop("image_id")

            def add_file_evidence_field(manifest):
                manifest["files"]["database.dump"]["etag"] = "not evidence"

            def make_schema_version_boolean(manifest):
                manifest["schema_version"] = True

            def make_image_noncanonical(manifest):
                manifest["image_id"] = "sha256:abc123"

            cases = (
                ("extra top-level field", add_top_level_field),
                ("missing top-level field", remove_top_level_field),
                ("extra file-evidence field", add_file_evidence_field),
                ("boolean schema version", make_schema_version_boolean),
                ("noncanonical image", make_image_noncanonical),
            )
            for label, mutate in cases:
                manifest = copy.deepcopy(original)
                mutate(manifest)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.subTest(label=label), self.assertRaises(ValueError):
                    module.verify_manifest(manifest_path, directory)

    def test_verification_rejects_boolean_manifest_counts_and_sizes(self):
        module = load_manifest_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "database.dump"
            database.write_bytes(b"x")
            audit = directory / "audit.jsonl.gz"
            with gzip.open(audit, "wt", encoding="utf-8", newline="\n") as stream:
                stream.write('{"id":1,"action":"request.created"}\n')
            manifest_path = directory / "manifest.json"
            original = module.create_manifest(
                database_path=database,
                audit_path=audit,
                output_path=manifest_path,
                backup_id="11111111-2222-4333-8444-555555555555",
                created_at="2026-08-06T03:00:00Z",
                environment="prod",
                image_id=IMAGE_ID,
                bucket="psd401-prr-backups-prod",
            )

            boolean_audit = copy.deepcopy(original)
            boolean_audit["audit_export"] = {"row_count": True, "max_id": True}
            manifest_path.write_text(json.dumps(boolean_audit), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "audit export boundary"):
                module.verify_manifest(manifest_path, directory)

            boolean_size = copy.deepcopy(original)
            boolean_size["files"]["database.dump"]["bytes"] = True
            manifest_path.write_text(json.dumps(boolean_size), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "database.dump evidence"):
                module.verify_manifest(manifest_path, directory)

    def test_verification_rejects_an_impossible_nonempty_audit_boundary(self):
        module = load_manifest_module()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database, audit = self._artifacts(directory)
            manifest_path = directory / "manifest.json"
            manifest = module.create_manifest(
                database_path=database,
                audit_path=audit,
                output_path=manifest_path,
                backup_id="11111111-2222-4333-8444-555555555555",
                created_at="2026-08-06T03:00:00Z",
                environment="prod",
                image_id=IMAGE_ID,
                bucket="psd401-prr-backups-prod",
            )
            manifest["audit_export"] = {"row_count": 2, "max_id": 1}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "audit export boundary is invalid"):
                module.verify_manifest(manifest_path, directory)

    def test_restore_evidence_must_contain_the_exported_audit_boundary(self):
        module = load_manifest_module()
        manifest = valid_manifest()

        module.verify_restored_audit(manifest, restored_count=13, restored_max_id=20)
        with self.assertRaisesRegex(ValueError, "fewer audit rows"):
            module.verify_restored_audit(manifest, restored_count=11, restored_max_id=20)
        with self.assertRaisesRegex(ValueError, "maximum audit ID"):
            module.verify_restored_audit(manifest, restored_count=13, restored_max_id=18)

    def test_restore_verification_rejects_boolean_counts(self):
        module = load_manifest_module()
        manifest = valid_manifest()

        with self.assertRaisesRegex(ValueError, "restore audit counts are invalid"):
            module.verify_restored_audit(
                manifest,
                restored_count=True,
                restored_max_id=1,
            )
        with self.assertRaisesRegex(ValueError, "restore audit counts are invalid"):
            module.verify_restored_audit(
                manifest,
                restored_count=1,
                restored_max_id=True,
            )

    def test_restore_verification_requires_exact_schema_and_identity(self):
        module = load_manifest_module()

        def make_partial(manifest):
            return {"audit_export": manifest["audit_export"]}

        def add_extra_field(manifest):
            manifest["unexpected"] = "must be rejected"
            return manifest

        def change_schema(manifest):
            manifest["schema_version"] = 2
            return manifest

        def change_bucket_identity(manifest):
            manifest["bucket"] = "psd401-prr-backups-other"
            return manifest

        def change_prefix_identity(manifest):
            manifest["s3_prefix"] = "app-backups/2026/08/06/other"
            return manifest

        cases = (
            ("partial", make_partial),
            ("extra", add_extra_field),
            ("schema", change_schema),
            ("bucket identity", change_bucket_identity),
            ("prefix identity", change_prefix_identity),
        )
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "manifest.json"
            for label, mutate in cases:
                manifest = mutate(copy.deepcopy(valid_manifest()))
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.subTest(boundary="function", case=label):
                    with self.assertRaises(ValueError):
                        module.verify_restored_audit(
                            manifest,
                            restored_count=12,
                            restored_max_id=19,
                        )
                argv = [
                    "backup_manifest.py",
                    "verify-restored",
                    "--manifest",
                    str(manifest_path),
                    "--restored-count",
                    "12",
                    "--restored-max-id",
                    "19",
                ]
                with self.subTest(boundary="cli", case=label), patch(
                    "sys.argv", argv
                ), self.assertRaises(ValueError):
                    module.main()


class BackupDeploymentContractTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.aws = self.root / "infra" / "aws"

    def _run_backup_until_docker(self, routing_environment=None):
        routing_environment = routing_environment or {}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "app"
            project.mkdir()
            (project / "docker-compose.deploy.yml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            (project / "docker-compose.aws.yml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            helper = directory / "backup_manifest.py"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            helper.chmod(0o700)
            docker_log = directory / "docker.argv"
            docker_config_log = directory / "docker.config"
            environment = os.environ.copy()
            for name in ("COMPOSE_FILE", "DOCKER_HOST", "DOCKER_CONTEXT"):
                environment.pop(name, None)
            environment.update(
                {
                    "PRR_PROJECT_DIR": str(project),
                    "PRR_BACKUP_STATE_DIR": str(directory / "state"),
                    "PRR_BACKUP_HELPER": str(helper),
                    "PRR_TEST_DOCKER_LOG": str(docker_log),
                    "PRR_TEST_DOCKER_CONFIG_LOG": str(docker_config_log),
                }
            )
            environment.update(routing_environment)
            harness = r"""
aws() { :; }
flock() { :; }
python3() {
  if [ "${1:-}" = "-c" ]; then
    printf '%s\n' 11111111-2222-4333-8444-555555555555
  fi
}
if ! command -v mapfile >/dev/null 2>&1; then
  mapfile() {
    local option="$1" target="$2" line=""
    IFS= read -r line || true
    eval "$target=(\"\$line\")"
  }
fi
docker() {
  printf '%s\n' "${DOCKER_CONFIG:-}" >"$PRR_TEST_DOCKER_CONFIG_LOG"
  case " $* " in
    *" ps -q "*"label=com.docker.compose.service=web"*)
      printf '%s\n' web-container
      return 0
      ;;
    *" ps -q "*"label=com.docker.compose.service=db"*)
      printf '%s\n' db-container
      return 0
      ;;
  esac
  printf '%s\0' "$@" >"$PRR_TEST_DOCKER_LOG"
  return 99
}
source "$1"
"""
            result = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    harness,
                    "backup-test",
                    str(self.aws / "run-app-backup.sh"),
                ],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )
            docker_argv = (
                docker_log.read_bytes().rstrip(b"\0").split(b"\0")
                if docker_log.exists()
                else []
            )
            docker_config = (
                docker_config_log.read_text(encoding="utf-8").strip()
                if docker_config_log.exists()
                else ""
            )
            return (
                result,
                [argument.decode() for argument in docker_argv],
                project,
                docker_config,
            )

    def test_backup_rejects_hostile_docker_routing_before_invoking_docker(self):
        cases = (
            ("COMPOSE_FILE", "/tmp/hostile-compose.yml"),
            ("DOCKER_HOST", "tcp://attacker.invalid:2375"),
            ("DOCKER_CONTEXT", "attacker-controlled"),
        )
        for name, value in cases:
            with self.subTest(variable=name):
                result, docker_argv, _, _ = self._run_backup_until_docker(
                    {name: value}
                )
                self.assertEqual(result.returncode, 64)
                self.assertIn(f"{name} must be unset", result.stderr)
                self.assertEqual(docker_argv, [])

    def test_backup_discovers_running_web_container_and_exports_directly(self):
        result, docker_argv, _, _ = self._run_backup_until_docker()

        self.assertEqual(result.returncode, 99, result.stderr)
        self.assertEqual(
            docker_argv,
            [
                "--host",
                "unix:///var/run/docker.sock",
                "exec",
                "web-container",
                "python",
                "manage.py",
                "export_audit_log",
            ],
        )

    def test_backup_uses_a_private_docker_config_instead_of_ambient_context(self):
        result, docker_argv, _, docker_config = self._run_backup_until_docker(
            {"DOCKER_CONFIG": "/tmp/hostile-docker-config"}
        )

        self.assertEqual(result.returncode, 99, result.stderr)
        self.assertEqual(
            docker_argv[:2],
            ["--host", "unix:///var/run/docker.sock"],
        )
        self.assertNotEqual(docker_config, "/tmp/hostile-docker-config")
        self.assertTrue(docker_config.endswith("/docker-config"), docker_config)

    def test_backup_job_is_private_atomic_and_integrity_checked(self):
        source = (self.aws / "run-app-backup.sh").read_text()

        for required in (
            "umask 077",
            "flock -n",
            "mktemp -d",
            "export_audit_log",
            "pg_dump",
            "--format=custom",
            "pg_restore --list",
            '--if-none-match "*"',
            "--checksum-sha256",
            "--server-side-encryption AES256",
            "cloudwatch put-metric-data",
        ):
            self.assertIn(required, source)
        self.assertLess(
            source.index('audit.jsonl.gz"'), source.index('manifest.json"')
        )
        self.assertLess(
            source.index('database.dump"'), source.index('manifest.json"')
        )
        self.assertIn('upload "$manifest_path"', source)
        self.assertGreater(
            source.index('upload "$manifest_path"'),
            source.index('upload "$database_path"'),
        )
        self.assertLess(
            source.index("backup_complete=true"),
            source.index('install -m 600 "$manifest_path"'),
        )
        self.assertNotIn("s3 cp", source)

    def test_backup_records_the_common_actual_running_application_image(self):
        source = (self.aws / "run-app-backup.sh").read_text()

        self.assertIn("for service in web worker beat", source)
        self.assertIn(
            "docker --host unix:///var/run/docker.sock inspect",
            source,
        )
        self.assertIn("{{.Image}}", source)
        self.assertIn("^sha256:[0-9a-f]{64}$", source)
        self.assertIn("application services do not share one running image", source)
        self.assertNotIn('images -q web', source)
        self.assertIn(
            "--filter label=com.docker.compose.project=app",
            source,
        )
        self.assertIn(
            '--filter "label=com.docker.compose.service=${service}"',
            source,
        )
        self.assertNotIn("PRR_RELEASE_IMAGE", source)
        self.assertNotIn("docker compose", source)

    def test_restore_drill_uses_an_ephemeral_networkless_postgres(self):
        source = (self.aws / "verify-app-backup-restore.sh").read_text()

        for required in (
            'python3 "$helper" verify',
            "postgres:16",
            "--network none",
            "--env POSTGRES_HOST_AUTH_METHOD=trust",
            "pg_restore",
            "audit_auditlog",
            "docker rm -f",
        ):
            self.assertIn(required, source)

    def test_systemd_job_is_hardened_and_timer_is_persistent(self):
        service = (self.aws / "prr-app-backup@.service").read_text()
        timer = (self.aws / "prr-app-backup@.timer").read_text()

        for required in (
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "PrivateTmp=true",
            "ReadWritePaths=/srv/prr/backups",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ):
            self.assertIn(required, service)
        for required in (
            "OnCalendar=",
            "Persistent=true",
            "RandomizedDelaySec=",
        ):
            self.assertIn(required, timer)

    def test_systemd_job_finds_host_clis_without_exposing_the_user_home(self):
        service = (self.aws / "prr-app-backup@.service").read_text()
        backup = (self.aws / "run-app-backup.sh").read_text()

        self.assertIn("ProtectHome=true", service)
        self.assertIn("WorkingDirectory=/srv/prr/app", service)
        self.assertIn("Environment=DOCKER_CONFIG=/tmp/prr-docker-config", service)
        self.assertIn("/snap/aws-cli/current/bin", service)
        self.assertIn("/snap/aws-cli/current/bin", backup)
        self.assertLess(
            backup.index("/snap/aws-cli/current/bin"), backup.index("/snap/bin")
        )

    def test_backup_startup_does_not_emit_the_deprecated_fitz_warning(self):
        for relative_path in (
            "apps/records/exemption_logs.py",
            "apps/records/preview.py",
            "apps/records/review.py",
            "apps/records/vault_ingest.py",
        ):
            source = (self.root / relative_path).read_text()
            self.assertNotRegex(source, r"(?m)^\s*import fitz\s*$")
            self.assertIn("import pymupdf as fitz", source)

    def test_installer_enables_timer_and_runs_initial_backup(self):
        source = (self.aws / "install-app-backup.sh").read_text()

        self.assertIn("systemctl enable --now", source)
        self.assertIn("systemctl start", source)
        self.assertIn("prr-app-backup@", source)

    def test_ci_shellchecks_every_backup_script(self):
        workflow = (self.root / ".github" / "workflows" / "psd-ci-jobs.yml").read_text()
        for name in (
            "run-app-backup.sh",
            "verify-app-backup-restore.sh",
            "install-app-backup.sh",
        ):
            self.assertIn(name, workflow)

    def test_ci_shellcheck_does_not_depend_on_an_unbounded_package_install(self):
        workflow = (self.root / ".github" / "workflows" / "psd-ci-jobs.yml").read_text()

        self.assertNotIn("apt-get install -y -qq shellcheck", workflow)
        self.assertIn("shellcheck --version", workflow)
