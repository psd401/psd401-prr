#!/usr/bin/env python3
"""Patch-coverage gate: of the lines this change adds, how many does a test run?

psd-dev-standards `standards/05-testing.md` rule 4 gates on patch coverage, not
on a project percentage: "that number is gameable and lies". An agent can hit
any project figure with assertion-free tests, and a project floor punishes a
change that touches a long-untested file. Changed lines are the thing a reviewer
actually cares about.

Inputs:

* a git range, from which the ADDED lines are read (deletions are nothing to
  cover, and context lines belong to earlier changes);
* one or more coverage reports, from any mix of the two formats this repository
  produces:
  - coverage.py JSON (`coverage json`), for the Django suite;
  - Istanbul/V8 JSON (`coverage-final.json`), for Vitest.

A line counts toward the denominator only if the coverage report says it is
executable, so comments, blank lines, docstrings, and type-only declarations
neither help nor hurt. A changed file with no coverage data at all is reported
and skipped -- the report, not this script, decides what is instrumented.

That last rule is the gate's deliberate boundary, and it is worth stating
plainly: a change confined to code no report instruments passes with "patch
coverage does not apply". Today that is `infra/cdk` (gated instead by its own
`bun test`, typecheck, and credential-free `cdk synth` in the `infrastructure`
CI job), plus docs, workflows, and shell. Widening the gate means widening a
coverage report's scope -- `[tool.coverage.run] source` in `pyproject.toml`, or
`coverage.include` in `frontend/vite.config.ts` -- not special-casing paths
here.

Stdlib only, so it runs in any CI step that has Python.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

#: The file a hunk belongs to is read from `diff --git a/<old> b/<new>`, never
#: from the `+++ b/<path>` line. Git prefixes added content with one `+`, so a
#: line whose own text starts with `++ ` arrives as `+++ ...` and a parser
#: keying on that prefix would mistake it for the next file's header and drop
#: every remaining hunk of the file it is in -- silently, as missing lines.
DIFF_HEADER = re.compile(r'^diff --git (?:"?a/(?P<old>.*?)"?) (?:"?b/(?P<new>.*?)"?)$')


def added_lines(repo_root: Path, base: str, head: str) -> dict[str, set[int]]:
    """Line numbers added by `base...head`, keyed by repository-relative path."""
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--diff-filter=d",
            # Explicit, rather than inherited from whatever `diff.renames` the
            # runner's git config happens to carry. Without rename detection a
            # `git mv` arrives as a delete plus an add, and the moved file's
            # whole body would be charged as added lines.
            "--find-renames",
            f"{base}...{head}",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    added: dict[str, set[int]] = defaultdict(set)
    path: str | None = None
    for line in diff.splitlines():
        header = DIFF_HEADER.match(line)
        if header:
            path = header.group("new")
            continue
        hunk = HUNK.match(line)
        if hunk and path:
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            added[path].update(range(start, start + count))
    return dict(added)


def _relative(repo_root: Path, raw: str) -> str | None:
    """A report path as a repository-relative path, or None if it escapes.

    A coverage report is generated in-run by a trusted step, but it names
    paths this script then reads back, so containment is checked for relative
    paths too rather than only for absolute ones.
    """
    root = repo_root.resolve()
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def read_coverage(repo_root: Path, report: Path) -> dict[str, tuple[set[int], set[int]]]:
    """Return `{path: (executable_lines, covered_lines)}` from either format."""
    document = json.loads(report.read_text(encoding="utf-8"))
    if isinstance(document.get("files"), dict):
        return _read_coverage_py(repo_root, document["files"])
    return _read_istanbul(repo_root, document)


def _read_coverage_py(repo_root, files) -> dict[str, tuple[set[int], set[int]]]:
    parsed: dict[str, tuple[set[int], set[int]]] = {}
    for raw, entry in files.items():
        path = _relative(repo_root, raw)
        if path is None:
            continue
        executed = set(entry.get("executed_lines", ()))
        missing = set(entry.get("missing_lines", ()))
        parsed[path] = (executed | missing, executed)
    return parsed


def _read_istanbul(repo_root, document) -> dict[str, tuple[set[int], set[int]]]:
    """Istanbul reports statements, not lines; fold them onto their first line.

    A line is executable when a statement starts on it and covered when any
    statement starting there ran. That is how Istanbul's own `lines` summary is
    derived, so the number here matches the report a developer reads locally.
    """
    parsed: dict[str, tuple[set[int], set[int]]] = {}
    for raw, entry in document.items():
        if not isinstance(entry, dict) or "statementMap" not in entry:
            continue
        path = _relative(repo_root, entry.get("path", raw))
        if path is None:
            continue
        hits = entry.get("s", {})
        ran: dict[int, bool] = {}
        for statement_id, location in entry["statementMap"].items():
            line = location.get("start", {}).get("line")
            if not line:
                continue
            # A line holding several statements counts as covered only when
            # every one of them ran, so a half-executed ternary is a miss.
            ran[line] = ran.get(line, True) and hits.get(statement_id, 0) > 0
        executable = set(ran)
        covered = {line for line, was_run in ran.items() if was_run}
        parsed[path] = (executable, covered)
    return parsed


def merge(reports: list[dict[str, tuple[set[int], set[int]]]]):
    merged: dict[str, tuple[set[int], set[int]]] = {}
    for report in reports:
        for path, (executable, covered) in report.items():
            previous = merged.get(path, (set(), set()))
            merged[path] = (previous[0] | executable, previous[1] | covered)
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Commit the change branches from.")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--coverage",
        action="append",
        default=[],
        type=Path,
        required=True,
        help="A coverage.py JSON or Istanbul coverage-final.json report. Repeatable.",
    )
    parser.add_argument("--minimum", type=float, default=80.0)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    missing_reports = [report for report in args.coverage if not report.is_file()]
    if missing_reports:
        for report in missing_reports:
            print(f"error: no coverage report at {report}", file=sys.stderr)
        return 1

    coverage = merge([read_coverage(repo_root, report) for report in args.coverage])
    changed = added_lines(repo_root, args.base, args.head)

    total = 0
    hit = 0
    uninstrumented: list[str] = []
    rows: list[tuple[str, int, int, list[int]]] = []
    for path in sorted(changed):
        if path not in coverage:
            uninstrumented.append(path)
            continue
        executable, covered = coverage[path]
        relevant = changed[path] & executable
        if not relevant:
            continue
        missed = sorted(relevant - covered)
        total += len(relevant)
        hit += len(relevant) - len(missed)
        rows.append((path, len(relevant) - len(missed), len(relevant), missed))

    print(f"Patch coverage for {args.base}...{args.head}")
    for path, path_hit, path_total, missed in rows:
        percent = 100.0 * path_hit / path_total
        detail = f" missing: {', '.join(str(line) for line in missed)}" if missed else ""
        print(f"  {percent:6.1f}%  {path_hit:4d}/{path_total:<4d} {path}{detail}")
    if uninstrumented:
        print("  not instrumented by any report (not counted):")
        for path in uninstrumented:
            print(f"    {path}")

    if total == 0:
        print("No instrumented lines were added; patch coverage does not apply.")
        return 0

    percent = 100.0 * hit / total
    print(f"TOTAL {percent:.1f}% ({hit}/{total} added lines covered)")
    if percent + 1e-9 < args.minimum:
        print(
            f"error: patch coverage {percent:.1f}% is below the required "
            f"{args.minimum:.0f}% (psd-dev-standards standards/05 rule 4).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
