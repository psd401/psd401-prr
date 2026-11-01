# ABOUTME: Regression tests for authentication, CSRF, throttling, and headers.
# ABOUTME: Security-sensitive routes fail closed and sensitive data is not cached.
import ast
import re
import shlex
import subprocess
import sys

import yaml
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory

from apps.api.auth import LoginView
from apps.api.checks import production_security_checks


class StaffLoginSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = get_user_model().objects.create_user(
            "security-officer",
            "security-officer@psd401.net",
            "correct-password",
            is_staff=True,
        )
        get_user_model().objects.create_user(
            "known-nonstaff",
            "known@example.org",
            "correct-password",
            is_staff=False,
        )

    def test_login_requires_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)

        missing = client.post(
            "/api/staff/auth/login",
            {"username": self.staff.username, "password": "correct-password"},
            content_type="application/json",
        )
        client.get("/")
        csrf_token = client.cookies["csrftoken"].value
        accepted = client.post(
            "/api/staff/auth/login",
            {"username": self.staff.username, "password": "correct-password"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(accepted.status_code, 200)

    @override_settings(PRR_STAFF_LOGIN_RATE="1/min")
    def test_login_attempts_are_throttled_by_source_ip(self):
        first = self.client.post(
            "/api/staff/auth/login",
            {"username": "missing", "password": "wrong"},
            content_type="application/json",
        )
        second = self.client.post(
            "/api/staff/auth/login",
            {"username": "missing", "password": "wrong"},
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 400)
        self.assertEqual(second.status_code, 429)

    def test_login_does_not_reveal_whether_a_nonstaff_account_exists(self):
        missing = self.client.post(
            "/api/staff/auth/login",
            {"username": "missing", "password": "correct-password"},
            content_type="application/json",
        )
        known_nonstaff = self.client.post(
            "/api/staff/auth/login",
            {"username": "known-nonstaff", "password": "correct-password"},
            content_type="application/json",
        )

        self.assertEqual(known_nonstaff.status_code, missing.status_code)
        self.assertEqual(known_nonstaff.json(), missing.json())

    def test_application_staff_cannot_enter_break_glass_django_admin(self):
        self.client.force_login(self.staff)

        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.headers["Location"])

    @override_settings(PRR_AUTH_MODE="google")
    def test_password_login_view_is_not_available_in_google_mode(self):
        request = APIRequestFactory().post(
            "/api/staff/auth/login",
            {"username": self.staff.username, "password": "correct-password"},
            format="json",
        )

        response = LoginView.as_view()(request)

        self.assertEqual(response.status_code, 404)


class ResponseSecurityHeaderTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(
            "header-officer",
            "header-officer@psd401.net",
            "pw",
            is_staff=True,
        )

    def test_staff_api_responses_are_never_cacheable(self):
        self.client.login(username="header-officer", password="pw")
        response = self.client.get("/api/staff/requests/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_spa_shell_has_a_restrictive_content_security_policy(self):
        response = self.client.get("/")

        policy = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")

    def test_spa_routes_remain_unindexed_until_a_public_ui_exists(self):
        public_response = self.client.get("/request")
        staff_response = self.client.get("/requests")

        self.assertEqual(
            public_response.headers["X-Robots-Tag"],
            "noindex, nofollow",
        )
        self.assertEqual(
            staff_response.headers["X-Robots-Tag"],
            "noindex, nofollow",
        )

    def test_spa_shell_rejects_unsafe_http_methods(self):
        response = self.client.post(
            "/registry",
            {"unexpected": "data"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 405)


class ProductionSecurityCheckTests(SimpleTestCase):
    @override_settings(
        PRR_DEPLOYMENT_MODE="production",
        DEBUG=False,
        SECRET_KEY="dev-only",
        SECURE_SSL_REDIRECT=False,
        SESSION_COOKIE_SECURE=False,
        CSRF_COOKIE_SECURE=False,
    )
    def test_insecure_production_configuration_fails_deployment_checks(self):
        ids = {message.id for message in production_security_checks(None)}

        # api.E006 through api.E009 are gone: the sign-in mode, the OAuth
        # client, its secret, and the group reader's credential are settings
        # an Application Admin changes without a redeploy, so a verdict taken
        # once at process start could not be right for long. The rule that
        # replaced them refuses, on the save path, any sign-in change that
        # would leave nobody able to sign in.
        self.assertSetEqual(
            ids,
            {"api.E001", "api.E002", "api.E003", "api.E004"},
        )

    @override_settings(
        PRR_DEPLOYMENT_MODE="production",
        DEBUG=False,
        SECRET_KEY="production-secret-with-more-than-fifty-characters-1234567890",
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        PRR_AUTH_MODE="google",
        OIDC_RP_CLIENT_ID="client-id",
        OIDC_RP_CLIENT_SECRET="client-secret",
        PRR_GOOGLE_GROUP_CREDENTIALS_FILE="/run/secrets/google-groups.json",
    )
    def test_secure_production_configuration_passes_custom_checks(self):
        self.assertEqual(production_security_checks(None), [])

    @override_settings(
        PRR_DEPLOYMENT_MODE="production",
        DEBUG=False,
        SECRET_KEY="production-secret-with-more-than-fifty-characters-1234567890",
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        ALLOWED_HOSTS=["*"],
        PRR_AUTH_MODE="google",
        OIDC_RP_CLIENT_ID="client-id",
        OIDC_RP_CLIENT_SECRET="client-secret",
        PRR_GOOGLE_GROUP_CREDENTIALS_FILE="/run/secrets/google-groups.json",
    )
    def test_wildcard_host_configuration_fails_deployment_checks(self):
        ids = {message.id for message in production_security_checks(None)}

        self.assertEqual(ids, {"api.E005"})

    @override_settings(
        PRR_DEPLOYMENT_MODE="development",
        DEBUG=True,
    )
    def test_local_debug_skips_production_checks(self):
        self.assertEqual(production_security_checks(None), [])

    # Plaintext Alta transport (api.E016) is refused on the save path now that
    # the Alta address is a stored setting; see
    # apps.configuration.tests.test_phase_c_remainder.ShapeTests. The client
    # still refuses a non-HTTPS address at use.

    @override_settings(
        PRR_DEPLOYMENT_MODE="production",
        DEBUG=True,
        PRR_ENVIRONMENT="development",
        SECRET_KEY="production-secret-with-more-than-fifty-characters-1234567890",
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        PRR_AUTH_MODE="google",
        OIDC_RP_CLIENT_ID="client-id",
        OIDC_RP_CLIENT_SECRET="client-secret",
        PRR_GOOGLE_GROUP_CREDENTIALS_FILE="/run/secrets/google-groups.json",
    )
    def test_deployment_mode_cannot_use_debug_or_environment_label_to_bypass_checks(self):
        ids = {message.id for message in production_security_checks(None)}

        self.assertEqual(ids, {"api.E015"})


class StrictTransportSettingsTests(SimpleTestCase):
    def _import_settings(self, **environment):
        process_environment = {
            "PYTHON_DOTENV_DISABLED": "1",
            "DJANGO_DEBUG": "true",
            **environment,
        }
        return subprocess.run(
            [sys.executable, "-c", "import config.settings"],
            cwd=settings.BASE_DIR,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_deployment_mode_rejects_unknown_values(self):
        result = self._import_settings(PRR_DEPLOYMENT_MODE="staging")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "PRR_DEPLOYMENT_MODE must be exactly development or production.",
            result.stderr,
        )

    # The object-store timeouts and the retry ceiling are application-managed
    # settings since Phase B, so an out-of-range value is refused on the save
    # path rather than at settings import. Their exact bounds are asserted
    # against that path in
    # apps/configuration/tests/test_phase_b_settings.py (BoundedValueTests),
    # which also names each one, so the record of what moved is not lost.
    # ClamAV's host, port, and timeout moved into the settings store in
    # Phase C; their bounds are the registry's and are refused on the save
    # path (apps.configuration.tests.test_phase_c_remainder).

    def test_the_advisory_bootstrap_environment_still_imports_cleanly(self):
        """A deployment's existing advisory environment must not break boot.

        The three values are stored settings now, but they remain in the
        production overlay as the one-time bootstrap seed the settings-store
        import reads. Settings must therefore ignore them without complaint
        rather than refusing to load.
        """
        result = self._import_settings(
            PRR_AI_ADVISORY_ENABLED="true",
            PRR_AI_PROVIDER="bedrock",
            PRR_AI_AWS_REGION="us-west-2",
            PRR_AI_MODEL="us.amazon.nova-2-lite-v1:0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_staff_notifications_require_explicit_enablement(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import config.settings as configured; "
                    "print(configured.PRR_STAFF_NOTIFICATIONS_ENABLED)"
                ),
            ],
            cwd=settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "PRR_DEPLOYMENT_MODE": "production",
                "PRR_GMAIL_ENABLED": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_production_has_no_parallel_django_smtp_transport(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import django; "
                    "django.setup(); "
                    "from django.core.mail import send_mail; "
                    "send_mail('should fail', 'body', "
                    "'publicrecords@psd401.net', "
                    "['records.officer@psd401.net'])"
                ),
            ],
            cwd=settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_SETTINGS_MODULE": "config.settings",
                "DJANGO_DEBUG": "true",
                "PRR_DEPLOYMENT_MODE": "production",
                "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Generic Django email is disabled in production",
            result.stderr,
        )

    def test_safety_net_requires_independent_explicit_enablement(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import config.settings as configured; "
                    "print(configured.AUTO_ACK_SAFETY_NET)"
                ),
            ],
            cwd=settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "PRR_DEPLOYMENT_MODE": "production",
                "PRR_GMAIL_ENABLED": "true",
                "PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_automatic_record_review_requires_independent_explicit_enablement(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import config.settings as configured; "
                    "print(configured.PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED)"
                ),
            ],
            cwd=settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "PRR_AI_ADVISORY_ENABLED": "true",
                "PRR_AI_PROVIDER": "bedrock",
                "PRR_AI_AWS_REGION": "us-west-2",
                "PRR_AI_MODEL": "us.amazon.nova-2-lite-v1:0",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_automatic_record_review_cannot_enable_provider_calls_by_itself(self):
        """The automatic switch never implies the advisory switch.

        This used to be a startup ``ImproperlyConfigured``. The three advisory
        switches are application-managed now, so the dependency is enforced
        where an administrator meets it -- the settings store refuses to
        enable a dependent whose parent is off
        (apps/configuration/tests/test_settings_store.py). What settings must
        still guarantee is that turning the automatic switch on does not drag
        the advisory switch on with it.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import config.settings as configured; "
                    "print(configured.PRR_AI_ADVISORY_ENABLED)"
                ),
            ],
            cwd=settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "PRR_AI_ADVISORY_ENABLED": "false",
                "PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_automatic_record_review_setting_is_a_strict_boolean(self):
        result = self._import_settings(
            PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED="sometimes",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED", result.stderr)

    def test_the_advisory_provider_region_and_model_left_settings_entirely(self):
        """"Enabled implies configured" is asked of the store, not of import.

        Phase A moved the advisory switch, and settings answered the question
        with ``PRR_AI_ADVISORY_MODEL_CONFIGURED``, computed from three
        environment variables. Phase B moved those three variables too, so
        the computed attribute is gone and the question is the
        ``advisory_model_configured`` prerequisite, which reads the store
        before it will let an administrator enable advisory AI
        (apps/configuration/tests/test_settings_store.py asserts each
        refusal; test_phase_b_settings asserts the provider, region, and
        model bounds on the save path).

        What settings must still guarantee is that a stray environment
        variable cannot reintroduce a second answer.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import config.settings as configured; "
                    "print(["
                    "name for name in ("
                    "'PRR_AI_PROVIDER', 'PRR_AI_AWS_REGION', 'PRR_AI_MODEL', "
                    "'PRR_AI_ADVISORY_MODEL_CONFIGURED') "
                    "if hasattr(configured, name)])"
                ),
            ],
            cwd=settings.BASE_DIR,
            env={
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "PRR_AI_PROVIDER": "openai",
                "PRR_AI_AWS_REGION": "US West 2",
                "PRR_AI_MODEL": "us.amazon.nova-2-lite-v1:0",
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")


class ApiRoutePermissionInventoryTests(SimpleTestCase):
    def _api_views(self, patterns, prefix=""):
        for pattern in patterns:
            route = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                yield from self._api_views(pattern.url_patterns, route)
            elif isinstance(pattern, URLPattern) and route.startswith("api/"):
                view_class = getattr(pattern.callback, "cls", None)
                if view_class:
                    yield route, view_class

    def test_only_deliberately_csrf_protected_api_routes_are_anonymous(self):
        anonymous_routes = []
        for route, view_class in self._api_views(get_resolver().url_patterns):
            permissions = getattr(view_class, "permission_classes", ())
            if not permissions or AllowAny in permissions:
                anonymous_routes.append(route)

        self.assertEqual(
            anonymous_routes,
            ["api/staff/auth/login", "api/public/requests/"],
        )


class DependencySecurityAutomationTests(SimpleTestCase):
    def test_application_code_cannot_bypass_gmail_with_django_mail(self):
        violations = []
        application_root = settings.BASE_DIR / "apps"
        for path in sorted(application_root.rglob("*.py")):
            if "tests" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                imports_django_mail = (
                    isinstance(node, ast.Import)
                    and any(
                        alias.name.startswith("django.core.mail")
                        for alias in node.names
                    )
                ) or (
                    isinstance(node, ast.ImportFrom)
                    and (
                        (node.module or "").startswith("django.core.mail")
                        or (
                            node.module == "django.core"
                            and any(alias.name == "mail" for alias in node.names)
                        )
                    )
                )
                if imports_django_mail:
                    violations.append(
                        f"{path.relative_to(settings.BASE_DIR)}:{node.lineno}"
                    )

        self.assertEqual(violations, [])

    def test_ci_audits_python_and_frontend_dependencies(self):
        workflow = (settings.BASE_DIR / ".github/workflows/psd-ci-jobs.yml").read_text()

        # The audited environment is the one `uv sync --locked` built, so this
        # covers the locked transitive closure, not just the declared floors.
        self.assertIn("uv run --frozen pip-audit", workflow)
        self.assertIn("uv run --frozen bandit -q -r apps config integrations/mcp", workflow)
        self.assertIn("bun audit --production", workflow)

    def test_every_python_lane_installs_from_the_committed_lockfile(self):
        """#293: production runs exactly the versions CI tested.

        psd-dev-standards standards/02 (uv runtime) and standards/04 rule 10.
        A lane that resolved dependencies itself -- `pip install -r`, or
        `uv sync` without `--locked` -- could test versions the image never
        installs, which is the drift a lockfile exists to remove.
        """
        # Every workflow and every local composite action: the `uv sync` lines
        # now live in .github/actions/setup-python-deps, and the scheduled
        # lanes (nightly, mutation testing) must hold to the lockfile too.
        workflow = "\n".join(
            path.read_text() for path in self._workflow_and_action_files()
        )
        dockerfile = (settings.BASE_DIR / "Dockerfile").read_text()

        self.assertTrue((settings.BASE_DIR / "uv.lock").is_file())
        self.assertTrue((settings.BASE_DIR / "pyproject.toml").is_file())
        # Nothing may read a manifest that no longer exists.
        self.assertFalse((settings.BASE_DIR / "requirements.txt").exists())

        self.assertNotIn("pip install", workflow)
        self.assertNotIn("pip install", dockerfile)

        # No count of lanes: every `uv sync` must carry `--locked` and every
        # `uv run` must carry `--locked` or `--frozen`, so a new or edited lane
        # cannot slip through without a test edit. `--frozen` alone is not
        # enough on `uv sync`: it installs a stale uv.lock without checking it
        # against pyproject.toml, while `--locked` fails on that drift. A bare
        # `uv run` would re-lock a stale lockfile and install what it resolved.
        self.assertIn("astral-sh/setup-uv@", workflow)
        # The `@` is what distinguishes the GitHub action from the local
        # `./.github/actions/setup-python-deps` composite that wraps uv.
        self.assertNotIn("actions/setup-python@", workflow)
        commands = [
            line.strip()
            for line in (workflow + dockerfile).splitlines()
            if ("uv sync" in line or "uv run" in line)
            and not line.strip().startswith("#")
        ]
        syncs = [command for command in commands if "uv sync" in command]
        self.assertTrue(syncs, "nothing installs the locked dependencies")
        for command in commands:
            with self.subTest(command=command):
                if "uv sync" in command:
                    self.assertIn("--locked", command)
                else:
                    self.assertRegex(command, r"uv run --(locked|frozen) ")
        self.assertIn("uv sync --locked --no-dev", dockerfile)
        # The hosted runner ships an old system 3.12 (3.12.3) whose email
        # header folding differs from the image's current 3.12; uv would pick
        # it over a managed build unless told not to. The composite action
        # cannot set it for its caller's later `uv run` steps, so every job
        # that uses the action must see it, from the workflow or job `env`.
        callers = []
        for workflow_path in sorted((settings.BASE_DIR / ".github/workflows").glob("*.yml")):
            parsed = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
            for job_name, job in parsed["jobs"].items():
                if not any(
                    step.get("uses") == "./.github/actions/setup-python-deps"
                    for step in job.get("steps", [])
                ):
                    continue
                callers.append(f"{workflow_path.name}:{job_name}")
                env = {**parsed.get("env", {}), **job.get("env", {})}
                with self.subTest(job=callers[-1]):
                    self.assertEqual(env.get("UV_PYTHON_PREFERENCE"), "only-managed")
        self.assertIn("psd-ci-jobs.yml:backend", callers)

    def test_ci_executes_legal_workflow_races_on_postgresql_16(self):
        workflow = (settings.BASE_DIR / ".github/workflows/psd-ci-jobs.yml").read_text()

        self.assertIn("image: postgres:16", workflow)
        self.assertIn(
            "apps.records.tests.test_lifecycle_authorization_concurrency",
            workflow,
        )
        self.assertIn(
            "apps.correspondence.tests.test_inbound_concurrency",
            workflow,
        )
        self.assertIn(
            "apps.correspondence.tests.test_outbound_concurrency",
            workflow,
        )
        self.assertIn(
            "apps.api.tests.test_identity_config_concurrency",
            workflow,
        )
        self.assertIn("--settings=config.postgres_test_settings", workflow)

    def _dependabot_updates(self):
        config = yaml.safe_load(
            (settings.BASE_DIR / ".github/dependabot.yml").read_text()
        )
        return config["updates"]

    def test_dependabot_monitors_every_dependency_manifest(self):
        """Every manifest that ships code has an update stream.

        psd-dev-standards standards/04 Tier 1 rule 4. The CDK app is a bun
        workspace, Google identity is Terraform, and the application image is
        a Dockerfile plus compose overlays; none of them were covered before.
        """
        covered = {
            (entry["package-ecosystem"], entry["directory"])
            for entry in self._dependabot_updates()
        }

        self.assertEqual(
            covered,
            {
                ("github-actions", "/"),
                ("uv", "/"),
                ("npm", "/frontend"),
                ("bun", "/infra/cdk"),
                ("terraform", "/infra/terraform/google-identity"),
                ("docker", "/"),
                ("docker-compose", "/"),
            },
        )

    def test_dependabot_holds_new_releases_before_opening_a_pull_request(self):
        """standards/04 Tier 1 rule 4: 3-day cooldown, 7-14 days for npm."""
        for entry in self._dependabot_updates():
            ecosystem = entry["package-ecosystem"]
            with self.subTest(ecosystem=ecosystem, directory=entry["directory"]):
                days = entry.get("cooldown", {}).get("default-days")
                self.assertIsNotNone(days, "no cooldown declared")
                # npm and bun both resolve the npm registry, where a yanked
                # release is the common failure mode, so they wait longer.
                floor = 7 if ecosystem in {"npm", "bun"} else 3
                self.assertGreaterEqual(days, floor)
                if floor == 7:
                    self.assertLessEqual(days, 14)

    def test_dependabot_groups_only_minor_and_patch_updates(self):
        """Majors stay separate: they are migration work a human schedules."""
        for entry in self._dependabot_updates():
            groups = entry.get("groups", {})
            # Asserted so a new ecosystem added without groups cannot pass this
            # test by contributing no assertions at all.
            self.assertTrue(groups, f"{entry['package-ecosystem']} declares no groups")
            for name, group in groups.items():
                with self.subTest(group=name):
                    self.assertEqual(
                        sorted(group.get("update-types", [])),
                        ["minor", "patch"],
                    )

    def test_dependabot_keeps_the_documented_uninstallable_version_pins(self):
        """#263: one uninstallable bump fails the whole grouped pull request."""
        python = next(
            entry
            for entry in self._dependabot_updates()
            if entry["package-ecosystem"] == "uv"
        )
        ignored = {
            rule["dependency-name"]: rule["versions"] for rule in python["ignore"]
        }

        self.assertEqual(ignored["django"], [">=6.1"])
        self.assertEqual(ignored["redis"], [">=6.5"])

    def test_only_patch_level_dev_dependency_updates_auto_merge(self):
        """standards/04 Tier 1 rule 4: nothing else auto-merges."""
        workflow = yaml.load(
            (
                settings.BASE_DIR / ".github/workflows/dependabot-auto-merge.yml"
            ).read_text(),
            Loader=yaml.BaseLoader,
        )

        # A Dependabot run starts from a read-only `GITHUB_TOKEN`, but
        # Dependabot `pull_request` runs respect the `permissions` key (GitHub,
        # 2021-10-11), so these two writes are what let the merge step work.
        # `pull_request_target` is documented as read-only with no secrets when
        # the base ref was created by Dependabot, so switching the trigger
        # would break the merge rather than fix it.
        self.assertEqual(workflow["on"], "pull_request")
        job = workflow["jobs"]["auto-merge"]
        self.assertEqual(
            job["permissions"],
            {"contents": "write", "pull-requests": "write"},
        )
        # github.actor reflects whoever last triggered the run, so the author
        # has to come from the pull request payload.
        self.assertIn(
            "github.event.pull_request.user.login == 'dependabot[bot]'",
            job["if"],
        )
        merge_step = next(
            (
                step
                for step in job["steps"]
                if "gh pr merge" in step.get("run", "")
            ),
            None,
        )
        self.assertIsNotNone(merge_step, "no step merges the pull request")
        # Both conditions are positive matches, so an empty or unexpected
        # metadata value leaves the pull request for a human.
        self.assertIn(
            "update-type == 'version-update:semver-patch'", merge_step["if"]
        )
        self.assertIn("dependency-type == 'direct:development'", merge_step["if"])
        # `--auto` waits only for the checks the base branch requires, and the
        # org ruleset that requires psd-ci, claude-review and license-check was
        # still in `evaluate` mode on 2026-10-13. Without this gate the
        # standard's "when CI is green" would not hold.
        self.assertIn("steps.gate.outputs.enforced == 'true'", merge_step["if"])
        gate_step = next(
            (step for step in job["steps"] if step.get("id") == "gate"), None
        )
        self.assertIsNotNone(gate_step, "no step checks the base branch's rules")
        self.assertIn("required_status_checks", gate_step["run"])
        self.assertIn("--auto", merge_step["run"])
        # The job's author check is the pull request's author, which survives a
        # further commit pushed to the branch by someone else.
        self.assertIn(
            "steps.provenance.outputs.dependabot == 'true'", merge_step["if"]
        )
        provenance_step = next(
            (step for step in job["steps"] if step.get("id") == "provenance"), None
        )
        self.assertIsNotNone(
            provenance_step, "no step checks who wrote the commits"
        )
        self.assertIn('author.login != "dependabot[bot]"', provenance_step["run"])
        self.assertIn("verification.verified", provenance_step["run"])
        # The gate must fail closed when the commits API call fails. Actions
        # runs `bash -e {0}` without `pipefail`, so a `gh api | wc -l` pipeline
        # reports `wc`'s success even when `gh` failed -- which would read as
        # "no offending commits" and wave the pull request through. Two things
        # keep that shut: `pipefail`, and taking the decision from a bare
        # command substitution rather than the tail of a pipeline.
        self.assertIn("set -o pipefail", provenance_step["run"])
        # `-z` on the captured output, not a numeric compare of a piped count:
        # an empty capture can only come from a successful call that matched
        # nothing, because a failed `gh api` exits the step before this branch.
        self.assertIn('if [ -z "$offenders" ]', provenance_step["run"])

    def test_the_wiki_cannot_restate_host_and_account_runbooks(self):
        """standards/06 section 3: `.openwikiignore` is the read boundary.

        Docs pull requests merge themselves, so nothing reviews generated wiki
        prose before it lands. OpenWiki never reads an ignored path.
        """
        ignored = {
            line.strip()
            for line in (settings.BASE_DIR / ".openwikiignore").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        self.assertEqual(
            ignored,
            {
                "docs/google-identity-bootstrap.md",
                "docs/phase-2-production-handoffs.md",
                "docs/operations/",
                "infra/cdk/cdk.context.json",
                "infra/terraform/google-identity/identity.auto.tfvars",
                ".gitleaksignore",
            },
        )

    def test_the_wiki_is_told_not_to_name_people_or_identities(self):
        """standards/06 section 3: the brief for what `.openwikiignore` can't cover.

        The sensitive identities are in the source and its tests, not only in
        the docs -- the retired records officer's address is in
        `apps/correspondence/tests/test_inbound_mailbox_hardening.py` as well as
        in `docs/gmail-transport.md` -- so ignoring a path cannot hide them.
        OpenWiki reads this file as the brief for every run and never rewrites
        it.
        """
        brief = (settings.BASE_DIR / "openwiki/INSTRUCTIONS.md").read_text()

        # Both rules have to survive, not just the identity one: a brief that
        # kept the naming rule and dropped the control-evasion rule would still
        # pass a check for the first heading alone.
        self.assertIn("## Describe people and identities by role, never by name", brief)
        self.assertIn("## Do not describe how to get around a control", brief)
        # The whole enumerated list of identifiers, so dropping one is a
        # failure rather than a silent weakening. Matched against the brief
        # with its line wrapping collapsed, so reflowing a paragraph is not a
        # test failure.
        reflowed = " ".join(brief.split())
        for forbidden in (
            "staff or student name",
            "personal or named mailbox address",
            "service-account address",
            "tenant ID",
            "AWS account ID",
            "ARN",
            "credential location",
        ):
            self.assertIn(forbidden, reflowed)

    # Every `uses:` ref, the district's own PSD401/.github reusables included,
    # is pinned to a full commit SHA (psd-dev-standards standards/06 §4
    # Pinning, CIO decision 2026-10-21); Dependabot bumps them.
    @staticmethod
    def _workflow_and_action_files():
        """Workflows plus local composite actions, whose `uses:` run the same way."""
        github = settings.BASE_DIR / ".github"
        return sorted(github.glob("workflows/*.yml")) + sorted(
            github.glob("actions/*/action.yml")
        )

    def test_all_third_party_actions_are_pinned_to_commits(self):
        unpinned = []
        for workflow_path in self._workflow_and_action_files():
            for line_number, line in enumerate(workflow_path.read_text().splitlines(), 1):
                match = re.search(r"\buses:\s*([^\s]+)@([^\s#]+)", line)
                if not match:
                    continue
                _target, ref = match.groups()
                if not re.fullmatch(r"[0-9a-f]{40}", ref):
                    unpinned.append(
                        f"{workflow_path.relative_to(settings.BASE_DIR)}:{line_number}"
                    )

        self.assertEqual(unpinned, [])

    def test_every_workflow_job_declares_its_own_permissions(self):
        """psd-dev-standards standards/06 §4: per-job `permissions:` everywhere."""
        missing = []
        for workflow_path in sorted((settings.BASE_DIR / ".github/workflows").glob("*.yml")):
            workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
            for job_name, job in workflow["jobs"].items():
                if "permissions" not in job:
                    missing.append(f"{workflow_path.name}:{job_name}")

        self.assertEqual(missing, [])

    def test_reusable_workflow_callers_preserve_local_security_contracts(self):
        workflow_dir = settings.BASE_DIR / ".github/workflows"
        claude = yaml.load(
            (workflow_dir / "claude-code-review.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        openwiki = yaml.load(
            (workflow_dir / "openwiki-update.yml").read_text(),
            Loader=yaml.BaseLoader,
        )

        # `claude-review / claude-review` is a required check on Tier A, so
        # it must run on every PR head commit (psd-dev-standards standards/03).
        self.assertEqual(
            claude["on"]["pull_request"]["types"],
            ["opened", "synchronize", "ready_for_review", "reopened"],
        )
        self.assertEqual(
            claude["jobs"]["claude-review"]["permissions"],
            {
                "contents": "read",
                "pull-requests": "read",
                "issues": "read",
                "id-token": "write",
            },
        )
        # The review caller is the org starter template (PSD401/.github
        # workflow-templates/claude-review.yml): the reusable owns the turn
        # cap and per-PR concurrency, so the caller overrides neither.
        self.assertNotIn("with", claude["jobs"]["claude-review"])
        self.assertNotIn("concurrency", claude)
        self.assertEqual(
            openwiki["jobs"]["openwiki"]["permissions"],
            {"contents": "read", "pull-requests": "read"},
        )
        self.assertEqual(
            openwiki["on"]["push"]["paths-ignore"],
            ["openwiki/**"],
        )
        # The OpenWiki caller is the org starter template (PSD401/.github
        # workflow-templates/openwiki.yml): the reusable owns auto-merge and
        # concurrency, so the caller overrides neither (standards/06 §3).
        self.assertEqual(openwiki["jobs"]["openwiki"]["with"], {"base_branch": "main"})
        self.assertNotIn("concurrency", openwiki)

    def test_org_ruleset_required_checks_are_reported(self):
        """The org rulesets require `psd-ci / psd-ci` and `license-check`."""
        workflow_dir = settings.BASE_DIR / ".github/workflows"
        ci = yaml.load((workflow_dir / "ci.yml").read_text(), Loader=yaml.BaseLoader)
        jobs = yaml.load(
            (workflow_dir / "psd-ci-jobs.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        license_check = yaml.load(
            (workflow_dir / "license-check.yml").read_text(),
            Loader=yaml.BaseLoader,
        )

        # The caller job's id `psd-ci` plus the inner job's name `psd-ci`
        # report as `psd-ci / psd-ci`; every CI job runs under that caller.
        self.assertEqual(list(ci["jobs"]), ["psd-ci"])
        self.assertEqual(
            ci["jobs"]["psd-ci"]["uses"], "./.github/workflows/psd-ci-jobs.yml"
        )
        self.assertIn("workflow_call", jobs["on"])
        gate = jobs["jobs"]["psd-ci"]
        self.assertEqual(gate["name"], "psd-ci")
        self.assertEqual(gate["if"], "always()")
        self.assertEqual(
            sorted(gate["needs"]),
            sorted(name for name in jobs["jobs"] if name != "psd-ci"),
        )
        self.assertIn('"success"', gate["steps"][-1]["run"])
        self.assertTrue(
            license_check["jobs"]["license-check"]["uses"].startswith(
                "PSD401/.github/.github/workflows/reusable-license-check.yml@"
            )
        )
        self.assertTrue((settings.BASE_DIR / ".github/CODEOWNERS").is_file())

    def test_claude_comment_trigger_is_limited_to_trusted_authors(self):
        workflow = (settings.BASE_DIR / ".github/workflows/claude.yml").read_text()

        self.assertIn("author_association", workflow)
        # Public repository: an outside collaborator must not be able to
        # spend the district's Bedrock key with an @claude mention.
        self.assertEqual(workflow.count('["OWNER","MEMBER"]'), 4)
        self.assertNotIn("COLLABORATOR", workflow)

    def test_claude_mention_workflow_bills_inference_to_the_district(self):
        """standards/03 Serving: Bedrock with the org key, never a personal token.

        A repo-level `CLAUDE_CODE_OAUTH_TOKEN` is one person's subscription: it
        expires, it overrides the org secret, and its spend never reaches the
        district AWS bill (CIO decision 2026-10-08).
        """
        path = settings.BASE_DIR / ".github/workflows/claude.yml"
        text = path.read_text()
        workflow = yaml.load(text, Loader=yaml.BaseLoader)

        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", text)
        step = next(
            step
            for step in workflow["jobs"]["claude"]["steps"]
            if step.get("uses", "").startswith("anthropics/claude-code-action@")
        )
        self.assertEqual(step["with"]["use_bedrock"], "true")
        self.assertEqual(step["env"]["AWS_REGION"], "us-west-2")
        self.assertEqual(
            step["env"]["AWS_BEARER_TOKEN_BEDROCK"],
            "${{ secrets.BEDROCK_API_KEY }}",
        )
        # Asserted as the whole value, not a `us.` prefix match: a prefix match
        # passes on a truncated or renamed inference profile, and a model ID
        # Bedrock cannot resolve fails only at run time.
        args = shlex.split(step["with"]["claude_args"])
        self.assertEqual(args[args.index("--model") + 1], "us.anthropic.claude-sonnet-5-5")

    def test_claude_mention_workflow_keeps_its_hard_guardrails(self):
        """standards/09 hard guardrails: bounded turns, time, and tools.

        A mention-triggered agent with no turn cap, no timeout, or blanket Bash
        is an unbounded spend and an unbounded command surface on the runner.
        """
        path = settings.BASE_DIR / ".github/workflows/claude.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        job = workflow["jobs"]["claude"]

        self.assertLessEqual(int(job["timeout-minutes"]), 30)
        self.assertIn("github.event.issue.number", job["concurrency"]["group"])
        self.assertIn("github.event.pull_request.number", job["concurrency"]["group"])

        step = next(
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("anthropics/claude-code-action@")
        )
        args = shlex.split(step["with"]["claude_args"])
        self.assertLessEqual(int(args[args.index("--max-turns") + 1]), 60)
        tools = args[args.index("--allowed-tools") + 1].split(",")
        self.assertTrue(tools)
        for tool in tools:
            # Every Bash grant is a scoped, read-or-comment `gh` command.
            self.assertRegex(tool, r"^Bash\(gh (issue|pr|run|search)( [a-z]+)?:\*\)$")

    def test_claude_code_action_meets_the_bedrock_version_floor(self):
        """standards/03: pin v1.0.247 or later (Claude Code >= 2.1.280).

        2.1.220 was rejected by Bedrock for Sonnet 5.5.
        """
        text = (settings.BASE_DIR / ".github/workflows/claude.yml").read_text()
        match = re.search(
            r"anthropics/claude-code-action@[0-9a-f]{40}\s*#\s*v(\d+)\.(\d+)\.(\d+)",
            text,
        )
        self.assertIsNotNone(match, "action pin must carry a `# vX.Y.Z` comment")
        self.assertGreaterEqual(
            tuple(int(part) for part in match.groups()), (1, 0, 247)
        )
