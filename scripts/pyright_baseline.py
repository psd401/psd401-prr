#!/usr/bin/env python3
"""Pyright ratchet: no file may gain a type error, and fixes get recorded.

psd-dev-standards `standards/05-testing.md` rule 1 asks for a strict typecheck
on every repository. This one cannot pass clean today: pyright reports 1,848
errors on the commit that added it, and nearly all of them are django-stubs not
modelling a reverse accessor or a test-client keyword rather than a defect in
this code. Turning that into a merge gate on day one would mean either 1,848
`# type: ignore` comments or a gate everybody routes around.

So the gate is a ratchet instead. `pyright-baseline.json` records how many
errors each file had, and this script fails when:

* a file has MORE errors than the baseline -- including a file the baseline has
  never seen, which starts at zero. That is the regression half;
* a file has FEWER errors than the baseline. That is not a failure of the code,
  it is a stale baseline, and the message says to rewrite it. Without this half
  the number never actually ratchets down: an improvement would sit unrecorded
  and the slack it left could be spent again tomorrow.

The comparison is per file, never a whole-tree total. A total would let a fix
in one module pay for a break in another, which is the one thing a ratchet
exists to prevent.

Usage:

    python scripts/pyright_baseline.py            # run pyright and check
    python scripts/pyright_baseline.py --update   # rewrite the baseline
    python scripts/pyright_baseline.py --report r.json   # use a saved run

After merging two branches that both touched `pyright-baseline.json`, do not
hand-resolve the conflict: taking either side wholesale throws away the other
branch's recorded fixes. Take either version, then run `--update` on the
merged tree and commit what it writes.

The counts are read from whatever environment pyright finds (`venvPath` in
`pyproject.toml` points at `.venv`), so a checkout whose `.venv` predates the
current `uv.lock` type-checks against older stubs and can disagree with CI.
`uv sync --locked` first if the numbers look unfamiliar.

Stdlib only, so it runs in any CI step that has Python.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "pyright-baseline.json"


def counts(report: dict[str, Any], *, root: Path) -> dict[str, int]:
    """Error counts per repository-relative path, zero-count files omitted."""
    tally: Counter[str] = Counter()
    for diagnostic in report.get("generalDiagnostics", ()):
        if diagnostic.get("severity") != "error":
            continue
        path = Path(diagnostic["file"])
        try:
            name = path.relative_to(root).as_posix()
        except ValueError:
            # A diagnostic outside the checkout (a stub inside the virtualenv,
            # say) is not this repository's to ratchet.
            continue
        tally[name] += 1
    return dict(tally)


def compare(
    baseline: dict[str, int], current: dict[str, int]
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int, int]]]:
    """`(regressions, improvements)` as `(path, was, now)`, each sorted."""
    regressions: list[tuple[str, int, int]] = []
    improvements: list[tuple[str, int, int]] = []
    for name in sorted(set(baseline) | set(current)):
        was = baseline.get(name, 0)
        now = current.get(name, 0)
        if now > was:
            regressions.append((name, was, now))
        elif now < was:
            improvements.append((name, was, now))
    return regressions, improvements


def read_baseline(path: Path) -> dict[str, int]:
    """The recorded counts, or an empty mapping when there is no baseline."""
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8"))["files"])


def write_baseline(path: Path, current: dict[str, int], *, version: str = "") -> None:
    """Record the counts, sorted, so a later diff reads as the change itself."""
    payload = {
        "pyright": version or _pinned_pyright_version(),
        "total": sum(current.values()),
        "files": {name: current[name] for name in sorted(current)},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _pinned_pyright_version() -> str:
    """The version `pyproject.toml` pins, so the baseline says what made it."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    marker = '"pyright=='
    start = text.index(marker) + len(marker)
    return text[start : text.index('"', start)]


def run_pyright() -> dict[str, Any]:
    """Run pyright over the paths `[tool.pyright]` names and parse its report."""
    completed = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # Pyright exits non-zero whenever it reported an error, which is the normal
    # case here; only an unparseable stdout means the run itself failed.
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit("pyright did not produce a JSON report") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="the recorded per-file counts (default: pyright-baseline.json)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="a saved `pyright --outputjson` run instead of a fresh one",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the baseline from this run instead of checking it",
    )
    args = parser.parse_args(argv)

    report = (
        json.loads(args.report.read_text(encoding="utf-8"))
        if args.report
        else run_pyright()
    )
    current = counts(report, root=REPO_ROOT)

    if args.update:
        write_baseline(args.baseline, current)
        print(
            f"Recorded {sum(current.values())} pyright errors "
            f"across {len(current)} files in {args.baseline.name}."
        )
        return 0

    regressions, improvements = compare(read_baseline(args.baseline), current)

    for name, was, now in regressions:
        print(f"REGRESSED {name}: {was} -> {now} pyright errors")
    for name, was, now in improvements:
        print(f"IMPROVED  {name}: {was} -> {now} pyright errors")

    if regressions:
        print(
            f"\n{len(regressions)} file(s) gained pyright errors. Fix them, or "
            "if the new errors are unavoidable say so in the pull request and "
            "rerun with --update.",
            file=sys.stderr,
        )
        return 1
    if improvements:
        print(
            f"\n{len(improvements)} file(s) have fewer pyright errors than the "
            "baseline records. Rerun with --update and commit "
            f"{args.baseline.name} so the gate keeps the ground you took.",
            file=sys.stderr,
        )
        return 1

    print(f"pyright baseline holds: {sum(current.values())} errors, none new.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
