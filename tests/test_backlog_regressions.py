"""Regression contracts for the September backlog integration."""
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from jevkit import skillpick

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SkillGateTests(unittest.TestCase):
    def test_pure_social_openers_and_continuations_skip(self):
        for text in ("cool. next?", "hey how are you doing today", "good morning!", "well done", "what's next?"):
            with self.subTest(text=text):
                self.assertTrue(skillpick.looks_trivial(text))

    def test_real_questions_and_instructions_survive(self):
        for text in ("all working?", "next, fix the config", "do it", "go on to the next file", "what time is it"):
            with self.subTest(text=text):
                self.assertFalse(skillpick.looks_trivial(text))

    def test_meta_skill_removed_before_catalog_cap(self):
        skills = [{"name": "using-superpowers", "description": "pick a skill"},
                  {"name": "useful", "description": "a real procedure"}]
        with mock.patch.object(skillpick, "_rank", return_value={"skills": []}) as rank:
            skillpick.pick("debug this", skills)
        self.assertEqual([x["name"] for x in rank.call_args.args[1]], ["useful"])

    def test_discovery_excludes_selector_meta_skill(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("using-superpowers", "useful"):
                path = pathlib.Path(d) / name
                path.mkdir()
                (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: description\n---\n")
            self.assertEqual([x["name"] for x in skillpick.discover([pathlib.Path(d)])], ["useful"])


class LauncherTests(unittest.TestCase):
    def test_launcher_runs_from_unrelated_directory_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            result = subprocess.run([str(ROOT / "bin" / "jev"), "--version"], cwd=d,
                                    env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.19", result.stdout)


class ManifestTests(unittest.TestCase):
    def test_declares_registered_search_tool_and_request_middleware(self):
        manifest = (ROOT / "hermes/plugin/hermes-jev/plugin.yaml").read_text()
        self.assertIn("  - jev_search\n", manifest)
        self.assertIn("provides_middleware:\n  - llm_request\n", manifest)


if __name__ == "__main__":
    unittest.main()
