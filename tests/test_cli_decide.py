"""The new `jev` subcommands keep the CLI's contract: JSON out, bad input exit 2, Jev down exit 0."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import Scripted, TempHome  # noqa: E402
from jevkit import cli, client  # noqa: E402


class Cli(TempHome):
    def run_cli(self, argv, stdin="", transport=None):
        out = io.StringIO()
        patches = [mock.patch.object(sys, "stdin", io.StringIO(stdin))]
        if transport is not None:
            patches.append(mock.patch.object(client, "_http_transport", transport))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            code = cli.main(argv)
        return code, json.loads(out.getvalue())

    def write(self, name, value):
        path = self.home / name
        path.write_text(json.dumps(value))
        return str(path)

    def test_decide_with_a_policy_and_explain(self):
        state = self.write("s.json", {"outcome": "timed_out", "error_tail": "still running step 4"})
        code, out = self.run_cli(["decide", "--policy", "retry", "--state", state, "--explain"],
                                 transport=Scripted({"retry_helps": 0.8}))
        self.assertEqual(code, 0)
        self.assertEqual(out["action"], "retry")
        self.assertTrue(any(line.startswith("rule 0") for line in out["explain"]))

    def test_decide_ad_hoc_from_stdin(self):
        code, out = self.run_cli(["decide", "--question", "done=The work is finished"], stdin='"all tests pass"',
                                 transport=Scripted({"done": 0.95}))
        self.assertEqual(code, 0)
        self.assertEqual(out["verdicts"], {"done": "yes"})

    def test_jev_down_is_exit_zero_with_the_fallback(self):
        state = self.write("s.json", {"job": "nightly", "changes": "a new failure"})
        code, out = self.run_cli(["decide", "--policy", "cron-wake", "--state", state],
                                 transport=Scripted(fail="overloaded"))
        self.assertEqual(code, 0)
        self.assertEqual(out["action"], "wake")
        self.assertTrue(out["fallback_used"])

    def test_bad_input_is_exit_two(self):
        for argv, stdin in ((["decide", "--policy", "no-such-policy"], "{}"),
                            (["decide"], "{}"),
                            (["decide", "--policy", "retry", "--question", "a=b"], "{}"),
                            (["decide", "--question", "noequals"], "{}"),
                            (["decide", "--policy", "retry"], "not json"),
                            (["decide", "--policy", "owner"], "{}")):
            with self.subTest(argv=argv):
                code, out = self.run_cli(argv, stdin=stdin, transport=Scripted())
                self.assertEqual(code, 2)
                self.assertEqual(out["error"], "invalid_request")

    def test_owner_takes_options(self):
        state = self.write("s.json", {"title": "Fix the login test", "body": "It fails on CI"})
        options = self.write("o.json", {"owner": {"coder": "writes code", "writer": "writes prose"}})
        code, out = self.run_cli(["decide", "--policy", "owner", "--state", state, "--options", options],
                                 transport=Scripted({"owner": "coder"}, confidence=0.95, rest=0.05))
        self.assertEqual(code, 0)
        self.assertEqual(out["action"], "suggest_owner")

    def test_score_route_gate(self):
        state = self.write("s.json", {"task": "t", "output": "o"})
        code, out = self.run_cli(["score", "--state", state], transport=Scripted(
            {"quality": 4, "relevance": 4, "completeness": 3, "evidence_present": 0.9}))
        self.assertEqual((code, out["action"]), (0, "continue"))
        dests = self.write("d.json", {"coder": "writes code", "ops": "deploys"})
        code, out = self.run_cli(["route-to", "--destinations", dests, "--fallback", "triage"], stdin='"fix the test"',
                                 transport=Scripted({"dest": "coder"}))
        self.assertEqual((code, out["dest"]), (0, "coder"))
        code, out = self.run_cli(["gate", "--command", "ls -la"], transport=Scripted({"verdict": "approve"}))
        self.assertEqual((code, out["action"]), (0, "approve"))
        self.assertIsNone(out["would_return"]["ask"])
        self.assertEqual(out["tiebreak"], "approve")

    def test_gate_replay_asks_before_paying(self):
        rows = self.home / "rows.jsonl"
        rows.write_text(json.dumps({"id": "a", "command": "ls", "truth": "approve"}) + "\n")
        code, out = self.run_cli(["gate", "replay", str(rows), "--out", str(self.home / "out.jsonl")])
        self.assertEqual(code, 3)
        self.assertEqual(out["status"], "needs_yes")

    def test_policies_ledger_switches(self):
        code, out = self.run_cli(["policies"])
        self.assertIn("gate-strict", [p["name"] for p in out["policies"]])
        code, out = self.run_cli(["policies", "show", "gate-strict"])
        self.assertTrue(out["label"].startswith("gate-strict@1#"))
        code, out = self.run_cli(["ledger", "--total"])
        self.assertEqual(code, 0)
        self.assertIn("limits", out)
        code, out = self.run_cli(["switches"])
        self.assertEqual(out["features"]["gate"]["mode"], "off")
        code, out = self.run_cli(["switches", "gate", "on"])
        self.assertEqual(code, 2)

    def test_triage_preset(self):
        code, out = self.run_cli(["triage", "--preset", "urgency"],
                                 stdin=json.dumps([{"text": "the site is down for every customer"}]),
                                 transport=Scripted({"urgency": 4}))
        self.assertEqual(code, 0)
        self.assertEqual(out["counts"], {"escalate": 1})


if __name__ == "__main__":
    unittest.main()
