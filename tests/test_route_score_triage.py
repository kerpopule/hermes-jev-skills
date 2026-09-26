"""route-to, score and the triage presets, offline.

route-to: a pick is returned only when it is clear (confidence AND margin), none_fits is always
offered, and more than one Choice can hold needs groups. score: the article's thresholds on a
rubric, with the caller's own levels. triage: support mail is exactly what it was; the
presets are named policies.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import Scripted, TempHome  # noqa: E402
from jevkit import evaluate, policy as policies, route_to, triage  # noqa: E402

DESTS = {"coder": "Writes and fixes code", "writer": "Writes documents and posts", "ops": "Deploys and monitors"}


class RouteTo(TempHome):
    def test_a_clear_pick_is_routed(self):
        out = route_to.route_to({"task": "fix the failing test"}, DESTS, fallback="triage",
                                transport=Scripted({"dest": "coder"}, confidence=0.93, rest=0.05))
        self.assertEqual(out["dest"], "coder")
        self.assertTrue(out["routed"])
        self.assertGreaterEqual(out["margin"], 0.25)

    def test_none_fits_is_offered_and_means_fallback(self):
        fake = Scripted({"dest": "none_fits"}, confidence=0.95)
        out = route_to.route_to("water the plants", DESTS, fallback="triage", transport=fake)
        self.assertIn("none_fits", fake.requests[0]["questions"]["dest"]["criteria"])
        self.assertEqual(out["dest"], "triage")
        self.assertFalse(out["routed"])
        self.assertEqual(out["pick"], "none_fits")

    def test_low_confidence_or_a_thin_lead_falls_back(self):
        low = route_to.route_to("x", DESTS, fallback="triage", transport=Scripted({"dest": "coder"}, confidence=0.7))
        self.assertEqual(low["dest"], "triage")
        thin = route_to.route_to("x", DESTS, fallback="triage",
                                 transport=Scripted({"dest": "coder"}, confidence=0.9, rest=0.6))
        self.assertLess(thin["margin"], 0.25)
        self.assertEqual(thin["dest"], "triage")

    def test_failure_falls_back(self):
        out = route_to.route_to("x", DESTS, fallback="triage", transport=Scripted(fail="timeout"))
        self.assertEqual(out["dest"], "triage")
        self.assertTrue(out["fallback_used"])

    def test_none_fits_is_reserved(self):
        with self.assertRaises(ValueError):
            route_to.route_to("x", {"none_fits": "no"}, transport=Scripted())

    def test_too_many_without_groups_is_refused_locally(self):
        fake = Scripted()
        with self.assertRaises(ValueError) as raised:
            route_to.route_to("x", {f"d{i}": "a lane" for i in range(300)}, transport=fake)
        self.assertIn("group", str(raised.exception))
        self.assertEqual(fake.bodies, [])

    def test_two_stages_with_groups(self):
        many = {f"d{i}": {"description": f"lane {i}", "group": "even" if i % 2 == 0 else "odd"} for i in range(300)}
        fake = Scripted({"dest": "even"}, confidence=0.95)

        def transport(body, headers, timeout):
            request = json.loads(body)
            options = request["questions"]["dest"]["criteria"]
            fake.values["dest"] = "even" if "even" in options else "d42"
            return fake(body, headers, timeout)

        out = route_to.route_to("x", many, fallback="triage", transport=transport)
        self.assertEqual(out["stage"], "member")
        self.assertEqual(out["group"], "even")
        self.assertEqual(out["dest"], "d42")
        self.assertEqual(len(fake.bodies), 2)


class Score(TempHome):
    GOOD = {"quality": 4, "relevance": 4, "completeness": 3, "risk": 0, "evidence_present": 0.9, "asks_for_human": 0.05}

    def test_continue_retry_review(self):
        state = {"task": "write the release note", "output": "Release note at docs/release.md, tests pass"}
        self.assertEqual(evaluate.score(state, transport=Scripted(self.GOOD))["action"], "continue")
        self.assertEqual(evaluate.score(state, transport=Scripted({**self.GOOD, "quality": 2}))["action"], "retry")
        self.assertEqual(evaluate.score(state, transport=Scripted({**self.GOOD, "risk": 3}))["action"], "human_review")
        out = evaluate.score(state, transport=Scripted(self.GOOD))
        self.assertEqual(out["scores"]["quality"], 1.0)

    def test_failure_is_the_callers_default(self):
        out = evaluate.score({"task": "t", "output": "o"}, transport=Scripted(fail="timeout"))
        self.assertEqual(out["action"], "caller_default")

    def test_a_rubric_rewrites_the_levels_and_keeps_the_rules(self):
        fake = Scripted(self.GOOD)
        rubric = {"quality": {"instructions": "How well does `output` answer the customer?",
                              "levels": ["Wrong", "Vague", "Right"]}}
        out = evaluate.score({"task": "t", "output": "o"}, rubric=rubric, transport=fake)
        sent = fake.requests[0]["questions"]["quality"]
        self.assertEqual(sent["criteria"], ["Wrong", "Vague", "Right"])
        self.assertNotEqual(out["policy"], policies.label(policies.load("evaluator-default")))

    def test_rubric_lint(self):
        for rubric, needle in (({"quality": {"instructions": "x", "levels": ["only"]}}, "levels"),
                               ({"nonsense": {"instructions": "x", "levels": ["a", "b"]}}, "not a score")):
            with self.subTest(needle=needle), self.assertRaises(policies.PolicyError) as raised:
                evaluate.score({"task": "t"}, rubric=rubric, transport=Scripted())
            self.assertIn(needle, str(raised.exception))


class TriagePresets(TempHome):
    def test_support_mail_is_classify_unchanged(self):
        message = {"subject": "Cannot log in", "body": "Every login fails since this morning.", "sender": "a@b.example"}
        one = triage.classify_state(message, "support-mail", transport=Scripted({"urgency": 4, "kind": "problem"}))
        two = triage.classify(message["subject"], message["body"], sender=message["sender"],
                              transport=Scripted({"urgency": 4, "kind": "problem"}))
        one.pop("latency_ms", None)
        two.pop("latency_ms", None)
        self.assertEqual(one, two)
        self.assertEqual(one["route"], "now")

    def test_presets_are_policies(self):
        out = triage.classify_state({"event": "blocked", "card_title": "Ship the site", "text": "production is down"},
                                    "kanban-event", transport=Scripted({"urgency": 4}))
        self.assertEqual(out["action"], "escalate")
        out = triage.classify_state({"job": "nightly", "purpose": "watch the build", "changes": "timestamps only",
                                     "last_report": "all green"}, "cron-wake", transport=Scripted())
        self.assertEqual(out["action"], "skip")
        out = triage.classify_state({"job": "nightly"}, "cron-wake", transport=Scripted(fail="timeout"))
        self.assertEqual(out["action"], "wake")

    def test_unknown_preset(self):
        with self.assertRaises(ValueError):
            triage.classify_state({}, "nope")


if __name__ == "__main__":
    unittest.main()
