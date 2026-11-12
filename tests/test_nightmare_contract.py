"""Deterministic hostile-wire cases. No credentials, network or model calls."""
import json
import unittest
from unittest.mock import patch

from jevkit import client


def ask_raw(raw, question=None):
    question = question or client.noul("Does the fixture report success?")
    with patch.dict("os.environ", {}, clear=True):
        return client.ask("Synthetic public fixture", {"q": question}, api_key="fixture-only",
                          provider="typesafe", retries=0, transport=lambda *args: raw)


class NightmareWireTests(unittest.TestCase):
    def test_duplicate_json_members_never_last_win(self):
        for raw in [b'{"answers":{"q":{"type":"noul","noul":0,"noul":1}}}',
                    b'{"answers":{},"answers":{"q":{"type":"noul","noul":1}}}']:
            with self.subTest(raw=raw), self.assertRaises(client.JevError):
                ask_raw(raw)

    def test_unsolicited_answer_rejected(self):
        with self.assertRaises(client.JevError):
            ask_raw(b'{"answers":{"q":{"type":"noul","noul":1},"execute":{}}}')

    def test_huge_integer_is_typed_refusal_not_overflow(self):
        raw = ('{"answers":{"q":{"type":"noul","noul":' + '9' * 400 + '}}}').encode()
        with self.assertRaises(client.JevError):
            ask_raw(raw)

    def test_huge_usage_integer_is_ignored_not_overflow(self):
        raw = json.dumps({"answers": {"q": {"type": "noul", "noul": 1}},
                          "usage": {"input_tokens": 10**400}}).encode()
        self.assertIsNone(ask_raw(raw)["input_tokens"])

    def test_oversized_custom_transport_cannot_bypass_bound(self):
        raw = b'{"answers":{"q":{"type":"noul","noul":1}},"padding":"' + b'x' * client.MAX_RESPONSE_BYTES + b'"}'
        with self.assertRaises(client.JevError):
            ask_raw(raw)

    def test_malformed_and_aliased_score_spreads(self):
        question = client.score("How complete is the fixture?", ["missing", "complete"])
        for spread in [[0, 1], {"0": 0, "1": .5, "01": .5}, {"²": 1}]:
            raw = json.dumps({"answers": {"q": {"type": "score", "score": 1,
                              "probabilities": spread}}}).encode()
            with self.subTest(spread=spread), self.assertRaises(client.JevError):
                ask_raw(raw, question)

    def test_action_selector_declines_corrupt_wire_through_real_client(self):
        from jevkit import choose
        request = {"schema": choose.REQUEST_SCHEMA, "goal": "Read the synthetic fixture",
                   "observation_id": "fixture_screen", "candidates": [
                       {"id": "reobserve", "description": "Observe again"},
                       {"id": "abstain", "description": "Decline"},
                       {"id": "read", "description": "Read synthetic heading"}]}
        raw = (b'{"answers":{"next_action":{"type":"choice","choice":"abstain","choice":"read",'
               b'"probabilities":{"reobserve":0,"abstain":0,"read":1},"confidence":1}}}')
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(client.keystore, "resolve", return_value="fixture-only"):
            result = choose.choose(request, transport=lambda *args: raw)
        self.assertEqual(result["selected_id"], "reobserve")
        self.assertEqual(result["confidence"], 0)

    def test_question_id_string_collision_is_not_silent_overwrite(self):
        q = client.noul("Does the fixture report success?")
        with self.assertRaises(ValueError):
            client.check_questions({1: q, "1": q})

    def test_choice_empty_criterion_shorthand_stays_compatible(self):
        q = client.check_questions({"q": {"type": "choice", "instructions": "Pick a fixture action",
                                         "criteria": {"safe": "", "other": "Stop"}}})
        self.assertEqual(q["q"]["criteria"]["safe"], "")

    def test_valid_choice_and_score_still_pass(self):
        for question, answer in [
            (client.choice("Pick the fixture action", {"a": "Continue", "b": "Stop"}),
             {"type": "choice", "choice": "a", "probabilities": {"a": .9, "b": .1}, "confidence": .9}),
            (client.score("How complete is the fixture?", ["missing", "complete"]),
             {"type": "score", "score": 1, "probabilities": {"0": 0, "1": 1}})]:
            self.assertIn("q", ask_raw(json.dumps({"answers": {"q": answer}}).encode(), question)["answers"])
