"""The stakes-and-margin eval only means anything if it keeps measuring what it says.

`scripts/calibrate_choose_stakes.py` is run by hand against the live API. Nothing else in the
suite would notice if its bar were relaxed after the fact, if the stakes marking stopped
marking the irreversible traps, if the shipped floor dropped out of the family list, or if the
scorecard lost the numbers that make the decision durable.

These checks are offline: they read files and import the script, and never call Jev.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "calibrate_choose_stakes.py"
SCORECARD = REPO / "evals" / "choose-match" / "SCORECARD-2026-09-22-stakes-and-margin.md"

sys.path.insert(0, str(REPO))

from jevkit import choose  # noqa: E402


def load_script():
    spec = importlib.util.spec_from_file_location("calibrate_choose_stakes_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["calibrate_choose_stakes_under_test"] = module
    spec.loader.exec_module(module)
    return module


class StakesEvalTests(unittest.TestCase):
    def setUp(self):
        self.eval = load_script()

    def test_the_bar_is_stated_before_the_numbers(self):
        """"Zero wrong actions and fewer stalls" is the ship rule, and it has to live in the file."""
        doc = self.eval.__doc__ or ""
        self.assertIn("ZERO wrong actions", doc)
        self.assertIn("strictly fewer stalls", doc)

    def test_the_script_reuses_the_floor_calibration_cases(self):
        """A second case set would let one be tuned without the other noticing."""
        self.assertIs(self.eval.CASES, self.eval.cases_module.CASES)
        self.assertGreaterEqual(len(self.eval.CASES), 30)
        self.assertEqual({case["kind"] for case in self.eval.CASES}, set(self.eval.KINDS))

    def test_the_shipped_floor_is_one_of_the_families(self):
        labels = [label for label, _ in self.eval.families()]
        self.assertIn(f"floor {choose.MIN_CONFIDENCE:.2f} only", labels)
        self.assertTrue(any(label.startswith("stakes:") for label in labels))
        self.assertTrue(any("margin" in label for label in labels))

    def test_stakes_marks_the_irreversible_traps_and_not_the_ordinary_screens(self):
        marked = {case["goal"] for case in self.eval.CASES if self.eval.irreversible_ids(case)}
        self.assertIn("Cancel this dialog without losing my work.", marked,
                      "the case the whole finding turns on")
        for goal in ("Open the Sound settings pane.", "Turn on Dark mode.", "Mute the volume."):
            self.assertNotIn(goal, marked, "an ordinary reversible screen is not a stakes screen")

    def test_no_answer_cases_are_never_scored_as_right(self):
        """The metric's floor: a decline is never a wrong action, and never a right one."""
        row = {"right": None, "raw": "reobserve", "confidence": 1.0, "margin": 1.0, "stakes": []}
        self.assertFalse(self.eval.act(row, floor=0.0))
        self.assertEqual(self.eval.score([row], floor=0.0)["declined_ok"], 1)

    def test_the_safety_valves_are_never_acted_on_at_any_floor(self):
        """This is why the band is empty: `reobserve` and `abstain` are declined whatever the floor."""
        for floor in (0.0, 0.55, 0.65, 0.95):
            for valve in sorted(self.eval.SAFE):
                row = {"right": "btn-x", "raw": valve, "confidence": 1.0, "margin": 1.0, "stakes": []}
                self.assertFalse(self.eval.act(row, floor=floor), f"{valve} at floor {floor}")

    def test_the_finding_did_not_change_the_shipped_floor(self):
        """A measurement that was not shipped leaves the constant where it was."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_MIN_CONFIDENCE", None)
            self.assertEqual(choose._floor(), 0.65)

    def test_the_scorecard_records_the_decision_with_its_numbers(self):
        text = SCORECARD.read_text(encoding="utf-8")
        for needed in ("nothing was shipped", "calibrate_choose_stakes.py", "0.93", "0.42",
                       "Cancel this dialog without losing my work"):
            self.assertIn(needed, text, f"the scorecard lost {needed!r}")

    def test_no_jev_call_runs_at_import(self):
        live_calls = ("choose.choose(", "client.ask(")
        for line in SCRIPT.read_text(encoding="utf-8").splitlines():
            at_module_level = line[:1] not in ("", " ", "\t") and not line.startswith("#")
            if at_module_level:
                for call in live_calls:
                    self.assertNotIn(call, line, f"module-level live call: {line}")


if __name__ == "__main__":
    unittest.main()
