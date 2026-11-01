"""Repository contracts for the test-quality gates (psd-dev-standards 05).

Four gates are wired here, and each one is only as good as its wiring:

* rule 4 -- patch coverage on changed lines, backend and frontend;
* rule 5 -- a Playwright smoke subset gates the merge, the full suite runs
  nightly;
* rule 7 -- scheduled mutation testing on the Tier A critical modules;
* the CI shape -- PR checks stay near five minutes, which is why the Django
  suite is sharded rather than serial.

A shard list that quietly stops covering an app, or a nightly workflow that
silently runs the smoke subset, would leave the gate looking green while
testing less. These tests are the tripwire.
"""
import ast
import json
import re
import tomllib
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


class CiShapeTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.workflows = self.root / ".github" / "workflows"
        self.ci = yaml.safe_load(
            (self.workflows / "psd-ci-jobs.yml").read_text(encoding="utf-8")
        )

    def test_the_backend_shards_cover_every_installed_app(self):
        """Sharding is for speed; it must not become a hole in the gate."""
        shards = self.ci["jobs"]["backend"]["strategy"]["matrix"]["include"]
        covered = {
            label
            for shard in shards
            for label in str(shard["labels"]).split()
        }
        apps_on_disk = {
            f"apps.{path.name}"
            for path in sorted((self.root / "apps").iterdir())
            if (path / "__init__.py").is_file()
        }

        self.assertEqual(covered, apps_on_disk)
        self.assertGreater(len(shards), 1, "An unsharded suite is the slow case.")
        self.assertFalse(
            self.ci["jobs"]["backend"]["strategy"]["fail-fast"],
            "One shard's failure must not hide another's.",
        )

    def test_the_local_gate_shards_the_same_way_ci_does(self):
        """`verify-fast.sh` and the CI matrix are two copies of one list.

        The matrix is checked against the apps on disk, so adding an app and
        forgetting the matrix fails CI. Nothing checked the bash copy, which
        could keep a stale split and under-test locally with no red signal.
        """
        gate = (self.root / "scripts" / "verify-fast.sh").read_text(encoding="utf-8")
        local = {
            label
            for labels in re.findall(r'\$TEST_CMD ((?:apps\.[\w.]+ ?)+)', gate)
            for label in labels.split()
        }
        matrix = {
            label
            for shard in self.ci["jobs"]["backend"]["strategy"]["matrix"]["include"]
            for label in str(shard["labels"]).split()
        }

        self.assertEqual(local, matrix)

    def test_the_combined_gate_waits_for_every_job(self):
        """`psd-ci / psd-ci` is the one context the org ruleset requires, so a
        job missing from its `needs` is a gate that reports and never blocks.
        Adding a job and forgetting this list is the easy mistake; this is
        what makes it a red test rather than a silently weaker merge gate."""
        jobs = set(self.ci["jobs"]) - {"psd-ci"}

        self.assertEqual(jobs - set(self.ci["jobs"]["psd-ci"]["needs"]), set())

    def test_no_shard_is_listed_twice(self):
        shards = self.ci["jobs"]["backend"]["strategy"]["matrix"]["include"]
        labels = [
            label for shard in shards for label in str(shard["labels"]).split()
        ]

        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(
            len({shard["shard"] for shard in shards}),
            len(shards),
            "Shard names become artifact names, which must be unique.",
        )


class SharedSetupActionTests(SimpleTestCase):
    """Toolchain setup lives in one composite action per toolchain.

    Seven Python lanes and four Bun lanes used to repeat the same setup
    steps, so a version bump or a lockfile flag had to be made in every copy
    and a missed one drifted silently. Reusable *workflows* belong in
    PSD401/.github (psd-dev-standards standards/06); these are local
    composite actions, which that standard leaves to the repository.
    """

    PYTHON = "./.github/actions/setup-python-deps"
    BUN = "./.github/actions/setup-bun-deps"

    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.workflows = {
            path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted((self.root / ".github" / "workflows").glob("*.yml"))
        }

    def _action(self, name):
        path = self.root / ".github" / "actions" / name / "action.yml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def _jobs(self):
        for workflow_name, workflow in self.workflows.items():
            for job_name, job in workflow["jobs"].items():
                yield f"{workflow_name}:{job_name}", job.get("steps", [])

    def test_no_workflow_sets_up_a_toolchain_by_hand(self):
        for job, steps in self._jobs():
            for step in steps:
                uses = str(step.get("uses", ""))
                with self.subTest(job=job, step=step.get("name")):
                    self.assertFalse(uses.startswith("astral-sh/setup-uv@"))
                    self.assertFalse(uses.startswith("oven-sh/setup-bun@"))
                    self.assertNotIn("uv sync", str(step.get("run", "")))
                    self.assertNotIn(
                        "playwright install", str(step.get("run", ""))
                    )

    def test_every_lane_that_runs_uv_or_bun_sets_it_up_first(self):
        for job, steps in self._jobs():
            setup_index = {}
            for index, step in enumerate(steps):
                setup_index.setdefault(str(step.get("uses", "")), index)
            for index, step in enumerate(steps):
                run = str(step.get("run", ""))
                with self.subTest(job=job, step=step.get("name")):
                    if "uv run" in run:
                        self.assertLess(setup_index.get(self.PYTHON, index), index)
                    if re.search(r"\bbunx? ", run):
                        self.assertLess(setup_index.get(self.BUN, index), index)

    def test_the_python_action_installs_exactly_the_lockfile(self):
        action = self._action("setup-python-deps")

        self.assertEqual(action["runs"]["using"], "composite")
        self.assertEqual(action["inputs"]["include-dev"]["default"], "true")
        steps = action["runs"]["steps"]
        setup = next(s for s in steps if str(s.get("uses", "")).startswith("astral-sh/setup-uv@"))
        self.assertEqual(setup["with"]["python-version"], "3.12")
        syncs = [s for s in steps if "uv sync" in str(s.get("run", ""))]
        self.assertEqual(
            sorted(s["run"] for s in syncs),
            ["uv sync --locked", "uv sync --locked --no-dev"],
        )

    def test_the_bun_action_installs_frozen_and_playwright_only_on_request(self):
        action = self._action("setup-bun-deps")

        self.assertEqual(action["runs"]["using"], "composite")
        self.assertNotIn("default", action["inputs"]["bun-version"])
        self.assertNotIn("default", action["inputs"]["working-directory"])
        self.assertEqual(action["inputs"]["install-playwright"]["default"], "false")
        steps = action["runs"]["steps"]
        install = next(s for s in steps if "bun install" in str(s.get("run", "")))
        self.assertEqual(install["run"], "bun install --frozen-lockfile")
        playwright = next(s for s in steps if "playwright install" in str(s.get("run", "")))
        self.assertEqual(playwright["if"], "inputs.install-playwright == 'true'")

    def test_the_two_playwright_lanes_install_browsers_the_same_way(self):
        lanes = (
            self.workflows["psd-ci-jobs.yml"]["jobs"]["acceptance"],
            self.workflows["nightly.yml"]["jobs"]["full-acceptance"],
        )
        for job in lanes:
            setup = next(s for s in job["steps"] if s.get("uses") == self.BUN)
            self.assertIs(setup["with"]["install-playwright"], True)
            self.assertEqual(setup["with"]["working-directory"], "frontend")


class PatchCoverageGateTests(SimpleTestCase):
    """standards/05 rule 4: 80% of changed lines, and no project-% mandate."""

    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.ci = yaml.safe_load(
            (self.root / ".github/workflows/psd-ci-jobs.yml").read_text(encoding="utf-8")
        )

    def _coverage_config(self):
        with (self.root / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)["tool"]["coverage"]

    def test_the_required_minimum_is_eighty_percent(self):
        self.assertEqual(self.ci["env"]["PRR_PATCH_COVERAGE_MINIMUM"], "80")

    def test_both_suites_produce_a_coverage_report(self):
        backend = _step(self.ci["jobs"]["backend"], "Run backend tests")
        self.assertIn("coverage run", backend["run"])
        self.assertIn("uv run --frozen", backend["run"])
        self.assertEqual(
            _step(self.ci["jobs"]["backend"], "Upload shard coverage")["with"]["name"],
            "backend-coverage-${{ matrix.shard }}",
        )

        frontend = _step(self.ci["jobs"]["frontend"], "Run frontend tests with coverage")
        self.assertIn("test:coverage", frontend["run"])
        self.assertEqual(
            _step(self.ci["jobs"]["frontend"], "Upload frontend coverage")["with"]["name"],
            "frontend-coverage",
        )

    def test_the_gate_job_sees_the_whole_history_and_both_reports(self):
        job = self.ci["jobs"]["patch-coverage"]

        self.assertEqual(sorted(job["needs"]), ["backend", "frontend"])
        checkout = next(
            step for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(
            checkout["with"]["fetch-depth"],
            0,
            "A shallow clone has no base commit to diff against.",
        )

        gate = _step(job, "Require patch coverage on changed lines")
        self.assertIn("scripts/patch_coverage.py", gate["run"])
        self.assertIn("$PRR_PATCH_COVERAGE_MINIMUM", gate["run"])
        self.assertIn("backend-coverage.json", gate["run"])
        self.assertIn("frontend-coverage/coverage-final.json", gate["run"])

    def test_the_frontend_reports_every_source_file_not_only_imported_ones(self):
        """Without `coverage.include`, Vitest reports only imported files.

        That would hide a new untested module rather than fail it, which is
        the exact shape of change patch coverage exists to catch.
        """
        config = (self.root / "frontend" / "vite.config.ts").read_text(encoding="utf-8")

        self.assertIn("coverage:", config)
        self.assertIn("include: ['src/**/*.{ts,tsx}']", config)
        self.assertIn("reportsDirectory: '../.verification/frontend-coverage'", config)

    def test_the_fast_coverage_core_matches_what_the_config_measures(self):
        """`sysmon` is what keeps instrumentation inside the CI budget.

        CPython 3.12's own coverage core measures lines and not branches, so
        `branch = true` would silently need the slow trace core back -- and the
        shard that already runs closest to five minutes would go over it.
        """
        gate = (self.root / "scripts" / "verify-fast.sh").read_text(encoding="utf-8")

        self.assertEqual(self.ci["jobs"]["backend"]["env"]["COVERAGE_CORE"], "sysmon")
        self.assertIn("COVERAGE_CORE=sysmon", gate)
        self.assertFalse(self._coverage_config()["run"].get("branch", False))

    def test_there_is_no_project_coverage_floor(self):
        """standards/05 rule 4 forbids one: the number is gameable."""
        config = self._coverage_config()

        self.assertTrue(config["run"]["parallel"])
        self.assertNotIn("fail_under", config["report"])

    def test_the_tooling_is_a_development_dependency_not_a_runtime_one(self):
        """The production image runs `uv sync --no-dev` and ships none of it."""
        with (self.root / "pyproject.toml").open("rb") as handle:
            manifest = tomllib.load(handle)
        runtime = " ".join(manifest["project"]["dependencies"])
        development = " ".join(manifest["dependency-groups"]["dev"])

        for package in ("coverage", "hypothesis", "mutmut"):
            with self.subTest(package=package):
                self.assertIn(package, development)
                self.assertNotIn(package, runtime)
        self.assertIn(
            "uv sync --locked --no-dev",
            (self.root / "Dockerfile").read_text(encoding="utf-8"),
        )


class NightlyFullSuiteTests(SimpleTestCase):
    """standards/05 rule 5: smoke blocks the merge, the full suite runs nightly."""

    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.nightly = yaml.safe_load(
            (self.root / ".github/workflows/nightly.yml").read_text(encoding="utf-8")
        )

    def test_it_runs_on_a_schedule_and_on_demand(self):
        self.assertTrue(self.nightly[True]["schedule"][0]["cron"])
        self.assertIn("workflow_dispatch", self.nightly[True])

    def test_it_runs_every_flow_not_the_smoke_subset(self):
        job = self.nightly["jobs"]["full-acceptance"]
        run = _step(job, "Run every Playwright acceptance flow")["run"]

        self.assertEqual(run, "bash scripts/run-casework-acceptance.sh")
        self.assertNotIn("--smoke", run)

    def test_it_uploads_evidence_even_when_a_flow_fails(self):
        job = self.nightly["jobs"]["full-acceptance"]
        upload = _step(job, "Upload acceptance evidence")

        self.assertEqual(upload["if"], "always()")

    def test_a_failure_files_an_issue_rather_than_only_an_email(self):
        """The net under a thinner merge gate has to close the loop itself."""
        report = self.nightly["jobs"]["report"]

        self.assertIn("failure()", report["if"])
        self.assertEqual(report["permissions"]["issues"], "write")
        self.assertIn("gh issue", str(report["steps"]))

    def test_the_property_tests_are_reproducible_on_one_commit(self):
        """A random search that only sometimes finds a bad date reads as flake.

        FLAKY.md's triage-versus-quarantine decision turns on whether a test
        fails every time, and CI keeps no Hypothesis example database between
        runs, so the deadline properties must not re-randomise per run.
        """
        source = (
            self.root / "apps/records/tests/test_deadlines_properties.py"
        ).read_text(encoding="utf-8")

        self.assertIn("derandomize=True", source)


class MutationTestingTests(SimpleTestCase):
    """standards/05 rule 7: scheduled mutation testing on Tier A modules."""

    def setUp(self):
        self.root = Path(settings.BASE_DIR)
        self.workflow = yaml.safe_load(
            (self.root / ".github/workflows/mutation-testing.yml").read_text(
                encoding="utf-8"
            )
        )
        self.modules = self.workflow["jobs"]["mutate"]["strategy"]["matrix"]["include"]

    def test_it_is_scheduled_and_never_gates_a_merge(self):
        self.assertTrue(self.workflow[True]["schedule"][0]["cron"])
        ci = yaml.safe_load(
            (self.root / ".github/workflows/psd-ci-jobs.yml").read_text(encoding="utf-8")
        )
        self.assertNotIn("mutation", str(ci["jobs"]["psd-ci"]["needs"]))

    def test_it_covers_the_deadline_engine_identity_and_intake(self):
        mutated = {
            path
            for module in self.modules
            for path in str(module["paths"]).split(",")
        }

        self.assertIn("apps/records/deadlines.py", mutated)
        self.assertIn("apps/api/identity.py", mutated)
        self.assertIn("apps/records/intake.py", mutated)
        for path in mutated:
            with self.subTest(path=path):
                self.assertTrue((self.root / path).is_file())

    def test_every_module_names_tests_that_exist(self):
        for module in self.modules:
            for dotted in str(module["tests"]).split():
                with self.subTest(tests=dotted):
                    relative = Path(dotted.replace(".", "/") + ".py")
                    self.assertTrue((self.root / relative).is_file())

    def test_the_score_is_the_gate_not_mutmut_s_exit_code(self):
        """mutmut's exit status is a bit field; the kill rate is the finding."""
        score_step = _step(self.workflow["jobs"]["mutate"], "Score the run")

        self.assertIn("scripts/mutation_score.py", score_step["run"])
        self.assertIn("mutmut junitxml", score_step["run"])
        self.assertTrue((self.root / "scripts" / "mutation_score.py").is_file())

    def test_survivors_become_an_issue(self):
        report = self.workflow["jobs"]["report"]

        self.assertIn("failure()", report["if"])
        self.assertEqual(report["permissions"]["issues"], "write")
        self.assertIn("gh issue", str(report["steps"]))


class PropertyTestTests(SimpleTestCase):
    """standards/05 rule 8: property-based tests for date math."""

    def test_the_deadline_engine_has_property_tests(self):
        source = (
            Path(settings.BASE_DIR)
            / "apps/records/tests/test_deadlines_properties.py"
        ).read_text(encoding="utf-8")

        self.assertIn("from hypothesis import", source)
        # The four inputs the engine's docstring calls out.
        for concern in ("holiday", "closure", "toll", "after"):
            with self.subTest(concern=concern):
                self.assertIn(concern, source.lower())


#: What a test may reach an assertion through: `self.assertEqual`, a bare
#: `assertSomething(...)` helper, `self.fail(...)`, and the `assert_...`
#: helpers the correspondence and MIME suites share.
ASSERTION_PREFIXES = ("assert", "_assert")

#: Assertions whose whole content can be literals, and then say nothing.
#: `self.assertTrue(True)` and `self.assertEqual(1, 1)` are the shape an agent
#: reaches for to make a gate go green. `assertNumQueries(3)` and
#: `self.fail("...")` are deliberately not in here: a constant argument is the
#: whole point of those two.
TAUTOLOGY_PRONE = frozenset(
    {
        "assertTrue",
        "assertFalse",
        "assertEqual",
        "assertNotEqual",
        "assertIs",
        "assertIsNot",
        "assertIn",
        "assertNotIn",
        "assertGreater",
        "assertLess",
    }
)


def _called_name(call: ast.Call) -> str:
    """The bare name being called: `assertEqual`, for `self.assertEqual(...)`."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _is_tautology(call: ast.Call) -> bool:
    """True for an assertion that compares literals and so checks nothing."""
    if _called_name(call) not in TAUTOLOGY_PRONE:
        return False
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    return bool(arguments) and all(
        isinstance(argument, ast.Constant) for argument in arguments
    )


def _asserts_directly(node) -> bool:
    """True when this body actually runs an assertion that could fail.

    Three things have to hold, and each one is a hole somebody would
    otherwise walk through: the name has to be CALLED (a local named
    `assert_ready` is not an assertion), a bare `assert x == y` statement
    counts (it is an assertion even though it is not a call), and an
    assertion over nothing but literals does not count (`assertTrue(True)`
    passes forever).
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.Assert):
            if not isinstance(inner.test, ast.Constant):
                return True
            continue
        if not isinstance(inner, ast.Call):
            continue
        name = _called_name(inner)
        if not name.startswith(ASSERTION_PREFIXES) and name != "fail":
            continue
        if _is_tautology(inner):
            continue
        return True
    return False


def _self_calls(node) -> set[str]:
    return {
        inner.attr
        for inner in ast.walk(node)
        if isinstance(inner, ast.Attribute)
        and isinstance(inner.value, ast.Name)
        and inner.value.id == "self"
    }


def _assertion_helpers(tree: ast.Module) -> set[str]:
    """Every method in this module that asserts, so `self.check(...)` counts.

    Module-wide rather than per class on purpose: a shared assertion helper
    lives on a base class or a mixin at least as often as on the class that
    calls it, and a gate that flagged those would teach people to silence it.
    """
    return {
        member.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for member in node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _asserts_directly(member)
    }


def _test_functions(tree: ast.Module):
    """`(owner, node)` for every test, in a class or at module level."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            owner = f"{node.name}."
            body = node.body
        elif isinstance(node, ast.Module):
            owner = ""
            body = node.body
        else:
            continue
        for member in body:
            if isinstance(
                member, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and member.name.startswith("test"):
                yield owner, member


def _decorator_name(decorator: ast.expr) -> str:
    """The bare name of a decorator: `skip`, for `@unittest.skip("why")`."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _first_method(source: str) -> ast.FunctionDef:
    """The first method of the first class in `source`, for the self-tests."""
    klass = ast.parse(source).body[0]
    assert isinstance(klass, ast.ClassDef)
    method = klass.body[0]
    assert isinstance(method, ast.FunctionDef)
    return method


class DjangoTestQualityTests(SimpleTestCase):
    """standards/05 rule 2, the Django half.

    oxlint enforces `expect-expect` and the no-committed-`.only`/`.skip` rules
    on the frontend through `vitest/*` rules in `frontend/.oxlintrc.json`.
    There is no linter that understands `unittest`, so the same two rules are
    enforced here, by reading the suite's own AST.

    A test with no assertion is the specific failure mode the standard names:
    an agent will write one to make a number go up, and it passes forever
    because it checks nothing. A test that only has to not raise is a real
    pattern, so it is allowed -- out loud, with `# no-assert: <why>` on the
    `def` line, which a reviewer can see and count.
    """

    #: `# no-assert: <reason>` on the `def` line of a test that passes by not
    #: raising. The reason is required: the point is that a human wrote it.
    NO_ASSERT = re.compile(r"#\s*no-assert:\s*\S")

    #: Skipping, the Django spellings. `unittest.skip` without a reason is a
    #: TypeError, but `@expectedFailure` and `@skipIf(cond)` take none, so the
    #: reason is checked here rather than trusted to the decorator.
    SKIP_DECORATORS = {"skip", "skipIf", "skipUnless", "expectedFailure"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(settings.BASE_DIR)
        cls.modules = sorted(
            path
            for path in cls.root.glob("apps/**/*.py")
            if path.name.startswith("test_")
        )

    def test_the_scan_sees_the_whole_suite(self):
        """A glob that silently matches nothing would pass every test below."""
        self.assertGreater(len(self.modules), 100)

    def test_every_test_asserts_something(self):
        bare = []
        for path in self.modules:
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            tree = ast.parse(source)
            helpers = _assertion_helpers(tree)
            # Module level as well as inside a class: pytest-style
            # `def test_x():` at the top of a file is collected by other
            # runners and would otherwise be the one shape this gate cannot
            # see.
            for owner, member in _test_functions(tree):
                if _asserts_directly(member):
                    continue
                if _self_calls(member) & helpers:
                    continue
                if self.NO_ASSERT.search(lines[member.lineno - 1]):
                    continue
                bare.append(
                    f"{path.relative_to(self.root)}:{member.lineno} "
                    f"{owner}{member.name}"
                )

        self.assertEqual(
            bare,
            [],
            "These tests assert nothing. Add an assertion, or say why the "
            "test passes by not raising with `# no-assert: <reason>` on the "
            "`def` line.",
        )

    def test_nothing_is_skipped_without_a_reason(self):
        unexplained = []
        for path in self.modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for decorator in node.decorator_list:
                        if _decorator_name(decorator) not in self.SKIP_DECORATORS:
                            continue
                        given = [
                            *getattr(decorator, "args", ()),
                            *(
                                keyword.value
                                for keyword in getattr(decorator, "keywords", ())
                            ),
                        ]
                        said = [
                            argument
                            for argument in given
                            if isinstance(argument, ast.Constant)
                            and isinstance(argument.value, str)
                            and argument.value.strip()
                        ]
                        if not said:
                            unexplained.append(
                                f"{path.relative_to(self.root)}:{decorator.lineno}"
                            )
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "skipTest"
                ):
                    reason = node.args[0] if node.args else None
                    if not isinstance(reason, ast.Constant) or not str(
                        reason.value
                    ).strip():
                        unexplained.append(
                            f"{path.relative_to(self.root)}:{node.lineno}"
                        )

        self.assertEqual(
            unexplained,
            [],
            "A skipped test must say why it is skipped. A quarantined flaky "
            "test uses `@tag(\"quarantine\")` and FLAKY.md instead, so it "
            "keeps running.",
        )

    def test_the_detector_notices_a_test_that_asserts_nothing(self):
        """The gate above is only worth having if it can actually fail."""
        method = _first_method(
            "class T:\n"
            "    def test_nothing(self):\n"
            "        self.client.get('/')\n"
        )

        self.assertFalse(_asserts_directly(method))

    def test_the_detector_accepts_an_assertion_through_a_context_manager(self):
        method = _first_method(
            "class T:\n"
            "    def test_raises(self):\n"
            "        with self.assertRaises(ValueError):\n"
            "            boom()\n"
        )

        self.assertTrue(_asserts_directly(method))

    def test_a_name_that_merely_starts_with_assert_is_not_an_assertion(self):
        """`assert_ready = True` is a local, not a check, and a detector that
        matched the identifier would hand an agent a one-line bypass."""
        method = _first_method(
            "class T:\n"
            "    def test_nothing(self):\n"
            "        assert_ready = True\n"
            "        self.assertion_count = 1\n"
            "        callback = self.assertTrue\n"
            "        do_thing(assert_ready, callback)\n"
        )

        self.assertFalse(_asserts_directly(method))

    def test_an_assertion_over_only_literals_is_not_an_assertion(self):
        """`assertTrue(True)` is the shape that makes a gate go green while
        checking nothing. It is the failure mode this class exists for."""
        for body in (
            "        self.assertTrue(True)\n",
            "        self.assertEqual(1, 1)\n",
            '        self.assertIn("a", "abc")\n',
            "        assert True\n",
        ):
            with self.subTest(body=body.strip()):
                method = _first_method(
                    "class T:\n    def test_nothing(self):\n" + body
                )

                self.assertFalse(_asserts_directly(method))

    def test_a_bare_assert_statement_counts(self):
        method = _first_method(
            "class T:\n"
            "    def test_value(self):\n"
            "        assert compute() == 7\n"
        )

        self.assertTrue(_asserts_directly(method))

    def test_an_assertion_with_one_real_argument_counts(self):
        """`assertEqual(result, 1)` is a real check even though `1` is a
        literal; only an all-literal comparison says nothing."""
        method = _first_method(
            "class T:\n"
            "    def test_value(self):\n"
            "        self.assertEqual(compute(), 1)\n"
        )

        self.assertTrue(_asserts_directly(method))

    def test_a_helper_on_a_mixin_counts_for_the_class_that_calls_it(self):
        tree = ast.parse(
            "class AssertsMixin:\n"
            "    def assert_ok(self, value):\n"
            "        self.assertEqual(value, self.expected)\n"
            "\n"
            "class T(AssertsMixin):\n"
            "    async def test_delegates(self):\n"
            "        self.assert_ok(await compute())\n"
        )

        self.assertIn("assert_ok", _assertion_helpers(tree))

    def test_an_async_helper_counts_too(self):
        """The test loop accepts `async def test_...`, so the helper scan has
        to accept `async def assert_...` or it flags its own supported case."""
        tree = ast.parse(
            "class T:\n"
            "    async def assert_ok(self, value):\n"
            "        self.assertEqual(value, 1)\n"
        )

        self.assertIn("assert_ok", _assertion_helpers(tree))

    def test_a_module_level_test_function_is_scanned_too(self):
        """A `def test_x():` outside a class is a test to every runner that
        collects by name, and was the one shape the first draft missed."""
        tree = ast.parse(
            "def test_at_module_level():\n"
            "    pass\n"
            "\n"
            "class T:\n"
            "    def test_in_a_class(self):\n"
            "        pass\n"
        )

        self.assertEqual(
            [(owner, node.name) for owner, node in _test_functions(tree)],
            [("", "test_at_module_level"), ("T.", "test_in_a_class")],
        )

    def test_a_skip_reason_may_be_a_keyword_argument(self):
        """`@skipIf(cond, reason="...")` is as explained as the positional
        form, and flagging it would teach people to silence the gate."""
        decorated = ast.parse(
            '@skipIf(cond, reason="not on sqlite")\ndef t(): ...'
        ).body[0]
        assert isinstance(decorated, ast.FunctionDef)
        decorator = decorated.decorator_list[0]
        assert isinstance(decorator, ast.Call)

        self.assertEqual(_decorator_name(decorator), "skipIf")
        self.assertEqual(
            [keyword.arg for keyword in decorator.keywords], ["reason"]
        )


class PlaywrightTestQualityTests(SimpleTestCase):
    """standards/05 rule 2, the Playwright half.

    oxlint's `vitest/*` rules only recognise the vitest and jest globals, so
    a `test.only` imported from `@playwright/test` passes the frontend lint
    untouched -- verified by focusing a flow and watching `bun run lint` stay
    green. `forbidOnly` in `playwright.config.ts` catches it, but only when
    the suite actually runs under CI; a committed `.only` still focuses every
    local run until then. These read the specs instead.
    """

    #: `test.only`, `test.skip`, `test.fixme` and their `describe` forms.
    FOCUS_OR_SKIP = re.compile(r"\b(test|describe)\.(only|skip|fixme)\b")

    #: A top-level flow: `test('name', ...)` at the start of a line.
    FLOW = re.compile(r"^test\(\s*(['\"])(?P<name>.+?)\1", re.MULTILINE)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.specs = sorted(
            (Path(settings.BASE_DIR) / "frontend" / "e2e").glob("*.spec.ts")
        )

    def test_the_scan_sees_the_acceptance_suite(self):
        self.assertGreater(len(self.specs), 20)

    def test_no_flow_is_focused_or_skipped(self):
        committed = [
            f"{path.name}:{path.read_text(encoding='utf-8')[:match.start()].count(chr(10)) + 1}"
            for path in self.specs
            for match in [self.FOCUS_OR_SKIP.search(path.read_text(encoding="utf-8"))]
            if match
        ]

        self.assertEqual(
            committed,
            [],
            "A focused flow hides every other flow and a skipped one gates "
            "nothing. A flow that cannot be made reliable is quarantined "
            "through FLAKY.md, where it keeps running.",
        )

    def test_every_flow_asserts_something(self):
        silent = []
        for path in self.specs:
            source = path.read_text(encoding="utf-8")
            flows = list(self.FLOW.finditer(source))
            ends = [match.start() for match in flows[1:]] + [len(source)]
            for flow, end in zip(flows, ends, strict=True):
                if "expect(" not in source[flow.start() : end]:
                    silent.append(f"{path.name}: {flow.group('name')}")

        self.assertEqual(
            silent,
            [],
            "A Playwright flow that never calls `expect` passes as long as "
            "nothing throws, which is not the same as the journey working.",
        )

    def test_the_focus_pattern_matches_what_playwright_would_run(self):
        """The gate above is only worth having if it can actually fail."""
        self.assertTrue(self.FOCUS_OR_SKIP.search("test.only('x', async () => {"))
        self.assertTrue(self.FOCUS_OR_SKIP.search("test.describe.skip('x', () => {"))
        self.assertIsNone(self.FOCUS_OR_SKIP.search("test('x', async () => {"))


class StaticAnalysisGateTests(SimpleTestCase):
    """standards/05 rules 1-2: strict typecheck and lint, wired to the merge.

    A configured linter nobody runs is worse than none -- it reads as a gate
    in the repository and gates nothing. These tests hold the wiring: the CI
    job exists, the combined `psd-ci` gate waits for it, the local script
    runs the same two commands, and the versions are pinned so CI and a
    developer's checkout disagree only when somebody changes the pin.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = Path(settings.BASE_DIR)
        cls.ci = yaml.safe_load(
            (cls.root / ".github/workflows/psd-ci-jobs.yml").read_text(
                encoding="utf-8"
            )
        )
        cls.job = cls.ci["jobs"]["backend-static"]
        cls.pyproject = tomllib.loads(
            (cls.root / "pyproject.toml").read_text(encoding="utf-8")
        )
        cls.dev = cls.pyproject["dependency-groups"]["dev"]

    def test_the_combined_gate_waits_for_it(self):
        self.assertIn("backend-static", self.ci["jobs"]["psd-ci"]["needs"])

    def test_it_runs_both_the_linter_and_the_typechecker(self):
        commands = " ".join(
            str(step.get("run", "")) for step in self.job["steps"]
        )

        self.assertIn("ruff check .", commands)
        self.assertIn("scripts/pyright_baseline.py", commands)

    def test_the_local_gate_runs_the_same_two_commands(self):
        gate = (self.root / "scripts" / "verify-fast.sh").read_text(encoding="utf-8")

        self.assertIn("ruff check .", gate)
        self.assertIn("scripts/pyright_baseline.py", gate)

    def test_both_tools_are_pinned_to_an_exact_version(self):
        """A floor would let a tool upgrade turn somebody else's branch red,
        and the pyright baseline only compares against one version."""
        for tool in ("ruff", "pyright", "django-stubs", "djangorestframework-stubs"):
            with self.subTest(tool=tool):
                self.assertTrue(
                    any(entry.startswith(f"{tool}==") for entry in self.dev),
                    f"{tool} must be pinned with == in the dev group.",
                )

    def test_the_type_stubs_are_development_only(self):
        """They are type information; the production image must not carry
        them, and nothing may import them at runtime."""
        runtime = " ".join(self.pyproject["project"]["dependencies"])

        self.assertNotIn("stubs", runtime)
        self.assertNotIn("pyright", runtime)
        self.assertNotIn("ruff", runtime)

    def test_the_ruff_selection_is_explicit(self):
        """Inheriting ruff's defaults would let an upgrade widen or narrow
        the gate between two commits with no change in this repository."""
        lint = self.pyproject["tool"]["ruff"]["lint"]

        self.assertIn("F", lint["select"])
        self.assertIn("B", lint["select"])
        self.assertGreater(len(lint["select"]), 10)

    def test_generated_migrations_are_outside_both_tools(self):
        self.assertIn("*/migrations/*", self.pyproject["tool"]["ruff"]["extend-exclude"])
        self.assertIn("**/migrations", self.pyproject["tool"]["pyright"]["exclude"])

    def test_the_typechecker_covers_the_same_tree_coverage_measures(self):
        """A module outside the typecheck's `include` is a module the ratchet
        can never notice regressing."""
        measured = set(self.pyproject["tool"]["coverage"]["run"]["source"])
        checked = set(self.pyproject["tool"]["pyright"]["include"])

        self.assertEqual(measured - checked, set())

    def test_the_frontend_lints_its_tests_for_quality_not_only_its_source(self):
        """standards/05 rule 2 names these two by name."""
        oxlint = json.loads(
            (self.root / "frontend/.oxlintrc.json").read_text(encoding="utf-8")
        )

        self.assertIn("vitest", oxlint["plugins"])
        for rule in (
            "vitest/expect-expect",
            "vitest/no-focused-tests",
            "vitest/no-disabled-tests",
        ):
            with self.subTest(rule=rule):
                self.assertEqual(oxlint["rules"][rule], "error")

    def test_the_frontend_lint_fails_on_a_warning(self):
        """Otherwise `only-export-components` and anything else configured as
        a warning would report and merge."""
        package = json.loads(
            (self.root / "frontend/package.json").read_text(encoding="utf-8")
        )

        self.assertIn("--deny-warnings", package["scripts"]["lint"])
