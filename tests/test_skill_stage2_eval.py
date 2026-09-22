"""The stage-2 eval is only worth its numbers if its cases stay well formed.

`scripts/calibrate_skill_stage2.py` is run by hand against the live API, so nothing else in
this suite would notice if a case lost its expectation, named a skill this repo does not ship,
or if someone quietly deleted the four turns that want no skill at all — which are the only
ones where the two policies disagree on the small catalog.

These checks are offline: they read files and import the script, and never call Jev.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "calibrate_skill_stage2.py"
SCORECARD = REPO / "evals" / "skill-pick" / "SCORECARD-2026-09-22.md"


def load_script():
    spec = importlib.util.spec_from_file_location("calibrate_skill_stage2_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["calibrate_skill_stage2_under_test"] = module
    spec.loader.exec_module(module)
    return module


class StageTwoEvalTests(unittest.TestCase):
    def setUp(self):
        self.script = load_script()
        self.shipped = {path.parent.name for path in (REPO / "skills").glob("*/SKILL.md")}

    def test_every_case_carries_a_turn_and_an_expectation(self):
        for case in self.script.CASES:
            self.assertTrue(case["turn"].strip(), case)
            self.assertIn("expect", case, case)

    def test_an_expectation_names_a_skill_this_repo_ships(self):
        """"The right skill" has to mean something a clone of this repo can also reach."""
        for case in self.script.CASES:
            if case["expect"] is not None:
                self.assertIn(case["expect"], self.shipped, case)

    def test_the_no_skill_cases_are_still_there(self):
        """They are the whole small-catalog result: stage 1 offers on all of them, stage 2 on none."""
        none_cases = [c for c in self.script.CASES if c["expect"] is None]
        self.assertGreaterEqual(len(none_cases), 4)
        self.assertGreaterEqual(len(self.script.CASES) - len(none_cases), 8)

    def test_the_scorecard_records_the_decision_and_its_numbers(self):
        text = SCORECARD.read_text(encoding="utf-8")
        self.assertIn("Stage 2 stays", text)
        self.assertIn("calibrate_skill_stage2.py", text)
        self.assertIn("dogfood", text, "the confident-wrong case is the reason the decision went this way")

    def test_no_jev_call_runs_at_import(self):
        """A call at module level would fire during collection, in a suite that has to stay offline."""
        live_calls = ("client.ask(", "skillpick.pick(", "turn.decide_turn(")
        for line in SCRIPT.read_text(encoding="utf-8").splitlines():
            at_module_level = line[:1] not in ("", " ", "\t") and not line.startswith("#")
            if at_module_level:
                for call in live_calls:
                    self.assertNotIn(call, line, f"module-level Jev call: {line}")


if __name__ == "__main__":
    unittest.main()
