"""Bugs found by people reading the code, each pinned by the case that was reported.

These were all reported in one issue by someone reviewing the repository from outside,
with a reproduction for each. They are kept together because the value is the reproduction:
every one of them passed the existing suite.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevkit import client, compact, keystore, route  # noqa: E402

# Well-formed wire answers (complete distributions that sum to one) live in one place, so a
# fake here cannot accidentally describe a reply the API cannot produce.
from _wire import choice_answer, score_answer  # noqa: E402

CATALOG = [
    {"provider": "openrouter", "model": "cheap/tiny-v1", "price": 0.1, "context": 32_000,
     "vision": False, "reasoning": False},
    {"provider": "openrouter", "model": "mid/general-v2", "price": 1.0, "context": 1_000_000,
     "vision": False, "reasoning": False},
]
BIG = "openrouter:mid/general-v2"
SMALL = "openrouter:cheap/tiny-v1"


def trivial_answer(body, headers, timeout):
    """Jev says this turn is trivial and general: the cheapest tier, whatever the size."""
    return json.dumps({"answers": {
        "difficulty": score_answer({"criteria": route.DIFFICULTY}, 0.0),
        "kind": choice_answer({"criteria": list(route.KIND)}, "general"),
        "costly_mistake": {"type": "noul", "noul": 0.02}}, "usage": {}}).encode()


class RoutingCacheKeyTests(unittest.TestCase):
    """A cached decision returned before `_pick` ran, so the context checks never happened."""

    def setUp(self):
        config = dict(route.DEFAULT_CONFIG)
        config["tiers"] = {"simple": {"general": [SMALL]}, "medium": {"general": [BIG]},
                           "hard": {"general": [BIG]}}
        self.config = config
        route._DECISIONS.clear()
        self.addCleanup(route._DECISIONS.clear)

    def decide(self, context_tokens, config=None, transport=trivial_answer):
        use = config or self.config
        with mock.patch.object(route, "load_config", lambda profile=None: use), \
                mock.patch.object(route.catalog_mod, "models", lambda *a, **k: CATALOG), \
                mock.patch.object(keystore, "resolve", lambda *a, **k: "apikey_" + "a" * 40):
            return route.decide("continue", current=BIG, context_tokens=context_tokens, transport=transport)

    def test_a_repeated_instruction_in_a_grown_session_is_not_answered_from_the_small_one(self):
        """Reported: a cron turn or "continue" is first seen small, then again at 300k, and
        the second was answered from the first - a 300,000-token turn sent to a model whose
        window is 32,000. README: "A large context never switches to a cheaper model"."""
        small = self.decide(1_000)
        self.assertEqual(small["model"], SMALL)
        large = self.decide(300_000)
        self.assertNotEqual(large.get("cached"), True)
        self.assertEqual(large["model"], BIG)

    def test_the_cache_still_answers_the_repeat_it_exists_for(self):
        """Bucketed, not exact: an exact size would make every turn a miss."""
        calls = []

        def counted(body, headers, timeout):
            calls.append(1)
            return trivial_answer(body, headers, timeout)
        self.decide(1_000, transport=counted)
        again = self.decide(1_200, transport=counted)
        self.assertTrue(again.get("cached"))
        self.assertEqual(len(calls), 1)

    def test_editing_the_pools_takes_effect_without_a_restart(self):
        """Also reported: the key omitted the config, so an edit to tiers did nothing until
        the process restarted, which reads as "my edit was ignored"."""
        first = self.decide(1_000)
        self.assertEqual(first["model"], SMALL)
        edited = dict(self.config)
        edited["tiers"] = dict(self.config["tiers"], simple={"general": [BIG]})
        after = self.decide(1_000, config=edited)
        self.assertNotEqual(after.get("cached"), True)
        self.assertEqual(after["model"], BIG)

    def test_a_context_bigger_than_every_bucket_is_its_own_class(self):
        self.assertNotEqual(route._context_bucket(2_000_000), route._context_bucket(500_000))
        self.assertEqual(route._context_bucket(0), route._context_bucket(3_999))


class CompactionBatchingTests(unittest.TestCase):
    """Batching by turn count sent states several times over the client's limit."""

    def cjk(self, turns=48):
        return [{"role": "user", "content": "日本語のテキストです。これはテスト用の長い文章で、" * 20}
                for _ in range(turns)]

    def run_select(self, messages, transport):
        with mock.patch.object(keystore, "resolve", lambda *a, **k: "apikey_" + "a" * 40):
            return compact.select(messages, transport=transport)

    def test_a_japanese_transcript_is_split_into_requests_that_actually_fit(self):
        """Reported: 40 turns x 700 chars x 6 bytes per CJK character came to 120,601
        characters against a 60,000 limit, and every batch failed as state_too_large."""
        sizes = []

        def record(body, headers, timeout):
            request = json.loads(body)
            sizes.append(len(json.dumps(request["state"], separators=(",", ":"))))
            return json.dumps({"answers": {name: choice_answer(question, "summarize")
                               for name, question in request["questions"].items()}, "usage": {}}).encode()
        out = self.run_select(self.cjk(), record)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), client.MAX_STATE_CHARS)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["errors"], [])

    def test_one_failed_batch_is_reported_instead_of_being_hidden_by_a_good_one(self):
        """`"ok" if calls` meant one successful batch masked every failure, and the turns in
        the failed batch sat at the fail-open default while the caller was told all was well.
        The compaction skill tells an agent to use the plain transcript on anything but ok."""
        seen = []

        def flaky(body, headers, timeout):
            seen.append(1)
            if len(seen) == 1:
                raise client.JevError("state_too_large")
            request = json.loads(body)
            return json.dumps({"answers": {name: choice_answer(question, "summarize")
                               for name, question in request["questions"].items()}, "usage": {}}).encode()
        out = self.run_select(self.cjk(), flaky)
        self.assertEqual(out["status"], "partial")
        self.assertIn("state_too_large", out["errors"])
        self.assertTrue(out["unjudged"])
        for index in out["unjudged"]:
            self.assertEqual(out["fates"][str(index)], "summarize")

    def test_every_batch_failing_is_still_fail_open_and_nothing_is_dropped(self):
        def broken(body, headers, timeout):
            raise client.JevError("network")
        out = self.run_select(self.cjk(), broken)
        self.assertEqual(out["status"], "fail_open")
        self.assertEqual(out["counts"]["drop"], 0)

    def test_an_ascii_transcript_still_packs_by_the_turn_cap(self):
        """The size rule must not make short-turn batches smaller than they were."""
        messages = [{"role": "user", "content": f"turn {i} is short"} for i in range(90)]
        groups = compact._pack(messages, list(range(84)))
        self.assertEqual(max(len(g) for g in groups), compact.BATCH)

    def test_nothing_to_judge_is_still_ok(self):
        out = self.run_select([{"role": "user", "content": "hi"}], lambda *a: None)
        self.assertEqual(out["status"], "ok")


class GuardsReadTheWholeTurnTests(unittest.TestCase):
    """The risk-word floor and the sensitivity check ran on the clipped copy."""

    def decide(self, prompt, transport=trivial_answer):
        config = dict(route.DEFAULT_CONFIG)
        config["tiers"] = {"simple": {"general": [SMALL]}, "medium": {"general": [BIG]},
                           "hard": {"general": [BIG]}}
        route._DECISIONS.clear()
        self.addCleanup(route._DECISIONS.clear)
        catalog = [dict(CATALOG[0], context=900_000), CATALOG[1]]
        with mock.patch.object(route, "load_config", lambda profile=None: config), \
                mock.patch.object(route.catalog_mod, "models", lambda *a, **k: catalog), \
                mock.patch.object(keystore, "resolve", lambda *a, **k: "apikey_" + "a" * 40):
            return route.decide(prompt, current=BIG, context_tokens=1_000, transport=transport)

    def test_a_risk_word_in_the_middle_of_a_long_turn_still_sets_the_floor(self):
        """Reported with an 11,957-character prompt: present in the full text, absent from
        the clipped copy the check read, so it routed to the cheapest tier."""
        filler = "filler sentence. " * 400
        decision = self.decide(filler + " please drop the production database now " + filler)
        self.assertNotEqual(decision["tier"], "simple")

    def test_a_secret_in_the_middle_of_a_long_turn_still_sends_features_only(self):
        """The same clipping decided whether the TEXT was sent at all."""
        sent = {}

        def record(body, headers, timeout):
            sent["state"] = json.loads(body)["state"]
            return trivial_answer(body, headers, timeout)
        filler = "filler sentence. " * 400
        secret = "the api_key is sk-" + "z" * 24 + " do not share it"
        self.decide(filler + secret + filler, transport=record)
        self.assertIn("turn_features", sent["state"])
        self.assertNotIn("user_turn", sent["state"])

    def test_an_ordinary_long_turn_is_still_sent_as_text(self):
        sent = {}

        def record(body, headers, timeout):
            sent["state"] = json.loads(body)["state"]
            return trivial_answer(body, headers, timeout)
        self.decide("please tidy up the notes. " * 400, transport=record)
        self.assertIn("user_turn", sent["state"])


if __name__ == "__main__":
    unittest.main()
