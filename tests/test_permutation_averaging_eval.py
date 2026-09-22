"""Permutation averaging was measured and NOT adopted — pin the mechanics and the failure.

`scripts/calibrate_choose_permutations.py` and `scripts/calibrate_skillpick_permutations.py`
are run by hand against the live API, so nothing else in this suite would notice if the
replay math drifted, or if someone later wired pijev-style confidence into `choose` without
seeing the measurement that rejected it. The trap fixtures below are the REAL recorded
numbers from the 2026-09-22 runs (raw at /tmp/perm-avg-choose-raw.json and
/tmp/perm-avg-trap-repeat.json; numbers restated in
evals/permutation-averaging/SCORECARD-2026-09-22.md).

Everything here is offline: no network, no key, no model.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHOOSE_SCRIPT = REPO / "scripts" / "calibrate_choose_permutations.py"
SKILLPICK_SCRIPT = REPO / "scripts" / "calibrate_skillpick_permutations.py"
SCORECARD = REPO / "evals" / "permutation-averaging" / "SCORECARD-2026-09-22.md"

sys.path.insert(0, str(REPO))
from jevkit import choose  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


P = load("calibrate_choose_permutations_under_test", CHOOSE_SCRIPT)
SK = load("calibrate_skillpick_permutations_under_test", SKILLPICK_SCRIPT)


def trap_answers(pairs):
    """pairs: [(p(btn-cancel), p(btn-save)), ...] as {"name": answer}, in order."""
    answers = {}
    for index, (cancel, save) in enumerate(pairs):
        name = "next_action" if index == 0 else f"__p{index}"
        answers[name] = {"choice": "btn-cancel" if cancel > save else "btn-save",
                         "confidence": max(cancel, save),
                         "probabilities": {"btn-cancel": cancel, "btn-save": save,
                                           "reobserve": round(1.0 - cancel - save, 3)}}
    return answers


# First collection run of the known trap, all nine orders, recorded verbatim
# ("Cancel this dialog without losing my work"; the right action is btn-cancel).
RUN1 = [(0.55, 0.44), (0.46, 0.53), (0.43, 0.57), (0.46, 0.54), (0.38, 0.62),
        (0.45, 0.55), (0.54, 0.45), (0.36, 0.64), (0.36, 0.63)]
# `--trap-repeat` runs 1 and 3: nine orders each, the wrong answer won 9-0 both times.
REPEAT_RUN1 = [(0.41, 0.58), (0.36, 0.63), (0.29, 0.71), (0.22, 0.78), (0.31, 0.68),
               (0.30, 0.70), (0.25, 0.75), (0.23, 0.76), (0.31, 0.69)]
REPEAT_RUN3 = [(0.33, 0.67), (0.27, 0.73), (0.32, 0.68), (0.26, 0.74), (0.21, 0.79),
               (0.16, 0.84), (0.29, 0.71), (0.20, 0.79), (0.28, 0.71)]


class ReplayMathTests(unittest.TestCase):
    def test_mean_probs_normalizes_each_vector_then_averages(self):
        answers = {"a": {"probabilities": {"x": 0.5, "y": 0.5}},
                   "b": {"probabilities": {"x": 0.2, "y": 0.6}}}   # sums to 0.8; pijev normalizes
        mean = P.mean_probs(answers, ["a", "b"])
        self.assertAlmostEqual(sum(mean.values()), 1.0)
        self.assertAlmostEqual(mean["x"], (0.5 + 0.25) / 2)
        self.assertAlmostEqual(mean["y"], (0.5 + 0.75) / 2)

    def test_mean_probs_of_no_answer_is_no_answer(self):
        self.assertEqual(P.mean_probs({}, []), {})

    def test_a_label_only_some_orders_know_about_counts_as_zero_there(self):
        answers = {"a": {"probabilities": {"x": 1.0}}, "b": {"probabilities": {"x": 0.5, "y": 0.5}}}
        mean = P.mean_probs(answers, ["a", "b"])
        self.assertAlmostEqual(mean["y"], 0.25)

    def test_winner_breaks_ties_deterministically(self):
        self.assertEqual(P.winner({"b": 0.5, "a": 0.5}), "a")

    def test_average_answer_reports_the_winners_mean_probability_not_jevs_confidence(self):
        """The pijev confidence swap is the mechanism this eval priced — it must stay visible."""
        answers = trap_answers(RUN1)
        top, confidence = P.average_answer(answers, list(answers))
        self.assertEqual(top, "btn-save")
        self.assertAlmostEqual(confidence, 0.552, places=3)
        natives = {a["confidence"] for a in answers.values()}
        self.assertNotIn(round(confidence, 3), {round(v, 3) for v in natives})


class TheMeasuredFailureTests(unittest.TestCase):
    """The recorded numbers behind the non-adoption. If these drift, the story changed."""

    def test_the_two_order_fix_was_pair_luck(self):
        """B2 flipped the trap right; every larger m picked the wrong answer. Recorded why."""
        answers = trap_answers(RUN1)
        self.assertEqual(P.average_answer(answers, ["next_action", "__p1"])[0], "btn-cancel")
        self.assertEqual(P.average_answer(answers, list(answers))[0], "btn-save")

    def test_every_aggregation_picked_the_wrong_answer_over_nine_orders(self):
        answers = trap_answers(RUN1)
        mean = P.mean_probs(answers, list(answers))
        median = {k: sorted(v["probabilities"].get(k, 0.0) for v in answers.values())[len(answers) // 2]
                  for k in mean}
        self.assertEqual(P.winner(mean), "btn-save")
        self.assertEqual(P.winner(median), "btn-save")

    def test_the_confidence_swap_took_the_trap_through_the_shipped_floor(self):
        """0.698 and 0.740 at a 0.65 floor = acting wrong actions, twice in three repeats.

        Solo's native confidence sat at 0.52 and 0.43 — declined every time. This is the
        reason permutation averaging is NOT in `choose`, pinned so it is not re-litigated
        from pijev's Jensen argument alone.
        """
        for pairs, recorded in ((REPEAT_RUN1, 0.698), (REPEAT_RUN3, 0.740)):
            with self.subTest(recorded=recorded):
                answers = trap_answers(pairs)
                top, confidence = P.average_answer(answers, list(answers))
                self.assertEqual(top, "btn-save")          # the wrong action
                self.assertAlmostEqual(confidence, recorded, places=3)
                self.assertGreaterEqual(confidence, 0.65)  # the shipped default floor acts here
                self.assertTrue(all(a["choice"] == "btn-save" for a in answers.values()),
                                "agreement is not honesty: 9-0 twice")

    def test_native_confidence_stayed_under_the_floor_where_averaging_crossed_it(self):
        for pairs, native in ((REPEAT_RUN1, 0.52), (REPEAT_RUN3, 0.43)):
            answers = trap_answers(pairs)
            _, confidence = P.average_answer(answers, list(answers))
            self.assertGreater(confidence, native)
            self.assertLess(native, choose.MIN_CONFIDENCE)


class SkillpickReplayTests(unittest.TestCase):
    def test_normalize_scales_one_vector_to_sum_one(self):
        self.assertEqual(SK.normalize({"a": 2.0, "b": 2.0}), {"a": 0.5, "b": 0.5})
        self.assertEqual(SK.normalize({}), {})

    def test_averaged_stage_one_groups_by_batch_and_averages_each_label(self):
        row = {"shuf": {"pick:0": {"S0": 0.4, "none": 0.6}, "shuf:0:0": {"S0": 0.8, "none": 0.2},
                        "pick:120": {"S120": 0.3, "none": 0.7}, "shuf:120:0": {"S120": 0.3, "none": 0.7}}}
        stage = SK.averaged_stage_one(row, list(row["shuf"]))
        self.assertEqual(set(stage), {0, 120})
        self.assertAlmostEqual(stage[0]["S0"], (0.4 + 0.8) / 2)
        self.assertAlmostEqual(stage[120]["S120"], 0.3)


class TheRecordTests(unittest.TestCase):
    def test_the_scorecard_exists_and_says_not_adopted(self):
        text = SCORECARD.read_text(encoding="utf-8")
        self.assertIn("NOT adopted", text)
        for name in ("calibrate_choose_permutations.py", "calibrate_skillpick_permutations.py"):
            self.assertIn(name, text)
        self.assertIn("0.698", text)
        self.assertIn("0.740", text)


if __name__ == "__main__":
    unittest.main()
