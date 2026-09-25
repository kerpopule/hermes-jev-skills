"""The GUI runner shipped with no tests and three ways to report success falsely.

Every test here is a bug that was live in a released version, not a hypothetical.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

_CACHE_HOME = tempfile.TemporaryDirectory()
_ENV = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": _CACHE_HOME.name})


def setUpModule():
    """A --plan run that fails a step now forgets its plan, which writes to the plan cache.
    No test here may do that to the cache of whoever runs the suite, or behave differently
    because their shell exports JEV_MEMO."""
    _ENV.start()
    os.environ.pop("JEV_MEMO", None)


def tearDownModule():
    _ENV.stop()
    _CACHE_HOME.cleanup()

_spec = importlib.util.spec_from_file_location(
    "jev_gui_agent", REPO / "skills" / "jev-computer-use" / "scripts" / "jev_gui_agent.py")
gui = importlib.util.module_from_spec(_spec)
sys.modules["jev_gui_agent"] = gui
_spec.loader.exec_module(gui)


class VerifyTests(unittest.TestCase):
    """`verify` decides the exit code, so a lenient one turns every run into a pass."""

    def test_a_matching_element_label_is_not_proof_the_page_is_open(self):
        """The skill's own example: --expect 'Library' on YouTube Music, whose left nav
        carries a Library link on every page. It passed without clicking anything."""
        rows = [{"label": "Library", "role": "AXLink"}, {"label": "Home", "role": "AXLink"}]
        self.assertFalse(gui.verify(rows, "Home - YouTube Music", "Library"))

    def test_the_window_title_is_proof(self):
        self.assertTrue(gui.verify([], "Library - YouTube Music", "Library"))

    def test_no_expectation_is_unverified_not_verified(self):
        """--expect defaults to "", so this made every run without it report PASS."""
        self.assertFalse(gui.verify([{"label": "anything"}], "Any Window", ""))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(gui.verify([], "LIBRARY - Music", "library"))


class CandidateBudgetTests(unittest.TestCase):
    """Above the cap the contract rejects the table on every step, at step 1, forever."""

    def _rows(self, n):
        return [{"label": f"item {i}", "role": "AXButton", "token": f"tok{i}"} for i in range(n)]

    def test_the_default_budget_lands_exactly_on_the_contract_limit(self):
        _, candidates = gui.build_table(self._rows(gui.MAX_REGIONS))
        self.assertEqual(len(candidates), gui.MAX_CANDIDATES)

    def test_the_budget_is_derived_not_hardcoded(self):
        self.assertEqual(gui.MAX_REGIONS, gui.MAX_CANDIDATES - len(gui.STANDARD_ACTIONS))

    def test_every_standard_action_is_offered(self):
        _, candidates = gui.build_table(self._rows(3))
        ids = {c["id"] for c in candidates}
        for name, _ in gui.STANDARD_ACTIONS:
            self.assertIn(name, ids)
        # Without these two the chooser has no safe way out of a screen it cannot read.
        self.assertIn("reobserve", ids)
        self.assertIn("abstain", ids)

class LoopProgressTests(unittest.TestCase):
    """Repeated ineffective actions must not burn the entire model-call budget."""

    def _state(self, token="fresh"):
        return {"window_title": "Home", "window_bounds": {"x": 0, "y": 0, "width": 400, "height": 400},
                "elements": [{"role": "AXButton", "label": "Library", "element_token": token,
                              "element_index": 1, "frame": {"x": 20, "y": 20, "w": 70, "h": 30}}]}

    def test_unchanged_screen_after_repeated_click_stops_before_third_choice(self):
        driver = mock.Mock()
        driver.tool.return_value = {"result": {"structuredContent": {"effect": "delivered"}}}
        states = [self._state("token-a"), self._state("token-b"), self._state("token-c")]
        with mock.patch.object(gui, "observe", side_effect=states) as obs, \
                mock.patch.object(gui, "jev_choose", return_value={"selected_id": "click:library", "confidence": .91}) as choose:
            out = gui.run_goal(driver, 1, 2, "", "Open Library", expect="Library", values=[],
                               regions_cap=26, budget=10)
        self.assertEqual(out["ended"], "stalled_action")
        self.assertEqual(choose.call_count, 2)
        self.assertEqual(obs.call_count, 3)
        self.assertEqual(driver.tool.call_count, 2)

    def test_changed_screen_does_not_trigger_the_guard(self):
        driver = mock.Mock()
        driver.tool.return_value = {"result": {"structuredContent": {"effect": "delivered"}}}
        states = [self._state(), {**self._state(), "window_title": "Loading"},
                  {**self._state(), "window_title": "Library"}]
        with mock.patch.object(gui, "observe", side_effect=states), \
                mock.patch.object(gui, "jev_choose", return_value={"selected_id": "click:library", "confidence": .91}) as choose:
            out = gui.run_goal(driver, 1, 2, "", "Open Library", expect="Library", values=[],
                               regions_cap=26, budget=10)
        self.assertEqual(out["ended"], "verified")
        self.assertEqual(choose.call_count, 2)

    def test_unknown_id_does_not_dispatch_or_claim_a_planned_click(self):
        driver = mock.Mock()
        with mock.patch.object(gui, "observe", return_value=self._state()), \
                mock.patch.object(gui, "jev_choose", return_value={"selected_id": "click:missing", "confidence": .99}):
            out = gui.run_goal(driver, 1, 2, "", "Open Library", expect="Library", values=[],
                               regions_cap=26, budget=1, until_op="click")
        self.assertEqual(out["ended"], "invalid_choice")
        driver.tool.assert_not_called()

    def test_digest_ignores_rotating_tokens_but_detects_a_changed_field(self):
        a = self._state("ephemeral-a")
        b = self._state("ephemeral-b")
        self.assertEqual(gui.screen_digest(a), gui.screen_digest(b))
        b["elements"][0]["value"] = "new typed value"
        self.assertNotEqual(gui.screen_digest(a), gui.screen_digest(b))

    def test_no_private_field_value_goes_to_jev_or_diagnostics(self):
        driver = mock.Mock()
        driver.tool.return_value = {"result": {"structuredContent": {"effect": "delivered"}}}
        state = self._state()
        state["elements"].append({"role": "AXTextField", "label": "Password", "value": "PRIVATE_SENTINEL",
                                  "element_index": 2, "frame": {"x": 20, "y": 60, "w": 70, "h": 30}})
        import io
        from contextlib import redirect_stdout
        capture = io.StringIO()
        with mock.patch.object(gui, "observe", return_value=state), \
                mock.patch.object(gui, "jev_choose", return_value={"selected_id": "abstain", "confidence": 1}) as choose, \
                redirect_stdout(capture):
            out = gui.run_goal(driver, 1, 2, "", "Open Library", expect="Library", values=[],
                               regions_cap=26, budget=1)
        self.assertEqual(out["ended"], "abstain")
        self.assertNotIn("PRIVATE_SENTINEL", str(choose.call_args))
        self.assertNotIn("PRIVATE_SENTINEL", capture.getvalue())


class PortabilityTests(unittest.TestCase):
    def test_no_hardcoded_home_or_account_in_the_runner(self):
        """check_release blocks /Users/... paths but not a bare account name, and one
        was shipped: the Keychain lookup hardcoded `-a vibex`."""
        import ast
        path = REPO / "skills" / "jev-computer-use" / "scripts" / "jev_gui_agent.py"
        tree = ast.parse(path.read_text())
        # Prose may name the trap it exists to prevent; a string the code USES may not.
        # Docstrings are documentation, so they are excluded via the AST rather than by
        # guessing at comment syntax.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        offenders = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value not in docstrings
            and ("vibex" in node.value or node.value.startswith("/Users/"))
        ]
        self.assertEqual(offenders, [], "machine-specific value in executable code")

    def test_the_driver_is_resolved_not_assumed(self):
        self.assertTrue(callable(gui._find_driver))


class LinuxTreeTests(unittest.TestCase):
    """cua-driver on X11 reports AT-SPI roles in bare lower case - "push button", "entry",
    "check box" - and carries no AXPress action. INTERACTIVE_ROLES held only the macOS
    spellings, so every control on the window was filtered out: a tree with 35 labelled
    controls came back as "no interactive elements observed" and the loop could not move.
    """

    def _state(self, role: str, label: str = "Appearance") -> dict:
        return {"window_bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
                "elements": [{"role": role, "label": label, "element_index": 1,
                              "frame": {"x": 10, "y": 10, "w": 120, "h": 28}}]}

    def test_a_lower_case_at_spi_role_is_offered_to_the_chooser(self):
        for role in ("push button", "entry", "check box", "page tab", "menu item", "combo box"):
            with self.subTest(role=role):
                rows = gui.element_rows(self._state(role), gui.MAX_REGIONS)
                self.assertEqual([r["label"] for r in rows], ["Appearance"])

    def test_a_lower_case_role_that_is_not_a_control_is_still_dropped(self):
        """Widening the role set must not widen it to everything: a click on a label or a
        panel is a candidate spent on nothing, and the contract holds 32 of them."""
        for role in ("label", "panel", "filler", "separator", "frame"):
            with self.subTest(role=role):
                self.assertEqual(gui.element_rows(self._state(role), gui.MAX_REGIONS), [])


class LinuxTextEntryTests(unittest.TestCase):
    """A GTK entry on X11 comes back from cua-driver as role "text". It was offered to Jev as
    "Click", so a typing goal clicked the field and stalled with nothing typed. Found on a
    real Linux desktop (Debian 13, Xvfb + Openbox, cua-driver 0.28.2), not in a mock."""

    ROW = {"label": "Search the library", "role": "text", "token": "s00000001:5",
           "x": 10.0, "y": 10.0, "w": 300.0, "h": 30.0}

    def test_a_linux_text_field_is_offered_as_a_typing_target(self):
        for role in ("text", "entry", "text box", "search box"):
            with self.subTest(role=role):
                _, table = gui.build_table([dict(self.ROW, role=role)])
                self.assertTrue(table[0]["id"].startswith("type:"), table[0])
                self.assertTrue(table[0]["description"].startswith("Type into"))

    def test_a_linux_button_is_still_a_click(self):
        _, table = gui.build_table([dict(self.ROW, role="push button", label="Library")])
        self.assertTrue(table[0]["id"].startswith("click:"))

    def test_a_password_field_is_never_made_a_typing_target(self):
        self.assertNotIn("password text", gui.TEXT_INPUT_ROLES)

    def test_the_chosen_linux_field_receives_the_allowed_value(self):
        rows = [dict(self.ROW)]
        gui.build_table(rows)

        class Driver:
            calls = []

            def tool(self, name, args, timeout=90.0):
                self.calls.append((name, args))
                return {"result": {"content": [{"type": "text", "text": "ok"}]}}

        driver = Driver()
        op, _ = gui.execute(driver, 7, 8, "", rows[0]["cid"], rows, "Type jazz into the search box", ["jazz"])
        self.assertEqual(op, "type")
        name, args = driver.calls[-1]
        self.assertEqual((name, args["text"], args["element_token"]), ("type_text", "jazz", "s00000001:5"))


class DriverReplyShapeTests(unittest.TestCase):
    """cua-driver 0.23.x answers ``get_window_state`` with ``content[0].text`` - the state as a
    JSON string - and no ``structuredContent``. Reading only the structured side returned ``{}``
    for every observation, so the runner reported an empty window on a window it could see.
    """

    def _driver(self, reply: dict):
        class Driver:
            def tool(self, name, args):
                return reply

        return Driver()

    def test_the_text_reply_is_parsed_when_there_is_no_structured_content(self):
        state = {"window_bounds": {"x": 0, "y": 0, "width": 100, "height": 100}, "elements": []}
        driver = self._driver({"result": {"content": [{"type": "text", "text": json.dumps(state)}]}})
        self.assertEqual(gui.observe(driver, 1, 2, ""), state)

    def test_structured_content_still_wins_and_a_text_part_that_is_not_json_is_not_an_answer(self):
        both = {"result": {"structuredContent": {"elements": [{"role": "push button"}]},
                           "content": [{"type": "text", "text": "{\"elements\": [\"from-text\"]}"}]}}
        self.assertEqual(gui.observe(self._driver(both), 1, 2, ""), {"elements": [{"role": "push button"}]})
        junk = {"result": {"content": [{"type": "text", "text": "not json at all"}]}}
        self.assertEqual(gui.observe(self._driver(junk), 1, 2, ""), {})


if __name__ == "__main__":
    unittest.main()


class SidebarRowTests(unittest.TestCase):
    """macOS sidebars are AXOutline -> AXRow -> AXStaticText. The row is clickable and
    unlabelled; the label sits on a non-interactive child. Filtering on role alone dropped
    every sidebar item, so on System Settings "Displays" was in the tree and never offered
    - Jev answered at 0.35 confidence because the right answer was not on the table.
    With the pairing it answered at 0.90 and the click verified.
    """

    # The driver's real shape: a flat list carrying element_index / parent_index.
    STATE = {"elements": [
        {"element_index": 1, "parent_index": 0, "role": "AXOutline"},
        {"element_index": 2, "parent_index": 1, "role": "AXRow", "selected": True,
         "frame": {"x": 949, "y": 880, "w": 215, "h": 32}, "actions": ["AXShowDefaultUI"]},
        {"element_index": 3, "parent_index": 2, "role": "AXCell"},
        {"element_index": 4, "parent_index": 3, "role": "AXStaticText", "label": "Displays",
         "element_token": "t1", "frame": {"x": 963, "y": 884, "w": 78, "h": 24}},
        {"element_index": 5, "parent_index": 0, "role": "AXStaticText", "label": "Just a caption",
         "element_token": "t2", "frame": {"x": 100, "y": 100, "w": 120, "h": 20}},
        # below the fold: in the tree, no frame
        {"element_index": 6, "parent_index": 1, "role": "AXRow"},
        {"element_index": 7, "parent_index": 6, "role": "AXStaticText", "label": "Sound",
         "element_token": "t3"},
    ]}

    def test_a_label_inside_a_row_is_offered_as_that_row(self):
        rows = gui.element_rows(self.STATE, 26, [])
        offered = {r["label"]: r for r in rows}
        self.assertIn("Displays", offered)
        self.assertEqual(offered["Displays"]["role"], "AXRow")
        self.assertEqual(offered["Displays"]["token"], "t1")

    def test_loose_static_text_is_still_not_clickable(self):
        """Otherwise every caption on screen becomes a candidate and floods the table."""
        rows = gui.element_rows(self.STATE, 26, [])
        self.assertNotIn("Just a caption", {r["label"] for r in rows})


class InstalledLocationTests(unittest.TestCase):
    def test_jevkit_is_found_from_an_installed_skill_directory(self):
        """Walking parent directories only works inside the checkout. Installed under
        ~/.hermes/skills/ - the only place an agent runs it - every call failed with
        "jevkit not importable". It passed every test because every test ran from the
        checkout, which is the whole lesson."""
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            (home / "plugins" / "hermes-jev" / "jevkit").mkdir(parents=True)
            (home / "plugins" / "hermes-jev" / "jevkit" / "choose.py").write_text("")
            profile = home / "profiles" / "donna"
            profile.mkdir(parents=True)
            saved = os.environ.get("HERMES_HOME")
            try:
                os.environ["HERMES_HOME"] = str(profile)     # a PROFILE dir, as in production
                found = gui._repo_root()
            finally:
                if saved is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = saved
        self.assertIsNotNone(found)



class SidebarSelectionTests(unittest.TestCase):
    STATE = SidebarRowTests.STATE

    def test_selection_travels_from_the_row_to_its_label(self):
        """The ROW is selected; the LABEL is what we offer. Proof of arrival needs both."""
        offered = {r["label"]: r for r in gui.element_rows(self.STATE, 26, [])}
        self.assertTrue(offered["Displays"]["selected"])

    def test_a_row_below_the_fold_is_not_offered_but_is_reported(self):
        """It has no frame so it cannot be clicked - but Jev must be told it exists, or
        "open Sound" scores 0.33 and stalls when the right move is simply to scroll."""
        self.assertNotIn("Sound", {r["label"] for r in gui.element_rows(self.STATE, 26, ["sound"])})
        self.assertEqual(gui.offscreen_matches(self.STATE, ["sound"]), ["Sound"])

    def test_the_scroll_candidate_names_what_is_below(self):
        _, candidates = gui.build_table([], ["Sound"])
        scroll = next(c for c in candidates if c["id"] == "scroll-down")
        self.assertIn("Sound", scroll["description"])


class VerifyProofTests(unittest.TestCase):
    def test_a_non_breaking_hyphen_does_not_defeat_verification(self):
        """macOS titles the pane "Wi\u2011Fi". --expect Wi-Fi never matched, so a click that
        landed first time at 0.96 was called unverified and repeated five more times."""
        self.assertTrue(gui.verify([], "Wi\u2011Fi", "Wi-Fi"))

    def test_a_selected_row_proves_arrival_when_the_window_has_no_title(self):
        self.assertTrue(gui.verify([{"label": "General", "selected": True}], "", "General"))

    def test_an_unselected_row_is_still_not_proof(self):
        """The original false pass, which the selected-row rule must not reintroduce."""
        self.assertFalse(gui.verify([{"label": "Library", "selected": False}], "Home", "Library"))


class MemoryTests(unittest.TestCase):
    """A loop with no memory cannot pursue an end goal, only a single step.

    Candidate ids were `click:<element_token>` and the driver reissues every token on every
    observation, so nothing in `history` was ever still on the table. Given "open General,
    then open Storage" it clicked General TEN times running - each click confirmed, none
    of them progress - and failed in 22 s. With ids that survive re-observation it took two
    steps and 7.8 s, the second at 0.98 confidence.
    """

    def _rows(self, token_suffix, selected=False):
        return [{"label": "General", "role": "AXRow", "token": f"s{token_suffix}:1", "selected": selected},
                {"label": "Storage", "role": "AXRow", "token": f"s{token_suffix}:2"}]

    def test_an_element_keeps_its_id_when_the_driver_reissues_its_token(self):
        _, first = gui.build_table(self._rows("0001"))
        _, second = gui.build_table(self._rows("0002"))          # new snapshot, new tokens
        ids = lambda table: [c["id"] for c in table if c["id"].startswith("click:")]
        self.assertEqual(ids(first), ids(second))
        self.assertNotIn("s0001", " ".join(ids(first)))          # no snapshot handle in an id

    def test_ids_satisfy_the_chooser_contract(self):
        import re
        rows = [{"label": "AirDrop & Continuity / (beta) #1", "role": "AXRow", "token": "t"}]
        _, table = gui.build_table(rows)
        self.assertRegex(table[0]["id"], r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

    def test_duplicate_labels_still_get_distinct_ids(self):
        rows = [{"label": "Search", "role": "AXButton", "token": "a"},
                {"label": "Search", "role": "AXButton", "token": "b"}]
        _, table = gui.build_table(rows)
        clicks = [c["id"] for c in table if c["id"].startswith("click:")]
        self.assertEqual(len(clicks), len(set(clicks)))

    def test_jev_is_told_which_item_is_already_selected(self):
        """Otherwise re-clicking the open pane looks like a perfectly good next move."""
        _, table = gui.build_table(self._rows("0001", selected=True))
        general = next(c for c in table if "General" in c["description"])
        storage = next(c for c in table if "Storage" in c["description"])
        self.assertIn("currently selected", general["description"])
        self.assertNotIn("currently selected", storage["description"])


# ---------------------------------------------------------------- --plan
#
# Everything below runs against a fake driver, a fake `open` and a fake Jev. No window is
# touched, no process is launched except where the test is about launching one, and no
# secret store is read: the keys main() looks for are put in the environment first.

import contextlib
import io
import json
import os
import shutil
from unittest import mock

FAKE_KEYS = {"TYPESAFE_API_KEY": "t" * 40, "OPENROUTER_API_KEY": "o" * 40}

WINDOWS = [
    # The driver's own cursor overlay: always on top, never the window anyone means.
    {"window_id": 1, "pid": 10, "app_name": "Cua Driver", "title": "", "z_index": 99,
     "is_on_screen": True, "on_current_space": True},
    {"window_id": 22, "pid": 20, "app_name": "Safari", "title": "Start Page", "z_index": 8,
     "is_on_screen": True, "on_current_space": True},
    {"window_id": 33, "pid": 30, "app_name": "System Settings", "title": "General", "z_index": 5,
     "is_on_screen": True, "on_current_space": True},
]

STATE = {"window_title": "General", "elements": [
    {"element_index": 1, "role": "AXButton", "label": "Storage", "element_token": "tok-storage",
     "frame": {"x": 10, "y": 10, "w": 90, "h": 24}},
    {"element_index": 2, "role": "AXSearchField", "label": "Search", "element_token": "tok-search",
     "frame": {"x": 10, "y": 50, "w": 200, "h": 24}},
]}


APPS = [{"name": "Safari", "bundle_id": "com.apple.Safari", "pid": 20, "running": True},
        {"name": "System Settings", "bundle_id": "com.apple.systempreferences", "pid": 30, "running": True},
        {"name": "Numbers", "bundle_id": "com.apple.iWork.Numbers", "pid": 0, "running": False}]


class FakeDriver:
    def __init__(self, failing=None, state=None, windows=None, windows_after=None):
        self.calls = []
        self.failing = failing or {}
        self.state = STATE if state is None else state
        self.windows = WINDOWS if windows is None else windows
        self.windows_after = windows_after      # what list_windows says from the second call on

    def tool(self, name, args, timeout=90.0):
        self.calls.append((name, dict(args)))
        if name in self.failing:
            return {"result": {"isError": True, "content": [{"type": "text", "text": self.failing[name]}]}}
        if name == "list_windows":
            later = self.windows_after is not None and len(self.named("list_windows")) > 1
            return {"result": {"structuredContent": {"windows": self.windows_after if later else self.windows}}}
        if name == "list_apps":
            return {"result": {"structuredContent": {"apps": APPS}}}
        if name == "get_window_state":
            return {"result": {"structuredContent": self.state}}
        return {"result": {"structuredContent": {"effect": "confirmed"}}}

    def named(self, name):
        return [args for called, args in self.calls if called == name]

    def stop(self):
        self.calls.append(("stop", {}))


class FakeOpen:
    def __init__(self, returncode=0):
        self.commands = []
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        return mock.Mock(returncode=self.returncode, stderr="Unable to find application" if self.returncode else "")


def pick(prefix):
    """A Jev that always chooses the first offered action of one kind."""
    seen = []

    def choose(request):
        seen.append(request)
        chosen = next(c["id"] for c in request["candidates"] if c["id"].startswith(prefix))
        return {"selected_id": chosen, "confidence": 0.93, "reason": "chosen"}

    choose.seen = seen
    return choose


def planner(steps, status="planned", reason=""):
    def plan(command, **context):
        plan.context = dict(context, command=command)
        return {"status": status, "reason": reason, "steps": steps, "dropped": [],
                "latency_ms": 812, "model": "test/model"}
    return plan


def plan_args(goal, **over):
    import argparse
    return argparse.Namespace(**dict({"goal": goal, "session": "", "expect": "", "max_steps": 10}, **over))


def run_plan(goal, steps, driver=None, chooser=None, opener=None, **over):
    driver = driver or FakeDriver()
    opener = opener or FakeOpen()
    where = {"pid": over.pop("pid", None), "window_id": over.pop("window_id", None)}
    with mock.patch.object(gui, "jev_choose", chooser or pick("click:")), \
            mock.patch.object(gui.sys, "platform", "darwin"), \
            mock.patch.object(gui, "default_browser", lambda: "com.apple.safari"), \
            contextlib.redirect_stdout(io.StringIO()):
        out = gui.run_plan(driver, where, plan_args(goal, **over), [], gui.MAX_REGIONS,
                           planner=steps if callable(steps) else planner(steps),
                           opener=opener, sleep=lambda s: None)
    return out, driver, opener, where


@unittest.skipIf(gui.jev_plan is None, "jevkit.plan is not importable here")
class DirectOperationTests(unittest.TestCase):
    """The direct ops are where a string a model wrote is handed to the operating system."""

    def direct(self, step, driver=None, where=None, browser="com.apple.safari"):
        driver, opener = driver or FakeDriver(), FakeOpen()
        where = {"pid": None, "window_id": None} if where is None else where
        with mock.patch.object(gui.sys, "platform", "darwin"):
            ok, detail = gui.run_direct(driver, where, "", step, opener=opener, sleep=lambda s: None,
                                        browser=browser)
        return ok, detail, opener, driver, where

    def test_an_address_that_is_not_http_or_https_never_reaches_open(self):
        """`open` launches whatever a scheme is registered to: file: opens local files,
        tel: dials, and any installed app can claim its own."""
        for hostile in ("file:///etc/hosts", "javascript:alert(1)", "tel:+15555550100",
                        "x-apple.systempreferences:com.apple.preference.security",
                        "https://example.com -a Terminal", "https://apple.com@evil.example/"):
            with self.subTest(target=hostile):
                ok, detail, opener, _, _ = self.direct({"kind": "open_url", "target": hostile})
                self.assertFalse(ok)
                self.assertIn("refused", detail)
                self.assertEqual(opener.commands, [])

    def test_a_web_address_is_opened_as_the_only_argument(self):
        ok, _, opener, _, _ = self.direct({"kind": "open_url", "target": "https://example.com/a?b=c"})
        self.assertTrue(ok)
        self.assertEqual(opener.commands, [[gui.OPEN, "https://example.com/a?b=c"]])

    def test_the_next_step_is_aimed_at_the_browser_not_at_whatever_is_in_front(self):
        """Recorded live. `open` loaded the page in a browser window BEHIND System Settings,
        because a background process is not always allowed to take focus. "The front window
        afterwards" was System Settings, and the following click would have landed there."""
        behind = [dict(w, z_index={"Safari": 3, "System Settings": 9}.get(w["app_name"], w["z_index"]))
                  for w in WINDOWS]
        _, _, _, _, where = self.direct({"kind": "open_url", "target": "https://example.com"},
                                        driver=FakeDriver(windows=behind))
        self.assertEqual(where, {"pid": 20, "window_id": 22})

    def test_with_no_known_browser_only_a_window_that_changed_is_accepted(self):
        loaded = [dict(w, title="Example Domain") if w["app_name"] == "Safari" else w for w in WINDOWS]
        _, _, _, _, where = self.direct({"kind": "open_url", "target": "https://example.com"},
                                        driver=FakeDriver(windows_after=loaded), browser="")
        self.assertEqual(where, {"pid": 20, "window_id": 22})
        ok, detail, _, _, where = self.direct({"kind": "open_url", "target": "https://example.com"}, browser="")
        self.assertFalse(ok)                      # nothing moved: no evidence of where it opened
        self.assertEqual(where, {"pid": None, "window_id": None})

    def test_an_app_is_opened_by_name_never_by_path_or_flag(self):
        for hostile in ("/tmp/Evil.app", "../Evil", "-n", "--args"):
            with self.subTest(target=hostile):
                ok, _, opener, _, _ = self.direct({"kind": "open_app", "target": hostile})
                self.assertFalse(ok)
                self.assertEqual(opener.commands, [])

    def test_opening_an_app_aims_the_next_step_at_that_apps_window(self):
        """Without this a command that opens an app has nowhere to click afterwards: the
        pid and window id cannot be known before the app exists."""
        ok, _, opener, _, where = self.direct({"kind": "open_app", "target": "System Settings"})
        self.assertTrue(ok)
        self.assertEqual(opener.commands, [[gui.OPEN, "-a", "System Settings"]])
        self.assertEqual(where, {"pid": 30, "window_id": 33})

    def test_an_app_that_never_shows_a_window_fails_the_step(self):
        """Falling back to "whatever is in front" would aim the following clicks at an
        unrelated app."""
        ok, detail, _, _, where = self.direct({"kind": "open_app", "target": "Numbers"})
        self.assertFalse(ok)
        self.assertIn("no window", detail)
        self.assertEqual(where, {"pid": None, "window_id": None})

    def test_open_failing_is_reported_not_assumed_to_have_worked(self):
        driver, opener = FakeDriver(), FakeOpen(returncode=1)
        with mock.patch.object(gui.sys, "platform", "darwin"):
            ok, detail = gui.run_direct(driver, {"pid": None, "window_id": None}, "",
                                        {"kind": "open_app", "target": "Nonesuch"}, opener=opener)
        self.assertFalse(ok)
        self.assertIn("open failed", detail)

    def test_keys_and_menus_use_the_drivers_real_tool_names_and_arguments(self):
        """Taken from the driver's own tools/list: `press_key` takes `key`, a chord is
        `hotkey` with `keys`, and a menu is `invoke_menu` with `path`. A guessed name is
        refused by the driver as an unclassified tool."""
        where = {"pid": 30, "window_id": 33}
        _, _, _, driver, _ = self.direct({"kind": "press_key", "target": "return"}, where=dict(where))
        self.assertEqual(driver.named("press_key"),
                         [{"pid": 30, "window_id": 33, "key": "return", "delivery_mode": "foreground"}])
        _, _, _, driver, _ = self.direct({"kind": "press_key", "target": "cmd+shift+t"}, where=dict(where))
        self.assertEqual(driver.named("hotkey"), [{"pid": 30, "window_id": 33, "keys": ["cmd", "shift", "t"],
                                                   "delivery_mode": "foreground"}])
        _, _, _, driver, _ = self.direct({"kind": "menu", "target": "File > New Window"}, where=dict(where))
        self.assertEqual(driver.named("invoke_menu"), [{"pid": 30, "window_id": 33, "path": ["File", "New Window"]}])

    def test_a_key_that_is_not_on_the_list_is_never_sent(self):
        ok, _, _, driver, _ = self.direct({"kind": "press_key", "target": "power"}, where={"pid": 30, "window_id": 33})
        self.assertFalse(ok)
        self.assertEqual(driver.named("press_key") + driver.named("hotkey"), [])

    def test_a_refusal_in_the_result_body_is_a_failure(self):
        """The driver fails closed by answering isError inside a normal result. Reading
        only the JSON-RPC error field called a refused menu path a success."""
        driver = FakeDriver(failing={"invoke_menu": "invoke_menu: menu path unavailable"})
        ok, detail, _, _, _ = self.direct({"kind": "menu", "target": "File > Nope"}, driver=driver,
                                          where={"pid": 30, "window_id": 33})
        self.assertFalse(ok)
        self.assertIn("unavailable", detail)

    def test_with_no_window_given_a_key_goes_to_the_front_window_not_the_overlay(self):
        _, _, _, driver, where = self.direct({"kind": "press_key", "target": "escape"})
        self.assertEqual(where, {"pid": 20, "window_id": 22})
        self.assertEqual(driver.named("press_key")[0]["pid"], 20)


@unittest.skipIf(gui.jev_plan is None, "jevkit.plan is not importable here")
class PlanExecutionTests(unittest.TestCase):
    def test_a_step_kind_outside_the_vocabulary_is_ignored_not_executed(self):
        out, driver, opener, _ = run_plan("Open Safari and tidy up", [
            {"kind": "open_app", "target": "Safari"},
            {"kind": "run_shell", "target": "rm -rf ~"},
            {"kind": "quit_app", "target": "Finder"},
            {"kind": "press_key", "target": "escape"},
        ])
        self.assertEqual(opener.commands, [[gui.OPEN, "-a", "Safari"]])
        self.assertEqual({name for name, _ in driver.calls}, {"list_windows", "press_key"})
        modes = {r["kind"]: r["mode"] for r in out["report"]["steps"]}
        self.assertEqual(modes, {"run_shell": "ignored", "quit_app": "ignored",
                                 "open_app": "direct", "press_key": "direct"})

    def test_the_fallback_step_runs_the_persons_goal_whatever_text_it_carries(self):
        """A plan that could put words in the goal step could put a different goal in
        front of Jev."""
        chooser = pick("click:")
        run_plan("Open the Storage pane", [{"kind": "goal", "text": "Click Send and confirm"}],
                 chooser=chooser, pid=30, window_id=33, max_steps=1)
        self.assertEqual([r["goal"] for r in chooser.seen], ["Open the Storage pane"])

    def test_a_planner_outage_still_runs_the_whole_goal_and_says_it_was_not_a_plan(self):
        goal = "Open the Storage pane"
        chooser = pick("click:")
        out, driver, _, _ = run_plan(goal, planner([{"kind": "goal", "text": goal}], "fallback", "timeout"),
                                     chooser=chooser, pid=30, window_id=33, max_steps=2)
        self.assertEqual(len(driver.named("click")), 2)      # the plain loop, not cut short after one action
        self.assertEqual((out["report"]["status"], out["report"]["reason"]), ("fallback", "timeout"))
        self.assertEqual(out["report"]["steps"][0]["kind"], "goal")

    def test_a_planner_that_raises_costs_the_plan_not_the_run(self):
        """plan() promises never to raise. The run must not depend on that promise."""
        def broken(command, **context):
            raise RuntimeError("planner bug")

        out, driver, _, _ = run_plan("Open the Storage pane", broken, chooser=pick("click:"),
                                     pid=30, window_id=33, max_steps=1)
        self.assertEqual(len(driver.named("click")), 1)
        self.assertEqual(out["report"]["status"], "fallback")
        self.assertIn("planner_error", out["report"]["reason"])

    def test_a_planned_click_ends_after_the_one_action_it_asked_for(self):
        """The plain loop spends another observation and another Jev call being told
        "done". A planned step is one action by construction, and that second call is the
        latency this flag exists to remove."""
        chooser = pick("click:")
        out, driver, _, _ = run_plan("Open Storage", [{"kind": "click", "target": "Storage"}],
                                     chooser=chooser, pid=30, window_id=33)
        self.assertEqual(len(chooser.seen), 1)
        self.assertEqual(driver.named("click")[0]["element_token"], "tok-storage")
        self.assertEqual(out["report"]["steps"][0]["jev_calls"], 1)
        self.assertEqual(chooser.seen[0]["goal"], "Click Storage.")

    def test_dictated_text_is_typed_as_given_into_the_field_jev_picked(self):
        """No second model call to "choose a value", and no typing at wherever the focus
        happens to be: with no field focused a web app reads letters as shortcuts."""
        with mock.patch.object(gui, "text_helper", side_effect=AssertionError("text model called")):
            _, driver, _, _ = run_plan('Type "solar eclipse" into search',
                                       [{"kind": "type_text", "target": "search field", "text": "solar eclipse"}],
                                       chooser=pick("type:"), pid=30, window_id=33)
        typed = driver.named("type_text")
        self.assertEqual(len(typed), 1)
        self.assertEqual((typed[0]["text"], typed[0]["element_token"]), ("solar eclipse", "tok-search"))

    def test_the_dictated_text_is_not_sent_to_jev(self):
        chooser = pick("type:")
        run_plan('Type "the launch is on the ninth" into search',
                 [{"kind": "type_text", "target": "search field", "text": "the launch is on the ninth"}],
                 chooser=chooser, pid=30, window_id=33)
        self.assertNotIn("ninth", json.dumps(chooser.seen))

    def test_a_failed_step_ends_the_plan_before_anything_is_typed(self):
        """The later steps assumed this one happened. Typing into a window that did not
        open is how text ends up somewhere it was never meant to go."""
        driver = FakeDriver(failing={"invoke_menu": "menu path unavailable"})
        out, driver, _, _ = run_plan('Make a new note and type "hello"', [
            {"kind": "menu", "target": "File > New Note"},
            {"kind": "type_text", "target": "note", "text": "hello"},
        ], driver=driver, chooser=pick("type:"), pid=30, window_id=33)
        self.assertEqual(driver.named("type_text"), [])
        self.assertEqual([(r["mode"], r["ok"]) for r in out["report"]["steps"]],
                         [("direct", False), ("not_run", False)])

    def test_max_steps_bounds_the_whole_plan_not_each_step(self):
        chooser = pick("click:")
        out, _, _, _ = run_plan("Open General then Storage", [
            {"kind": "click", "target": "General"}, {"kind": "click", "target": "Storage"},
        ], chooser=chooser, pid=30, window_id=33, max_steps=1)
        self.assertEqual(len(chooser.seen), 1)
        self.assertEqual(out["used"], 1)
        self.assertIn("budget", out["report"]["steps"][1]["detail"])

    def test_the_runner_enforces_never_send_itself_whoever_wrote_the_plan(self):
        out, driver, _, _ = run_plan("Open Mail and have a look", [
            {"kind": "open_app", "target": "Safari"}, {"kind": "click", "target": "Send"},
            {"kind": "click", "target": "Confirm"}], chooser=pick("click:"))
        self.assertEqual(driver.named("click"), [])
        self.assertEqual([d["target"] for d in out["report"]["dropped"]], ["Send", "Confirm"])

    def test_the_planner_is_told_what_is_in_front_without_naming_the_overlay(self):
        fake = planner([{"kind": "wait", "amount": 1}])
        run_plan("Wait a second", fake)
        self.assertEqual(fake.context["front_app"], "Safari")
        self.assertEqual(fake.context["running_apps"], ["Safari", "System Settings"])

    def test_jev_abstaining_on_a_step_is_carried_out_to_the_exit_code(self):
        out, _, _, _ = run_plan("Open Storage", [{"kind": "click", "target": "Storage"}],
                                chooser=pick("abstain"), pid=30, window_id=33)
        self.assertTrue(out["abstained"])
        self.assertFalse(out["report"]["steps"][0]["ok"])


class CredentialIsolationTests(unittest.TestCase):
    """The GUI runner must not relabel one provider's key as another's."""

    def test_selected_transport_and_authorization_follow_key_provenance(self):
        from jevkit import client, keystore

        keys = {"typesafe": "synthetic-ts", "openrouter": "synthetic-or",
                "venice": "synthetic-vn", "none": None}
        for selected, key in keys.items():
            with self.subTest(selected=selected):
                env = {name: "" for name in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY",
                                            "VENICE_API_KEY", "TYPESAFE_BASE_URL")}
                if key:
                    env[{"typesafe": "TYPESAFE_API_KEY", "openrouter": "OPENROUTER_API_KEY",
                         "venice": "VENICE_API_KEY"}[selected]] = key
                calls = []

                def fake_transport(destination):
                    def send(body, headers, timeout):
                        calls.append((destination, headers.get("Authorization")))
                        raise client.JevError("malformed", "synthetic offline response")
                    return send

                with mock.patch.dict(os.environ, env), \
                     mock.patch.object(keystore, "_from_keychain", return_value=None), \
                     mock.patch.object(keystore, "_from_file", return_value=None), \
                     mock.patch.object(gui, "Driver", return_value=FakeDriver()), \
                     mock.patch.object(gui, "_find_driver", return_value="synthetic-driver"), \
                     mock.patch.object(client, "_http_transport", fake_transport("api.typesafe.ai")), \
                     mock.patch.object(client, "_openrouter_transport", fake_transport("openrouter.ai")), \
                     mock.patch.object(client, "_venice_transport", fake_transport("api.venice.ai")), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    code = gui.main(["--pid", "30", "--window-id", "33", "--goal", "Open Storage",
                                     "--max-steps", "1"])
                    self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), env["TYPESAFE_API_KEY"])
                if selected == "none":
                    self.assertEqual(code, 2)
                    self.assertIn("no Jev credential", output.getvalue())
                    self.assertEqual(calls, [])
                else:
                    self.assertEqual(calls, [({"typesafe": "api.typesafe.ai",
                                               "openrouter": "openrouter.ai",
                                               "venice": "api.venice.ai"}[selected], f"Bearer {key}")])


class MainTests(unittest.TestCase):
    def main(self, argv, driver=None, plan=None, chooser=None, env=None):
        driver = driver or FakeDriver()
        patches = [mock.patch.dict(os.environ, dict(FAKE_KEYS, **(env or {}))),
                   mock.patch.object(gui, "Driver", lambda binary: driver),
                   mock.patch.object(gui, "jev_choose", chooser or pick("click:")),
                   mock.patch.object(gui.sys, "platform", "darwin"),
                   mock.patch.object(gui, "default_browser", lambda: "com.apple.safari"),
                   mock.patch.object(gui.time, "sleep", lambda s: None),
                   mock.patch.object(gui.subprocess, "run", FakeOpen())]
        if plan is not None and gui.jev_plan is not None:
            patches.append(mock.patch.object(gui.jev_plan, "plan", plan))
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            code = gui.main(argv)
        return code, out.getvalue(), driver

    @unittest.skipIf(gui.jev_plan is None, "jevkit.plan is not importable here")
    def test_the_json_result_reports_plan_latency_and_each_step(self):
        state = dict(STATE, window_title="Storage")
        steps = [{"kind": "open_app", "target": "System Settings"}, {"kind": "press_key", "target": "escape"}]
        code, printed, _ = self.main(["--plan", "--goal", "Open System Settings and press escape",
                                      "--expect", "Storage", "--json"],
                                     driver=FakeDriver(state=state), plan=planner(steps))
        result = json.loads(printed.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(result["plan"]["latency_ms"], 812)
        self.assertEqual(result["plan"]["status"], "planned")
        self.assertEqual([(s["kind"], s["mode"], s["ok"]) for s in result["plan"]["steps"]],
                         [("open_app", "direct", True), ("press_key", "direct", True)])
        for record in result["plan"]["steps"]:
            self.assertIsInstance(record["duration_ms"], int)

    def test_without_the_flag_nothing_is_planned_and_the_result_is_the_old_shape(self):
        called = []
        code, printed, _ = self.main(["--pid", "30", "--window-id", "33", "--goal", "Open Storage",
                                      "--max-steps", "1", "--json"],
                                     plan=lambda *a, **k: called.append(a))
        result = json.loads(printed.strip().splitlines()[-1])
        self.assertEqual(called, [])
        self.assertNotIn("plan", result)
        self.assertEqual(sorted(result), sorted(["schema", "goal", "window_title", "steps", "elapsed_ms", "expected",
                                                 "verified", "abstained", "regions_last", "actions"]))
        self.assertEqual((code, result["steps"]), (4, 1))

    def test_without_the_flag_pid_and_window_id_are_still_required(self):
        with self.assertRaises(SystemExit) as stop, contextlib.redirect_stderr(io.StringIO()):
            gui.main(["--goal", "Open Storage"])
        self.assertEqual(stop.exception.code, 2)

    def test_an_older_jevkit_without_a_planner_costs_the_speed_up_not_the_run(self):
        """The installer vendors jevkit into the Hermes plugin, so a newer script can meet
        an older jevkit. --plan must then run the goal as one loop, not refuse."""
        with mock.patch.object(gui, "jev_plan", None):
            code, printed, driver = self.main(["--plan", "--goal", "Open Storage", "--max-steps", "1", "--json"])
        result = json.loads(printed.strip().splitlines()[-1])
        self.assertEqual(len(driver.named("click")), 1)
        self.assertNotIn("plan", result)
        self.assertEqual(code, 4)


class MissingDriverTests(unittest.TestCase):
    """The docstring promises "exit 2 refused to start". A missing binary was a
    FileNotFoundError traceback and exit 1, which a caller reads as a crash."""

    def refused(self, binary):
        out = io.StringIO()
        with mock.patch.dict(os.environ, dict(FAKE_KEYS, CUA_DRIVER_BIN=binary)), contextlib.redirect_stdout(out):
            code = gui.main(["--pid", "1", "--window-id", "1", "--goal", "Open the Library page"])
        return code, out.getvalue()

    def test_a_missing_driver_is_exit_2_with_one_line_and_no_traceback(self):
        code, printed = self.refused(str(REPO / "no-such-dir" / "cua-driver"))
        self.assertEqual(code, 2)
        self.assertEqual(len(printed.strip().splitlines()), 1)
        self.assertTrue(printed.startswith("FAIL: cannot start"))
        self.assertIn("CUA_DRIVER_BIN", printed)

    @unittest.skipUnless(shutil.which("true"), "needs a binary that exits without speaking MCP")
    def test_a_binary_that_is_not_an_mcp_server_is_also_a_refusal(self):
        """It starts, so Popen is happy, and then every call fails. That used to end as
        "unverified", which blames the screen for a broken install."""
        code, printed = self.refused(shutil.which("true"))
        self.assertEqual(code, 2)
        self.assertIn("did not answer the MCP handshake", printed)


SELECTED_STATE = {"window_title": "", "elements": [
    {"element_index": 1, "parent_index": 0, "role": "AXOutline"},
    {"element_index": 2, "parent_index": 1, "role": "AXRow", "selected": True},
    {"element_index": 3, "parent_index": 2, "role": "AXStaticText", "label": "General",
     "element_token": "tok-general", "frame": {"x": 10, "y": 10, "w": 90, "h": 24}},
    {"element_index": 4, "role": "AXButton", "label": "About", "element_token": "tok-about",
     "frame": {"x": 200, "y": 10, "w": 90, "h": 24}},
]}


def torn(probabilities):
    """Jev below the floor, as choose() reports it: reobserve plus the spread it saw."""
    def choose(request):
        choose.calls += 1
        return {"selected_id": "reobserve", "confidence": max(probabilities.values()),
                "reason": "low confidence", "probabilities": probabilities}
    choose.calls = 0
    return choose


@unittest.skipIf(gui.jev_plan is None, "jevkit.plan is not importable here")
class PlannedStepOutcomeTests(unittest.TestCase):
    def test_a_step_whose_target_is_already_selected_is_finished_not_stalled(self):
        """Recorded live: "go to General" with General already showing. Jev gave 0.66 to
        clicking the selected row and 0.14 to `done`. Both are right, neither clears the
        0.65 floor, and the plan stalled on a screen that was already correct."""
        chooser = torn({"click:general": 0.66, "done": 0.14, "click:about": 0.05})
        out, driver, _, _ = run_plan("Go to General", [{"kind": "click", "target": "General"}],
                                     driver=FakeDriver(state=SELECTED_STATE), chooser=chooser,
                                     pid=30, window_id=33)
        self.assertTrue(out["report"]["steps"][0]["ok"])
        self.assertEqual(chooser.calls, 1)
        self.assertEqual(driver.named("click"), [])      # resolved by NOT acting, never by guessing a click

    def test_low_confidence_about_an_unselected_target_is_still_not_acted_on(self):
        """The floor exists because wrong answers live under it. Only "it is already
        selected" may be resolved below it, and only into doing nothing."""
        chooser = torn({"click:about": 0.60, "done": 0.20})
        out, driver, _, _ = run_plan("Open About", [{"kind": "click", "target": "About"}],
                                     driver=FakeDriver(state=SELECTED_STATE), chooser=chooser,
                                     pid=30, window_id=33)
        self.assertFalse(out["report"]["steps"][0]["ok"])
        self.assertEqual(driver.named("click"), [])

    def test_a_click_the_driver_refused_does_not_complete_the_step(self):
        driver = FakeDriver(failing={"click": "click: element is not pressable"})
        out, _, _, _ = run_plan("Open Storage", [{"kind": "click", "target": "Storage"}],
                                driver=driver, chooser=pick("click:"), pid=30, window_id=33, max_steps=2)
        self.assertFalse(out["report"]["steps"][0]["ok"])

    def test_a_refusal_is_logged_with_its_reason(self):
        """It was read from a field the driver never sets, so a refused click was logged as
        {"effect": "refused", "message": ""} and the one line that said why was lost."""
        refused = {"result": {"isError": True, "content": [{"type": "text", "text": "window_id does not belong to pid"}],
                              "structuredContent": {"status": "refused"}}}
        self.assertEqual(gui._brief(refused), {"effect": "refused", "message": "window_id does not belong to pid"})
        self.assertEqual(gui._brief({"result": {"structuredContent": {"effect": "confirmed"}}}),
                         {"effect": "confirmed", "message": ""})


class TextHelperTests(unittest.TestCase):
    """``--values`` is the whole of what may be typed. The text model only picks from it."""

    def ask(self, values, answer=None, env=None):
        """text_helper with a key present and the network replaced by ``answer``."""
        body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
        opened = mock.Mock(side_effect=lambda request, timeout=None: io.BytesIO(body))
        with mock.patch.dict(os.environ, FAKE_KEYS if env is None else env), \
                mock.patch.object(gui.urllib.request, "urlopen", opened):
            chosen = gui.text_helper("Sign the guest book", "Name", values)
        return chosen, opened

    def test_one_allowed_value_is_typed_without_asking_a_model(self):
        """It asked a model to "choose the best one" from a list of one: a network call
        with a 30 second timeout, mid-step, for an answer that was never in doubt."""
        chosen, opened = self.ask(["Jane Doe"], answer='{"text": "Someone Else"}')
        self.assertEqual(chosen, "Jane Doe")
        opened.assert_not_called()

    def test_a_reply_that_is_not_an_allowed_value_is_never_typed(self):
        """The reply was typed as it came. A model that answered with anything else put
        text the person never allowed into a field on their screen."""
        for reply in ('{"text": "Jane Doe; DROP TABLE guests"}', '{"text": "jane doe"}', '{"text": ""}',
                      '{"text": ["Jane Doe"]}', '["Jane Doe"]', "Jane Doe", "null", "{broken"):
            with self.subTest(reply=reply):
                chosen, opened = self.ask(["Jane Doe", "J. Doe"], answer=reply)
                self.assertEqual(chosen, "Jane Doe")
                opened.assert_called_once()              # the model WAS asked; its answer was refused

    def test_a_reply_that_is_an_allowed_value_is_used(self):
        """The control for the test above: without it, a helper that ignored the model
        altogether would pass."""
        for reply in ('{"text": "J. Doe"}', '```json\n{"text": "J. Doe"}\n```', '{"text": "  J. Doe "}'):
            with self.subTest(reply=reply):
                self.assertEqual(self.ask(["Jane Doe", "J. Doe"], answer=reply)[0], "J. Doe")

    def test_no_key_or_no_values_still_makes_no_call(self):
        chosen, opened = self.ask(["Jane Doe", "J. Doe"], env={"TEXT_MODEL_API_KEY": "", "OPENROUTER_API_KEY": ""})
        self.assertEqual(chosen, "Jane Doe")
        self.assertEqual(self.ask([])[0], "")
        opened.assert_not_called()

    def test_a_network_failure_types_the_first_value_not_a_traceback(self):
        for failure in (OSError("connection reset"), TimeoutError("timed out"), ValueError("unknown url type")):
            with self.subTest(failure=failure), mock.patch.dict(os.environ, FAKE_KEYS), \
                    mock.patch.object(gui.urllib.request, "urlopen", side_effect=failure):
                self.assertEqual(gui.text_helper("Sign the guest book", "Name", ["Jane Doe", "J. Doe"]), "Jane Doe")


@unittest.skipIf(gui.jev_plan is None, "jevkit.plan is not importable here")
@unittest.skipIf(gui.jev_plan is None, "jevkit.plan is not importable here")
class FakeIsReallyUsedTests(unittest.TestCase):
    """The fakes in this file have to be the only thing these tests touch.

    `run_plan(..., opener=subprocess.run)` bound the real function as a DEFAULT ARGUMENT,
    at import, so patching `gui.subprocess.run` afterwards changed nothing. On macOS the
    tests then ran the real `/usr/bin/open -a "System Settings"` and opened that app on
    whoever ran them; on Linux the same call was ENOENT, so three tests failed and CI went
    red for every push. Both went unnoticed because the suite was green on the machine that
    wrote it.
    """

    def steps(self):
        return [{"kind": "open_app", "target": "System Settings"}]

    def test_patching_the_module_is_enough_to_stop_a_step_shelling_out(self):
        """No opener= is passed here, exactly as main() calls it."""
        fake = FakeOpen()
        driver = FakeDriver(state=dict(STATE, window_title="Storage"))
        with mock.patch.object(gui.subprocess, "run", fake), \
                mock.patch.object(gui, "jev_choose", pick("click:")), \
                mock.patch.object(gui.sys, "platform", "darwin"), \
                mock.patch.object(gui.time, "sleep", lambda s: None), \
                mock.patch.object(gui, "default_browser", lambda: "com.apple.safari"), \
                contextlib.redirect_stdout(io.StringIO()):
            out = gui.run_plan(driver, {"pid": None, "window_id": None},
                               plan_args("Open System Settings"), [], gui.MAX_REGIONS,
                               planner=planner(self.steps()))
        self.assertEqual([s["ok"] for s in out["report"]["steps"]], [True],
                         out["report"]["steps"][0].get("detail"))
        self.assertEqual(fake.commands, [[gui.OPEN, "-a", "System Settings"]])

    def test_the_same_holds_for_the_sleep_between_polls(self):
        """`sleep=time.sleep` was bound the same way, so a patched sleep was ignored and a
        failing poll loop waited for real. Here the window is never found, so the loop runs
        to its end: every wait must land in the fake."""
        slept = []
        driver = FakeDriver()                      # no Calculator window will ever appear
        with mock.patch.object(gui.subprocess, "run", FakeOpen()), \
                mock.patch.object(gui, "jev_choose", pick("click:")), \
                mock.patch.object(gui.sys, "platform", "darwin"), \
                mock.patch.object(gui.time, "sleep", lambda s: slept.append(s)), \
                mock.patch.object(gui, "default_browser", lambda: "com.apple.safari"), \
                contextlib.redirect_stdout(io.StringIO()):
            out = gui.run_plan(driver, {"pid": None, "window_id": None},
                               plan_args("Open Calculator"), [], gui.MAX_REGIONS,
                               planner=planner([{"kind": "open_app", "target": "Calculator"}]))
        self.assertFalse(out["report"]["steps"][0]["ok"])
        self.assertGreaterEqual(len(slept), 20)    # the whole 8 s wait, in the fake, instantly


class PlanCacheRunnerTests(unittest.TestCase):
    """A cached plan is served for a week, so a run that went wrong must not leave one behind."""

    GOAL = "Open System Settings and press escape"
    STEPS = [{"kind": "open_app", "target": "System Settings"}, {"kind": "press_key", "target": "escape"}]

    def main(self, expect, driver=None, plan=None, env=None):
        return MainTests.main(self, ["--plan", "--goal", self.GOAL, "--expect", expect, "--json"],
                              driver=driver or FakeDriver(state=dict(STATE, window_title="Storage")),
                              plan=plan or planner(self.STEPS), env=env)

    def test_an_unverified_plan_run_forgets_the_plan(self):
        with mock.patch.object(gui.jev_plan, "forget") as forget:
            code, printed, _ = self.main("Somewhere Else")
        self.assertEqual(code, 4)
        forget.assert_called_once_with(self.GOAL, front_app="Safari", running_apps=["Safari", "System Settings"])
        self.assertIn("plan cache: forgot this plan", printed)

    def test_a_verified_plan_run_keeps_the_plan(self):
        with mock.patch.object(gui.jev_plan, "forget") as forget:
            code, _, _ = self.main("Storage")
        self.assertEqual(code, 0)
        forget.assert_not_called()

    def test_a_failed_step_forgets_the_plan_once_even_though_the_run_is_also_unverified(self):
        driver = FakeDriver(failing={"press_key": "press_key: no such window"})
        with mock.patch.object(gui.jev_plan, "forget") as forget:
            code, _, _ = self.main("Somewhere Else", driver=driver)
        self.assertEqual(code, 4)
        forget.assert_called_once()

    def test_a_failed_step_forgets_the_plan_without_main(self):
        """run_plan has other callers than main(), and they have no exit code to go on."""
        with mock.patch.object(gui.jev_plan, "forget") as forget:
            run_plan(self.GOAL, self.STEPS, driver=FakeDriver(failing={"press_key": "no such window"}))
        forget.assert_called_once_with(self.GOAL, front_app="Safari", running_apps=["Safari", "System Settings"])

    def test_a_plan_run_that_is_interrupted_forgets_the_plan(self):
        """Ctrl-C because the plan is doing the wrong thing is the run that most needs its
        plan forgotten, and the exception used to leave run_plan before anything was."""
        for failure in (KeyboardInterrupt(), RuntimeError("the driver died")):
            with self.subTest(failure=failure), mock.patch.object(gui.jev_plan, "forget") as forget, \
                    mock.patch.object(gui, "run_direct", side_effect=failure):
                with self.assertRaises(type(failure)):
                    run_plan(self.GOAL, self.STEPS)
                forget.assert_called_once_with(self.GOAL, front_app="Safari", running_apps=["Safari", "System Settings"])

    def test_with_the_cache_off_nothing_is_forgotten_and_nothing_says_it_was(self):
        """forget() returns at once when JEV_MEMO=off, but the runner still printed "forgot
        this plan", about a plan that was never kept."""
        def off(command, **context):
            return dict(planner(self.STEPS)(command, **context), cache="off")

        with mock.patch.object(gui.jev_plan, "forget") as forget:
            code, printed, _ = self.main("Somewhere Else", plan=off)
        self.assertEqual(code, 4)
        forget.assert_not_called()
        self.assertNotIn("plan cache: forgot", printed)

    def test_a_fallback_has_nothing_to_forget(self):
        """Only a real plan is ever stored. The whole-goal loop ending unverified says
        nothing about a plan that was never used."""
        with mock.patch.object(gui.jev_plan, "forget") as forget:
            self.main("Somewhere Else", plan=planner([{"kind": "goal", "text": self.GOAL}], "fallback", "timeout"))
        forget.assert_not_called()

    def test_forget_blowing_up_does_not_change_the_exit_code(self):
        with mock.patch.object(gui.jev_plan, "forget", side_effect=RuntimeError("cache bug")):
            code, _, _ = self.main("Somewhere Else")
        self.assertEqual(code, 4)

    def test_an_older_jevkit_whose_planner_has_no_cache_still_runs(self):
        """The installer vendors jevkit, so this script can meet a plan.py with no forget()."""
        older = mock.Mock(spec=["plan", "clean_step", "enforce_never_send", "safe_app_name", "parse_keys"],
                          plan=planner(self.STEPS), clean_step=gui.jev_plan.clean_step,
                          enforce_never_send=gui.jev_plan.enforce_never_send,
                          safe_app_name=gui.jev_plan.safe_app_name, parse_keys=gui.jev_plan.parse_keys)
        with mock.patch.object(gui, "jev_plan", older):
            code, printed, _ = MainTests.main(self, ["--plan", "--goal", self.GOAL, "--expect", "Somewhere Else", "--json"])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(printed.strip().splitlines()[-1])["plan"]["cache"], "miss")

    def test_the_report_and_the_printed_plan_line_say_what_the_cache_did(self):
        def cached(command, **context):
            return dict(planner(self.STEPS)(command, **context), cache="hit", latency_ms=1)

        code, printed, _ = self.main("Storage", plan=cached)
        result = json.loads(printed.strip().splitlines()[-1])
        self.assertEqual(result["plan"]["cache"], "hit")
        self.assertIn("1 ms, cache hit", printed)

    def test_a_cached_plan_that_ends_unverified_is_not_served_again(self):
        """End to end, with the real plan(), the real forget() and a real cache file: the
        one thing a mocked forget() cannot show is that both work out the SAME key."""
        real_plan = gui.jev_plan.plan          # taken now: main() below swaps the attribute for `plan`

        reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"steps": [
            dict(step, text="", amount=0) for step in self.STEPS]})}}]}).encode()
        for expect, calls_expected, caches in (("Somewhere Else", 2, ["miss", "miss"]), ("Storage", 1, ["miss", "hit"])):
            calls = []

            def transport(url, body, headers, timeout):
                calls.append(url)
                return reply

            def plan(command, **context):
                return real_plan(command, transport=transport, **context)

            with self.subTest(expect=expect), tempfile.TemporaryDirectory() as home:
                env = {"XDG_CACHE_HOME": home, "JEV_MEMO": "on", "JEV_PLAN_MODEL": "test/model",
                       "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1", "TEXT_MODEL_API_KEY": ""}
                seen = [json.loads(self.main(expect, plan=plan, env=env)[1].strip().splitlines()[-1])["plan"]["cache"]
                        for _ in range(2)]
                self.assertEqual((len(calls), seen), (calls_expected, caches))
