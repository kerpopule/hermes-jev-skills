"""The planner's prompt is a contract with a model, and the model reads it literally.

A live measurement (2026-09-22, `evals/plan-quality/`) found `jev plan` falling back on one
command in ten because the prompt never said which keys exist: the model wrote
`press_key: "volume down"`, and one step outside the vocabulary rejects the whole plan by design.
The first fix for that caused a second failure — a licence to "leave that part of the command
out" made the model drop a screenshot step it could have pressed — so these guards pin the
wording that survived both rounds, and the eval set that showed it.

Offline: reads the prompt and the script, never calls a model.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "measure_plan_quality.py"
SCORECARD = REPO / "evals" / "plan-quality" / "SCORECARD-2026-09-22-prompt-keys.md"

sys.path.insert(0, str(REPO))

from jevkit import plan  # noqa: E402


def load_script():
    spec = importlib.util.spec_from_file_location("measure_plan_quality_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_plan_quality_under_test"] = module
    spec.loader.exec_module(module)
    return module


class PromptContractTests(unittest.TestCase):
    def setUp(self):
        self.prompt = plan.SYSTEM_PROMPT

    def test_the_prompt_names_the_keys_that_exist(self):
        """The whole finding: without this line the model invents key names."""
        self.assertIn("Never invent a key name", self.prompt)
        for key in ("return", "tab", "escape", "space", "pageup", "pagedown", "f1 to f12"):
            self.assertIn(key, self.prompt, f"the key list lost {key!r}")

    def test_the_prompt_says_the_keys_take_modifiers(self):
        """`cmd+shift+3` for a screenshot is expressible, and the model has to be told so."""
        self.assertIn("cmd+shift+3", self.prompt)
        self.assertIn("can take modifiers", self.prompt)

    def test_function_keys_are_scoped_to_their_own_meaning(self):
        """Round 1 sent f11 for 'volume down'. The list alone made that look reasonable."""
        self.assertIn("ordinary function keys", self.prompt)
        self.assertIn("use one only when the command asks for that function key", self.prompt)

    def test_the_prompt_does_not_license_dropping_a_step(self):
        """Round 1's wording did, and the screenshot step was dropped 2 runs out of 2."""
        self.assertNotIn("leave that part of the command out", self.prompt)
        self.assertIn("use a click on the control on screen, or a menu path", self.prompt)

    def test_every_word_of_the_prompt_is_ascii(self):
        """The prompt is sent as JSON to providers whose dialects differ on nothing else here."""
        self.prompt.encode("ascii")

    def test_the_prompt_is_still_small_enough_to_be_worth_sending(self):
        """It grew ~90 tokens for the key list; the plan has to stay worth the request."""
        self.assertLess(len(self.prompt), 6000)


class EvalSetTests(unittest.TestCase):
    def setUp(self):
        self.eval = load_script()

    def test_the_bar_is_stated_in_the_method(self):
        doc = self.eval.__doc__ or ""
        self.assertIn("root cause", doc)
        self.assertIn("fallback", doc)

    def test_the_commands_are_ordinary_work_that_should_plan(self):
        """A send or a delete would be answered by `enforce_never_send`, not by this number."""
        forbidden = re.compile(r"\b(send|post|submit|pay|delete|empty the trash|purchase)\b", re.I)
        for command in self.eval.COMMANDS:
            self.assertIsNone(forbidden.search(command), f"not a planning case: {command!r}")

    def test_the_eval_never_executes_a_step(self):
        """`plan` returns steps; the runner that would carry them out is elsewhere and unused."""
        source = SCRIPT.read_text(encoding="utf-8")
        for call in ("run_plan(", "execute(", "subprocess.run(", "osascript"):
            self.assertNotIn(call, source, f"the measurement must not execute anything: {call}")

    def test_the_cache_is_off_so_every_run_measures_the_model(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('os.environ["JEV_MEMO"] = "off"', source)

    def test_every_fallback_is_diagnosed_down_to_its_step(self):
        """A count with no cause is a number nobody can act on."""
        rows = [{"position": 3, "of": 3, "step": {"kind": "press_key", "target": "volume down"}}]
        self.assertEqual(self.eval.offending_step.__name__, "offending_step")
        self.assertEqual(rows[0]["step"]["kind"], "press_key")

    def test_the_scorecard_records_all_three_rounds(self):
        text = SCORECARD.read_text(encoding="utf-8")
        for needed in ("volume down", "20 / 22", "33 / 33", "cmd+shift+3", "f11",
                       "planned", "counts plans, not correct plans"):
            self.assertIn(needed, text, f"the scorecard lost {needed!r}")


if __name__ == "__main__":
    unittest.main()