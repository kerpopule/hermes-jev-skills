"""`decide`: what is sent, what is logged, and every way it falls back to "what you did before".

Each fail-open path is a test, because each one is a promise: no key, a timeout, a malformed
reply, a state that looks like it holds a secret, the budget spent, a Jev version the
thresholds were not tuned on. In every case the caller gets the policy's own fallback action
and ``fallback_used: true`` — and for a secret, nothing leaves the machine at all.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import Scripted, TempHome  # noqa: E402
from jevkit import client, decide as engine, ledger, limits, policy as policies  # noqa: E402

STATE = {"outcome": "crashed", "exit_kind": "killed", "error_tail": "connection reset by peer",
         "summary_tail": "ran the migration, 3 of 5 steps done", "same_error_as_last_attempt": False,
         "budget_exhausted": False, "unlisted": "this field is not in state_fields"}


class Decide(TempHome):
    def test_a_decision_and_its_ledger_row(self):
        fake = Scripted({"retry_helps": 0.9, "cause": "transient_infra", "progress_made": 0.8})
        out = engine.decide(STATE, "retry", transport=fake)
        self.assertEqual(out["action"], "retry")
        self.assertEqual(out["status"], "ok")
        self.assertFalse(out["fallback_used"])
        self.assertEqual(out["jev_model"], "jev-1.13.0")
        self.assertFalse(out["drift"])
        self.assertAlmostEqual(out["cost_usd"], 500 * 0.042 / 1e6)
        self.assertTrue(out["policy"].startswith("retry@1#"))
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["feature"], "retry")
        self.assertEqual(row["action"], "retry")
        self.assertEqual(row["n_questions"], 3)
        self.assertEqual(row["input_tokens"], 500)
        text = json.dumps(row)
        self.assertNotIn("connection reset", text, "the ledger must never hold the state")
        mode = (self.home / "logs" / "jev-ledger.jsonl").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_only_the_policys_fields_are_sent(self):
        fake = Scripted()
        engine.decide(STATE, "retry", transport=fake)
        sent = fake.requests[0]["state"]
        self.assertNotIn("unlisted", sent)
        self.assertEqual(sent["outcome"], "crashed")

    def test_every_noul_carries_its_criteria_on_the_wire(self):
        fake = Scripted()
        engine.decide(STATE, "retry", transport=fake)
        for name, question in fake.requests[0]["questions"].items():
            if question["type"] == "noul":
                self.assertEqual(set(question["criteria"]), {"true", "false"}, name)

    def test_long_fields_are_capped_and_redacted(self):
        fake = Scripted()
        engine.decide({**STATE, "error_tail": "x" * 9000 + " mail me at someone@example.com"}, "retry", transport=fake)
        sent = fake.requests[0]["state"]["error_tail"]
        self.assertLessEqual(len(sent), engine.DEFAULT_FIELD_CHARS + 10)
        self.assertNotIn("someone@example.com", sent)

    def test_a_state_that_looks_like_a_secret_is_never_sent(self):
        fake = Scripted()
        out = engine.decide({**STATE, "error_tail": "export GITHUB_TOKEN=abc123"}, "retry", transport=fake)
        self.assertEqual(fake.bodies, [])
        self.assertEqual(out["action"], "retry")          # the policy's on_error: today's behaviour
        self.assertTrue(out["fallback_used"])
        self.assertEqual(out["error"], "sensitive_not_sent")
        self.assertEqual(self.ledger_rows()[0]["status"], "skipped")

    def test_every_jev_failure_is_the_fallback(self):
        for code in ("timeout", "network", "auth_failed", "rate_limited", "malformed", "invalid_response"):
            with self.subTest(code=code):
                out = engine.decide(STATE, "blockcheck", transport=Scripted(fail=code), retries=0)
                self.assertEqual(out["action"], "needs_owner")
                self.assertTrue(out["fallback_used"])
                self.assertEqual(out["error"], code)

    def test_no_key_is_the_fallback_and_nothing_is_sent(self):
        import os
        os.environ.pop("TYPESAFE_API_KEY", None)
        from unittest import mock
        with mock.patch.object(client.keystore, "resolve", return_value=None), \
                mock.patch.object(client.keystore, "provider", return_value="typesafe"):
            out = engine.decide(STATE, "cron-wake", transport=Scripted())
        self.assertEqual(out["action"], "wake")
        self.assertEqual(out["error"], "no_key")
        self.assertFalse(out["sent_to_jev"])

    def test_drift_takes_the_fallback_live_and_keeps_measuring_in_shadow(self):
        fake = Scripted({"retry_helps": 0.05, "cause": "needs_person_or_access"}, model="jev-1.14.0")
        live = engine.decide(STATE, "retry", transport=fake)
        self.assertTrue(live["drift"])
        self.assertEqual(live["rule_action"], "hold_for_person")
        self.assertEqual(live["action"], "retry")
        self.assertTrue(live["fallback_used"])
        shadow = engine.decide(STATE, "retry", transport=fake, mode="shadow")
        self.assertTrue(shadow["drift"])
        self.assertEqual(shadow["action"], "hold_for_person")
        self.assertFalse(shadow["fallback_used"])

    def test_the_daily_cap_skips_the_call(self):
        (self.home / "jev").mkdir(parents=True, exist_ok=True)
        (self.home / "jev" / "limits.json").write_text(json.dumps({"daily_usd": 0.0}))
        fake = Scripted()
        out = engine.decide(STATE, "retry", transport=fake)
        self.assertEqual(fake.bodies, [])
        self.assertEqual(out["error"], "skipped_budget")
        self.assertEqual(out["action"], "retry")

    def test_a_policy_with_open_options_refuses_to_run_unbound(self):
        with self.assertRaises(policies.PolicyError):
            engine.decide({"title": "t", "body": "b"}, "owner", transport=Scripted())

    def test_trusted_context_goes_in_the_instructions_not_the_state(self):
        fake = Scripted()
        engine.decide(STATE, "retry", transport=fake, trusted_context={"operator_policy": "never retry on Fridays"})
        request = fake.requests[0]
        self.assertNotIn("Fridays", json.dumps(request["state"]))
        for question in request["questions"].values():
            self.assertEqual(question["instructions"]["operator_policy"], "never retry on Fridays")
            self.assertIn("question", question["instructions"])

    def test_a_ledger_that_cannot_be_written_never_breaks_a_decision(self):
        import os
        blocker = self.home / "a-file"
        blocker.write_text("not a folder")
        os.environ["JEV_LEDGER_PATH"] = str(blocker / "jev-ledger.jsonl")
        out = engine.decide(STATE, "retry", transport=Scripted())
        self.assertEqual(out["status"], "ok")


class Rescore(TempHome):
    def test_recorded_wire_answers_rescore_to_the_live_action(self):
        fake = Scripted({"retry_helps": 0.1, "cause": "needs_person_or_access", "progress_made": 0.2})
        live = engine.decide(STATE, "retry", transport=fake)
        recorded = json.loads(fake.bodies[0])  # the request; rebuild the reply the same way
        reply = json.loads(fake(json.dumps(recorded).encode(), {}, 1.0))
        again = engine.rescore("retry", reply["answers"], jev_model=reply.get("model"))
        self.assertEqual(again["action"], live["action"])
        from_readings = engine.rescore("retry", live["answers"])
        self.assertEqual(from_readings["action"], live["action"])

    def test_a_malformed_recording_is_refused_like_a_live_one(self):
        with self.assertRaises(client.JevError):
            engine.rescore("retry", {"retry_helps": {"type": "noul", "noul": 7},
                                     "cause": {"type": "choice", "choice": "unknown", "probabilities": {}, "confidence": 1},
                                     "progress_made": {"type": "noul", "noul": 0.5}})


class Adhoc(TempHome):
    def test_verdicts_follow_the_band(self):
        fake = Scripted({"done": 0.95, "blocked": 0.5, "risky": 0.05})
        out = engine.adhoc("The build passed and the PR is merged.",
                           {"done": "The work is finished", "blocked": "Something is waiting on a person",
                            "risky": {"instructions": "Is anything risky here?", "criteria": {"true": "yes", "false": "no"}}},
                           transport=fake)
        self.assertEqual(out["verdicts"], {"done": "yes", "blocked": "unsure", "risky": "no"})
        self.assertEqual(out["action"], "answered")

    def test_at_most_eight(self):
        with self.assertRaises(ValueError):
            engine.adhoc("s", {f"q{i}": "Is it?" for i in range(9)}, transport=Scripted())

    def test_failure_gives_no_verdicts(self):
        out = engine.adhoc("s", {"done": "Is it done?"}, transport=Scripted(fail="timeout"), )
        self.assertEqual(out["action"], "no_answer")
        self.assertEqual(out["verdicts"], {})


class Limits(TempHome):
    def test_shadow_is_refused_before_live(self):
        (self.home / "jev").mkdir(parents=True, exist_ok=True)
        (self.home / "jev" / "limits.json").write_text(json.dumps({"rpm": 5, "shadow_share": 0.4}))
        results = [limits.admit(shadow=True) for _ in range(3)]
        self.assertEqual([r[0] for r in results], [True, True, False])
        self.assertEqual(results[-1][1], "skipped_rate")
        self.assertTrue(limits.admit(shadow=False)[0])
        self.assertTrue(limits.admit(shadow=False)[0])
        self.assertTrue(limits.admit(shadow=False)[0])
        self.assertFalse(limits.admit(shadow=False)[0])

    def test_charge_counts_toward_the_cap(self):
        (self.home / "jev").mkdir(parents=True, exist_ok=True)
        (self.home / "jev" / "limits.json").write_text(json.dumps({"daily_usd": 0.001}))
        self.assertTrue(limits.admit(shadow=False)[0])
        limits.charge(0.002)
        self.assertEqual(limits.admit(shadow=False), (False, "skipped_budget"))
        self.assertAlmostEqual(limits.status()["spent_today_usd"], 0.002)

    def test_off_switch(self):
        import os
        os.environ["JEV_LIMITS"] = "off"
        self.assertEqual(limits.admit(shadow=True), (True, "limits_off"))


class Ledger(TempHome):
    def test_free_tier_bills_zero_at_the_same_size(self):
        self.assertEqual(ledger.cost(1_000_000, "zen", "jev-1.13-free"), {"cost_usd": 0.0, "list_usd": 0.042})
        self.assertEqual(ledger.cost(1_000_000, "typesafe", "jev-1.13.0")["cost_usd"], 0.042)

    def test_summary_per_feature(self):
        rows = [{"ts": 1_790_000_000 + i, "feature": "gate", "status": "ok", "latency_ms": ms, "cost_usd": 0.00002,
                 "list_usd": 0.00002, "action": "approve", "llm_avoided_est": 100}
                for i, ms in enumerate([100, 200, 300, 400, 1000])]
        rows.append({"ts": 1_790_000_010, "feature": "gate", "status": "error", "latency_ms": 1500, "action": "no_opinion"})
        rows.append({"ts": 1_790_000_011, "feature": "gate", "status": "skipped", "action": "no_opinion", "drift": True})
        summary = ledger.summarize(rows, by_day=False)
        group = summary["groups"][0]
        self.assertEqual(group["calls"], 7)
        self.assertEqual(group["sent"], 6)
        self.assertEqual(group["skipped"], 1)
        self.assertEqual(group["p50_ms"], 300)
        self.assertEqual(group["p99_ms"], 1500)
        self.assertAlmostEqual(group["error_rate"], 1 / 6, places=4)
        self.assertEqual(group["drift"], 1)
        self.assertEqual(summary["totals"]["llm_avoided_est_tokens"], 500)
        self.assertIn("estimate", summary["note"])

    def test_switched_off(self):
        import os
        os.environ["JEV_LEDGER"] = "off"
        ledger.append({"feature": "x"})
        self.assertEqual(self.ledger_rows(), [])


if __name__ == "__main__":
    unittest.main()
