#!/usr/bin/env python3
"""Turn a mutmut JUnit XML report into a kill rate, and gate on it.

psd-dev-standards `standards/05-testing.md` rule 7: scheduled mutation testing
on Tier A critical modules, target ~80% kill, "<50% = test theater, treated as
an incident". Coverage says a line ran; a mutation score says an assertion
would have noticed it changing. For statutory deadline math that difference is
the whole point.

mutmut reports one JUnit "test case" per mutant: a failure means the mutant
SURVIVED, because the suite stayed green with the code changed underneath it.
`mutmut junitxml` is the only machine-readable output it offers, and its
emoji progress table is not something to parse.

XML is parsed through `defusedxml`, already a direct dependency in
`pyproject.toml`, and NOT through a stdlib fallback: a fallback would be the
one code path nobody exercises, and `bandit -ll` rejects
`xml.etree.ElementTree.parse` outright.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from defusedxml import ElementTree

#: mutmut's message for a mutant the suite did not notice -- the only outcome
#: that says an assertion was missing.
#:
#: Its other failure messages, `bad_timeout` and `bad_suspicious`, mean the run
#: under that mutant hung or took suspiciously long. That is not evidence an
#: assertion caught the change, and not evidence one was missing, so those
#: mutants are excluded from the rate entirely: counting them as kills would
#: flatter the score, and counting them as survivors would fail the gate on a
#: slow runner.
SURVIVED = "bad_survived"


def score(report: Path) -> tuple[int, int, list[str], list[str]]:
    """Return `(killed, scored, survivors, inconclusive)` for one report.

    `scored` is the denominator: every mutant except the inconclusive ones.
    """
    root = ElementTree.parse(report).getroot()
    # `iter` recurses, so collecting suites and then iterating each suite's
    # descendants would visit a nested suite's cases twice. Walk the document
    # once for testcases instead.
    cases = [root] if root.tag == "testcase" else list(root.iter("testcase"))
    total = 0
    survivors: list[str] = []
    inconclusive: list[str] = []
    for case in cases:
        total += 1
        name = case.get("name", "<unnamed mutant>")
        outcome = case.find("failure")
        if outcome is None:
            outcome = case.find("error")
        if outcome is None:
            continue
        if outcome.get("message") == SURVIVED:
            survivors.append(name)
        else:
            inconclusive.append(name)
    scored = total - len(inconclusive)
    return scored - len(survivors), scored, survivors, inconclusive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Output of `mutmut junitxml`.")
    parser.add_argument("--label", default="", help="Module name, for the report.")
    parser.add_argument(
        "--minimum",
        type=float,
        default=80.0,
        help="Required kill rate, as a percentage.",
    )
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(f"error: no mutation report at {args.report}", file=sys.stderr)
        return 1

    killed, scored, survivors, inconclusive = score(args.report)
    label = args.label or args.report.name
    if scored == 0:
        print(
            f"error: {label}: the report scored no mutants.",
            file=sys.stderr,
        )
        return 1

    rate = 100.0 * killed / scored
    print(f"{label}: {rate:.1f}% killed ({killed}/{scored} scored mutants)")
    for name in survivors:
        print(f"  survived: {name}")
    for name in inconclusive:
        print(f"  timed out or suspicious (not scored): {name}")

    if rate + 1e-9 < args.minimum:
        print(
            f"error: {label} mutation score {rate:.1f}% is below the required "
            f"{args.minimum:.0f}% (psd-dev-standards standards/05 rule 7). "
            "Each surviving mutant is a change the suite would not notice.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
