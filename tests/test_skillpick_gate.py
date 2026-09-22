"""Offline tests for the skill picker: the local gate, the catalog cap, the batch merge
and the folders it searches.

No network and no real secret store. Every Jev reply comes from an injected transport.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevkit import client, keystore, skillpick  # noqa: E402

KEY = "apikey_" + "a1" * 30

# Same on a machine with a real key and on one with none: never consult the real store.
_key_patch = mock.patch.object(keystore, "resolve", return_value=KEY)


def setUpModule():
    _key_patch.start()


def tearDownModule():
    _key_patch.stop()


def catalog_of(count):
    return [{"name": f"skill-{i:03d}", "description": f"Procedure number {i}", "path": f"skills/skill-{i:03d}/SKILL.md"}
            for i in range(count)]


class FakeJev:
    """A transport that plays both stages of a pick.

    `shortlist` maps a catalog index to the stage-1 probability Jev gives that skill, in
    whichever batch offers it; everything unlisted gets nothing and "none" takes the rest.
    `verdicts` maps a stage-2 question name to its yes-probability.
    """

    def __init__(self, shortlist=None, verdicts=None, fail_batch_offering=None, fail_stage_two=False):
        self.shortlist = shortlist or {}
        self.verdicts = verdicts or {}
        self.fail_batch_offering = fail_batch_offering
        self.fail_stage_two = fail_stage_two
        self.stage_one = []                      # the option keys each batch offered
        self.stage_two = []                      # the question names of each verification
        self._lock = threading.Lock()            # batches arrive on several threads at once

    def __call__(self, body, headers, timeout):
        request = json.loads(body)
        questions = request["questions"]
        if "pick" in questions:
            offered = list(questions["pick"]["criteria"])
            with self._lock:
                self.stage_one.append(offered)
            if self.fail_batch_offering in offered:
                raise client.JevError("credits_exhausted")      # not retryable, so the test does not sleep
            scores = {key: float(self.shortlist.get(key, self.shortlist.get(int(key[1:]), 0.0)))
                      for key in offered if key != "none"}
            scores["none"] = max(0.0, 1.0 - sum(scores.values()))
            best = max(scores, key=lambda key: scores[key])
            answers = {"pick": {"type": "choice", "choice": best, "confidence": scores[best], "probabilities": scores}}
        else:
            with self._lock:
                self.stage_two.append(sorted(questions))
            if self.fail_stage_two:
                raise client.JevError("credits_exhausted")
            answers = {name: {"type": "noul", "noul": self.verdicts.get(name, 0.05)} for name in questions}
        return json.dumps({"model": "jev-test", "answers": answers, "usage": {}}).encode()


class RequestsInOtherScriptsTests(unittest.TestCase):
    """The gate split turns on everything that is not an ASCII letter, which left a request
    in any other script with no words at all, and "no words" was read as "emoji only".
    Every such turn was answered locally, at any length. Bypassing the gate, Jev got them
    right: the Korean one below picked xlsx at 0.71, the Japanese one github-code-review
    at 0.83.
    """

    REQUESTS = {
        "Chinese": "请仔细审查这个拉取请求中的所有代码改动，重点检查错误处理、并发安全以及单元测试的覆盖率，然后把发现的问题按严重程度整理成一份清单发给我，越详细越好",
        "Japanese": "このリポジトリのプルリクエストをレビューしてください",
        "Korean": "이 스프레드시트에 합계 열을 추가해 주세요",
        "Russian": "Проверь код в этом запросе на слияние",
        "Arabic": "راجع الكود في طلب السحب هذا",
        "Hebrew": "בדוק את הקוד בבקשת המשיכה הזו",
        "Greek": "Έλεγξε τον κώδικα σε αυτό το αίτημα",
        "Hindi": "इस स्प्रेडशीट में कुल कॉलम जोड़ें",
        "Thai": "ตรวจสอบโค้ดในคำขอดึงนี้",
    }

    def test_a_request_in_any_script_is_never_answered_locally(self):
        for language, turn in self.REQUESTS.items():
            self.assertFalse(skillpick.looks_trivial(turn), language)

    def test_length_does_not_rescue_or_condemn_a_foreign_request(self):
        self.assertGreaterEqual(len(self.REQUESTS["Chinese"]), 70)
        self.assertFalse(skillpick.looks_trivial(self.REQUESTS["Chinese"]))
        self.assertFalse(skillpick.looks_trivial("审查代码"))

    def test_an_english_acknowledgement_in_front_does_not_hide_the_request_behind_it(self):
        self.assertFalse(skillpick.looks_trivial("ok 请审查这个拉取请求"))
        self.assertFalse(skillpick.looks_trivial("thanks, теперь проверь код"))

    def test_a_word_the_vocabulary_cannot_read_is_asked_not_guessed(self):
        """"谢谢" is "thanks", but the gate cannot know that, and "cannot read" is not "trivial"."""
        for turn in ("谢谢", "да", "はい", "gracias", "café"):
            self.assertFalse(skillpick.looks_trivial(turn), turn)

    def test_only_a_turn_with_nothing_to_read_is_skipped_unread(self):
        for turn in ("👍", "🎉🎉", "...", "!!", "—", "", "   "):
            self.assertTrue(skillpick.looks_trivial(turn), repr(turn))
        for turn in ("2", "10 / 3", "v2"):
            self.assertFalse(skillpick.looks_trivial(turn), turn)

    def test_a_korean_request_reaches_jev_and_comes_back_with_its_skill(self):
        skills = [{"name": "xlsx", "description": "Edit spreadsheets", "path": "skills/xlsx/SKILL.md"},
                  {"name": "video", "description": "Render a promo video", "path": "skills/video/SKILL.md"}]
        jev = FakeJev(shortlist={0: 0.9}, verdicts={"needs_skill": 0.8, "s0": 0.71})
        out = skillpick.pick(self.REQUESTS["Korean"], skills, transport=jev)
        self.assertNotIn("skipped", out)
        self.assertEqual(len(jev.stage_one), 1)
        self.assertEqual([(s["name"], s["match"]) for s in out["skills"]], [("xlsx", 0.71)])


class InstructionsBuiltFromSmallWordsTests(unittest.TestCase):
    """The vocabulary held verbs and pronouns next to the acknowledgements, so a follow-up
    made only of those was skipped as if it were "ok". These are instructions.
    """

    FOLLOW_UPS = ["do all of them now", "keep going, do them all", "you keep working on that", "do that now",
                  "please do that now", "go do that again", "stop it", "stop all", "right do that", "you do it",
                  "do them", "all of them please", "keep it up", "go on then", "hold that", "keep that working",
                  "do this again now", "all working?", "that done?", "do it"]

    def test_a_follow_up_instruction_is_never_answered_locally(self):
        for turn in self.FOLLOW_UPS:
            self.assertFalse(skillpick.looks_trivial(turn), turn)

    def test_a_verb_or_pronoun_is_not_an_acknowledgement_even_next_to_one(self):
        for word in ("do", "go", "keep", "hold", "continue", "it", "that", "them", "this", "all", "you", "now", "again"):
            self.assertFalse(skillpick.looks_trivial(word), word)
            self.assertFalse(skillpick.looks_trivial(f"ok {word}"), f"ok {word}")

    def test_a_question_is_never_trivial_whatever_it_is_made_of(self):
        for turn in ("all working?", "that done?", "ok?", "ready?", "done\uff1f", "???"):
            self.assertFalse(skillpick.looks_trivial(turn), turn)

    def test_plain_acknowledgements_are_still_answered_locally(self):
        for turn in ("ok", "thanks", "cool", "got it", "never mind", "yep", "hmm", "Thanks!!", "ok 👍",
                     "thanks, that worked", "no worries", "all done"):
            self.assertTrue(skillpick.looks_trivial(turn), turn)

    def test_consent_skips_only_as_the_whole_phrase(self):
        """The deliberate exception: "go ahead" and "please do" name no task, so they skip.
        Their words do not, so anything that continues into an instruction is asked."""
        for turn in ("yes go ahead", "sure, go ahead", "please do", "yes please do"):
            self.assertTrue(skillpick.looks_trivial(turn), turn)
        for turn in ("go ahead and do them all", "please do it", "please do that now", "go ahead with that",
                     "go", "ahead", "please do again", "carry on", "keep going", "go on"):
            self.assertFalse(skillpick.looks_trivial(turn), turn)

    def test_stop_interrupts_alone_and_instructs_with_an_object(self):
        self.assertTrue(skillpick.looks_trivial("stop"))
        self.assertTrue(skillpick.looks_trivial("ok stop, thanks"))
        for turn in ("stop it", "stop all", "stop that now", "stop the gateway service"):
            self.assertFalse(skillpick.looks_trivial(turn), turn)

    def test_a_phone_keyboard_apostrophe_reads_like_a_typed_one(self):
        self.assertEqual(skillpick.looks_trivial("hey what\u2019s up"), skillpick.looks_trivial("hey what's up"))
        self.assertTrue(skillpick.looks_trivial("that\u2019s great"))


class CatalogCapTests(unittest.TestCase):
    """The cap used to be applied inside discover(), in directory order, with no trace.
    On a real fleet it removed 60 of 460 skills: all of them from the shared folder."""

    def setUp(self):
        skillpick._WARNED.clear()

    def test_discover_returns_every_skill_it_finds(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(skillpick, "MAX_SKILLS", 5):
            for i in range(8):
                folder = Path(tmp) / f"skill-{i}"
                folder.mkdir()
                (folder / "SKILL.md").write_text(f"---\nname: skill-{i}\ndescription: Procedure {i}\n---\n", encoding="utf-8")
            self.assertEqual(len(skillpick.discover([Path(tmp)])), 8)

    def test_a_catalog_over_the_cap_says_how_many_skills_it_left_out(self):
        jev = FakeJev()
        with mock.patch.object(skillpick, "MAX_SKILLS", 5), self.assertLogs("jevkit.skillpick", level="WARNING") as logs:
            out = skillpick.pick("count the lines of code", catalog_of(8), transport=jev)
        self.assertEqual(out["skills_dropped"], 3)
        self.assertEqual(sorted(jev.stage_one[0]), ["S0", "S1", "S2", "S3", "S4", "none"])
        self.assertIn("3 of 8 skills were not ranked", logs.output[0])
        self.assertIn("skill-005, skill-006, skill-007", logs.output[0])

    def test_an_outage_does_not_swallow_the_dropped_count(self):
        jev = FakeJev(fail_batch_offering="S0")
        with mock.patch.object(skillpick, "MAX_SKILLS", 5), self.assertLogs("jevkit.skillpick", level="WARNING"):
            out = skillpick.pick("count the lines of code", catalog_of(8), transport=jev)
        self.assertEqual((out["status"], out["skills_dropped"]), ("fail_open", 3))

    def test_the_overflow_is_logged_once_not_on_every_turn(self):
        with mock.patch.object(skillpick, "MAX_SKILLS", 5), self.assertLogs("jevkit.skillpick", level="WARNING") as logs:
            for _ in range(3):
                out = skillpick.pick("count the lines of code", catalog_of(8), transport=FakeJev())
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(out["skills_dropped"], 3, "the reply still carries the count after the log goes quiet")

    def test_a_catalog_that_fits_reports_nothing_dropped(self):
        out = skillpick.pick("count the lines of code", catalog_of(8), transport=FakeJev())
        self.assertNotIn("skills_dropped", out)


class ManyBatchesTests(unittest.TestCase):
    """A real catalog is several hundred skills, ranked as parallel batches whose answers
    are merged. The only test of pick() used three skills, which is one batch, so none of
    the merge had ever run under test."""

    SKILLS = catalog_of(2 * skillpick.BATCH + 10)          # three batches, the last one short
    LAST = len(SKILLS) - 5                                # a skill that only the last batch offers

    def ask(self, jev, **options):
        return skillpick.pick("count the lines of code in this repo", self.SKILLS, transport=jev, **options)

    def test_every_skill_is_offered_exactly_once_and_every_batch_may_answer_none(self):
        jev = FakeJev()
        self.ask(jev)
        self.assertEqual(len(jev.stage_one), 3)
        offered = [key for batch in jev.stage_one for key in batch if key != "none"]
        self.assertEqual(sorted(offered, key=lambda key: int(key[1:])), [f"S{i}" for i in range(len(self.SKILLS))])
        self.assertTrue(all("none" in batch for batch in jev.stage_one))
        self.assertTrue(all(len(batch) <= skillpick.BATCH + 1 for batch in jev.stage_one))

    def test_a_winner_in_the_last_batch_comes_back_under_its_own_name(self):
        """Option keys are numbered across the whole catalog. Numbered per batch, S5 of the
        last batch would come back as the fifth skill of the first."""
        jev = FakeJev(shortlist={self.LAST: 0.9}, verdicts={"needs_skill": 0.9, f"s{self.LAST}": 0.88})
        out = self.ask(jev)
        self.assertEqual(out["skills"], [{"name": self.SKILLS[self.LAST]["name"], "path": self.SKILLS[self.LAST]["path"], "match": 0.88}])
        self.assertEqual(jev.stage_two, [["needs_skill", f"s{self.LAST}"]])
        self.assertNotIn("skills_dropped", out)

    def test_a_skill_under_the_shortlist_floor_is_not_verified(self):
        jev = FakeJev(shortlist={3: skillpick.SHORTLIST_FLOOR - 0.001, self.LAST: skillpick.SHORTLIST_FLOOR + 0.001},
                      verdicts={"needs_skill": 0.9, "s3": 0.99, f"s{self.LAST}": 0.9})
        out = self.ask(jev)
        self.assertEqual(jev.stage_two, [["needs_skill", f"s{self.LAST}"]])
        self.assertEqual([s["name"] for s in out["skills"]], [self.SKILLS[self.LAST]["name"]])

    def test_nothing_over_the_floor_ends_the_pick_without_a_second_request(self):
        jev = FakeJev(shortlist={3: 0.01, 130: 0.015, self.LAST: 0.019})
        out = self.ask(jev)
        self.assertEqual((out["status"], out["needs_skill"], out["skills"]), ("ok", 0.0, []))
        self.assertEqual(jev.stage_two, [])

    def test_only_the_five_strongest_across_all_batches_are_verified(self):
        shortlist = {1: 0.30, 2: 0.10, 125: 0.40, 126: 0.20, 127: 0.05, self.LAST: 0.50, self.LAST + 1: 0.04}
        jev = FakeJev(shortlist=shortlist, verdicts={"needs_skill": 0.9})
        self.ask(jev)
        self.assertEqual(jev.stage_two, [sorted(["needs_skill", "s1", "s2", "s125", "s126", f"s{self.LAST}"])])

    def test_a_strong_match_is_withheld_when_no_skill_is_needed(self):
        """Stage 2 judges each finalist alone, so the closest of a bad bunch can still score
        high. needs_skill is the question that says none of them should load."""
        jev = FakeJev(shortlist={self.LAST: 0.9}, verdicts={"needs_skill": 0.3, f"s{self.LAST}": 0.95})
        out = self.ask(jev)
        self.assertEqual((out["status"], out["needs_skill"], out["skills"]), ("ok", 0.3, []))

    def test_none_winning_every_batch_means_no_skill_and_one_round_trip(self):
        jev = FakeJev()
        out = self.ask(jev)
        self.assertEqual((out["status"], out["skills"]), ("ok", []))
        self.assertEqual((len(jev.stage_one), len(jev.stage_two)), (3, 0))

    def test_one_lost_batch_fails_the_pick_open_instead_of_ranking_the_rest(self):
        """The two batches that answered both said "none". Reporting that would be an outage
        dressed as a clean result: the right skill may have been in the batch that was lost."""
        jev = FakeJev(fail_batch_offering=f"S{self.LAST}")
        out = self.ask(jev)
        self.assertEqual((out["status"], out["skills"]), ("fail_open", []))
        self.assertIn("credits_exhausted", out["reason"])
        self.assertNotIn("needs_skill", out)

    def test_an_outage_during_verification_is_not_reported_as_no_skill_needed(self):
        jev = FakeJev(shortlist={self.LAST: 0.9}, fail_stage_two=True)
        out = self.ask(jev)
        self.assertEqual((out["status"], out["skills"]), ("fail_open", []))
        self.assertNotIn("needs_skill", out)


class HermesRootsTests(unittest.TestCase):
    """The plugin searched `<home>/skills` and nothing else. Hermes also reads
    `skills.external_dirs`, where a fleet keeps what every profile shares."""

    def setUp(self):
        skillpick._WARNED.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.home = self.base / "hermes"
        self.shared = self.base / "team" / "shared-skills"
        for folder in (self.home / "skills", self.shared):
            folder.mkdir(parents=True)

    def skill(self, root, name, description):
        (root / name).mkdir()
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n", encoding="utf-8")

    def write_config(self, text):
        (self.home / "config.yaml").write_text(text, encoding="utf-8")

    def test_a_shared_skill_outside_the_home_is_found(self):
        self.skill(self.home / "skills", "local-only", "Lives in the profile")
        self.skill(self.shared, "fleet-wide", "Lives in the shared folder")
        roots = skillpick.discover_roots(self.home, {"skills": {"external_dirs": [str(self.shared)]}})
        self.assertEqual(roots, [self.home / "skills", self.shared])
        self.assertEqual(sorted(s["name"] for s in skillpick.discover(roots)), ["fleet-wide", "local-only"])

    def test_config_yaml_is_read_when_no_parsed_config_is_passed(self):
        """The shape Hermes itself writes: list items at the key's own indent, siblings after."""
        self.write_config("model:\n  default: some-model\nskills:\n  external_dirs:\n  - " + str(self.shared) +
                          "\n  template_vars: true\n  platform_disabled:\n    telegram:\n    - video\nterminal:\n  external_dirs:\n  - /\n")
        self.assertEqual(skillpick.discover_roots(self.home), [self.home / "skills", self.shared])

    def test_every_shape_hermes_accepts_for_the_key_is_read(self):
        other = self.base / "other skills"
        other.mkdir()
        shapes = {
            "one string": f"skills:\n  external_dirs: {self.shared}\n",
            "inline list": f"skills:\n  external_dirs: [{self.shared}, \"{other}\"]   # both team folders\n",
            "nested block list": f"skills:\n  external_dirs:\n    - {self.shared}   # the team folder\n    - '{other}'\n",
        }
        for shape, text in shapes.items():
            self.write_config(text)
            expected = [self.home / "skills", self.shared] + ([] if shape == "one string" else [other])
            self.assertEqual(skillpick.discover_roots(self.home), expected, shape)

    def test_a_relative_entry_is_resolved_against_the_home_not_the_working_directory(self):
        (self.home / "extra").mkdir()
        roots = skillpick.discover_roots(self.home, {"skills": {"external_dirs": ["extra"]}})
        self.assertEqual(roots, [self.home / "skills", self.home / "extra"])

    def test_tilde_and_environment_variables_are_expanded(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.base), "TEAM_SKILLS": str(self.shared)}):
            self.assertEqual(skillpick.discover_roots(self.home, {"skills": {"external_dirs": "~/team/shared-skills"}}),
                             [self.home / "skills", self.shared])
            self.assertEqual(skillpick.discover_roots(self.home, {"skills": {"external_dirs": ["${TEAM_SKILLS}"]}}),
                             [self.home / "skills", self.shared])

    def test_missing_folders_duplicates_and_the_local_folder_itself_are_dropped(self):
        entries = [str(self.shared), str(self.base / "gone"), str(self.shared) + "/", str(self.home / "skills"), "", None]
        self.assertEqual(skillpick.discover_roots(self.home, {"skills": {"external_dirs": entries}}),
                         [self.home / "skills", self.shared])

    def test_the_local_skill_wins_a_name_clash_with_a_shared_one(self):
        self.skill(self.home / "skills", "deploy", "The profile's own deploy procedure")
        self.skill(self.shared, "deploy", "The fleet's deploy procedure")
        found = skillpick.discover(skillpick.discover_roots(self.home, {"skills": {"external_dirs": [str(self.shared)]}}))
        self.assertEqual([s["description"] for s in found], ["The profile's own deploy procedure"])

    def test_a_yaml_null_never_becomes_the_home_folder(self):
        """`~` is YAML for nothing. Read as a path it is the person's home folder, and the
        picker would walk their whole disk on every turn."""
        with mock.patch.dict(os.environ, {"HOME": str(self.base)}):
            for text in ("skills:\n  external_dirs: ~\n", "skills:\n  external_dirs:\n  - ~\n  - null\n",
                         "skills:\n  external_dirs: []\n", "skills:\n  external_dirs:\n"):
                self.write_config(text)
                self.assertEqual(skillpick.discover_roots(self.home), [self.home / "skills"], text)

    def test_a_key_of_the_same_name_elsewhere_is_not_mistaken_for_it(self):
        self.write_config(f"backup:\n  external_dirs:\n  - {self.shared}\nskills:\n  platform_disabled:\n"
                          f"    external_dirs:\n    - {self.shared}\n  write_approval: true\n")
        self.assertEqual(skillpick.discover_roots(self.home), [self.home / "skills"])

    def test_a_value_it_cannot_read_is_said_out_loud_once(self):
        self.write_config("shared: &dirs\n- /somewhere\nskills:\n  external_dirs: *dirs\n")
        with self.assertLogs("jevkit.skillpick", level="WARNING") as logs:
            for _ in range(2):
                self.assertEqual(skillpick.discover_roots(self.home), [self.home / "skills"])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("pass the parsed config", logs.output[0])

    def test_a_home_with_no_config_or_a_malformed_one_still_gives_the_local_folder(self):
        self.assertEqual(skillpick.discover_roots(self.home), [self.home / "skills"])
        for config in ({}, {"skills": None}, {"skills": "on"}, {"skills": {"external_dirs": 5}}, {"skills": {"external_dirs": {"a": 1}}}):
            self.assertEqual(skillpick.discover_roots(self.home, config), [self.home / "skills"], config)


if __name__ == "__main__":
    unittest.main()
