"""Skill suggestions that earn their place: no repeats inside a session, and a skill the agent
keeps declining stops being offered (but is still offered now and then).

Offline and hermetic: the picker, Hermes's loader and the clock are faked.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
FOLDER = REPO / "hermes" / "plugin" / "hermes-jev"
_spec = importlib.util.spec_from_file_location("hermes_jev_feedback_under_test", FOLDER / "__init__.py",
                                               submodule_search_locations=[str(FOLDER), str(REPO)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules["hermes_jev_feedback_under_test"] = plugin
_spec.loader.exec_module(plugin)

TURN = "please fix the flaky deploy check in the release pipeline"


class FeedbackCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = 1_800_000_000.0
        self.picked = "release-checks"
        patches = [
            mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}),
            mock.patch.object(plugin, "_hermes_skill_roots", lambda: []),
            mock.patch.object(plugin, "_hermes_config", lambda: {}),
            mock.patch.object(plugin, "_disabled_skills", lambda: set()),
            mock.patch.object(plugin.skillpick, "discover", lambda roots, disabled=(): [{"name": self.picked}]),
            mock.patch.object(plugin.skillpick, "pick", lambda text, skills, top_k=1, **kw: {
                "status": "ok", "needs_skill": True, "skills": [{"name": self.picked, "match": 0.9}]}),
            mock.patch.object(plugin, "_reachable_skill", lambda name: name),
            mock.patch.object(plugin.time, "time", lambda: self.now),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for store in (plugin._TURNS, plugin._LOADED, plugin._PENDING):
            store.clear()
            self.addCleanup(store.clear)
        state = self.home / "jev" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"skills": "on"}), encoding="utf-8")

    def suggest(self, session):
        return plugin._on_pre_llm_call(session_id=session, turn_id=f"{session}-t", user_message=TURN)

    def load(self, session, name=None, status="ok"):
        plugin._on_post_tool_call(tool_name="skill_view", args={"name": name or self.picked},
                                  session_id=session, status=status)

    def outcomes(self, name=None):
        data = json.loads((self.home / "jev" / "skill-feedback.json").read_text())
        return [row[1] for row in data["skills"][name or self.picked]["outcomes"]]

    def log(self, kind):
        path = self.home / "logs" / "jev-decisions.jsonl"
        return [e for e in map(json.loads, path.read_text().splitlines()) if e["kind"] == kind] if path.exists() else []


class FeedbackTests(FeedbackCase):
    def test_a_suggestion_counts_as_ignored_until_the_session_loads_it(self):
        self.assertIsNotNone(self.suggest("s1"))
        self.assertEqual(self.outcomes(), [0], "recorded when made, so a worker that exits still counts")
        self.load("s1")
        self.assertEqual(self.outcomes(), [1])
        self.assertEqual(len(self.log("skill_accepted")), 1)

    def test_a_skill_the_session_already_loaded_is_not_suggested_again(self):
        self.load("s1")
        self.assertIsNone(self.suggest("s1"))
        self.assertEqual(self.log("skill_repeat")[0]["candidate"], self.picked)
        self.assertIsNotNone(self.suggest("s2"), "another session has not loaded it")

    def test_a_skill_declined_five_times_stops_being_offered_then_is_offered_again_later(self):
        for n in range(5):
            self.assertIsNotNone(self.suggest(f"s{n}"))
            self.now += 60
        self.assertIsNone(self.suggest("s5"))
        self.assertIn("0 of its last 5", self.log("skill_suppressed")[0]["reason"])
        self.now += plugin.FEEDBACK_REPROBE_S
        self.assertIsNotNone(self.suggest("s6"), "a suppressed skill is still offered once in a while")
        self.load("s6")
        self.now += 60
        self.assertIsNotNone(self.suggest("s7"), "1 of 6 is above the floor, so it is back")

    def test_one_load_in_five_keeps_a_skill_on_offer(self):
        for n in range(5):
            self.suggest(f"s{n}")
            if n == 2:
                self.load(f"s{n}")
            self.now += 60
        self.assertIsNotNone(self.suggest("s5"))

    def test_outcomes_older_than_the_window_do_not_count(self):
        for n in range(5):
            self.suggest(f"s{n}")
        self.now += plugin.FEEDBACK_WINDOW_S + 1
        self.assertIsNotNone(self.suggest("s9"))

    def test_other_skills_are_not_affected(self):
        for n in range(5):
            self.suggest(f"s{n}")
        self.picked = "another-skill"
        self.assertIsNotNone(self.suggest("s5"))

    def test_a_failed_skill_view_is_not_a_load(self):
        self.suggest("s1")
        self.load("s1", status="error")
        self.assertEqual(self.outcomes(), [0])

    def test_a_corrupt_file_suppresses_nothing(self):
        path = self.home / "jev" / "skill-feedback.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNotNone(self.suggest("s1"))

    def test_the_switch_turns_the_feedback_off(self):
        (self.home / "jev" / "state.json").write_text(json.dumps({"skills": "on", "skill_feedback": "off"}))
        for n in range(7):
            self.assertIsNotNone(self.suggest(f"s{n}"))
        self.assertFalse((self.home / "jev" / "skill-feedback.json").exists())

    def test_the_file_holds_names_and_times_only(self):
        self.suggest("s1")
        raw = (self.home / "jev" / "skill-feedback.json").read_text()
        self.assertNotIn("deploy", raw)
        self.assertNotIn("s1", raw.replace(self.picked, ""), "no session ids either")


if __name__ == "__main__":
    unittest.main()
