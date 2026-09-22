"""The two-question gate eval: one live call, two questions, and a finding of "no".

`scripts/calibrate_choose_match.py` exists to answer one question - does gating `choose`
on a second `noul` question ("does any candidate match the goal?") buy fewer wrong
actions? The measured answer is no, and the scorecard in `evals/choose-match/` records
it. These tests keep the harness honest, because a harness that mis-reports its own rows
is what the first run of this script did: it counted every good row as a failure.

Nothing here calls Jev. The transport is faked in every test.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "calibrate_choose_match", REPO / "scripts" / "calibrate_choose_match.py")
match_eval = importlib.util.module_from_spec(_spec)
sys.modules["calibrate_choose_match"] = match_eval
_spec.loader.exec_module(match_eval)

from jevkit import client  # noqa: E402


def _row(kind: str = "answerable", right: str | None = "btn-go",
         goal: str = "Go back to the previous page.", raw: str | None = "btn-go",
         confidence: float = 0.99, match: float | None = 0.95, error: str | None = None):
    return {"kind": kind, "goal": goal, "right": right, "raw": raw,
            "confidence": confidence, "match": match, "error": error}


def _case():
    """A real case from the shared set, so the request is a real one."""
    return match_eval._cases()[0]


class AskShapeTests(unittest.TestCase):
    """One request carrying both questions is the whole cost argument."""

    def _capture(self, reply):
        calls = []

        def fake_ask(state, questions, **kwargs):
            calls.append((state, questions, kwargs))
            return reply

        with mock.patch.object(match_eval.client, "ask", fake_ask):
            row = match_eval.run(_case())
        return calls, row

    def test_both_questions_go_in_one_request(self):
        calls, _ = self._capture({"answers": {
            "next_action": {"type": "choice", "choice": "reobserve", "confidence": 1.0, "probabilities": {}},
            "any_match": {"type": "noul", "noul": 0.9}}, "latency_ms": 12})
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0][1]), {"next_action", "any_match"})
        self.assertEqual(calls[0][1]["next_action"]["type"], "choice")
        self.assertEqual(calls[0][1]["any_match"]["type"], "noul")

    def test_the_match_question_names_the_two_safety_valves(self):
        """`reobserve` and `abstain` are always on the table and always "match" in the weak
        sense. A question that does not exclude them is answered yes on every screen ever
        observed, which would make the second question look free by being useless."""
        calls, _ = self._capture({"answers": {
            "next_action": {"type": "choice", "choice": "reobserve", "confidence": 1.0, "probabilities": {}},
            "any_match": {"type": "noul", "noul": 0.9}}})
        instructions = calls[0][1]["any_match"]["instructions"]
        self.assertIn("reobserve", instructions)
        self.assertIn("abstain", instructions)

    def test_the_state_sent_is_the_one_choose_validates(self):
        """The eval is only meaningful if it asks Jev what production asks Jev."""
        case = _case()
        valid = match_eval.choose.validate(case["request"])
        calls, _ = self._capture({"answers": {
            "next_action": {"type": "choice", "choice": "reobserve", "confidence": 1.0, "probabilities": {}},
            "any_match": {"type": "noul", "noul": 0.9}}})
        self.assertEqual(calls[0][0]["goal"], valid["goal"])
        self.assertEqual(calls[0][0]["on_screen"], valid["regions"])
        self.assertEqual(calls[0][1]["next_action"]["criteria"], valid["table"])

    def test_a_good_row_keeps_both_answers(self):
        _, row = self._capture({"answers": {
            "next_action": {"type": "choice", "choice": "btn-mute", "confidence": 0.97, "probabilities": {"btn-mute": 0.97}},
            "any_match": {"type": "noul", "noul": 0.91}}, "latency_ms": 20})
        self.assertIsNone(row["error"])
        self.assertEqual(row["raw"], "btn-mute")
        self.assertEqual(row["confidence"], 0.97)
        self.assertEqual(row["match"], 0.91)

    def test_a_jev_failure_is_an_error_and_never_a_guess(self):
        def boom(*_args, **_kwargs):
            raise client.JevError("network")

        with mock.patch.object(match_eval.client, "ask", boom):
            row = match_eval.run(_case())
        self.assertIn("network", row["error"])
        self.assertIsNone(row["raw"])
        self.assertIsNone(row["match"])


class SplitTests(unittest.TestCase):
    """The bug the first run had: a success carries a reason for acting, so a filter on
    truthiness sent all 31 good rows down the failure path and printed "no usable rows"
    for a run that had worked."""

    def test_successful_rows_are_answered_not_failed(self):
        rows = [_row(), _row(goal="Mute the volume."), _row(error="Jev unavailable (timeout)")]
        answered, failed = match_eval.split(rows)
        self.assertEqual(len(answered), 2)
        self.assertEqual(len(failed), 1)
        self.assertTrue(all(r["error"] is None for r in answered))

    def test_an_empty_run_is_reported_as_empty(self):
        answered, failed = match_eval.split([])
        self.assertEqual((answered, failed), ([], []))


class ScoreTests(unittest.TestCase):
    """`score` decides what the scorecard says, so its four outcomes must stay distinct."""

    def test_a_no_answer_case_that_acts_counts_as_a_wrong_action(self):
        # conf above the floor, no right action on screen: decline was free, acting was not
        s = match_eval.score([_row(kind="no_answer", right=None, raw="btn-play", confidence=0.72)], 0.65, None)
        self.assertEqual(s["wrong_action"], 1)
        self.assertEqual(s["declined_ok"], 0)

    def test_a_no_answer_case_the_gate_blocks_is_declined_not_stalled(self):
        # declining here is correct, so it must never be counted as a stall
        s = match_eval.score([_row(kind="no_answer", right=None, raw="btn-play", confidence=0.30)], 0.65, None)
        self.assertEqual((s["declined_ok"], s["stalled"], s["wrong_action"]), (1, 0, 0))

    def test_a_right_answer_the_floor_blocks_is_a_stall(self):
        s = match_eval.score([_row(confidence=0.60)], 0.65, None)
        self.assertEqual((s["stalled"], s["right"]), (1, 0))

    def test_a_right_answer_the_match_question_blocks_is_also_a_stall(self):
        s = match_eval.score([_row(confidence=0.99, match=0.55)], 0.65, 0.70)
        self.assertEqual((s["stalled"], s["right"]), (1, 0))

    def test_the_two_gates_are_a_conjunction_not_either_one(self):
        # confidence passes, the second question does not: the answer must not be acted on
        s = match_eval.score([_row(confidence=0.99, match=0.30)], 0.65, 0.60)
        self.assertEqual((s["stalled"], s["right"]), (1, 0))

    def test_the_trap_that_motivates_the_floor_stays_blocked_by_it(self):
        """The one known wrong answer, at the numbers five live runs gave it: confidence
        0.45-0.60, match 0.79-0.83. The floor blocks it; the second question on its own does
        not, which is the finding - the two questions are not interchangeable."""
        trap = _row(kind="trap", right="btn-cancel", raw="btn-save", confidence=0.48, match=0.79)
        self.assertEqual(match_eval.score([trap], 0.65, None)["wrong_action"], 0)
        self.assertEqual(match_eval.score([trap], 0.65, 0.70)["stalled"], 1)
        self.assertEqual(match_eval.score([trap], 0.0, 0.60)["wrong_action"], 1)

    def test_the_observed_slip_at_a_lower_floor_is_why_the_floor_is_065(self):
        """At 0.60 this case acted once in five live runs and was wrong. Encoded so that
        anyone tempted to lower the floor has to argue with the case, not with the number."""
        trap = _row(kind="trap", right="btn-cancel", raw="btn-save", confidence=0.60, match=0.83)
        self.assertEqual(match_eval.score([trap], 0.60, None)["wrong_action"], 1)
        self.assertEqual(match_eval.score([trap], 0.65, None)["wrong_action"], 0)


class FailOpenTests(unittest.TestCase):
    """No gate in this repo may crash a runner. A Jev outage is the case that matters."""

    def test_a_failed_call_is_excluded_rather_than_scored_as_a_decline(self):
        rows = [_row(), _row(kind="no_answer", right=None, raw=None, confidence=0.0, match=None,
                             error="Jev unavailable (no_key)")]
        answered, failed = match_eval.split(rows)
        self.assertEqual(len(failed), 1)
        s = match_eval.score(answered, 0.65, 0.60)
        self.assertEqual(sum(s.values()), len(answered))


if __name__ == "__main__":
    unittest.main()