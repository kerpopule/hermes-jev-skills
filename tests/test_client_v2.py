"""Client changes for the decision policies, each against the exact bytes sent.

* A noul's {"true", "false"} criteria are accepted and SENT — the API reads them, and
  LangChain's AutoModeMiddleware shows what happens when a default is written but never sent.
* Instructions and criteria may be objects or arrays (the API's structured form).
* The API's own limits are refused locally: > 255 choice options, a score outside 2..10 levels.
* The reply's `model` comes back as `jev_model`, with the input-token count and the provider.
* `retry-after` is honoured only by a patient (batch) caller.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import KEY, Scripted  # noqa: F401  (also puts the repo on sys.path)
from jevkit import client


class NoulCriteria(unittest.TestCase):
    def test_criteria_are_accepted_and_sent_byte_for_byte(self):
        fake = Scripted({"risky": 0.2})
        criteria = {"true": "It could delete real data.", "false": "It only reads."}
        client.ask({"command": "ls"}, {"risky": client.noul("Is running `command` risky?", criteria)},
                   api_key=KEY, transport=fake, timeout=2)
        sent = json.loads(fake.bodies[0])["questions"]["risky"]
        self.assertEqual(sent, {"type": "noul", "instructions": "Is running `command` risky?", "criteria": criteria})

    def test_either_side_alone_is_fine(self):
        checked = client.check_question("q", {"type": "noul", "instructions": "Is it late?",
                                              "criteria": {"true": "After the deadline"}})
        self.assertEqual(checked["criteria"], {"true": "After the deadline"})

    def test_anything_but_true_and_false_is_refused(self):
        for criteria in (["yes", "no"], {"yes": "a", "no": "b"}, {}, {"true": ""}, "yes"):
            with self.subTest(criteria=criteria), self.assertRaises(ValueError):
                client.check_question("q", {"type": "noul", "instructions": "Is it late?", "criteria": criteria})

    def test_a_noul_without_criteria_is_unchanged(self):
        self.assertEqual(client.noul("Is it late?"), {"type": "noul", "instructions": "Is it late?"})


class StructuredInstructions(unittest.TestCase):
    def test_object_instructions_and_object_criteria_are_sent(self):
        fake = Scripted()
        question = {"type": "choice",
                    "instructions": {"question": "Is the resume for `candidate`?", "candidate": {"name": "A. Person"}},
                    "criteria": {"same": {"meaning": "same person"}, "different": None}}
        client.ask("resume text", {"match": question}, api_key=KEY, transport=fake, timeout=2)
        sent = json.loads(fake.bodies[0])["questions"]["match"]
        self.assertEqual(sent["instructions"]["candidate"], {"name": "A. Person"})
        self.assertIsNone(sent["criteria"]["different"])

    def test_empty_structured_instructions_are_refused(self):
        for text in ({}, [], "  "):
            with self.subTest(text=text), self.assertRaises(ValueError):
                client.check_question("q", {"type": "noul", "instructions": text})

    def test_only_a_string_can_repeat_its_own_name(self):
        client.check_question("blocked", {"type": "noul", "instructions": {"question": "blocked"}})


class ApiLimits(unittest.TestCase):
    def test_a_choice_of_255_is_allowed_and_256_refused(self):
        client.check_question("c", {"type": "choice", "instructions": "Which one?",
                                    "criteria": {f"o{i}": None for i in range(255)}})
        with self.assertRaises(ValueError) as raised:
            client.check_question("c", {"type": "choice", "instructions": "Which one?",
                                        "criteria": {f"o{i}": None for i in range(256)}})
        self.assertIn("255", str(raised.exception))

    def test_a_score_takes_two_to_ten_levels(self):
        client.check_question("s", {"type": "score", "instructions": "How bad?", "criteria": ["a", "b"]})
        client.check_question("s", {"type": "score", "instructions": "How bad?", "criteria": [str(i) for i in range(10)]})
        for levels in (["a"], [str(i) for i in range(11)], ["a", ""]):
            with self.subTest(levels=len(levels)), self.assertRaises(ValueError):
                client.check_question("s", {"type": "score", "instructions": "How bad?", "criteria": levels})

    def test_a_refused_question_never_reaches_the_wire(self):
        fake = Scripted()
        with self.assertRaises(ValueError):
            client.ask("s", {"c": {"type": "choice", "instructions": "Which?",
                                   "criteria": {f"o{i}": None for i in range(300)}}},
                       api_key=KEY, transport=fake, timeout=1)
        self.assertEqual(fake.bodies, [])


class ReplyMetadata(unittest.TestCase):
    def test_model_tokens_and_provider_come_back(self):
        reply = client.ask("s", {"q": client.noul("Is it late?")}, api_key=KEY,
                           transport=Scripted(model="jev-1.13.0", tokens=321), timeout=2)
        self.assertEqual(reply["jev_model"], "jev-1.13.0")
        self.assertEqual(reply["input_tokens"], 321)
        self.assertEqual(reply["provider"], "typesafe")
        self.assertIn("answers", reply)
        self.assertIn("usage", reply)

    def test_a_reply_without_a_model_says_none(self):
        reply = client.ask("s", {"q": client.noul("Is it late?")}, api_key=KEY,
                           transport=Scripted(model=None), timeout=2)
        self.assertIsNone(reply["jev_model"])


class _Response:
    def __init__(self, headers):
        self._headers = {k.lower(): v for k, v in headers.items()}

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)


class RetryAfter(unittest.TestCase):
    def test_header_parsing(self):
        self.assertEqual(client.retry_after_seconds(_Response({"retry-after-ms": "1500"})), 1.5)
        self.assertEqual(client.retry_after_seconds(_Response({"retry-after": "2"})), 2.0)
        self.assertEqual(client.retry_after_seconds(_Response({"retry-after": "600"})), client.MAX_RETRY_AFTER)
        self.assertIsNone(client.retry_after_seconds(_Response({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})))
        self.assertIsNone(client.retry_after_seconds(_Response({})))

    def _flaky(self, wait):
        calls = {"n": 0}
        good = Scripted({"q": 0.9})

        def transport(body, headers, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise client.JevError("rate_limited", retry_after=wait)
            return good(body, headers, timeout)
        return transport, calls

    def test_a_patient_caller_waits_what_the_server_asked(self):
        transport, calls = self._flaky(2.0)
        with mock.patch.object(client.time, "sleep") as slept:
            reply = client.ask("s", {"q": client.noul("Is it late?")}, api_key=KEY, transport=transport,
                               timeout=10, patient=True)
        self.assertEqual(calls["n"], 2)
        slept.assert_called_once_with(2.0)
        self.assertEqual(reply["answers"]["q"]["noul"], 0.9)

    def test_a_live_caller_keeps_its_short_backoff(self):
        transport, calls = self._flaky(2.0)
        with mock.patch.object(client.time, "sleep") as slept:
            client.ask("s", {"q": client.noul("Is it late?")}, api_key=KEY, transport=transport, timeout=10)
        self.assertEqual(calls["n"], 2)
        self.assertLessEqual(slept.call_args[0][0], 0.25)

    def test_a_wait_past_the_budget_stops_now(self):
        transport, calls = self._flaky(30.0)
        with mock.patch.object(client.time, "sleep") as slept, self.assertRaises(client.JevError):
            client.ask("s", {"q": client.noul("Is it late?")}, api_key=KEY, transport=transport,
                       timeout=5, patient=True)
        slept.assert_not_called()
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
