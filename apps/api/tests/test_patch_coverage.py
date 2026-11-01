"""Tests for the patch-coverage gate (`scripts/patch_coverage.py`).

The gate decides whether a change may merge, so its own arithmetic has to be
pinned: an off-by-one in the diff parser would silently pass uncovered lines,
and nothing downstream would notice.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .gate_scripts import load_gate_script

gate = load_gate_script("patch_coverage.py")


class _Repository:
    """A throwaway git repository with two commits."""

    def __init__(self, stack):
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            # Never read the developer's own git config in a test.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


class AddedLinesTests(SimpleTestCase):
    def setUp(self):
        from contextlib import ExitStack

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.repo = _Repository(stack)

    def test_counts_added_lines_and_ignores_context_and_deletions(self):
        self.repo.write("mod.py", "a = 1\nb = 2\nc = 3\nd = 4\n")
        base = self.repo.commit("base")
        # Keep line 1, drop line 2, keep 3, replace 4, append two lines.
        self.repo.write("mod.py", "a = 1\nc = 3\nd = 44\ne = 5\nf = 6\n")
        self.repo.commit("change")

        added = gate.added_lines(self.repo.root, base, "HEAD")

        self.assertEqual(added, {"mod.py": {3, 4, 5}})

    def test_counts_every_line_of_a_new_file(self):
        self.repo.write("kept.py", "x = 1\n")
        base = self.repo.commit("base")
        self.repo.write("added.py", "one = 1\ntwo = 2\n")
        self.repo.commit("add a file")

        self.assertEqual(
            gate.added_lines(self.repo.root, base, "HEAD"),
            {"added.py": {1, 2}},
        )

    def test_added_content_that_looks_like_a_diff_header_is_not_one(self):
        """`git diff` prefixes added content with `+`, so a line whose own text
        starts with `++ ` arrives as `+++ ...`. A parser keying on that prefix
        would take it for the next file's header and silently drop every
        remaining hunk of the file it is in."""
        # Two separated hunks: the first carries the diff-shaped content, the
        # second is what a confused parser would drop on the floor.
        self.repo.write("doc.md", "intro\n" + "filler\n" * 8 + "outro\n")
        base = self.repo.commit("base")
        self.repo.write(
            "doc.md",
            "intro\n"
            "++ this line starts with two plus signs\n"
            + "filler\n" * 8
            + "outro\n"
            "--- and this one starts with three dashes\n",
        )
        self.repo.commit("add diff-shaped content")

        self.assertEqual(
            gate.added_lines(self.repo.root, base, "HEAD"),
            {"doc.md": {2, 12}},
        )

    def test_a_pure_rename_adds_no_lines(self):
        """Without rename detection a `git mv` charges the whole moved file."""
        body = "".join(f"line = {n}\n" for n in range(1, 40))
        self.repo.write("before.py", body)
        base = self.repo.commit("base")
        self.repo.git("mv", "before.py", "after.py")
        self.repo.commit("rename")

        self.assertEqual(gate.added_lines(self.repo.root, base, "HEAD"), {})

    def test_a_rename_with_an_edit_counts_only_the_edit(self):
        body = "".join(f"line = {n}\n" for n in range(1, 40))
        self.repo.write("before.py", body)
        base = self.repo.commit("base")
        self.repo.git("mv", "before.py", "after.py")
        self.repo.write("after.py", body + "added = True\n")
        self.repo.commit("rename and edit")

        self.assertEqual(
            gate.added_lines(self.repo.root, base, "HEAD"),
            {"after.py": {40}},
        )

    def test_a_deleted_file_contributes_nothing(self):
        self.repo.write("gone.py", "x = 1\ny = 2\n")
        self.repo.write("kept.py", "z = 3\n")
        base = self.repo.commit("base")
        (self.repo.root / "gone.py").unlink()
        self.repo.commit("delete")

        self.assertEqual(gate.added_lines(self.repo.root, base, "HEAD"), {})


class CoveragePyReportTests(SimpleTestCase):
    def test_executable_lines_are_executed_plus_missing(self):
        root = Path("/repo")
        report = {
            "files": {
                "apps/records/thing.py": {
                    "executed_lines": [1, 2],
                    "missing_lines": [5, 6],
                    "excluded_lines": [9],
                }
            }
        }

        parsed = gate._read_coverage_py(root, report["files"])

        self.assertEqual(parsed["apps/records/thing.py"], ({1, 2, 5, 6}, {1, 2}))

    def test_a_file_outside_the_repository_is_dropped(self):
        """A report may name a site-packages path; it is not this change."""
        parsed = gate._read_coverage_py(
            Path("/repo"),
            {
                "/elsewhere/lib/thing.py": {
                    "executed_lines": [1],
                    "missing_lines": [],
                }
            },
        )

        self.assertEqual(parsed, {})

    def test_a_path_escaping_the_repository_is_dropped(self):
        parsed = gate._read_coverage_py(
            Path("/repo"),
            {"../outside/thing.py": {"executed_lines": [1], "missing_lines": []}},
        )

        self.assertEqual(parsed, {})


class IstanbulReportTests(SimpleTestCase):
    def test_statements_fold_onto_their_first_line(self):
        parsed = gate._read_istanbul(
            Path("/repo"),
            {
                "/repo/frontend/src/a.ts": {
                    "path": "/repo/frontend/src/a.ts",
                    "statementMap": {
                        "0": {"start": {"line": 3}, "end": {"line": 3}},
                        "1": {"start": {"line": 7}, "end": {"line": 9}},
                    },
                    "s": {"0": 4, "1": 0},
                }
            },
        )

        self.assertEqual(parsed["frontend/src/a.ts"], ({3, 7}, {3}))

    def test_a_line_is_covered_only_when_all_its_statements_ran(self):
        parsed = gate._read_istanbul(
            Path("/repo"),
            {
                "/repo/frontend/src/b.ts": {
                    "path": "/repo/frontend/src/b.ts",
                    "statementMap": {
                        "0": {"start": {"line": 2}, "end": {"line": 2}},
                        "1": {"start": {"line": 2}, "end": {"line": 2}},
                    },
                    "s": {"0": 1, "1": 0},
                }
            },
        )

        self.assertEqual(parsed["frontend/src/b.ts"], ({2}, set()))

    def test_a_statement_with_no_start_line_is_skipped(self):
        parsed = gate._read_istanbul(
            Path("/repo"),
            {
                "/repo/frontend/src/c.ts": {
                    "path": "/repo/frontend/src/c.ts",
                    "statementMap": {
                        "0": {"start": {}, "end": {}},
                        "1": {"start": {"line": 5}, "end": {"line": 5}},
                    },
                    "s": {"0": 0, "1": 2},
                }
            },
        )

        self.assertEqual(parsed["frontend/src/c.ts"], ({5}, {5}))

    def test_an_istanbul_file_outside_the_repository_is_dropped(self):
        parsed = gate._read_istanbul(
            Path("/repo"),
            {
                "/elsewhere/vendor.ts": {
                    "path": "/elsewhere/vendor.ts",
                    "statementMap": {"0": {"start": {"line": 1}, "end": {"line": 1}}},
                    "s": {"0": 1},
                }
            },
        )

        self.assertEqual(parsed, {})


class GateExitCodeTests(SimpleTestCase):
    def setUp(self):
        from contextlib import ExitStack

        stack = ExitStack()
        self.addCleanup(stack.close)
        self.repo = _Repository(stack)
        self.repo.write("mod.py", "keep = 0\n")
        self.base = self.repo.commit("base")

    def _run(self, report, *extra):
        path = self.repo.root / "coverage.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return gate.main(
            [
                "--base",
                self.base,
                "--coverage",
                str(path),
                "--repo-root",
                str(self.repo.root),
                *extra,
            ]
        )

    def _add_five_lines(self):
        self.repo.write(
            "mod.py",
            "keep = 0\none = 1\ntwo = 2\nthree = 3\nfour = 4\nfive = 5\n",
        )
        self.repo.commit("add five lines")

    def test_passes_when_every_added_line_is_covered(self):
        self._add_five_lines()

        self.assertEqual(
            self._run(
                {"files": {"mod.py": {"executed_lines": [1, 2, 3, 4, 5, 6], "missing_lines": []}}}
            ),
            0,
        )

    def test_fails_just_below_the_threshold(self):
        self._add_five_lines()
        # Four of the five added lines covered = 80%; three = 60%.
        report = {
            "files": {
                "mod.py": {"executed_lines": [1, 2, 3, 4], "missing_lines": [5, 6]}
            }
        }

        self.assertEqual(self._run(report), 1)

    def test_passes_exactly_at_the_threshold(self):
        self._add_five_lines()
        report = {
            "files": {
                "mod.py": {"executed_lines": [1, 2, 3, 4, 5], "missing_lines": [6]}
            }
        }

        self.assertEqual(self._run(report), 0)

    def test_honours_a_custom_minimum(self):
        self._add_five_lines()
        report = {
            "files": {
                "mod.py": {"executed_lines": [1, 2, 3, 4], "missing_lines": [5, 6]}
            }
        }

        self.assertEqual(self._run(report, "--minimum", "60"), 0)

    def test_uninstrumented_changes_do_not_apply(self):
        """A docs-only change has no instrumented lines, and that is not a fail."""
        self.repo.write("README.md", "# docs\n\nnew paragraph\n")
        self.repo.commit("docs")

        self.assertEqual(
            self._run({"files": {"mod.py": {"executed_lines": [1], "missing_lines": []}}}),
            0,
        )

    def test_non_executable_added_lines_are_not_counted(self):
        """Blank lines and comments are in the diff but not in the report."""
        self.repo.write("mod.py", "keep = 0\n\n# a comment\nreal = 1\n")
        self.repo.commit("comment plus code")
        report = {
            "files": {"mod.py": {"executed_lines": [1, 4], "missing_lines": []}}
        }

        self.assertEqual(self._run(report), 0)

    def test_an_instrumented_file_with_no_executable_change_is_skipped(self):
        """One file gains only comments; another gains real code."""
        self.repo.write("mod.py", "keep = 0\n\n# just a comment\n")
        self.repo.write("other.py", "added = 1\n")
        self.repo.commit("comment here, code there")
        report = {
            "files": {
                "mod.py": {"executed_lines": [1], "missing_lines": []},
                "other.py": {"executed_lines": [1], "missing_lines": []},
            }
        }

        self.assertEqual(self._run(report), 0)

    def test_a_missing_report_is_an_error_not_a_pass(self):
        self._add_five_lines()

        self.assertEqual(
            gate.main(
                [
                    "--base",
                    self.base,
                    "--coverage",
                    str(self.repo.root / "absent.json"),
                    "--repo-root",
                    str(self.repo.root),
                ]
            ),
            1,
        )

    def test_reports_from_both_stacks_are_merged(self):
        self._add_five_lines()
        backend = self.repo.root / "coverage.json"
        backend.write_text(
            json.dumps(
                {"files": {"mod.py": {"executed_lines": [1, 2, 3], "missing_lines": [4, 5, 6]}}}
            ),
            encoding="utf-8",
        )
        frontend = self.repo.root / "coverage-final.json"
        frontend.write_text(
            json.dumps(
                {
                    str(self.repo.root / "mod.py"): {
                        "path": str(self.repo.root / "mod.py"),
                        "statementMap": {
                            "0": {"start": {"line": 4}, "end": {"line": 4}},
                            "1": {"start": {"line": 5}, "end": {"line": 5}},
                            "2": {"start": {"line": 6}, "end": {"line": 6}},
                        },
                        "s": {"0": 1, "1": 1, "2": 1},
                    }
                }
            ),
            encoding="utf-8",
        )

        # Backend alone covers 2 of 5 added lines; the union covers all 5.
        self.assertEqual(
            gate.main(
                [
                    "--base",
                    self.base,
                    "--coverage",
                    str(backend),
                    "--coverage",
                    str(frontend),
                    "--repo-root",
                    str(self.repo.root),
                ]
            ),
            0,
        )
