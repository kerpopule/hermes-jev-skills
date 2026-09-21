import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("jev_install", Path(__file__).resolve().parents[1] / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

CONFIG = """model:
  default: some/model   # keep this comment
plugins:
  enabled:
  - coagent-observer
  disabled: []
  entries:
    resource-lifecycle:
      allow_tool_override: false
security:
  redact_secrets: true
"""


def parse_plugin_list(text):
    """Read ``plugins.enabled`` out of a config as the YAML parser would see it.

    Deliberately not ``yaml.safe_load``: this repo keeps a no-dependencies promise and
    PyYAML is only a dashboard extra. The one YAML rule the installer's text edit can
    break is sequence indentation, so that is what this reads — items must share the
    indent of the first item, and a line indented deeper belongs to the item above it.
    Returns the list of names, or None when the key is absent.
    """
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "plugins:"), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i] and not lines[i].startswith((" ", "#"))), len(lines))
    block = lines[start + 1:end]
    key = next((i for i, line in enumerate(block)
                if re.match(r"^  enabled:\s*(\[\s*\])?\s*$", line)), None)
    if key is None:
        return None
    items = []
    base_indent = None
    for line in block[key + 1:]:
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue                      # a comment, or another key: not an item
        indent = len(line) - len(stripped)
        if base_indent is None:
            base_indent = indent
        if indent != base_indent:         # folded into the previous scalar by YAML
            items[-1] = items[-1] + " " + line.strip()
            continue
        items.append(stripped[2:].strip().strip("'\""))
    return items


def run_installer(argv, home, path="/usr/bin:/bin", hermes_home=None):
    """Run the installer in-process against a throwaway HOME and return (exit code, report).

    HERMES_HOME is stripped: on the machine this was written on it points at a real fleet,
    and a test that installs into it would rewrite live config. ``hermes_home`` puts a
    throwaway one back, for the agent shell whose HERMES_HOME is its own profile.
    """
    env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
    env["HOME"] = str(home)
    env["PATH"] = path
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(sys, "argv", ["install.py", *argv]), \
            contextlib.redirect_stdout(out):
        code = install.main()
    return code, json.loads(out.getvalue())


class ConfigEditTests(unittest.TestCase):
    def test_home_warning_flags_a_profile_scoped_install(self):
        self.assertIsNotNone(install.home_warning(Path("/srv/hermes/profiles/devbot")))
        self.assertIsNone(install.home_warning(Path("/srv/hermes")))

    def test_enable_and_disable_touch_only_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG)
            first = install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(set(first.values()), {"enabled"})
            again = install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(set(again.values()), {"already enabled"})
            text = config.read_text()
            self.assertIn("  enabled:\n" + "".join(f"  - {n}\n" for n in install.PLUGINS)
                          + "  - coagent-observer\n", text)
            self.assertIn("# keep this comment", text)
            off = install.enable_plugins(config, install.PLUGINS, False)
            self.assertEqual(set(off.values()), {"disabled"})
            self.assertEqual(config.read_text(), CONFIG)

    def test_unchanged_config_is_not_rewritten_or_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG)
            install.enable_plugins(config, install.PLUGINS, True)
            backups = len(list(Path(tmp).glob("config.yaml.bak-jev-*")))
            install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(len(list(Path(tmp).glob("config.yaml.bak-jev-*"))), backups)

    def test_empty_inline_list_and_missing_section(self):
        listed = "".join(f"  - {n}\n" for n in install.PLUGINS)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled: []\nother: 1\n")
            install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(config.read_text(), f"plugins:\n  enabled:\n{listed}other: 1\n")
            config.write_text("other: 1\n")
            install.enable_plugins(config, install.PLUGINS, True)
            self.assertEqual(config.read_text(), f"other: 1\nplugins:\n  enabled:\n{listed}")

    def test_an_indented_list_keeps_its_own_indent(self):
        """A config whose items are indented deeper than 2 spaces must not be mangled.

        YAML reads a sequence whose items disagree on indentation as ONE scalar with the
        deeper items folded into it, so emitting `  - name` into a list written as
        `    - name` silently drops every plugin already in it — no error, no warning,
        just a shorter list. Asserted on the parsed list, never on the text: the two
        forms are nearly identical in a diff, which is how this shipped.
        """
        body = "plugins:\n  enabled:\n    - coagent-observer\n    - other-plugin\n"
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(body)
            install.enable_plugins(config, install.PLUGINS, True)
            enabled = parse_plugin_list(config.read_text())
            self.assertIsNotNone(enabled)
            for name in ("coagent-observer", "other-plugin", *install.PLUGINS):
                self.assertIn(name, enabled)
            self.assertEqual(len(enabled), 4)

    def test_a_2_space_list_still_round_trips(self):
        """The originally supported shape keeps working — the fix follows, not replaces."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(CONFIG)
            install.enable_plugins(config, install.PLUGINS, True)
            enabled = parse_plugin_list(config.read_text())
            self.assertIsNotNone(enabled)
            for name in ("coagent-observer", *install.PLUGINS):
                self.assertIn(name, enabled)
            self.assertEqual(len(enabled), 1 + len(install.PLUGINS))


class HermesInstallTests(unittest.TestCase):
    def _fleet(self, tmp):
        root = Path(tmp) / "hermes"
        for home in (root, root / "profiles" / "alpha"):
            home.mkdir(parents=True)
            (home / "config.yaml").write_text(CONFIG)
        return root

    def test_full_install_links_every_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            report = install.install_hermes(root, "alpha", check=False)
            self.assertTrue((root / "plugins" / "hermes-jev" / "jevkit" / "client.py").is_file())
            self.assertTrue((root / "profiles" / "alpha" / "plugins" / "hermes-jev").is_symlink())
            self.assertTrue((root / "profiles" / "alpha" / "skills" / "jev" / "jev-setup" / "SKILL.md").is_file())
            self.assertEqual(list(report["enabled_in"]), ["alpha"])
            self.assertNotIn("hermes-jev", (root / "config.yaml").read_text())
            install.uninstall_hermes(root)
            self.assertFalse((root / "profiles" / "alpha" / "plugins" / "hermes-jev").exists())
            self.assertEqual((root / "profiles" / "alpha" / "config.yaml").read_text(), CONFIG)

    def test_every_shipped_plugin_is_installed_and_enabled(self):
        # The installer named one plugin, so hermes-handoff was unreachable however
        # faithfully you followed the docs.
        self.assertIn("hermes-jev", install.PLUGINS)
        self.assertIn("hermes-handoff", install.PLUGINS)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            report = install.install_hermes(root, "all", check=False)
            for name in install.PLUGINS:
                plugin_dir = root / "plugins" / name
                self.assertTrue((plugin_dir / "plugin.yaml").is_file(), name)
                self.assertTrue((plugin_dir / "jevkit" / "compact.py").is_file(), name)
                self.assertTrue((root / "profiles" / "alpha" / "plugins" / name).is_symlink(), name)
                self.assertEqual(report["enabled_in"]["default"][name], "enabled")
                self.assertEqual(report["enabled_in"]["alpha"][name], "enabled")
                self.assertIn(f"- {name}", (root / "config.yaml").read_text())

    def test_nightly_script_is_installed_and_stays_runnable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            install.install_hermes(root, "none", check=False)
            script = root / "scripts" / "nightly-handoff.py"
            self.assertTrue(script.is_file())
            self.assertTrue(os.access(script, os.X_OK))
            # It defaults --plugin to <home>/plugins/hermes-handoff, so the pair has to land together.
            self.assertTrue((root / "plugins" / "hermes-handoff" / "handoff.py").is_file())

    def test_uninstall_removes_what_was_installed_and_leaves_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            install.install_hermes(root, "all", check=False)
            theirs = root / "scripts" / "backup-nightly.sh"
            theirs.write_text("# someone else's cron job\n")
            install.uninstall_hermes(root)
            for name in install.PLUGINS:
                self.assertFalse((root / "plugins" / name).exists(), name)
                self.assertFalse((root / "profiles" / "alpha" / "plugins" / name).exists(), name)
            self.assertFalse((root / "skills" / "jev").exists())
            self.assertFalse((root / "scripts" / "nightly-handoff.py").exists())
            self.assertTrue(theirs.is_file())
            self.assertEqual((root / "config.yaml").read_text(), CONFIG)

    def test_uninstall_leaves_no_empty_scripts_directory_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            install.install_hermes(root, "none", check=False)
            install.uninstall_hermes(root)
            self.assertFalse((root / "scripts").exists())


def snapshot(top):
    """Every path under ``top`` and what it holds, so "wrote nothing" is looked at, not assumed."""
    seen = {}
    for folder, dirs, files in os.walk(top):
        for name in dirs + files:
            path = Path(folder) / name
            if path.is_symlink():
                seen[str(path)] = ("link", os.readlink(path))
            elif path.is_dir():
                seen[str(path)] = ("dir", None)
            else:
                seen[str(path)] = ("file", path.read_bytes())
    return seen


class CommandLinkTests(unittest.TestCase):
    """The `jev` link: one per Hermes home, and never at the cost of a file that is not ours."""

    THEIRS = "#!/bin/sh\necho a different jev\n"

    def _fleet(self, tmp):
        root = Path(tmp) / ".hermes"
        for home in (root, root / "profiles" / "alpha", root / "profiles" / "beta"):
            home.mkdir(parents=True)
            (home / "config.yaml").write_text(CONFIG)
        (root / "profiles" / "scratch").mkdir()            # no config.yaml, so not a lane
        return root

    def _shims(self, root):
        return [root / "bin" / "jev", root / "profiles" / "alpha" / "bin" / "jev",
                root / "profiles" / "beta" / "bin" / "jev"]

    def _foreign_file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.THEIRS)
        return path

    def test_every_profile_home_gets_its_own_jev_link(self):
        """An agent shell's PATH carries <HERMES_HOME>/bin, and HERMES_HOME is the profile's
        own home. The link went to the root only, so every profile lane still got
        command not found."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            code, report = run_installer([], tmp)
            self.assertEqual(code, 0)
            for shim in self._shims(root):
                self.assertTrue(shim.is_symlink(), shim)
                self.assertEqual(shim.resolve(), install.JEV)
            self.assertEqual(report["cli"]["agent_shell_commands"], [str(s) for s in self._shims(root)])
            self.assertNotIn("not_linked", report["cli"])
            self.assertFalse((root / "profiles" / "scratch" / "bin").exists())

    def test_a_jev_that_is_not_ours_is_left_alone_and_reported(self):
        """The link step unlinked whatever sat at bin/jev, so a person's own script of that
        name was gone with no backup and no word in the report."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            elsewhere = self._foreign_file(Path(tmp) / "other-checkout" / "bin" / "jev")
            a_file = self._foreign_file(root / "profiles" / "alpha" / "bin" / "jev")
            a_link = root / "profiles" / "beta" / "bin" / "jev"
            a_link.parent.mkdir()
            a_link.symlink_to(elsewhere)
            in_local_bin = self._foreign_file(Path(tmp) / ".local" / "bin" / "jev")
            code, report = run_installer([], tmp)
            self.assertEqual(code, 0)
            self.assertFalse(a_file.is_symlink())
            self.assertEqual(a_file.read_text(), self.THEIRS)
            self.assertEqual(os.readlink(a_link), str(elsewhere))
            self.assertEqual(in_local_bin.read_text(), self.THEIRS)
            self.assertEqual(set(report["cli"]["not_linked"]), {str(a_file), str(a_link), str(in_local_bin)})
            for left in (a_file, a_link, in_local_bin):
                self.assertIn(str(left), report["warning"])
            # The rest of the install carried on: the root still got its link, and the plugins went in.
            self.assertEqual(report["cli"]["agent_shell_commands"], [str(root / "bin" / "jev")])
            self.assertEqual((root / "bin" / "jev").resolve(), install.JEV)
            self.assertTrue((root / "plugins" / "hermes-jev" / "plugin.yaml").is_file())

    def test_a_dangling_link_into_this_checkout_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            stale = root / "profiles" / "alpha" / "bin" / "jev"
            stale.parent.mkdir()
            stale.symlink_to(install.REPO / "bin" / "jev-as-it-was-once-called")
            self.assertFalse(stale.exists())
            code, report = run_installer([], tmp)
            self.assertEqual(code, 0)
            self.assertEqual(stale.resolve(), install.JEV)
            self.assertNotIn("not_linked", report["cli"])

    def test_uninstall_removes_only_the_links_it_made(self):
        """--uninstall deleted ANY bin/jev without checking whose it was."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            theirs = self._foreign_file(root / "profiles" / "alpha" / "bin" / "jev")
            run_installer([], tmp)
            ours = [Path(tmp) / ".local" / "bin" / "jev", root / "bin" / "jev", root / "profiles" / "beta" / "bin" / "jev"]
            for link in ours:
                self.assertTrue(link.is_symlink(), link)
            code, report = run_installer(["--uninstall"], tmp)
            self.assertEqual(code, 0)
            for link in ours:
                self.assertFalse(link.is_symlink() or link.exists(), link)
            self.assertEqual(report["cli"]["removed"], [str(link) for link in ours])
            self.assertEqual(theirs.read_text(), self.THEIRS)
            self.assertEqual(list(report["cli"]["left_alone"]), [str(theirs)])
            self.assertIn(str(theirs), report["warning"])

    def test_uninstall_finds_the_link_in_a_lane_whose_config_has_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            run_installer([], tmp)
            (root / "profiles" / "beta" / "config.yaml").unlink()
            run_installer(["--uninstall"], tmp)
            self.assertFalse((root / "profiles" / "beta" / "bin" / "jev").is_symlink())

    def test_check_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            theirs = self._foreign_file(root / "profiles" / "alpha" / "bin" / "jev")
            stale = root / "profiles" / "beta" / "bin" / "jev"
            stale.parent.mkdir()
            stale.symlink_to(install.REPO / "bin" / "jev-as-it-was-once-called")
            before = snapshot(tmp)
            code, report = run_installer(["--check", "--skills-dir", f"{tmp}/agent"], tmp)
            self.assertEqual(code, 0)
            self.assertEqual(snapshot(tmp), before)
            # It still says what an install would do, the refusal included.
            self.assertEqual(report["cli"]["agent_shell_commands"], [str(root / "bin" / "jev"), str(stale)])
            self.assertEqual(list(report["cli"]["not_linked"]), [str(theirs)])
            self.assertIn("would not be linked", report["warning"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes through a read-only folder")
    def test_one_home_that_cannot_be_written_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            locked = root / "profiles" / "alpha" / "bin"
            locked.mkdir()
            locked.chmod(0o555)
            try:
                code, report = run_installer([], tmp)
            finally:
                locked.chmod(0o755)                        # or the temporary directory cannot be cleaned up
            self.assertEqual(code, 0)
            self.assertEqual(list(report["cli"]["not_linked"]), [str(locked / "jev")])
            self.assertIn(str(locked / "jev"), report["warning"])
            self.assertEqual((root / "bin" / "jev").resolve(), install.JEV)
            self.assertEqual((root / "profiles" / "beta" / "bin" / "jev").resolve(), install.JEV)
            self.assertTrue((root / "profiles" / "alpha" / "plugins" / "hermes-jev").is_symlink())

    def test_a_bin_that_is_a_file_costs_only_that_home_its_link(self):
        """The same failure with no permissions involved, so it is also covered when the tests run as root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            blocked = root / "profiles" / "alpha" / "bin"
            blocked.write_text("not a folder\n")
            for argv in (["--check"], []):
                code, report = run_installer(argv, tmp)
                self.assertEqual(code, 0)
                self.assertEqual(list(report["cli"]["not_linked"]), [str(blocked / "jev")], argv)
                self.assertEqual(report["cli"]["agent_shell_commands"],
                                 [str(root / "bin" / "jev"), str(root / "profiles" / "beta" / "bin" / "jev")], argv)
            self.assertEqual(blocked.read_text(), "not a folder\n")
            self.assertEqual((root / "profiles" / "beta" / "bin" / "jev").resolve(), install.JEV)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes through a read-only folder")
    def test_a_whole_lane_that_cannot_be_written_costs_only_that_lane_and_the_report_still_prints(self):
        """With the lane itself read-only, not just its bin, the `jev` step warned and then the
        plugin links raised: a traceback, no report, so the warning was never seen, and
        --uninstall stopped before it reached a single `jev` link."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            locked = root / "profiles" / "alpha"
            locked.chmod(0o555)
            try:
                code, report = run_installer([], tmp)
                self.assertEqual(code, 0)
                self.assertEqual(list(report["cli"]["not_linked"]), [str(locked / "bin" / "jev")])
                self.assertEqual(list(report["hermes"]["not_installed_in"]), [str(locked)])
                self.assertIn(f"{locked} could not be installed into", report["warning"])
                self.assertEqual((root / "profiles" / "beta" / "bin" / "jev").resolve(), install.JEV)
                self.assertTrue((root / "profiles" / "beta" / "plugins" / "hermes-jev").is_symlink())
                self.assertIn("hermes-jev", (root / "profiles" / "beta" / "config.yaml").read_text())
                locked.chmod(0o755)
                run_installer([], tmp)                     # now the lane holds an install to fail to remove
                locked.chmod(0o555)
                code, report = run_installer(["--uninstall"], tmp)
                self.assertEqual(code, 0)
                self.assertEqual(list(report["hermes"]["not_removed_from"]), [str(locked)])
                self.assertIn(str(locked), report["warning"])
                self.assertFalse((root / "bin" / "jev").is_symlink())
                self.assertFalse((root / "profiles" / "beta" / "bin" / "jev").is_symlink())
            finally:
                locked.chmod(0o755)                        # or the temporary directory cannot be cleaned up

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads through any mode")
    def test_a_lane_that_cannot_be_read_does_not_stop_the_run(self):
        """is_file raises on a folder it may not look into, so one such profile was a
        traceback in every mode, --check included."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            locked = root / "profiles" / "alpha"
            locked.chmod(0o000)
            try:
                for argv in (["--check"], [], ["--uninstall"]):
                    code, report = run_installer(argv, tmp)
                    self.assertEqual(code, 0, argv)
                    if argv == []:
                        self.assertEqual((root / "profiles" / "beta" / "bin" / "jev").resolve(), install.JEV)
            finally:
                locked.chmod(0o755)

    def test_check_does_not_promise_a_link_through_a_dangling_bin(self):
        """A bin that is a symlink to nowhere does not exist, so --check judged the lane by
        its writable parent and said yes; the install then failed on mkdir."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            (root / "profiles" / "alpha" / "bin").symlink_to(Path(tmp) / "nowhere")
            for argv in (["--check"], []):
                code, report = run_installer(argv, tmp)
                self.assertEqual(list(report["cli"]["not_linked"]), [str(root / "profiles" / "alpha" / "bin" / "jev")], argv)

    def test_a_root_that_only_lives_in_a_folder_called_profiles_is_not_mistaken_for_a_lane(self):
        """<x>/profiles/hermes as the ROOT was read as lane "hermes" of root <x>, and a `jev`
        link went to <x>/bin, outside any Hermes home."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles" / "hermes"
            root.mkdir(parents=True)
            (root / "config.yaml").write_text(CONFIG)
            code, report = run_installer(["--hermes-home", str(root)], tmp)
            self.assertEqual(report["cli"]["agent_shell_commands"], [str(root / "bin" / "jev")])
            self.assertFalse((Path(tmp) / "bin").exists())

    def test_a_relative_hermes_home_inside_a_lane_still_finds_the_fleet(self):
        """`--hermes-home .` has no parent called profiles until it is made absolute, so it
        linked that one lane and called it the root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            here = os.getcwd()
            os.chdir(root / "profiles" / "alpha")
            try:
                for spelling in (".", "../alpha"):
                    code, report = run_installer(["--check", "--hermes-home", spelling], tmp)
                    self.assertEqual(len(report["cli"]["agent_shell_commands"]), 3, spelling)
            finally:
                os.chdir(here)

    def test_uninstall_from_a_lane_that_has_been_deleted_still_clears_the_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fleet(tmp)
            lane = root / "profiles" / "alpha"
            run_installer([], tmp, hermes_home=lane)
            shutil.rmtree(lane)
            code, report = run_installer(["--uninstall"], tmp, hermes_home=lane)
            self.assertEqual(code, 0)
            self.assertFalse((root / "bin" / "jev").is_symlink())
            self.assertFalse((root / "profiles" / "beta" / "bin" / "jev").is_symlink())

    def test_an_install_run_from_one_lane_links_every_lane_and_any_shell_can_undo_it(self):
        """An agent runs the installer with HERMES_HOME set to its own profile. Linking only
        that home would leave the other lanes without `jev`, and an --uninstall from the
        person's shell, which sees the root, would look in a different set of places."""
        for undo_from in (None, "beta"):
            with self.subTest(undo_from=undo_from), tempfile.TemporaryDirectory() as tmp:
                root = self._fleet(tmp)
                code, report = run_installer([], tmp, hermes_home=root / "profiles" / "alpha")
                self.assertEqual(code, 0)
                self.assertEqual(report["cli"]["agent_shell_commands"], [str(s) for s in self._shims(root)])
                for shim in self._shims(root):
                    self.assertEqual(shim.resolve(), install.JEV)
                lane = root / "profiles" / undo_from if undo_from else None
                code, report = run_installer(["--uninstall"], tmp, hermes_home=lane)
                self.assertEqual(code, 0)
                for shim in self._shims(root):
                    self.assertFalse(shim.is_symlink(), shim)
                self.assertNotIn("left_alone", report["cli"])


class ReportWarningTests(unittest.TestCase):
    def test_path_warning_is_top_level_when_local_bin_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_installer(["--check", "--skills-dir", f"{tmp}/agent"], tmp)
            self.assertEqual(code, 0)
            self.assertFalse(report["cli"]["on_path"])
            warning = report["warning"]
            self.assertIn(str(Path(tmp) / ".local" / "bin"), warning)      # add this to PATH
            self.assertIn(str(install.REPO / "bin" / "jev"), warning)      # or call this instead
            self.assertIn("setup-key", warning)
            self.assertNotIn("No agent was found", warning)                # a skills folder was given

    def test_no_path_warning_when_local_bin_is_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            on_path = f"{tmp}/.local/bin:/usr/bin:/bin"
            code, report = run_installer(["--check", "--skills-dir", f"{tmp}/agent"], tmp, path=on_path)
            self.assertEqual(code, 0)
            self.assertTrue(report["cli"]["on_path"])
            self.assertNotIn("warning", report)

    def test_path_warning_survives_a_hermes_home(self):
        """The warning went quiet whenever a Hermes home had its own `jev` link. That link
        is for the agent's shell; `jev setup-key` is run by the person, in a terminal that
        never has <HERMES_HOME>/bin on PATH, and zsh on macOS does not add ~/.local/bin."""
        for argv in (["--check"], []):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp:
                hermes = Path(tmp) / ".hermes"
                hermes.mkdir()
                (hermes / "config.yaml").write_text(CONFIG)
                code, report = run_installer(argv, tmp)
                self.assertEqual(code, 0)
                self.assertFalse(report["cli"]["on_path"])
                warning = report["warning"]
                self.assertIn(str(Path(tmp) / ".local" / "bin"), warning)
                self.assertIn("setup-key", warning)
                self.assertIn(str(hermes / "bin" / "jev"), warning)        # agents are covered, and it says where

    def test_no_warning_at_all_when_a_hermes_install_has_local_bin_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp) / ".hermes"
            hermes.mkdir()
            (hermes / "config.yaml").write_text(CONFIG)
            code, report = run_installer([], tmp, path=f"{tmp}/.local/bin:/usr/bin:/bin")
            self.assertEqual(code, 0)
            self.assertNotIn("warning", report)

    def test_a_machine_with_no_agent_warns_instead_of_reporting_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report = run_installer(["--check"], tmp)
            self.assertEqual(code, 0)
            self.assertEqual(report["skill_folders"], [])
            self.assertNotIn("hermes", report)
            self.assertIn("--skills-dir", report["warning"])
            self.assertFalse([step for step in report["next"] if "Hermes" in step])

    def test_hermes_next_step_survives_when_a_hermes_home_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp) / ".hermes"
            hermes.mkdir()
            (hermes / "config.yaml").write_text(CONFIG)
            code, report = run_installer(["--check"], tmp)
            self.assertEqual(code, 0)
            self.assertIn("hermes", report)
            self.assertTrue([step for step in report["next"] if "Hermes" in step])
            self.assertNotIn("--skills-dir", report.get("warning", ""))


if __name__ == "__main__":
    unittest.main()
