"""The merged request: one Jev round trip for routing's questions and skill selection's stage 1.

Two things have to hold, and only the first is obvious:

1. **One request, not two.** Jev charges per request. Measured on 2026-09-21 against the live
   API: routing alone ~540 ms, skill-pick stage 1 alone ~647 ms, both together ~620 ms.
2. **The decision does not change.** The merged answers are handed to the same policy code
   (`route.decide(answers=...)`, `skillpick.pick(stage_one=...)`), so the tiers, the floors and
   the fail-open paths must come out identical to the two-call path. If they ever stop being
   identical, `test_the_merged_answers_give_the_same_decision_as_the_two_calls` fails.

And one that is not about speed at all: the privacy boundary does not move. A private profile,
a turn that looks sensitive, or routing configured to send features only means **no merged
request at all** — not a merged request with the text removed — because skill selection refuses
those turns outright. Those tests assert the transport was never called.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevkit import client, privacy, route, skillpick, turn  # noqa: E402

TURN = "count the lines of code in this repo and write the total into the changelog"

SKILLS = [
    {"name": "codebase-inspection", "description": "Count lines of code with pygount", "path": "skills/a/SKILL.md"},
    {"name": "promo-video", "description": "Render a promotional video", "path": "skills/b/SKILL.md"},
]

BIG = "example:big"
SMALL = "example:cheap"


def config(**extra):
    return {**route.DEFAULT_CONFIG,
            "tiers": {"simple": {"general": [SMALL]}, "medium": {"general": [BIG]}, "hard": {"general": [BIG]}},
            **extra}


ROWS = [{"provider": "example", "model": "cheap", "price": 0.1, "context": 200_000, "vision": False},
        {"provider": "example", "model": "big", "price": 5.0, "context": 200_000, "vision": False}]


class Wire:
    """A deterministic Jev: a hard, coding turn whose best skill is S0.

    Deliberately answers by question *type* rather than by name, so it replies correctly
    whether the two decisions arrive in one request or in two.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, body, headers, timeout):
        request = json.loads(body)
        self.calls.append(request)
        answers = {}
        for name, question in request["questions"].items():
            if question["type"] == "score":
                # 0.1 + 0.1 + 0.45*2 + 0.35*3 = 2.05, and the point estimate agrees with it.
                answers[name] = {"type": "score", "score": 2.05, "confidence": 0.9,
                                 "probabilities": {"0": 0.1, "1": 0.1, "2": 0.45, "3": 0.35}}
            elif question["type"] == "choice":
                keys = list(question["criteria"])
                best = "coding" if "coding" in keys else ("S0" if "S0" in keys else keys[0])
                rest = (1.0 - 0.9) / max(1, len(keys) - 1)
                answers[name] = {"type": "choice", "choice": best, "confidence": 0.9,
                                 "probabilities": {key: (0.9 if key == best else rest) for key in keys}}
            else:
                answers[name] = {"type": "noul", "noul": 0.9 if name in ("needs_skill", "s0") else 0.2}
        return json.dumps({"model": "jev-test", "answers": answers, "usage": {"input_tokens": 1}}).encode()


class OneRequestTests(unittest.TestCase):
    def test_one_request_carries_both_decisions(self):
        wire = Wire()
        merged = turn.decide_turn(TURN, SKILLS, config=config(), transport=wire)
        self.assertEqual(merged["status"], "ok")
        self.assertEqual(len(wire.calls), 1, "one request, never one per decision")
        asked = set(wire.calls[0]["questions"])
        self.assertEqual(asked, {"difficulty", "kind", "costly_mistake", "pick:0"})
        self.assertEqual(set(wire.calls[0]["state"]), {"user_turn", "context"})

    def test_the_merged_answers_give_the_same_decision_as_the_two_calls(self):
        """The whole point: fewer round trips, the same answers acted on.

        Skill selection is stage 1 + stage 2 (the verification depends on stage 1), so the
        counts here are 3 requests on the old path and 2 on the new one — one merged request
        plus the same stage 2.
        """
        separate = Wire()
        solo_route = route.decide(TURN, current="example:cheap", config=config(), rows=ROWS, transport=separate)
        solo_pick = skillpick.pick(TURN, SKILLS, transport=separate)
        self.assertEqual(len(separate.calls), 3, "routing + stage 1 + stage 2")

        merged_wire = Wire()
        merged = turn.decide_turn(TURN, SKILLS, config=config(), transport=merged_wire)
        self.assertEqual(merged["status"], "ok")
        merged_route = route.decide(TURN, current="example:cheap", config=config(), rows=ROWS,
                                    answers=merged["route_answers"], latency_ms=merged["latency_ms"])
        merged_pick = skillpick.pick(TURN, SKILLS, transport=merged_wire, stage_one=merged["stage_one"],
                                     stage_one_latency=merged["latency_ms"])
        self.assertEqual(len(merged_wire.calls), 2, "one merged request + stage 2")
        self.assertEqual((merged_route["model"], merged_route["tier"], merged_route["confidence"]),
                         (solo_route["model"], solo_route["tier"], solo_route["confidence"]))
        self.assertEqual((merged_pick["skills"], merged_pick["needs_skill"]),
                         (solo_pick["skills"], solo_pick["needs_skill"]))
        self.assertEqual(merged_pick["skills"][0]["name"], "codebase-inspection")

    def test_the_merged_state_carries_redacted_text(self):
        wire = Wire()
        turn.decide_turn("ping dana@northside-partners.test about " + TURN, SKILLS, config=config(), transport=wire)
        body = json.dumps(wire.calls[0])
        self.assertNotIn("dana@northside-partners.test", body)
        self.assertIn("count the lines of code", body)

    def test_the_turn_is_sent_once_not_twice(self):
        """Measured live: carrying the turn twice in the merged state dropped routing's
        difficulty confidence from 0.70-0.75 to 0.50-0.62 on the same turn, which is the
        difference between routing a borderline turn and leaving it where it was."""
        wire = Wire()
        marker = "count the lines of code in this repo"
        turn.decide_turn(marker, SKILLS, config=config(), transport=wire)
        self.assertEqual(json.dumps(wire.calls[0]).count(marker), 1)

    def test_a_lost_request_fails_open_rather_than_inventing_an_answer(self):
        def down(body, headers, timeout):
            raise client.JevError("network")
        merged = turn.decide_turn(TURN, SKILLS, config=config(), transport=down)
        self.assertEqual(merged["status"], "fail_open")
        self.assertIn("network", merged["reason"])
        self.assertNotIn("route_answers", merged)


class PrivacyBoundaryTests(unittest.TestCase):
    """Not merged means *no request at all*, so these assert on call counts."""

    def test_a_private_profile_is_never_merged(self):
        wire = Wire()
        merged = turn.decide_turn(TURN, SKILLS, profile="billing", config=config(private_profiles=["billing"]),
                                  transport=wire)
        self.assertEqual(merged["status"], "not_mergeable")
        self.assertIn("billing", merged["reason"])
        self.assertEqual(wire.calls, [])

    def test_a_turn_that_looks_sensitive_is_never_merged(self):
        self.assertTrue(privacy.is_sensitive("my password is hunter2"), "fixture must really look sensitive")
        wire = Wire()
        merged = turn.decide_turn("my password is hunter2, store it", SKILLS, config=config(), transport=wire)
        self.assertEqual((merged["status"], wire.calls), ("not_mergeable", []))
        self.assertIn("sensitive", merged["reason"])

    def test_features_only_routing_is_never_merged(self):
        wire = Wire()
        merged = turn.decide_turn(TURN, SKILLS, config=config(mode="features"), transport=wire)
        self.assertEqual((merged["status"], wire.calls), ("not_mergeable", []))

    def test_no_catalog_means_no_merge_and_no_skills_rather_than_a_wasted_call(self):
        wire = Wire()
        merged = turn.decide_turn(TURN, [], config=config(), transport=wire)
        self.assertEqual((merged["status"], wire.calls), ("not_mergeable", []))

    def test_an_empty_turn_is_never_merged(self):
        wire = Wire()
        self.assertEqual(turn.decide_turn("   ", SKILLS, config=config(), transport=wire)["status"], "not_mergeable")
        self.assertEqual(wire.calls, [])


class MergedStageOneTests(unittest.TestCase):
    def test_a_merged_batch_that_is_missing_fails_the_pick_open(self):
        """A merged request that lost a batch is the same failure as a lost round trip: the right
        skill may have been in it, and `none` from the batches that answered would read as clean."""
        out = skillpick.pick(TURN, SKILLS, stage_one={})
        self.assertEqual((out["status"], out["skills"]), ("fail_open", []))
        self.assertIn("stage 1 incomplete", out["reason"])

    def test_the_trivial_gate_still_answers_locally_without_touching_the_merged_answers(self):
        out = skillpick.pick("thanks, that worked", SKILLS, stage_one={})
        self.assertEqual((out["status"], out.get("skipped")), ("ok", "trivial"))


if __name__ == "__main__":
    unittest.main()
