"""Two files carry the version a person sees, and one release shipped them disagreeing.

0.19.0 bumped `jevkit/__init__.py` and left `hermes/plugin/hermes-jev/plugin.yaml` at
0.18.0. Every release before it bumped both (`git log -- hermes/plugin/hermes-jev/plugin.yaml`
shows 0.18.0, 0.17.0, 0.16.0, 0.14.0, 0.13.2), CONTRIBUTING asks for them to move together,
and nothing checked it - so the manifest Hermes reads, and the one a bug report quotes, was a
whole release behind the code that was running.

These three tests are the check. They read the files in this checkout only: no network, no
install, no Hermes.

The first test is the one that matters most and is easy to leave out: a version guard that
cannot tell "in sync" from "the parser found nothing" passes on a repo where somebody renames
a file. check_release.py already learned this the hard way.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "hermes" / "plugin" / "hermes-jev" / "plugin.yaml"
LIBRARY = REPO / "jevkit" / "__init__.py"
CHANGELOG = REPO / "CHANGELOG.md"

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def manifest_version() -> str:
    """The `version:` line of the plugin manifest, or "" if there is not one."""
    if not MANIFEST.is_file():
        return ""
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def library_version() -> str:
    """The `__version__` in jevkit, or "" if the line is gone."""
    if not LIBRARY.is_file():
        return ""
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', LIBRARY.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


def changelog_versions() -> list[str]:
    """Every released version heading, newest first. `## Unreleased` is not one."""
    headings = re.findall(r"^##\s+(\d+\.\d+\.\d+)(?:\s|$)", CHANGELOG.read_text(encoding="utf-8"), re.M)
    return headings


class GuardTests(unittest.TestCase):
    """A guard that reads nothing passes everything."""

    def test_both_parsers_find_a_version(self):
        self.assertRegex(library_version(), _VERSION)
        self.assertRegex(manifest_version(), _VERSION)

    def test_the_changelog_has_released_sections_to_check_against(self):
        self.assertTrue(changelog_versions(), "no `## x.y.z` heading in CHANGELOG.md")


class SyncTests(unittest.TestCase):
    def test_the_manifest_reports_the_library_version(self):
        """0.19.0 shipped 0.19.0 code under an 0.18.0 manifest."""
        self.assertEqual(manifest_version(), library_version(),
                         "hermes/plugin/hermes-jev/plugin.yaml and jevkit/__init__.py "
                         "must be bumped together (CONTRIBUTING.md)")

    def test_the_library_version_has_a_released_changelog_section(self):
        """The version a user reports has to be a version with release notes."""
        self.assertIn(library_version(), changelog_versions(),
                      f"no `## {library_version()}` section in CHANGELOG.md")

    def test_the_prerelease_version_is_what_the_top_section_reaches(self):
        """Nothing may sit above the newest released heading except `Unreleased`."""
        text = CHANGELOG.read_text(encoding="utf-8")
        first_heading = next((l.strip() for l in text.splitlines() if l.startswith("## ")), "")
        self.assertIn(first_heading, {"## Unreleased", f"## {library_version()}"},
                      f"CHANGELOG.md starts at {first_heading!r}; only `## Unreleased` or "
                      f"`## {library_version()}` belongs at the top")


if __name__ == "__main__":
    unittest.main()
