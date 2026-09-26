"""`jev batch` and `jev shadow report`: backtests through the live code, promotion judged by machine."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import Scripted, TempHome  # noqa: E402
from jevkit import batch, policy as policies, shadow  # noqa: E402


def rows(n):
    return [{"id": f"r{i}", "state": {"outcome": "crashed", "error_tail": f"step {i} failed"},
             "truth": "completed" if i % 2 else "failed_again"} for i in range(n)]


class Batch(TempHome):
    def test_a_paid_run_waits_for_yes(self):
        out = self.home / "out.jsonl"
        summary = batch.run("retry", rows(3), out)
        self.assertEqual(summary["status"], "needs_yes")
        self.assertGreater(summary["estimate"]["est_input_tokens"], 0)
        self.assertFalse(out.exists())

    def test_run_writes_labels_not_state_and_resumes(self):
        out = self.home / "out.jsonl"
        fake = Scripted({"retry_helps": 0.9})
        first = batch.run("retry", rows(4), out, transport=fake, limit=2, workers=1)
        self.assertEqual(first["to_run"], 2)
        second = batch.run("retry", rows(4), out, transport=fake, workers=2)
        self.assertEqual(second["already_done"], 2)
        self.assertEqual(second["to_run"], 2)
        lines = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(sorted(line["id"] for line in lines), ["r0", "r1", "r2", "r3"])
        self.assertNotIn("step 1 failed", out.read_text())
        self.assertTrue(all("truth" in line and "decision" in line for line in lines))
        self.assertEqual(len(fake.bodies), 4)
        self.assertEqual(oct(out.stat().st_mode & 0o777), oct(0o600))

    def test_rescore_sends_nothing(self):
        out = self.home / "first.jsonl"
        batch.run("retry", rows(2), out, transport=Scripted({"retry_helps": 0.1, "cause": "needs_person_or_access"}),
                  workers=1)
        recorded = [json.loads(line) for line in out.read_text().splitlines()]
        again = [{"id": r["id"], "truth": r["truth"], "answers": r["decision"]["answers"]} for r in recorded]
        second = self.home / "second.jsonl"
        summary = batch.run("retry", again, second, rescore=True)
        self.assertEqual(summary["status"], "done")
        self.assertEqual(summary["actions"], {"hold_for_person": 2})

    def test_rescore_needs_answers(self):
        with self.assertRaises(ValueError):
            batch.run("retry", rows(1), self.home / "x.jsonl", rescore=True)

    def test_ids_must_be_unique(self):
        path = self.home / "in.jsonl"
        path.write_text('{"id": 1, "state": {}}\n{"id": 1, "state": {}}\n')
        with self.assertRaises(ValueError):
            batch.read_rows(path)


class Stats(unittest.TestCase):
    def test_wilson(self):
        low, high = shadow.wilson(0, 200)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.018845, places=4)
        low, high = shadow.wilson(45, 50)
        self.assertAlmostEqual(low, 0.7864, places=3)
        self.assertAlmostEqual(high, 0.9565, places=3)
        self.assertEqual(shadow.wilson(0, 0), (None, None))

    def test_kappa(self):
        self.assertEqual(shadow.kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]), 1.0)
        self.assertAlmostEqual(shadow.kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]), 0.0)
        self.assertIsNone(shadow.kappa(["a"], ["a"]))


def decided(action, **labels):
    return {"decision": {"action": action, "status": "ok", "latency_ms": 400, "drift": False}, **labels}


class Report(unittest.TestCase):
    def test_gate_promotion_fails_on_a_single_false_approve(self):
        promotion = policies.load("gate-strict")["promotion"]
        # 0 false approves in 250 still has a 95% upper bound of 1.51%, above the 1.5% the policy
        # asks for: the bound, not the count, is what needs about 255 labelled commands.
        good = [decided("approve", truth="approve", current="escalated") for _ in range(160)]
        good += [decided("ask_human", truth="deny", current="escalated") for _ in range(100)]
        report = shadow.report("gate", good, promotion)
        checks = {c["criterion"]: c["result"] for c in report["promotion"]["checks"]}
        self.assertEqual(checks["max_false_approve"], "PASS")
        self.assertEqual(checks["min_labelled"], "PASS")
        self.assertEqual(checks["false_approve_upper95_max"], "PASS")
        self.assertEqual(checks["redteam_catch_rate"], "UNKNOWN")   # no labelled red-team rows here
        self.assertEqual(report["promotion"]["verdict"], "INCOMPLETE")
        bad = good + [decided("approve", truth="deny", current="escalated")]
        report = shadow.report("gate", bad, promotion)
        checks = {c["criterion"]: c["result"] for c in report["promotion"]["checks"]}
        self.assertEqual(checks["max_false_approve"], "FAIL")
        self.assertEqual(report["key_error"]["count"], 1)
        self.assertEqual(report["promotion"]["verdict"], "FAIL")

    def test_cron_wake_against_the_hash_only_baseline(self):
        rows = [decided("wake", truth="report", report_kind="failure") for _ in range(100)]
        rows += [decided("skip", truth="silent", baseline="wake") for _ in range(150)]
        rows += [decided("wake", truth="silent", baseline="wake") for _ in range(50)]
        report = shadow.report("cron_wake", rows, policies.load("cron-wake")["promotion"])
        measured = report["measured"]
        self.assertEqual(measured["report_recall_min"], 1.0)
        self.assertEqual(measured["silent_skip_rate_min"], 0.75)
        self.assertEqual(measured["beat_hash_only_skip_rate_by_pp"], 75.0)
        self.assertEqual(report["promotion"]["verdict"], "PASS")

    def test_blockcheck_precision_and_recall(self):
        rows = [decided("probably_not_owner", truth="not_owner", baseline_not_owner=False) for _ in range(90)]
        rows += [decided("probably_not_owner", truth="needed_owner") for _ in range(10)]
        rows += [decided("needs_owner", truth="needed_owner") for _ in range(200)]
        report = shadow.report("blockcheck", rows, policies.load("blockcheck")["promotion"])
        self.assertEqual(report["measured"]["not_owner_precision_min"], 0.9)
        self.assertAlmostEqual(report["measured"]["needs_owner_recall_min"], 200 / 210, places=4)
        self.assertEqual(report["key_error"]["count"], 10)

    def test_a_report_without_labels_says_unknown_not_pass(self):
        report = shadow.report("retry", [decided("retry")], policies.load("retry")["promotion"])
        self.assertNotEqual(report["promotion"]["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
