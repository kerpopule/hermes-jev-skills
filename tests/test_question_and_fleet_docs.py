"""Guard the two docs that carry a rule an agent is expected to follow.

`docs/writing-a-jev-question.md` states a phrasing rule, and
`docs/using-jev-in-a-hermes-fleet.md` states the posture a fleet runs Jev under. Both are
prose, so nothing else in the suite would notice if one were emptied, unlinked, or had its
rule quietly deleted while the file stayed where it was.

The last test is the one this repo has had to learn twice: a docs file is the easiest place
for a machine-specific absolute path to leak into a public repo, because prose does not look
like configuration.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
QUESTION_DOC = REPO / "docs" / "writing-a-jev-question.md"
FLEET_DOC = REPO / "docs" / "using-jev-in-a-hermes-fleet.md"

# An absolute path that names one machine. A tilde-relative path is fine: every Hermes home is
# `~/.hermes`, and that is the documented convention rather than a leak.
_MACHINE_PATH = re.compile(r"(?:/Users/|/home/[A-Za-z0-9._-]+/|/opt/data/|[A-Za-z]:\\\\Users)")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class TestTheDocsArePresent(unittest.TestCase):
    def test_both_docs_exist_and_are_not_stubs(self):
        for path in (QUESTION_DOC, FLEET_DOC):
            body = read(path)
            self.assertTrue(body, f"{path.name} is missing or empty")
            self.assertGreater(len(body), 1_000, f"{path.name} is too short to carry a rule")

    def test_the_readme_links_both(self):
        readme = read(README)
        for path in (QUESTION_DOC, FLEET_DOC):
            self.assertIn(f"docs/{path.name}", readme)


class TestTheQuestionRuleSurvives(unittest.TestCase):
    def test_it_still_says_which_attribute_is_the_requirement(self):
        body = read(QUESTION_DOC)
        self.assertIn("requirement", body)
        self.assertIn("preference", body)

    def test_it_keeps_the_measured_numbers_and_their_source(self):
        """The numbers are the whole argument for changing a habit. Guard them from drift."""
        body = read(QUESTION_DOC)
        for number in ("16 of 44", "33 of 44"):
            self.assertIn(number, body)
        self.assertIn("x.com/dsqjaffa/status/2102054148925526206", body)

    def test_it_keeps_the_caveat_beside_the_claim(self):
        """A self-reported vendor result must not lose its caveat in an edit."""
        body = read(QUESTION_DOC)
        self.assertIn("Self-reported", body)

    def test_it_covers_all_three_shapes(self):
        body = read(QUESTION_DOC)
        for shape in ("Choice", "Score", "Noul"):
            self.assertIn(shape, body)


class TestTheFleetPostureSurvives(unittest.TestCase):
    def test_it_still_forbids_a_bypass_and_separates_fail_open(self):
        body = read(FLEET_DOC)
        self.assertIn("Never bypass Jev", body)
        self.assertIn("Fail-open is not a bypass", body)
        self.assertIn("reobserve", body)

    def test_it_keeps_the_pointer_convention(self):
        body = read(FLEET_DOC)
        self.assertIn("shared/rules/", body)
        self.assertIn("-fleet.md", body)

    def test_it_keeps_the_threshold_warning(self):
        """Substituting a self-reporting model silently changes every floor. Say so."""
        body = read(FLEET_DOC)
        self.assertIn("reports its own confidence", body)
        self.assertIn("GUI action", body)


class TestNoMachinePathsLeak(unittest.TestCase):
    def test_neither_doc_names_a_machine(self):
        for path in (QUESTION_DOC, FLEET_DOC):
            match = _MACHINE_PATH.search(read(path))
            self.assertIsNone(
                match,
                f"{path.name} names a specific machine: {match.group(0) if match else ''}",
            )


if __name__ == "__main__":
    unittest.main()