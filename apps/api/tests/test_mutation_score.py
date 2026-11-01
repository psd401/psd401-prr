"""Tests for the mutation-score gate (`scripts/mutation_score.py`).

The XML shapes below are mutmut 2.x's real `junitxml` output, abridged: a
killed mutant is a bare `<testcase>`, a survivor carries
`<failure message="bad_survived">`, and a timeout carries
`message="bad_timeout"`. Getting that distinction backwards would either report
test theater as healthy or fail every scheduled run.
"""
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .gate_scripts import load_gate_script

gate = load_gate_script("mutation_score.py")


def _report(cases: str) -> str:
    return (
        '<?xml version="1.0" ?>\n'
        "<testsuites>\n"
        '  <testsuite name="mutmut">\n'
        f"{cases}"
        "  </testsuite>\n"
        "</testsuites>\n"
    )


KILLED = '    <testcase name="Mutant #{n}" file="m.py" line="1"/>\n'
SURVIVED = (
    '    <testcase name="Mutant #{n}" file="m.py" line="1">\n'
    '      <failure type="failure" message="bad_survived">diff</failure>\n'
    "    </testcase>\n"
)
TIMED_OUT = (
    '    <testcase name="Mutant #{n}" file="m.py" line="1">\n'
    '      <failure type="failure" message="bad_timeout">diff</failure>\n'
    "    </testcase>\n"
)


class MutationScoreTests(SimpleTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _write(self, cases):
        path = self.directory / "mutmut.xml"
        path.write_text(_report(cases), encoding="utf-8")
        return path

    def test_a_bare_testcase_is_a_killed_mutant(self):
        path = self._write("".join(KILLED.format(n=n) for n in range(1, 4)))

        self.assertEqual(gate.score(path), (3, 3, [], []))

    def test_an_inconclusive_mutant_leaves_the_rate_alone(self):
        """A timeout is neither a kill nor a survivor, so it is not scored."""
        killed_only = self._write("".join(KILLED.format(n=n) for n in range(1, 5)))
        with_timeout = self.directory / "timeout.xml"
        with_timeout.write_text(
            _report(
                "".join(KILLED.format(n=n) for n in range(1, 5))
                + TIMED_OUT.format(n=5)
            ),
            encoding="utf-8",
        )

        self.assertEqual(gate.score(killed_only)[:2], (4, 4))
        killed, scored, survivors, inconclusive = gate.score(with_timeout)
        self.assertEqual((killed, scored), (4, 4))
        self.assertEqual(survivors, [])
        self.assertEqual(inconclusive, ["Mutant #5"])

    def test_bad_survived_is_the_only_survivor(self):
        path = self._write(
            KILLED.format(n=1) + SURVIVED.format(n=2) + TIMED_OUT.format(n=3)
        )

        killed, scored, survivors, inconclusive = gate.score(path)

        # Three mutants, one of them not scored: 1 killed of 2 scored.
        self.assertEqual((killed, scored), (1, 2))
        self.assertEqual(survivors, ["Mutant #2"])
        self.assertEqual(inconclusive, ["Mutant #3"])

    def test_passes_at_exactly_the_floor(self):
        cases = "".join(KILLED.format(n=n) for n in range(1, 5)) + SURVIVED.format(n=5)

        self.assertEqual(gate.main([str(self._write(cases)), "--minimum", "80"]), 0)

    def test_fails_below_the_floor(self):
        cases = "".join(KILLED.format(n=n) for n in range(1, 4)) + "".join(
            SURVIVED.format(n=n) for n in range(4, 6)
        )

        self.assertEqual(gate.main([str(self._write(cases)), "--minimum", "80"]), 1)

    def test_an_unscored_mutant_is_reported_but_passes(self):
        cases = "".join(KILLED.format(n=n) for n in range(1, 5)) + TIMED_OUT.format(n=5)

        self.assertEqual(gate.main([str(self._write(cases)), "--minimum", "80"]), 0)

    def test_an_empty_report_is_an_error_not_a_pass(self):
        """A run that mutated nothing must not report a perfect score."""
        self.assertEqual(gate.main([str(self._write(""))]), 1)

    def test_a_report_of_nothing_but_timeouts_is_an_error(self):
        """Every mutant unscored is a broken run, not a 100% kill rate."""
        cases = "".join(TIMED_OUT.format(n=n) for n in range(1, 4))

        self.assertEqual(gate.main([str(self._write(cases))]), 1)

    def test_a_missing_report_is_an_error(self):
        self.assertEqual(gate.main([str(self.directory / "absent.xml")]), 1)

    def test_a_bare_testsuite_root_is_read(self):
        """mutmut has shipped both a `testsuites` and a `testsuite` root."""
        path = self.directory / "flat.xml"
        path.write_text(
            '<?xml version="1.0" ?>\n<testsuite name="mutmut">\n'
            + KILLED.format(n=1)
            + SURVIVED.format(n=2)
            + "</testsuite>\n",
            encoding="utf-8",
        )

        self.assertEqual(gate.score(path), (1, 2, ["Mutant #2"], []))

    def test_nested_suites_do_not_double_count(self):
        path = self.directory / "nested.xml"
        path.write_text(
            '<?xml version="1.0" ?>\n<testsuites>\n'
            '  <testsuite name="outer">\n    <testsuite name="inner">\n'
            + KILLED.format(n=1)
            + SURVIVED.format(n=2)
            + "    </testsuite>\n  </testsuite>\n</testsuites>\n",
            encoding="utf-8",
        )

        self.assertEqual(gate.score(path), (1, 2, ["Mutant #2"], []))
