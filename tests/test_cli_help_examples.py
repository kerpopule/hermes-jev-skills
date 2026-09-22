"""`jev ask --help` is a copy-paste surface: every example in it has to be a request the CLI takes.

Help text is where people learn the shape, and the shape is now enforced in one place
(`client.check_questions`, 2026-09-22). An example that the validator would refuse is worse than
no example: the person copies it, gets a `ValueError`, and concludes the tool is broken. Nothing
else in this suite reads the help — these tests render it, pull the JSON out of it, and run it
through the same checker the CLI runs it through.

Offline: no network, no key, no model.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from jevkit import cli, client  # noqa: E402


def ask_help() -> str:
    parser = cli.build_parser()
    for action in parser._actions:                      # noqa: SLF001 - the subparsers action
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "ask" in choices:
            block = choices["ask"]
            return "\n".join(filter(None, (block.description, block.epilog,
                                           " ".join(a.help or "" for a in block._actions))))  # noqa: SLF001
    raise AssertionError("no `ask` subcommand in the parser")


def json_examples(text: str) -> list:
    """Every brace-balanced object in the text, parsed. Objects the help wraps are re-joined."""
    flat = " ".join(text.split())
    out = []
    depth = 0
    start = 0
    for index, character in enumerate(flat):
        if character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                try:
                    out.append(json.loads(flat[start:index + 1]))
                except ValueError:
                    pass
    return out


class HelpExamplesTests(unittest.TestCase):
    def setUp(self):
        self.text = ask_help()
        self.examples = json_examples(self.text)

    def test_the_help_still_shows_an_example(self):
        """A help page with no example is how the advertised shape drifted before."""
        self.assertGreaterEqual(len(self.examples), 1, self.text[:400])

    def test_every_question_in_the_help_is_one_the_cli_accepts(self):
        checked = 0
        for example in self.examples:
            questions = example.get("questions") if isinstance(example, dict) else None
            if questions is None:
                continue
            if isinstance(questions, dict):
                pairs = list(questions.items())
            else:
                pairs = [(f"q{position}", raw) for position, raw in enumerate(questions, 1)]
            for name, raw in pairs:
                # The help documents the list form, where the id names the question if present.
                ident = raw.get("id") if isinstance(raw, dict) else None
                ident = ident if isinstance(ident, str) and ident else name
                client.check_question(str(ident), raw)
                checked += 1
        self.assertGreaterEqual(checked, 3, "the help no longer documents a request with questions")

    def test_the_help_documents_the_shape_as_the_wire_spells_it(self):
        """`{id, kind, text}` was advertised once and sent verbatim; both still parse, one is real."""
        self.assertIn('"instructions"', self.text)
        self.assertIn("type", self.text)

    def test_every_example_state_is_well_under_the_limit(self):
        for example in self.examples:
            state = example.get("state") if isinstance(example, dict) else None
            if isinstance(state, str):
                self.assertLessEqual(len(state), client.MAX_STATE_CHARS)


if __name__ == "__main__":
    unittest.main()
