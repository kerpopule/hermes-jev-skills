"""Hermetic tests for jevkit.effort — the difficulty→level mapping.

No network, no key, no Hermes: the pick is pure function over the answer object
routing already bought. Every fail-open path is asserted, because a wrong effort
is worse than no effort — it spends tokens the operator did not ask to spend.
"""
import unittest

from jevkit import effort


def answers(score, confidence=0.9, spread=None):
    return {"difficulty": {"type": "score", "score": score, "confidence": confidence,
                           "probabilities": spread or {}}}


class PickTests(unittest.TestCase):
    def test_trivial_turns_get_low(self):
        self.assertEqual(effort.pick(answers(0.2)), "low")

    def test_routine_turns_get_medium(self):
        self.assertEqual(effort.pick(answers(1.0)), "medium")

    def test_substantial_turns_get_high(self):
        self.assertEqual(effort.pick(answers(2.0)), "high")

    def test_expert_turns_get_xhigh(self):
        self.assertEqual(effort.pick(answers(3.0)), "xhigh")

    def test_a_custom_table_is_honored(self):
        levels = ("minimal", "low", "high", "max")
        self.assertEqual(effort.pick(answers(0.1), levels=levels), "minimal")
        self.assertEqual(effort.pick(answers(3.0), levels=levels), "max")

    def test_a_spread_beats_the_averaged_score(self):
        # Score averages 1.5 (would round to routine), but the probability mass sits on expert.
        spread = {0: 0.05, 1: 0.1, 2: 0.15, 3: 0.7}
        self.assertEqual(effort.pick(answers(1.5, spread=spread)), "xhigh")

    def test_string_keyed_spread_is_read(self):
        spread = {"0": 0.05, "1": 0.1, "2": 0.7, "3": 0.15}
        self.assertEqual(effort.pick(answers(1.5, spread=spread)), "high")

    def test_no_answers_means_no_effort(self):
        self.assertIsNone(effort.pick(None))
        self.assertIsNone(effort.pick({}))
        self.assertIsNone(effort.pick({"kind": {"choice": "coding"}}))

    def test_a_score_outside_the_rubric_is_rejected(self):
        self.assertIsNone(effort.pick(answers(-0.5)))
        self.assertIsNone(effort.pick(answers(3.4)))
        self.assertIsNone(effort.pick(answers("hard")))

    def test_a_bad_level_name_in_the_table_is_rejected(self):
        self.assertIsNone(effort.pick(answers(2.0), levels=("low", "medium", "drop table", "xhigh")))

    def test_a_short_table_is_rejected(self):
        self.assertIsNone(effort.pick(answers(2.0), levels=("low", "high")))


class LevelsFromConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertIsNone(effort.levels_from_config({}))
        self.assertIsNone(effort.levels_from_config({"effort": {"enabled": False}}))

    def test_enabled_with_no_table_uses_the_default(self):
        self.assertEqual(effort.levels_from_config({"effort": {"enabled": True}}),
                         effort.DEFAULT_LEVELS)
        # Explicit null means "no opinion": the default table, not a rejection.
        self.assertEqual(effort.levels_from_config({"effort": {"enabled": True, "levels": None}}),
                         effort.DEFAULT_LEVELS)

    def test_a_four_element_list(self):
        levels = ["low", "low", "high", "max"]
        self.assertEqual(effort.levels_from_config({"effort": {"enabled": True, "levels": levels}}),
                         ("low", "low", "high", "max"))

    def test_a_dict_names_buckets_and_falls_back(self):
        table = effort.levels_from_config({"effort": {"enabled": True,
                                                      "levels": {"expert": "max"}}})
        self.assertEqual(table, ("low", "medium", "high", "max"))

    def test_a_malformed_table_is_rejected_wholesale(self):
        self.assertIsNone(effort.levels_from_config(
            {"effort": {"enabled": True, "levels": ["low", "medium", "high"]}}))
        self.assertIsNone(effort.levels_from_config(
            {"effort": {"enabled": True, "levels": "low,medium,high,xhigh"}}))
        self.assertIsNone(effort.levels_from_config(
            {"effort": {"enabled": True, "levels": ["low", "medium", "high; rm -rf /", "xhigh"]}}))

    def test_non_mapping_config(self):
        self.assertIsNone(effort.levels_from_config(None))
        self.assertIsNone(effort.levels_from_config("off"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
