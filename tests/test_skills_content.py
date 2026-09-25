"""Guard the two agent-facing skills against drift.

These are the parts a fleet depends on when it points its AGENTS.md gate at
`jev-computer-use` / `jev-browser-use`:

- the fleet note pointer, so runtime facts have exactly one home,
- the withdrawn preview schema named as incompatible,
- the path rule that a fleet can make Jev Ultrafast the required default,
- no machine-specific paths leaking into the public repo.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
FLEET_NOTE = "shared/rules/jev-computer-use-fleet.md"


def read(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


class SkillContentTests(unittest.TestCase):
    def test_readme_skill_count_matches_shipped_files(self):
        count = len(list(SKILLS.glob("*/SKILL.md")))
        number_words = [
            "zero", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
            "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
        ]
        self.assertLessEqual(count, 20, "extend number_words for the new shipped skill count")
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"{number_words[count].title()} skills ship as plain `SKILL.md` files", readme)
        self.assertIn(f"skills/            {number_words[count]} SKILL.md skills", readme)

    def test_every_skill_has_matching_frontmatter_name(self):
        for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
            body = skill_md.read_text(encoding="utf-8")
            match = re.search(r"^name:\s*(\S+)\s*$", body, re.MULTILINE)
            if match is None:
                self.fail(f"{skill_md} has no name: in frontmatter")
            self.assertEqual(match.group(1), skill_md.parent.name)

    def test_computer_use_points_at_the_fleet_note(self):
        body = read("jev-computer-use")
        self.assertIn(FLEET_NOTE, body)
        self.assertIn("Managed fleets", body)

    def test_computer_use_names_the_withdrawn_schema(self):
        body = read("jev-computer-use")
        self.assertIn("hermes.cua_jev_choice_request_v1", body)
        self.assertIn("jev.action_choice_request_v1", body)
        self.assertIn("jev-latest", body)

    def test_browser_use_points_at_the_fleet_note_and_path_rule(self):
        body = read("jev-browser-use")
        self.assertIn(FLEET_NOTE, body)
        self.assertIn("Managed fleets", body)
        self.assertIn("required default", body)

    def test_every_description_survives_the_picker_whole(self):
        """The picker truncates a description; a skill whose tail is cut cannot be ranked on it.

        The picker can hide the clauses that distinguish otherwise similar skills when
        descriptions exceed its limit. Import the constant rather than repeating 200,
        so the bound moves with the picker.
        """
        from jevkit import skillpick
        shipped = skillpick.discover([SKILLS])
        # discover() drops a skill with no description at all, which would hide it from
        # this check and from the picker alike, so count them rather than trust the list.
        self.assertEqual(len(shipped), len(list(SKILLS.glob("*/SKILL.md"))),
                         "a shipped skill has no description for the picker to read")
        for skill in shipped:
            self.assertLessEqual(
                len(skill["description"]), skillpick.DESCRIPTION_CHARS,
                f"{skill['name']}: description is {len(skill['description'])} characters; "
                f"the picker reads only the first {skillpick.DESCRIPTION_CHARS}",
            )

    def test_no_machine_specific_paths_in_skills(self):
        for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
            body = skill_md.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"/Users/[a-z][a-z0-9_-]+/|/home/[a-z][a-z0-9_-]+/", body),
                f"{skill_md} carries a machine-specific path",
            )


if __name__ == "__main__":
    unittest.main()


class SkillCommandsExistTests(unittest.TestCase):
    """Every `jev <subcommand>` a skill tells an agent to run must be real.

    jev-frontier-work told agents to run `jev escalate` at three separate places. The
    command was renamed to `jev ladder` and the skill was never updated, so any agent
    that loaded that skill errored three times and had no way to discover the real name.
    In a repo whose whole premise is "an agent reads a SKILL.md and acts", this is the
    most damaging drift there is, and it is trivially checkable.
    """

    def test_every_jev_command_in_a_skill_is_a_real_subcommand(self):
        from jevkit import cli
        parser = cli.build_parser()
        known = set()
        for action in parser._actions:
            if getattr(action, "choices", None) and isinstance(action.choices, dict):
                known |= set(action.choices)
        self.assertIn("ladder", known, "parser introspection failed; fix this test")

        # `/jev routing shadow` is a Hermes slash command, not a CLI subcommand. The leading
        # slash is what tells them apart.
        pattern = re.compile(r"(?<![\w`/])jev\s+([a-z][a-z-]+)")
        allowed_words = {"choose", "status", "refuse", "clear"}      # ladder/choose sub-verbs
        offenders = []
        for skill in sorted(SKILLS.glob("*/SKILL.md")):
            for number, line in enumerate(skill.read_text(encoding="utf-8").splitlines(), 1):
                for match in pattern.finditer(line):
                    word = match.group(1)
                    if word not in known and word not in allowed_words:
                        offenders.append(f"{skill.parent.name}/SKILL.md:{number}: jev {word}")
        self.assertEqual(offenders, [], "skills reference commands that do not exist:\n" +
                         "\n".join(offenders))
