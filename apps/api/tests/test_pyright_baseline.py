"""Tests for the pyright ratchet (`scripts/pyright_baseline.py`).

The gate's whole job is asymmetry: a file that gains an error fails the build,
a file that loses one fails with "rewrite the baseline" rather than quietly
banking the improvement. Both halves are tested here, because a ratchet that
only checks one direction stops ratcheting the moment someone fixes something.

The JSON below is pyright's real `--outputjson` shape, abridged to the fields
the gate reads.
"""
import contextlib
import io
import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from .gate_scripts import load_gate_script

gate = load_gate_script("pyright_baseline.py")

ROOT = "/repo"


def _diagnostic(path, severity="error", rule="reportAttributeAccessIssue"):
    return {
        "file": f"{ROOT}/{path}",
        "severity": severity,
        "rule": rule,
        "message": "Cannot access attribute",
        "range": {"start": {"line": 0, "character": 0}},
    }


def _report(*diagnostics):
    return {
        "generalDiagnostics": list(diagnostics),
        "summary": {"errorCount": len(diagnostics)},
    }


class CountTests(SimpleTestCase):
    def test_it_counts_errors_per_file_relative_to_the_repository(self):
        report = _report(
            _diagnostic("apps/records/services.py"),
            _diagnostic("apps/records/services.py"),
            _diagnostic("apps/api/views.py"),
        )

        self.assertEqual(
            gate.counts(report, root=Path(ROOT)),
            {"apps/api/views.py": 1, "apps/records/services.py": 2},
        )

    def test_a_warning_is_not_an_error(self):
        report = _report(
            _diagnostic("apps/api/views.py"),
            _diagnostic("apps/api/views.py", severity="warning"),
            _diagnostic("apps/api/views.py", severity="information"),
        )

        self.assertEqual(gate.counts(report, root=Path(ROOT)), {"apps/api/views.py": 1})

    def test_a_clean_tree_counts_nothing(self):
        self.assertEqual(gate.counts(_report(), root=Path(ROOT)), {})


class CompareTests(SimpleTestCase):
    def test_an_unchanged_tree_passes(self):
        baseline = {"apps/api/views.py": 2}

        self.assertEqual(gate.compare(baseline, {"apps/api/views.py": 2}), ([], []))

    def test_a_file_that_gains_an_error_regresses(self):
        regressions, improvements = gate.compare(
            {"apps/api/views.py": 2}, {"apps/api/views.py": 3}
        )

        self.assertEqual(regressions, [("apps/api/views.py", 2, 3)])
        self.assertEqual(improvements, [])

    def test_a_file_the_baseline_never_saw_regresses_from_zero(self):
        regressions, improvements = gate.compare({}, {"apps/api/new.py": 1})

        self.assertEqual(regressions, [("apps/api/new.py", 0, 1)])
        self.assertEqual(improvements, [])

    def test_a_file_that_loses_an_error_is_an_improvement_to_record(self):
        regressions, improvements = gate.compare(
            {"apps/api/views.py": 2}, {"apps/api/views.py": 1}
        )

        self.assertEqual(regressions, [])
        self.assertEqual(improvements, [("apps/api/views.py", 2, 1)])

    def test_a_deleted_file_is_an_improvement_not_a_regression(self):
        regressions, improvements = gate.compare({"apps/api/gone.py": 4}, {})

        self.assertEqual(regressions, [])
        self.assertEqual(improvements, [("apps/api/gone.py", 4, 0)])

    def test_one_file_improving_never_pays_for_another_regressing(self):
        """A whole-tree total would let a fix in one file hide a break in
        another. The gate compares file by file for exactly that reason."""
        regressions, improvements = gate.compare(
            {"a.py": 5, "b.py": 1}, {"a.py": 1, "b.py": 5}
        )

        self.assertEqual(regressions, [("b.py", 1, 5)])
        self.assertEqual(improvements, [("a.py", 5, 1)])


class BaselineFileTests(SimpleTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "pyright-baseline.json"

    def test_it_reads_the_counts_a_write_produced(self):
        gate.write_baseline(self.path, {"b.py": 1, "a.py": 2})

        self.assertEqual(gate.read_baseline(self.path), {"a.py": 2, "b.py": 1})

    def test_it_is_written_sorted_so_a_diff_reads_as_the_change(self):
        gate.write_baseline(self.path, {"b.py": 1, "a.py": 2})

        self.assertEqual(
            list(json.loads(self.path.read_text(encoding="utf-8"))["files"]),
            ["a.py", "b.py"],
        )

    def test_it_ends_with_a_newline(self):
        gate.write_baseline(self.path, {"a.py": 1})

        self.assertTrue(self.path.read_text(encoding="utf-8").endswith("\n"))

    def test_a_missing_baseline_is_an_empty_one(self):
        self.assertEqual(gate.read_baseline(self.directory / "absent.json"), {})

    def test_the_recorded_total_matches_the_counts(self):
        gate.write_baseline(self.path, {"a.py": 2, "b.py": 3})

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["total"], 5
        )


class RepositoryBaselineTests(SimpleTestCase):
    """The committed baseline is itself part of the contract."""

    def setUp(self):
        self.root = Path(settings.BASE_DIR)

    def test_the_committed_baseline_is_internally_consistent(self):
        path = self.root / "pyright-baseline.json"
        recorded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(recorded["total"], sum(recorded["files"].values()))

    def test_it_names_the_pyright_version_that_produced_it(self):
        recorded = json.loads(
            (self.root / "pyright-baseline.json").read_text(encoding="utf-8")
        )
        pyproject = (self.root / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn(f'"pyright=={recorded["pyright"]}"', pyproject)

    def test_no_file_is_recorded_with_zero_errors(self):
        """A zero entry is noise: `compare` treats a missing file as zero."""
        recorded = json.loads(
            (self.root / "pyright-baseline.json").read_text(encoding="utf-8")
        )

        self.assertEqual([f for f, n in recorded["files"].items() if n == 0], [])


class MainTests(SimpleTestCase):
    """The exit codes and the messages, which are the whole user interface.

    A ratchet that exits 0 on a regression is not a ratchet, and one whose
    message does not say `--update` leaves the reader stuck, so both are
    asserted rather than assumed.
    """

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.baseline = self.directory / "pyright-baseline.json"
        self.report = self.directory / "report.json"

    def _run(self, *extra):
        """main() with stdout and stderr captured, returning (code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main(
                ["--baseline", str(self.baseline), "--report", str(self.report), *extra]
            )
        return code, out.getvalue(), err.getvalue()

    def _write_report(self, *paths):
        """`main()` resolves against the real checkout, so the fake
        diagnostics have to be rooted there rather than at a stand-in."""
        self.report.write_text(
            json.dumps(
                _report(
                    *(
                        {
                            "file": f"{gate.REPO_ROOT}/{path}",
                            "severity": "error",
                            "rule": "reportAttributeAccessIssue",
                            "message": "Cannot access attribute",
                        }
                        for path in paths
                    )
                )
            ),
            encoding="utf-8",
        )

    def test_update_records_the_run_and_succeeds(self):
        self._write_report("apps/api/views.py", "apps/api/views.py")

        code, out, _ = self._run("--update")

        self.assertEqual(code, 0)
        self.assertIn("Recorded 2 pyright errors across 1 files", out)
        self.assertEqual(
            gate.read_baseline(self.baseline), {"apps/api/views.py": 2}
        )

    def test_an_unchanged_tree_succeeds_and_says_the_total(self):
        self._write_report("apps/api/views.py")
        self._run("--update")

        code, out, err = self._run()

        self.assertEqual(code, 0)
        self.assertIn("baseline holds: 1 errors, none new", out)
        self.assertEqual(err, "")

    def test_a_regression_fails_and_names_the_file(self):
        self._write_report("apps/api/views.py")
        self._run("--update")
        self._write_report("apps/api/views.py", "apps/api/views.py")

        code, out, err = self._run()

        self.assertEqual(code, 1)
        self.assertIn("REGRESSED apps/api/views.py: 1 -> 2", out)
        self.assertIn("gained pyright errors", err)

    def test_a_new_file_with_an_error_is_a_regression_from_zero(self):
        self._write_report()
        self._run("--update")
        self._write_report("apps/api/brand_new.py")

        code, out, _ = self._run()

        self.assertEqual(code, 1)
        self.assertIn("REGRESSED apps/api/brand_new.py: 0 -> 1", out)

    def test_an_unrecorded_improvement_fails_and_says_to_update(self):
        """Not because the code got worse -- because an improvement nobody
        records leaves slack the next change can spend."""
        self._write_report("apps/api/views.py", "apps/api/views.py")
        self._run("--update")
        self._write_report("apps/api/views.py")

        code, out, err = self._run()

        self.assertEqual(code, 1)
        self.assertIn("IMPROVED  apps/api/views.py: 2 -> 1", out)
        self.assertIn("--update", err)

    def test_a_regression_outranks_an_improvement_elsewhere(self):
        """The two messages must not cancel: a fix in one file may not pay
        for a break in another, and the exit code has to say so."""
        self._write_report("a.py", "a.py", "b.py")
        self._run("--update")
        self._write_report("b.py", "b.py")

        code, out, err = self._run()

        self.assertEqual(code, 1)
        self.assertIn("REGRESSED b.py: 1 -> 2", out)
        self.assertIn("gained pyright errors", err)

    def test_a_missing_baseline_makes_every_error_a_regression(self):
        self._write_report("apps/api/views.py")

        code, out, _ = self._run()

        self.assertEqual(code, 1)
        self.assertIn("REGRESSED apps/api/views.py: 0 -> 1", out)


class RunPyrightTests(SimpleTestCase):
    def test_it_parses_the_report_even_though_pyright_exits_nonzero(self):
        """pyright exits 1 whenever it found an error, which is every run
        here. Treating that as a failed run would break the gate outright."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=json.dumps(_report(_diagnostic("a.py"))), stderr=""
        )

        with mock.patch.object(gate.subprocess, "run", return_value=completed):
            report = gate.run_pyright()

        self.assertEqual(len(report["generalDiagnostics"]), 1)

    def test_output_that_is_not_json_stops_the_gate_loudly(self):
        """A pyright that could not start prints a message, not JSON. Reading
        that as "no diagnostics" would report a clean tree."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="cannot find module", stderr="boom"
        )

        with mock.patch.object(gate.subprocess, "run", return_value=completed):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as raised:
                    gate.run_pyright()

        self.assertIn("did not produce a JSON report", str(raised.exception))
        self.assertIn("cannot find module", err.getvalue())
