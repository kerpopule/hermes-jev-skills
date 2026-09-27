"""Lanes: the first lane, each loop step, deterministic checks first, and completion that is earned.

Every rule from the orchestration pattern is a test here: one request for all questions with an
'other' escape; low confidence goes up, not down; failing checks and scope never reach Jev;
escalation moves one lane at a time and ends at a person; complete is refused when the facts
say otherwise.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import Scripted, TempHome  # noqa: E402
from jevkit import lanes, policy as policies  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class Classify(TempHome):
    def test_all_questions_go_in_one_request_with_an_other_option(self):
        fake = Scripted({"lane": "small", "security_sensitive": 0.05, "underspecified": 0.1})
        out = lanes.classify("rename parse_row to parse_line in utils.py", transport=fake)
        self.assertEqual(len(fake.requests), 1)
        questions = fake.requests[0]["questions"]
        self.assertEqual(sorted(questions), ["lane", "security_sensitive", "underspecified"])
        self.assertIn("other", questions["lane"]["criteria"])
        self.assertEqual(out["lane"], "small")
        self.assertEqual(out["target"], {"agent": "jev-lane-small", "model": "haiku", "effort": "low"})

    def test_a_small_pick_without_confidence_goes_up_to_medium(self):
        fake = Scripted({"lane": "small"}, confidence=0.6, rest=0.1)
        self.assertEqual(lanes.classify("tidy the readme", transport=fake)["lane"], "medium")

    def test_a_medium_pick_below_half_confidence_goes_to_high(self):
        fake = Scripted({"lane": "medium"}, confidence=0.4, rest=0.15)
        self.assertEqual(lanes.classify("fix the flaky sync", transport=fake)["lane"], "high")

    def test_security_sensitive_work_is_at_least_high(self):
        fake = Scripted({"lane": "small", "security_sensitive": 0.9})
        self.assertEqual(lanes.classify("rotate the webhook secret", transport=fake)["lane"], "high")

    def test_escalate_needs_confidence(self):
        self.assertEqual(lanes.classify("x", transport=Scripted({"lane": "escalate"}))["lane"], "escalate")
        unsure = Scripted({"lane": "escalate"}, confidence=0.5, rest=0.1)
        self.assertEqual(lanes.classify("x", transport=unsure)["lane"], "high")

    def test_other_keeps_the_current_model(self):
        out = lanes.classify("ask Steve which plan he wants", transport=Scripted({"lane": "other"}))
        self.assertEqual(out["lane"], "keep_current")
        self.assertIsNone(out["target"])

    def test_code_decides_first_and_nothing_is_sent(self):
        fake = Scripted({"lane": "small"})
        self.assertEqual(lanes.classify("x", facts={"person_named_model": True}, transport=fake)["lane"], "keep_current")
        self.assertEqual(lanes.classify("x", facts={"prior_failed_attempts": 2}, transport=fake)["lane"], "escalate")
        self.assertEqual(lanes.classify("x", facts={"security_paths": True}, transport=fake)["lane"], "high")
        self.assertEqual(fake.requests, [])

    def test_jev_down_keeps_the_current_model(self):
        out = lanes.classify("anything", transport=Scripted(fail="timeout"))
        self.assertEqual(out["lane"], "keep_current")
        self.assertTrue(out["decision"]["fallback_used"])

    def test_hermes_targets_and_a_local_override(self):
        self.assertEqual(lanes.targets("hermes")["escalate"]["model"], "gpt-6-astra")
        (self.home / "jev").mkdir(exist_ok=True)
        (self.home / "jev" / "lanes.json").write_text(json.dumps(
            {"hermes": {"small": {"model": "gpt-reserve"}}, "claude-code": {"nonsense": {"model": "x"}}}))
        mapped = lanes.targets("hermes")
        self.assertEqual(mapped["small"], {"provider": "openai-codex", "model": "gpt-reserve", "effort": "medium"})
        self.assertEqual(sorted(lanes.targets("claude-code")), sorted(lanes.LANES))


class Step(TempHome):
    GOOD = {"checks_run": True, "checks_available": True, "checks_failed": 0, "out_of_scope_files": 0,
            "diff_empty": False, "expects_changes": True, "security_changed": False}

    def test_a_failing_check_is_retried_without_asking_jev(self):
        fake = Scripted()
        out = lanes.step("x", lane="small", attempts=1, facts={**self.GOOD, "checks_failed": 1}, transport=fake)
        self.assertEqual((out["action"], out["lane"], out["source"]), ("retry", "small", "code"))
        self.assertEqual(fake.requests, [])

    def test_failing_again_escalates_one_lane(self):
        out = lanes.step("x", lane="small", attempts=2, facts={**self.GOOD, "checks_failed": 1}, transport=Scripted())
        self.assertEqual((out["action"], out["lane"]), ("escalate", "medium"))
        self.assertEqual(out["target"]["model"], "sonnet")
        same = lanes.step("x", lane="medium", attempts=1, facts={**self.GOOD, "checks_failed": 2},
                          same_failure_repeated=True, transport=Scripted())
        self.assertEqual(same["lane"], "high")

    def test_the_top_lane_escalates_to_a_person(self):
        out = lanes.step("x", lane="escalate", attempts=3, facts={**self.GOOD, "checks_failed": 1}, transport=Scripted())
        self.assertEqual((out["action"], out["lane"], out["target"]), ("escalate", "person", None))

    def test_scope_and_unrun_checks_are_code_decisions(self):
        fake = Scripted()
        self.assertEqual(lanes.step("x", lane="medium", facts={**self.GOOD, "out_of_scope_files": 2},
                                    transport=fake)["action"], "retry")
        self.assertEqual(lanes.step("x", lane="medium", facts={**self.GOOD, "checks_run": False},
                                    transport=fake)["action"], "verify")
        self.assertEqual(lanes.step("x", lane="medium", facts={**self.GOOD, "diff_empty": True},
                                    transport=fake)["action"], "continue")
        self.assertEqual(lanes.step("x", lane="small", facts={**self.GOOD, "security_changed": True},
                                    transport=fake)["lane"], "medium")
        self.assertEqual(fake.requests, [])

    def test_complete_when_checks_pass_and_jev_agrees(self):
        fake = Scripted({"implemented": 0.95, "in_scope": 0.92, "next": "complete"})
        out = lanes.step("add --json flag", lane="small", facts=self.GOOD,
                         state={"diff_stat": "cli.py | 4 +", "checks": "$ pytest\nexit 0\n3 passed"}, transport=fake)
        self.assertEqual(out["action"], "complete")
        sent = fake.requests[0]["state"]
        self.assertEqual(sorted(sent), ["checks", "diff_stat", "task"])

    def test_unsure_jev_escalates_and_undecided_verifies(self):
        low = Scripted({"implemented": 0.6, "in_scope": 0.9, "next": "needs_stronger_model"})
        self.assertEqual(lanes.step("x", lane="medium", facts=self.GOOD, transport=low)["lane"], "high")
        middle = Scripted({"implemented": 0.6, "in_scope": 0.9, "next": "complete"})
        self.assertEqual(lanes.step("x", lane="medium", facts=self.GOOD, transport=middle)["action"], "verify")

    def test_jev_down_means_verify(self):
        self.assertEqual(lanes.step("x", lane="high", facts=self.GOOD, transport=Scripted(fail="timeout"))["action"],
                         "verify")

    def test_complete_is_refused_when_the_facts_disagree(self):
        # A local override with no pre-rules: the guard in step() must still hold.
        rules = json.loads((REPO / "jevkit" / "policies" / "loop-step.json").read_text())
        rules["pre_rules"] = []
        (self.home / "jev" / "policies").mkdir(parents=True)
        (self.home / "jev" / "policies" / "loop-step.json").write_text(json.dumps(rules))
        fake = Scripted({"implemented": 0.99, "in_scope": 0.99, "next": "complete"})
        out = lanes.step("x", lane="small", facts={**self.GOOD, "checks_failed": 1}, transport=fake)
        self.assertEqual(out["action"], "verify")
        self.assertEqual(out["complete_refused"], "a check is failing")


class Evidence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for command in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(command, cwd=self.repo, check=True, env=env)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('hi')\n")
        (self.repo / "notes.txt").write_text("stray\n")
        (self.repo / ".env.local").write_text("X=1\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_scope_sensitive_paths_and_check_tails(self):
        long_output = "python3 -c \"import sys; [print(i) for i in range(500)]; sys.exit(3)\""
        found = lanes.evidence(self.repo, runs=["true", long_output], scope=["src/*"])
        facts = found["facts"]
        self.assertEqual(facts["checks_failed"], 1)
        self.assertEqual(facts["out_of_scope_files"], 2)
        self.assertTrue(facts["security_changed"])
        self.assertEqual(found["sensitive"], [".env.local"])
        failing = found["checks"][1]
        self.assertEqual(failing["exit"], 3)
        self.assertEqual(failing["tail"].splitlines()[-1], "499")
        self.assertLessEqual(len(failing["tail"].splitlines()), lanes.TAIL_LINES)
        self.assertIn("src/app.py", found["state"]["diff_stat"])

    def test_no_changes_and_no_checks(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
        facts = lanes.evidence(empty)["facts"]
        self.assertTrue(facts["diff_empty"])
        self.assertFalse(facts["checks_run"])
        self.assertFalse(facts["checks_available"])


class ClaudeAgents(unittest.TestCase):
    def test_each_lane_agent_matches_the_lane_map(self):
        folder = REPO / "claude" / "agents"
        for lane, target in lanes.TARGETS["claude-code"].items():
            text = (folder / f"{target['agent']}.md").read_text()
            head = text.split("---")[1]
            self.assertIn(f"name: {target['agent']}", head)
            self.assertRegex(head, rf"(?m)^model: {target['model']}$")
            self.assertRegex(head, rf"(?m)^effort: {target['effort']}$")
            self.assertIn("hermes-jev-skills", head)

    def test_the_policies_lint(self):
        for name in ("lane", "loop-step"):
            loaded = policies.load(name)
            self.assertLessEqual(len(loaded["questions"]), 8)


class ClaudeInstall(unittest.TestCase):
    """The installer's Claude Code target: additive, backed up, delimited, removable."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("jev_install_lanes", REPO / "install.py")
        self.install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.install)
        self._tmp = tempfile.TemporaryDirectory()
        self.claude = Path(self._tmp.name) / ".claude"
        self.claude.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_block_is_added_once_backed_up_and_removed_cleanly(self):
        mine = "# My rules\n\nAlways use tabs.\n"
        (self.claude / "CLAUDE.md").write_text(mine)
        first = self.install.install_claude(self.claude, check=False)
        self.assertEqual(first["claude_md_change"], "added")
        self.assertTrue(Path(first["claude_md_backup"]).read_text() == mine)
        text = (self.claude / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith(mine.rstrip()))
        self.assertEqual(text.count(self.install.BLOCK_BEGIN), 1)
        again = self.install.install_claude(self.claude, check=False)
        self.assertEqual(again["claude_md_change"], "unchanged")
        self.assertEqual(len(list(self.claude.glob("CLAUDE.md.bak-jev-*"))), 1)
        self.assertEqual(sorted(p.name for p in (self.claude / "agents").iterdir()),
                         [f"jev-lane-{lane}.md" for lane in sorted(lanes.LANES)])
        gone = self.install.uninstall_claude(self.claude)
        self.assertEqual(len(gone["agents_removed"]), 4)
        self.assertEqual((self.claude / "CLAUDE.md").read_text(), mine)

    def test_a_persons_own_agent_file_is_left_alone(self):
        (self.claude / "agents").mkdir()
        own = self.claude / "agents" / "jev-lane-small.md"
        own.write_text("---\nname: jev-lane-small\nmodel: sonnet\n---\nmine\n")
        out = self.install.install_claude(self.claude, check=False, with_block=False)
        self.assertIn(str(own), out["agents_left_alone"])
        self.assertIn("mine", own.read_text())
        self.assertFalse((self.claude / "CLAUDE.md").exists())
        self.install.uninstall_claude(self.claude)
        self.assertTrue(own.exists())

    def test_check_changes_nothing_and_a_fresh_file_is_deleted_on_uninstall(self):
        out = self.install.install_claude(self.claude, check=True)
        self.assertEqual(out["claude_md_change"], "added")
        self.assertFalse((self.claude / "CLAUDE.md").exists())
        self.assertFalse((self.claude / "agents").exists())
        self.install.install_claude(self.claude, check=False)
        self.install.uninstall_claude(self.claude)
        self.assertFalse((self.claude / "CLAUDE.md").exists())


class HermesShadow(TempHome):
    """The Kanban tick: off does nothing, shadow logs and changes nothing, on applies once."""

    def make_board(self, cards):
        import sqlite3
        db = self.home / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, status TEXT, assignee TEXT, "
                    "created_at INTEGER, model_override TEXT, reasoning_effort TEXT)")
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", cards)
        con.commit()
        con.close()
        return db

    def set_mode(self, mode):
        (self.home / "jev").mkdir(exist_ok=True)
        (self.home / "jev" / "state.json").write_text(json.dumps({"lanes": mode}))

    def test_off_shadow_and_on(self):
        from jevkit import lane_shadow
        now = 2_000_000_000
        db = self.make_board([
            ("t_a", "Fix typo in README", "", "todo", "qa", now - 60, None, None),
            ("t_b", "Pinned card", "", "todo", "qa", now - 50, "gpt-6-astra", None),
            ("t_c", "Old card", "", "todo", "qa", now - 99_999, None, None),
        ])
        fake = Scripted({"lane": "small", "security_sensitive": 0.05, "underspecified": 0.1})
        applied = []
        self.assertEqual(lane_shadow.tick(kanban_db=db, now=now, transport=fake,
                                          apply=lambda i, t: applied.append(i) or True)["mode"], "off")
        self.assertEqual(fake.requests, [])
        self.set_mode("shadow")
        out = lane_shadow.tick(kanban_db=db, now=now, transport=fake, apply=lambda i, t: applied.append(i) or True)
        self.assertEqual(out["classified"], 2)           # the old card is outside the first look-back
        self.assertEqual(out["lanes"], {"small": 1, "keep_current": 1})
        self.assertEqual(applied, [])
        self.assertEqual(len(fake.requests), 1)          # the pinned card is decided by code
        rows = [json.loads(l) for l in lane_shadow.log_path().read_text().splitlines()]
        self.assertNotIn("Fix typo", json.dumps(rows))
        self.assertEqual(rows[0]["target"]["model"], lanes.targets("hermes")["small"]["model"])
        again = lane_shadow.tick(kanban_db=db, now=now + 60, transport=fake)
        self.assertEqual(again["classified"], 0)        # the cursor moved on
        self.set_mode("on")
        (self.home / "jev" / "lanes-shadow.cursor.json").unlink()
        out = lane_shadow.tick(kanban_db=db, now=now, transport=fake, apply=lambda i, t: applied.append(i) or True)
        self.assertEqual(applied, ["t_a"])
        (self.home / "jev" / "LANES_OFF").write_text("")
        self.assertEqual(lane_shadow.tick(kanban_db=db, now=now, transport=fake)["mode"], "off")


class Replay(unittest.TestCase):
    """The backtest reads history read-only, never sends text, and prices both sides the same way."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_kanban_rows_join_sessions_and_skip_private_profiles(self):
        import sqlite3
        from jevkit import lane_replay
        db = self.root / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
                    "model_override TEXT, reasoning_effort TEXT)")
        con.execute("CREATE TABLE task_runs (id INTEGER, task_id TEXT, profile TEXT, outcome TEXT, status TEXT, "
                    "metadata TEXT, started_at INTEGER, ended_at INTEGER)")
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", [
            ("t1", "Fix the parser", "details", "done", "qa", 10, None, None),
            ("t2", "Pay the invoice", "", "done", "billing", 11, None, None)])
        con.executemany("INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?)", [
            (1, "t1", "qa", "crashed", "crashed", json.dumps({"worker_session_id": "s1"}), 1, 2),
            (2, "t1", "qa", "completed", "done", json.dumps({"worker_session_id": "s2"}), 3, 4),
            (3, "t2", "billing", "completed", "done", json.dumps({"worker_session_id": "s3"}), 5, 6)])
        con.commit()
        con.close()
        for profile, sessions in (("qa", ("s1", "s2")), ("billing", ("s3",))):
            (self.root / "profiles" / profile).mkdir(parents=True)
            con = sqlite3.connect(self.root / "profiles" / profile / "state.db")
            con.execute("CREATE TABLE sessions (id TEXT, model TEXT, model_config TEXT, input_tokens INT, output_tokens INT, "
                        "cache_read_tokens INT, reasoning_tokens INT, estimated_cost_usd REAL, api_call_count INT)")
            for sid in sessions:
                con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", (
                    sid, "sol", json.dumps({"reasoning_config": {"effort": "medium", "enabled": True}}), 100, 10, 5, 2, 0, 3))
            con.commit()
            con.close()
        (self.root / "jev").mkdir()
        (self.root / "jev" / "routing.json").write_text(json.dumps({"private_profiles": ["billing"]}))
        rows = lane_replay.build_rows(db, self.root)
        self.assertEqual([r["id"] for r in rows], ["t1"])
        row = rows[0]
        self.assertEqual((row["tokens"], row["runs"], row["failed_runs"]), (220, 2, 1))
        self.assertFalse(row["first_try_success"])
        self.assertEqual(row["outcome"], "success")
        self.assertEqual(row["effort"], "medium")

    def test_claude_report_prices_both_sides_on_the_same_tokens(self):
        from jevkit import lane_replay
        base = {"input_tokens": 0, "output_tokens": 1_000_000, "cache_read_tokens": 0, "cache_write_tokens": 0}
        rows = [{**base, "model": "claude-opus-5-5", "cost_usd": 20.0, "decision": {"action": "medium"}},
                {**base, "model": "claude-fable-5", "cost_usd": 50.0, "decision": {"action": "keep_current"}}]
        out = lane_replay.claude_report(rows)
        self.assertEqual(out["lanes"]["medium"]["cost_at_lane_model"], 10.0)     # Sonnet 5 output
        self.assertEqual(out["totals"]["cost_always_top"], 40.0)                 # both on Opus 5.5
        self.assertEqual(out["totals"]["cost_lanes_keep_current_on_top"], 30.0)
        self.assertEqual(out["totals"]["lanes_vs_always_top"], -0.25)

    def test_subagent_transcripts_count_each_message_once(self):
        from jevkit import lane_replay
        folder = self.root / "proj" / "session" / "subagents"
        folder.mkdir(parents=True)
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 1}
        lines = [{"type": "user", "message": {"role": "user", "content": "Rename foo to bar"}},
                 {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5", "usage": usage}},
                 {"type": "assistant", "message": {"id": "m1", "model": "claude-haiku-4-5", "usage": usage}},
                 {"type": "assistant", "message": {"id": "m2", "model": "<synthetic>", "usage": usage}}]
        (folder / "agent-x.jsonl").write_text("\n".join(json.dumps(l) for l in lines))
        rows = lane_replay.build_claude_rows(self.root / "proj")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["turns"], rows[0]["output_tokens"], rows[0]["model"]), (1, 5, "claude-haiku-4-5"))
        self.assertEqual(rows[0]["state"], {"task": "Rename foo to bar"})


if __name__ == "__main__":
    unittest.main()
