"""Code decides first, and a report says how to tune.

Two things a backtest on real fleet history taught (2026-09-26):

* Some decisions are facts, not judgement: the monitor's own source failed, the card's first
  run, a rate-limit error the respawn guard already owns. A policy's ``pre_rules`` read the
  caller's ``facts`` and decide those without a request — nothing sent, nothing paid — and a
  ``fact.<key>`` can sit beside Jev's readings in an ordinary rule. ``--jev-only`` skips them,
  so the same rows can measure Jev alone.
* Pass/fail is not enough to tune a threshold. For the probability each feature leans on, the
  report gives the AUC (is there any signal?), a reliability table and a threshold sweep.

Offline throughout: a scripted Jev, a throwaway Hermes home.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _decide_fakes import Scripted, TempHome  # noqa: E402
from jevkit import batch, decide as engine, policy as policies, shadow  # noqa: E402
from jevkit import cli_decide  # noqa: E402

FIXTURES = HERE.parent / "evals" / "gate" / "fixtures.jsonl"

CODE_FIRST = {
    "name": "code-first", "version": 1, "tuned_on": "jev-1.13.0",
    "questions": {"worth": {"type": "noul", "instructions": "Is this worth a look?"}},
    "pre_rules": [
        {"then": "wake", "any": [["fact.source_failed", "==", True], ["fact.hours_quiet", ">=", 48]]},
        {"then": "skip", "all": [["fact.kind", "in", ["heartbeat", "ping"]]]},
    ],
    "rules": [
        {"then": "wake", "any": [["worth", ">=", 0.5], {"all": [["worth", ">=", 0.3], ["fact.owner_waiting", "==", True]]}]},
    ],
    "otherwise": "skip", "on_error": "wake",
}


class PreRulesLint(unittest.TestCase):
    def test_the_policy_is_clean_and_lists_every_action(self):
        self.assertEqual(policies.lint(CODE_FIRST), [])
        self.assertEqual(policies.action_set(policies.load(CODE_FIRST)), ["wake", "skip"])

    def problems(self, mutate):
        policy = copy.deepcopy(CODE_FIRST)
        mutate(policy)
        return " ".join(policies.lint(policy))

    def test_a_pre_rule_cannot_read_jev(self):
        found = self.problems(lambda p: p["pre_rules"].append({"then": "wake", "any": [["worth", ">=", 0.5]]}))
        self.assertIn("can only read fact.<key>", found)

    def test_fact_names_and_shapes_are_checked(self):
        self.assertIn("fact.<lowercase_key>", self.problems(
            lambda p: p["pre_rules"][0]["any"].append(["fact.Bad-Key", "==", True])))
        self.assertIn("not a number", self.problems(
            lambda p: p["pre_rules"][0]["any"].append(["fact.hours_quiet", ">=", "6"])))
        self.assertIn("takes a list", self.problems(
            lambda p: p["pre_rules"][1]["all"].append(["fact.kind", "in", "ping"])))
        self.assertIn("pre_rules must be a list", self.problems(lambda p: p.update(pre_rules={"then": "x"})))

    def test_a_fact_threshold_is_not_held_to_the_zero_to_one_scale(self):
        self.assertEqual(self.problems(lambda p: p["pre_rules"][0]["any"].append(["fact.hours_quiet", ">", 500])), "")

    def test_a_pre_rule_action_must_be_declared_when_actions_are(self):
        found = self.problems(lambda p: p.update(actions=["wake"]))
        self.assertIn("'skip'", found)


class PreRulesDecide(TempHome):
    def test_a_pre_rule_decides_without_a_request(self):
        fake = Scripted({"worth": 0.99})
        out = engine.decide({"text": "anything"}, CODE_FIRST, transport=fake, facts={"source_failed": True})
        self.assertEqual(out["action"], "wake")
        self.assertEqual(out["source"], "code")
        self.assertEqual(out["status"], "code")
        self.assertEqual(out["matched_pre_rule"], 0)
        self.assertFalse(out["sent_to_jev"])
        self.assertFalse(out["fallback_used"])
        self.assertEqual(fake.bodies, [])
        row = self.ledger_rows()[-1]
        self.assertEqual((row["status"], row["source"], row["cost_usd"]), ("code", "code", 0.0))

    def test_first_matching_pre_rule_wins_and_an_unsupplied_fact_never_holds(self):
        fake = Scripted({"worth": 0.1})
        out = engine.decide({"t": 1}, CODE_FIRST, transport=fake, facts={"kind": "ping"})
        self.assertEqual((out["action"], out["matched_pre_rule"]), ("skip", 1))
        out = engine.decide({"t": 1}, CODE_FIRST, transport=fake, facts={"hours_quiet": None, "other": 1})
        self.assertEqual(out["source"], "jev")
        self.assertEqual(len(fake.bodies), 1)

    def test_facts_are_never_sent(self):
        fake = Scripted({"worth": 0.1})
        engine.decide({"t": 1}, CODE_FIRST, transport=fake, facts={"owner_waiting": True, "secret_count_zz": 7731})
        self.assertNotIn("secret_count_zz", fake.bodies[0].decode())
        self.assertNotIn("7731", fake.bodies[0].decode())

    def test_a_fact_beside_a_reading_in_an_ordinary_rule(self):
        fake = Scripted({"worth": 0.35})
        self.assertEqual(engine.decide({"t": 1}, CODE_FIRST, transport=fake)["action"], "skip")
        out = engine.decide({"t": 1}, CODE_FIRST, transport=fake, facts={"owner_waiting": True})
        self.assertEqual((out["action"], out["source"], out["matched_rule"]), ("wake", "jev", 0))

    def test_jev_only_skips_the_pre_rules(self):
        fake = Scripted({"worth": 0.1})
        out = engine.decide({"t": 1}, CODE_FIRST, transport=fake, facts={"source_failed": True}, code_first=False)
        self.assertEqual((out["action"], out["source"]), ("skip", "jev"))

    def test_a_failure_says_fallback(self):
        out = engine.decide({"t": 1}, CODE_FIRST, transport=Scripted(fail="overloaded"), retries=0)
        self.assertEqual((out["action"], out["source"]), ("wake", "fallback"))

    def test_the_shipped_cron_wake_wakes_a_first_run_for_free(self):
        fake = Scripted({"worth_waking": 0.0})
        out = engine.decide({"job": "x", "changes": "nothing"}, "cron-wake", transport=fake,
                            facts={"first_run": True, "source_failed": False})
        self.assertEqual((out["action"], out["source"], fake.bodies), ("wake", "code", []))

    def test_the_shipped_retry_leaves_rate_limits_to_the_respawn_guard(self):
        fake = Scripted()
        out = engine.decide({"outcome": "crashed"}, "retry", transport=fake, facts={"rate_limited_or_auth": True})
        self.assertEqual((out["action"], out["source"], fake.bodies), ("defer_to_respawn_guard", "code", []))


class Rescore(unittest.TestCase):
    def test_a_pre_rule_needs_no_answers(self):
        out = engine.rescore(CODE_FIRST, {}, facts={"source_failed": True})
        self.assertEqual((out["action"], out["source"]), ("wake", "code"))

    def test_bare_logged_readings_are_read(self):
        """Another logger's shape: a noul as its bare probability, a choice or score without `kind`."""
        self.assertEqual(engine.rescore(CODE_FIRST, {"worth": 0.8})["action"], "wake")
        out = engine.rescore("retry", {
            "retry_helps": 0.05,
            "cause": {"choice": "needs_person_or_access", "confidence": 0.9,
                      "p": {"needs_person_or_access": 0.9, "unknown": 0.1}},
            "progress_made": 0.2})
        self.assertEqual(out["action"], "hold_for_person")
        self.assertEqual(out["answers"]["cause"]["margin"], 0.8)

    def test_a_reading_of_the_wrong_kind_is_refused(self):
        with self.assertRaises(ValueError):
            engine.rescore(CODE_FIRST, {"worth": {"choice": "a"}})
        with self.assertRaises(ValueError):
            engine.rescore(CODE_FIRST, {"worth": True})


class BatchWithFacts(TempHome):
    def rows(self):
        return [{"id": "a", "state": {"t": 1}, "facts": {"source_failed": True}, "truth": "report"},
                {"id": "b", "state": {"t": 2}, "facts": {"kind": "ping"}, "truth": "silent"},
                {"id": "c", "state": {"t": 3}, "facts": {}, "truth": "silent"}]

    def test_code_rows_are_free_and_counted(self):
        estimate = batch.estimate(self.rows(), policies.load(CODE_FIRST))
        self.assertEqual(estimate["decided_by_code"], 2)
        fake = Scripted({"worth": 0.1})
        out = self.home / "out.jsonl"
        summary = batch.run(CODE_FIRST, self.rows(), out, transport=fake, workers=1)
        self.assertEqual(len(fake.bodies), 1)
        lines = {r["id"]: r for r in map(json.loads, out.read_text().splitlines())}
        self.assertEqual(lines["a"]["decision"]["source"], "code")
        self.assertEqual(lines["c"]["decision"]["source"], "jev")
        self.assertEqual(summary["actions"], {"skip": 2, "wake": 1})

    def test_an_all_code_run_needs_no_yes(self):
        summary = batch.run(CODE_FIRST, self.rows()[:2], self.home / "free.jsonl")
        self.assertEqual(summary["status"], "done")

    def test_jev_only_asks_about_every_row(self):
        fake = Scripted({"worth": 0.1})
        summary = batch.run(CODE_FIRST, self.rows(), self.home / "jev.jsonl", transport=fake, workers=1,
                            code_first=False)
        self.assertEqual(len(fake.bodies), 3)
        self.assertEqual(summary["estimate"]["decided_by_code"], 0)

    def test_rescore_asks_answers_only_of_rows_code_did_not_decide(self):
        rows = self.rows()
        rows[2]["answers"] = {"worth": 0.9}
        summary = batch.run(CODE_FIRST, rows, self.home / "re.jsonl", rescore=True)
        self.assertEqual(summary["actions"], {"skip": 1, "wake": 2})
        del rows[2]["answers"]
        with self.assertRaises(ValueError):
            batch.run(CODE_FIRST, rows, self.home / "re2.jsonl", rescore=True)


def row(action, truth, p=None, source="jev", **labels):
    decision = {"action": action, "status": "ok", "source": source, "latency_ms": 300, "input_tokens": 400,
                "jev_model": "jev-1.13.0"}
    if p is not None:
        decision["answers"] = {"worth_waking": {"kind": "noul", "p": p}}
    return {"decision": decision, "truth": truth, **labels}


class Calibration(unittest.TestCase):
    def test_auc(self):
        self.assertEqual(shadow.auc([0.9, 0.8, 0.2, 0.1], [True, True, False, False]), 1.0)
        self.assertEqual(shadow.auc([0.1, 0.2, 0.8, 0.9], [True, True, False, False]), 0.0)
        self.assertEqual(shadow.auc([0.5, 0.5, 0.5, 0.5], [True, False, True, False]), 0.5)
        self.assertIsNone(shadow.auc([0.3, 0.4], [True, True]))

    def test_the_report_calibrates_the_feature_probability(self):
        rows = [row("wake", "report", 0.9, id=f"r{i}", job="a") for i in range(40)]
        rows += [row("skip", "report", 0.2, id=f"m{i}", job="b") for i in range(10)]
        rows += [row("skip", "silent", 0.1, id=f"s{i}", job="a") for i in range(50)]
        rows += [row("wake", "silent", source="code", id="c0", job="b")]
        report = shadow.report("cron_wake", rows, by="job")
        cal = report["calibration"]
        self.assertEqual((cal["question"], cal["positive_truth"], cal["n"]), ("worth_waking", "report", 100))
        self.assertGreater(cal["auc"], 0.9)
        at_half = next(s for s in cal["sweep"] if s["p_at_least"] == 0.5)
        self.assertEqual((at_half["flagged"], at_half["precision"], at_half["positives_below"]), (40, 1.0, 10))
        self.assertEqual(sum(b["n"] for b in cal["reliability"]), 100)
        self.assertEqual(report["key_error"]["count"], 10)
        self.assertEqual(report["key_error"]["ids"][:2], ["m0", "m1"])
        self.assertEqual(report["sources"], {"jev": 100, "code": 1})
        self.assertEqual(report["jev"]["input_tokens"], 40_000)
        self.assertEqual(report["by"]["groups"]["b"]["key_error"], 10)
        self.assertEqual(report["by"]["groups"]["a"]["rows"], 90)

    def test_a_bare_logged_probability_is_read_too(self):
        rows = [{"decision": {"action": "wake", "answers": {"worth_waking": 0.9}}, "truth": "report"},
                {"decision": {"action": "skip", "answers": {"worth_waking": 0.1}}, "truth": "silent"}]
        self.assertEqual(shadow.report("cron_wake", rows)["calibration"]["auc"], 1.0)

    def test_the_cli_passes_by_through(self):
        import io
        from contextlib import redirect_stdout
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in [row("wake", "report", 0.9, id="x", job="j")]))
            parser = __import__("argparse").ArgumentParser()
            cli_decide.add_parsers(parser.add_subparsers())
            args = parser.parse_args(["shadow", "report", "--feature", "cron_wake", "--rows", str(path), "--by", "job"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(args.func(args), 0)
            self.assertEqual(json.loads(buffer.getvalue())["by"]["field"], "job")


class GateFixtures(TempHome):
    """The synthetic red-team set: its shape, that it is safe to publish, and the replay plumbing."""

    def load(self):
        return [json.loads(line) for line in FIXTURES.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]

    def test_shape(self):
        rows = self.load()
        self.assertGreaterEqual(len(rows), 100)
        self.assertEqual(len({r["id"] for r in rows}), len(rows))
        counts = {kind: sum(1 for r in rows if r["expect"] == kind) for kind in ("must_deny", "must_ask", "should_approve")}
        self.assertEqual(sum(counts.values()), len(rows))
        self.assertTrue(all(n >= 25 for n in counts.values()), counts)
        self.assertTrue(all(r["workdir_kind"] in ("home", "repo", "scratch", "system", "other") for r in rows))
        self.assertTrue(all(r["tool"] and r["command"] and r["flagged_as"] for r in rows))

    def test_nothing_real_in_it(self):
        text = FIXTURES.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"/Users/[a-z]|/home/[a-z]|\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.", text))
        for address in re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", text):
            self.assertTrue(address.startswith("203.0.113.") or address.startswith("169.254."), address)

    def test_replay_scores_false_approves_and_friction(self):
        rows = cli_decide._gate_rows(str(FIXTURES))
        self.assertTrue(all("command" not in r and r["state"]["workdir_kind"] for r in rows))
        out = self.home / "gate.jsonl"
        batch.run("gate-strict", rows, out, transport=Scripted({"verdict": "approve"}), feature="gate", workers=4)
        done = [json.loads(line) for line in out.read_text().splitlines()]
        from jevkit import gate
        scored = gate.score_fixtures(done, {str(r["id"]): r["decision"]["action"] for r in done})
        # A Jev that approves everything fails every dangerous command it is shown. The few that
        # look like they hold a secret are never shown: no opinion, and the host decides as before.
        shown = {r["id"] for r in done if r["decision"]["status"] == "ok"}
        unshown = [r for r in done if r["id"] not in shown]
        self.assertTrue(all(r["decision"]["action"] == "no_opinion" for r in unshown))
        self.assertLessEqual(len(unshown), 5)
        danger_shown = [r for r in done if r["expect"] != "should_approve" and r["id"] in shown]
        self.assertEqual(sorted(scored["false_approves"]), sorted(r["id"] for r in danger_shown))
        self.assertEqual(scored["approve_rate_on_harmless"], 1.0)
        report = shadow.report("gate", done)
        danger = [r for r in done if r["expect"] != "should_approve"]
        self.assertAlmostEqual(report["measured"]["redteam_catch_rate"],
                               (len(danger) - len(danger_shown)) / len(danger), places=5)


if __name__ == "__main__":
    unittest.main()
