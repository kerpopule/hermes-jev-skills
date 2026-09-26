"""The policy file format and the engine that applies it: lint, operands, rule order, drift.

Everything here is offline: made-up readings in, actions out. The point is that a threshold
written in a policy file means exactly what it says — and that a policy that cannot mean
anything is refused before it is ever used.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _decide_fakes  # noqa: E402,F401  (puts the repo on sys.path)
from jevkit import policy as policies  # noqa: E402

SMALL = {
    "name": "small", "version": 1, "tuned_on": "jev-1.13.0",
    "questions": {
        "risky": {"type": "noul", "instructions": "Is it risky?", "criteria": {"true": "yes", "false": "no"}},
        "lane": {"type": "choice", "instructions": "Which lane?", "criteria": {"a": "first", "b": "second", "c": None}},
        "sev": {"type": "score", "instructions": "How bad?", "criteria": ["none", "some", "much", "all"]},
    },
    "rules": [
        {"then": "stop", "any": [["risky", ">=", 0.9], {"all": [["sev.norm", ">=", 0.66], ["lane.choice", "==", "c"]]}]},
        {"then": "ask", "any": [["risky.unsure", "==", True], ["lane.margin", "<", 0.2]]},
        {"then": "go", "all": [["risky", "<", 0.3], ["lane.choice", "in", ["a", "b"]], ["lane.confidence", ">=", 0.8]]},
    ],
    "otherwise": "ask", "on_error": "fallback",
}


def values(risky=0.1, lane="a", lane_p=None, confidence=0.9, sev=0.0):
    lane_p = lane_p or {"a": 0.9, "b": 0.05, "c": 0.05} if lane == "a" else lane_p or {lane: 0.9, "a": 0.1}
    probabilities = {"a": 0.0, "b": 0.0, "c": 0.0}
    probabilities.update(lane_p)
    ordered = sorted(probabilities.values(), reverse=True)
    return {"risky": {"kind": "noul", "p": risky, "unsure": 0.3 <= risky <= 0.7},
            "lane": {"kind": "choice", "choice": lane, "confidence": confidence,
                     "margin": ordered[0] - ordered[1], "p": probabilities},
            "sev": {"kind": "score", "score": sev * 3, "norm": sev, "confidence": 0.9, "p": {}}}


class Lint(unittest.TestCase):
    def test_every_shipped_policy_is_clean(self):
        for name in policies.shipped_names():
            with self.subTest(policy=name):
                loaded = policies.load(name)
                self.assertEqual(loaded["_origin"], "shipped")
                self.assertTrue(loaded.get("tuned_on"))

    def test_the_small_policy_is_clean(self):
        self.assertEqual(policies.lint(SMALL), [])

    def bad(self, mutate, needle):
        policy = copy.deepcopy(SMALL)
        mutate(policy)
        problems = policies.lint(policy)
        self.assertTrue(any(needle in p for p in problems), problems)

    def test_refusals(self):
        self.bad(lambda p: p["rules"].append({"then": "x", "any": [["nothing", ">=", 0.5]]}), "names no question")
        self.bad(lambda p: p["rules"].append({"then": "x", "any": [["risky", ">=", 1.5]]}), "outside 0..1")
        self.bad(lambda p: p["rules"].append({"then": "x", "any": [["risky", "~=", 0.5]]}), "unknown op")
        self.bad(lambda p: p["rules"].append({"then": "x", "any": [["lane.choice", "==", "z"]]}), "not options")
        self.bad(lambda p: p["rules"].append({"then": "x", "any": [["lane.choice", ">=", 0.5]]}), "cannot be compared")
        self.bad(lambda p: p["rules"].append({"then": "x", "any": [["sev", ">=", 0.5]]}), "offers")
        self.bad(lambda p: p["rules"].append({"then": "x", "any": [["risky", "between", 0.7, 0.3]]}), "is empty")
        self.bad(lambda p: p["rules"].append({"then": "x", "all": []}), "empty all")
        self.bad(lambda p: p["rules"].append({"any": [["risky", ">=", 0.5]]}), "no \"then\"")
        self.bad(lambda p: p.update(otherwize="ask"), "unknown keys")
        self.bad(lambda p: p.update(precedence=["go", "stop", "ask"]), "does not match the rule order")
        self.bad(lambda p: p.update(actions=["stop", "go"]), "not declared")
        self.bad(lambda p: p.update(uncertain_band=[0.7, 0.3]), "uncertain_band")
        self.bad(lambda p: p.update(tuned_on="latest"), "tuned_on")
        self.bad(lambda p: p["questions"].update(bad={"type": "noul", "instructions": "Is it?", "criteria": ["y"]}),
                 "noul")

    def test_load_names_every_problem_at_once(self):
        policy = copy.deepcopy(SMALL)
        policy["rules"].append({"then": "x", "any": [["risky", ">=", 2]]})
        policy["otherwize"] = 1
        with self.assertRaises(policies.PolicyError) as raised:
            policies.load(policy)
        self.assertIn("outside", str(raised.exception))
        self.assertIn("unknown keys", str(raised.exception))

    def test_a_path_that_is_not_a_policy_is_refused(self):
        with self.assertRaises(policies.PolicyError):
            policies.load("../../etc/passwd")
        with self.assertRaises(policies.PolicyError):
            policies.load("no-such-policy")


class Apply(unittest.TestCase):
    def setUp(self):
        self.policy = policies.load(SMALL)

    def act(self, **kw):
        return policies.apply(self.policy, values(**kw))

    def test_table(self):
        cases = [
            (dict(risky=0.95), "stop", 0),
            (dict(risky=0.1, lane="c", lane_p={"c": 0.9, "a": 0.1}, sev=0.7), "stop", 0),
            (dict(risky=0.5), "ask", 1),
            (dict(risky=0.1, lane_p={"a": 0.5, "b": 0.45, "c": 0.05}), "ask", 1),
            (dict(risky=0.1), "go", 2),
            (dict(risky=0.2, confidence=0.7), "ask", None),       # no rule: otherwise
            (dict(risky=0.8), "ask", None),                       # above the band but below stop
        ]
        for kw, action, rule in cases:
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                out = self.act(**kw)
                self.assertEqual(out["action"], action)
                self.assertEqual(out["matched_rule"], rule)

    def test_first_match_wins_and_every_match_is_reported(self):
        out = self.act(risky=0.95, lane_p={"a": 0.5, "b": 0.45, "c": 0.05})
        self.assertEqual(out["action"], "stop")
        self.assertEqual(out["fired_rules"], [0, 1])

    def test_the_band_marks_unsure_readings(self):
        self.assertEqual(self.act(risky=0.3)["unsure"], ["risky"])
        self.assertEqual(self.act(risky=0.29)["unsure"], [])

    def test_explain_names_each_rule(self):
        lines = policies.explain(self.policy, values(risky=0.95))
        self.assertTrue(lines[0].startswith("rule 0 -> stop"))
        self.assertIn("MATCH", lines[0])
        self.assertEqual(lines[-1], "otherwise -> ask")

    def test_readings_turn_wire_answers_into_numbers(self):
        from _wire import choice_answer, score_answer

        q = self.policy["questions"]
        readings = policies.readings(q, {"risky": {"type": "noul", "noul": 0.4},
                                         "lane": {**choice_answer(q["lane"], "b", confidence=0.8, rest=0.2), "type": "choice"},
                                         "sev": score_answer(q["sev"], 3)})
        self.assertTrue(readings["risky"]["unsure"])
        self.assertAlmostEqual(readings["lane"]["margin"], 0.7)
        self.assertEqual(readings["sev"]["norm"], 1.0)


class Drift(unittest.TestCase):
    def test_versions(self):
        self.assertFalse(policies.drifted("jev-1.13.0", "jev-1.13.0"))
        self.assertFalse(policies.drifted("jev-1.13.0", "jev-1.13-free"))
        self.assertTrue(policies.drifted("jev-1.13.0", "jev-1.14.0"))
        self.assertTrue(policies.drifted("jev-1.13.0", "jev-1.13.2"))
        self.assertIsNone(policies.drifted("jev-1.13.0", None))
        self.assertIsNone(policies.drifted(None, "jev-1.14.0"))


class Bind(unittest.TestCase):
    def test_owner_takes_its_options_at_run_time_and_gets_none_fits(self):
        loaded = policies.load("owner", choices={"owner": {"coder": "writes code", "writer": "writes prose"}})
        self.assertEqual(set(loaded["questions"]["owner"]["criteria"]), {"coder", "writer", "none_fits"})
        self.assertEqual(loaded.get("dynamic_choices"), {})

    def test_options_for_a_fixed_question_are_refused(self):
        with self.assertRaises(policies.PolicyError):
            policies.load("retry", choices={"cause": {"a": "b"}})

    def test_different_options_give_a_different_label(self):
        one = policies.label(policies.load("owner", choices={"owner": {"a": "x", "b": "y"}}))
        two = policies.label(policies.load("owner", choices={"owner": {"a": "x", "c": "z"}}))
        self.assertNotEqual(one, two)


class ShippedPolicyTables(unittest.TestCase):
    """The shipped thresholds, read back as behaviour. A change to a number fails here first."""

    def run_policy(self, name, readings):
        loaded = policies.load(name)
        full = {}
        for qname, question in loaded["questions"].items():
            given = readings.get(qname, {})
            if question["type"] == "noul":
                p = given if isinstance(given, float) else 0.05
                full[qname] = {"kind": "noul", "p": p, "unsure": 0.3 <= p <= 0.7}
            elif question["type"] == "choice":
                options = list(question["criteria"])
                pick = given.get("choice", options[0]) if isinstance(given, dict) else options[0]
                conf = given.get("confidence", 0.95) if isinstance(given, dict) else 0.95
                full[qname] = {"kind": "choice", "choice": pick, "confidence": conf, "margin": 0.9,
                               "p": {o: (0.95 if o == pick else 0.0) for o in options}}
            else:
                norm = given if isinstance(given, float) else 0.0
                full[qname] = {"kind": "score", "score": norm * (len(question["criteria"]) - 1), "norm": norm,
                               "confidence": 0.9, "p": {}}
        return policies.apply(loaded, full)["action"]

    def test_gate_strict(self):
        g = "gate-strict"
        self.assertEqual(self.run_policy(g, {"verdict": {"choice": "approve"}}), "approve")
        self.assertEqual(self.run_policy(g, {"secrets_or_exfil": 0.9}), "deny")
        self.assertEqual(self.run_policy(g, {"destructive": 0.9, "severity": 0.7, "verdict": {"choice": "approve"}}), "deny")
        self.assertEqual(self.run_policy(g, {"verdict": {"choice": "deny", "confidence": 0.85}}), "deny")
        self.assertEqual(self.run_policy(g, {"risky": 0.5, "verdict": {"choice": "approve"}}), "ask_human")
        self.assertEqual(self.run_policy(g, {"system_or_remote": 0.4, "verdict": {"choice": "approve"}}), "ask_human")
        self.assertEqual(self.run_policy(g, {"verdict": {"choice": "approve", "confidence": 0.55}}), "ask_human")
        # approve needs every hazard low AND a confident approve verdict
        self.assertEqual(self.run_policy(g, {"risky": 0.2, "verdict": {"choice": "approve"}}), "ask_human")
        self.assertEqual(self.run_policy(g, {"verdict": {"choice": "approve", "confidence": 0.75}}), "ask_human")
        self.assertEqual(self.run_policy(g, {"risky": 0.9, "verdict": {"choice": "approve"}}), "ask_human")

    def test_gate_never_approves_on_error(self):
        for name in ("gate-strict", "gate-permissive"):
            loaded = policies.load(name)
            self.assertEqual(loaded["on_error"], "no_opinion")
            self.assertEqual(loaded["on_drift"], "no_opinion")
            self.assertEqual(loaded["otherwise"], "ask_human")

    def test_evaluator_default_is_the_articles_thresholds(self):
        e = "evaluator-default"
        good = {"quality": 0.8, "relevance": 0.95, "completeness": 0.8, "evidence_present": 0.9}
        self.assertEqual(self.run_policy(e, good), "continue")
        self.assertEqual(self.run_policy(e, {**good, "risk": 0.85}), "human_review")
        self.assertEqual(self.run_policy(e, {**good, "asks_for_human": 0.8}), "human_review")
        self.assertEqual(self.run_policy(e, {**good, "quality": 0.6}), "retry")
        self.assertEqual(self.run_policy(e, {**good, "evidence_present": 0.4}), "retry")
        self.assertEqual(self.run_policy(e, {**good, "relevance": 0.9}), "human_review")

    def test_cron_wake_wakes_easily_and_errs_toward_waking(self):
        c = "cron-wake"
        self.assertEqual(self.run_policy(c, {}), "skip")
        self.assertEqual(self.run_policy(c, {"worth_waking": 0.3}), "wake")
        self.assertEqual(self.run_policy(c, {"problem": 0.4}), "wake")
        self.assertEqual(self.run_policy(c, {"urgency": 0.75}), "wake")
        self.assertEqual(policies.load(c)["on_error"], "wake")

    def test_retry_holds_only_when_sure(self):
        r = "retry"
        self.assertEqual(self.run_policy(r, {"retry_helps": 0.1, "cause": {"choice": "needs_person_or_access"}}),
                         "hold_for_person")
        self.assertEqual(self.run_policy(r, {"retry_helps": 0.1, "cause": {"choice": "needs_person_or_access",
                                                                          "confidence": 0.7}}), "retry")
        self.assertEqual(self.run_policy(r, {"retry_helps": 0.3, "cause": {"choice": "task_spec_problem"}}), "retry")
        self.assertEqual(self.run_policy(r, {"cause": {"choice": "ran_out_of_budget"}, "progress_made": 0.7}),
                         "retry_more_budget")
        self.assertEqual(policies.load(r)["on_error"], "retry")

    def test_blockcheck_defaults_to_the_owner(self):
        b = "blockcheck"
        self.assertEqual(self.run_policy(b, {"needs_owner": 0.5}), "needs_owner")
        self.assertEqual(self.run_policy(b, {"needs_owner": 0.1, "blocker": {"choice": "other_card_or_agent"}}),
                         "probably_not_owner")
        self.assertEqual(self.run_policy(b, {"needs_owner": 0.1, "blocker": {"choice": "owner_decision"}}),
                         "needs_owner")
        self.assertEqual(self.run_policy(b, {"needs_owner": 0.9, "urgency": 0.95}), "needs_owner_now")

    def test_urgency_escalates_above_ninety(self):
        self.assertEqual(self.run_policy("triage-urgency", {"urgency": 0.95}), "escalate")
        self.assertEqual(self.run_policy("triage-urgency", {"urgency": 0.85}), "normal_queue")
        self.assertEqual(self.run_policy("kanban-event", {"urgency": 0.9}), "escalate")


if __name__ == "__main__":
    unittest.main()
