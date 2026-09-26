"""The shape every question has to have, checked where it is sent and across the package.

The rules are old and each one was learned from a failure: a choice with one option has
nothing to pick from, a question whose instructions repeat its own name was never written,
a noul's criteria that are not {"true", "false"} are sent and never read, and a state over the
limit is refused by the API for every batch in the request. (Until the decision-policy change any noul criteria
were refused; the API does read {"true", "false"}, and the risk gate depends on them.) Until now they were enforced by the three
builders — which only guard the callers that use them — so a dict written by hand reached the
wire. `client.ask` now applies them to everything it is handed.

That makes two things worth a test, and this module is both:

* the rules themselves, refused locally rather than sent (no request, no key, no quota);
* the guarantee that nothing in this package can bypass them — every question every feature
  sends is swept through the same checker, live features driven by a recording transport.

The sweep is the part that makes the rules durable. A rule only one person remembers is a
rule that comes back.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from jevkit import (  # noqa: E402
    choose, client, compact, mailbox, rerank, route, search, skillpick, triage,
)

KEY = "sk-test-key"


def violations(questions: Mapping[str, Any]) -> List[str]:
    """Every rule a question breaks, as messages, so a sweep reports all of them at once.

    Deliberately independent of `client.check_question`: it re-derives the rules from the
    question itself, so a change to the checker cannot quietly move the bar this test holds
    the package to.
    """
    out: List[str] = []
    for name, question in questions.items():
        if not isinstance(name, str) or not name.strip():
            out.append(f"a question is named {name!r}; a name is how its answer is found")
        if not isinstance(question, Mapping):
            out.append(f"{name}: not an object")
            continue
        kind = question.get("type")
        if kind not in client.QUESTION_TYPES:
            out.append(f"{name}: type {kind!r} is not one of {client.QUESTION_TYPES}")
            continue
        text = question.get("instructions")
        structured = isinstance(text, (dict, list)) and bool(text)
        if not structured and (not isinstance(text, str) or not text.strip()):
            out.append(f"{name}: no instructions; the meaning has to be in the question")
            continue
        if isinstance(text, str) and client._identifier(text) == client._identifier(name):
            out.append(f"{name}: the instructions only repeat the id; the id names the question, "
                       f"the instructions ask it")
        criteria = question.get("criteria")
        if kind == "noul":
            if criteria is not None and (not isinstance(criteria, dict) or not criteria
                                         or not set(criteria) <= {"true", "false"}):
                out.append(f"{name}: a noul's criteria are {{true, false}} or nothing; others are never read")
        elif kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
                out.append(f"{name}: a choice needs an object of 2 to 255 options")
        elif not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
            out.append(f"{name}: a score needs a list of 2 to 10 levels")
    return out


class Recording:
    """A transport that keeps every question asked and then fails, like an unreachable Jev.

    Failing is the point: it stops the feature after the request is built, so the test reads
    the questions it would have sent without needing a well-formed reply for each shape.
    """

    def __init__(self) -> None:
        self.questions: List[Dict[str, Any]] = []
        self.states: List[Any] = []

    def __call__(self, body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
        payload = json.loads(body)
        self.questions.append(payload["questions"])
        self.states.append(payload["state"])
        raise client.JevError("timeout")


# ── the sweep ────────────────────────────────────────────────────────────────

_DOCSTRING = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'')


def _code_only(source: str) -> str:
    """Source with its docstrings and help text removed, so prose is not read as a code path."""
    return _DOCSTRING.sub("", source)


@patch.dict(os.environ, {"TYPESAFE_API_KEY": KEY})
def questions_from_every_feature() -> Dict[str, Dict[str, Any]]:
    """The questions each feature actually sends, captured from the feature itself.

    Every call here goes through the feature's own public entry point with a recording
    transport, so this stays true when a feature changes: it measures what is sent, not what
    the test believes is sent. A synthetic key avoids relying on the host secret store.
    """
    captured: Dict[str, Dict[str, Any]] = {}

    captured["route"] = route.questions()
    captured["mailbox"] = mailbox.questions()

    recorder = Recording()
    triage.classify("Deploy blocked on the migration step",
                    "The second run failed on the same migration and I cannot proceed until "
                    "someone with access to production steps in.",
                    sender="ops@example.com", transport=recorder)
    captured["triage"] = recorder.questions[-1]

    recorder = Recording()
    choose.choose({"schema": choose.REQUEST_SCHEMA, "goal": "Open the Sound pane",
                   "observation_id": "obs-1",
                   "candidates": [{"id": "reobserve", "description": "Look at the screen again"},
                                  {"id": "abstain", "description": "Do nothing and stop"},
                                  {"id": "open_sound", "description": "Click the Sound pane"}]},
                  transport=recorder)
    captured["choose"] = recorder.questions[-1]

    recorder = Recording()
    compact.select([{"role": "user" if index % 2 == 0 else "assistant", "content": f"turn {index}"}
                    for index in range(12)], keep_last=4, transport=recorder)
    captured["compact"] = recorder.questions[-1]

    recorder = Recording()
    search.gate("does Jev pool connections", [
        {"id": "r1", "title": "Connection reuse", "url": "https://example.com/a",
         "snippet": "A pooled client keeps one TLS session per thread that needs it."},
        {"id": "r2", "title": "Connection pooling", "url": "https://example.com/b",
         "snippet": "Reusing a connection removes the handshake from each call."},
    ], queries_tried=["connection pooling"], candidate_queries=["tls session reuse"],
        transport=recorder, today=datetime.date(2026, 9, 22))
    captured["search"] = recorder.questions[-1]

    recorder = Recording()
    rerank.rerank("how does the client pool connections", [
        {"id": "p1", "text": "One connection is lent to one caller at a time, and a server that "
                            "closes an idle keep-alive socket costs one fresh connection."},
        {"id": "p2", "text": "The pool is bounded so a fan-out on threads cannot open a socket per batch."},
    ], transport=recorder, today=datetime.date(2026, 9, 22))
    captured["rerank"] = recorder.questions[-1]

    recorder = Recording()
    skillpick.pick("read this mailbox export and sort every message into a lane",
                   [{"name": "jev-mailbox", "description": "Sort a mailbox export into lanes."},
                    {"name": "jev-search", "description": "Pick which search results to open."},
                    {"name": "jev-compaction", "description": "Cut a transcript to a fixed size."}],
                   transport=recorder)
    captured["skillpick"] = recorder.questions[-1]

    # The decision policies: every shipped policy's questions, as the engine sends them. The
    # owner policy takes its options at run time, so it is bound to two example profiles.
    from jevkit import decide as engine, policy as policies

    for name in policies.shipped_names():
        recorder = Recording()
        options = {"owner": {"coder": "Writes and fixes code", "writer": "Writes prose"}} if name == "owner" else None
        engine.decide({"task": "t", "output": "o", "command": "ls", "text": "a card is blocked"}, name,
                      choices=options, transport=recorder, record=False, use_limits=False)
        captured[f"policy:{name}"] = recorder.questions[-1]

    return captured


class TestEveryQuestionThePackageSends(unittest.TestCase):
    def test_the_sweep_covers_the_features_it_claims_to(self):
        captured = questions_from_every_feature()
        from jevkit import policy as policies

        self.assertEqual(set(captured), {"route", "triage", "mailbox", "choose", "compact",
                                         "search", "rerank", "skillpick"}
                         | {f"policy:{name}" for name in policies.shipped_names()})
        for feature, questions in captured.items():
            self.assertTrue(questions, f"{feature} sent no questions; the sweep is not measuring it")

    def test_no_feature_sends_a_question_that_breaks_a_rule(self):
        for feature, questions in questions_from_every_feature().items():
            broken = violations(questions)
            self.assertEqual(broken, [], f"{feature}: " + "; ".join(broken))

    def test_every_captured_question_survives_the_checker_that_gates_the_wire(self):
        """The check `ask` applies, run over what the features send: it must not refuse any."""
        for feature, questions in questions_from_every_feature().items():
            checked = client.check_questions(questions)
            self.assertEqual(set(checked), set(questions), feature)


class TestOnlyTheBuildersBuildQuestions(unittest.TestCase):
    def test_no_module_outside_the_client_writes_a_question_dict_by_hand(self):
        """A question is only ever assembled from strings, which is what keeps the rules true.

        A module that spells `{"type": "choice", ...}` itself has bypassed the builders and
        the place the rules live, whatever it puts in them. Help text is the one exception:
        `jev ask --help` prints an example request, and prose is not a code path.
        """
        offenders = []
        for path in sorted((REPO / "jevkit").glob("*.py")):
            if path.name == "client.py":
                continue
            source = _code_only(path.read_text(encoding="utf-8"))
            for kind in client.QUESTION_TYPES:
                if f'"type": "{kind}"' in source or f"'type': '{kind}'" in source:
                    offenders.append(f"{path.name} spells a {kind} question itself")
        self.assertEqual(offenders, [])

    def test_the_builders_and_the_checker_agree(self):
        built = {"a_choice": client.choice("Which lane is this message in?",
                                           {"spam": "Junk", "updates": "A change", "reply": "Wants an answer"}),
                 "a_score": client.score("How urgent is this?", ["nothing", "slight", "needs doing today"]),
                 "a_noul": client.noul("The deploy is blocked on a decision only a person can make")}
        self.assertEqual(violations(built), [])
        self.assertEqual(client.check_questions(built), built)


class TestTheRulesAreRefusedRatherThanSent(unittest.TestCase):
    """One request is one quota unit and one wait: a caller bug must not become either."""

    def ask(self, questions: Mapping[str, Any], state: Any = "state") -> Recording:
        sent = Recording()
        with self.assertRaises(ValueError):
            client.ask(state, questions, api_key=KEY, transport=sent, timeout=1)
        self.assertEqual(sent.questions, [], "a refused question reached the wire")
        return sent

    def test_the_shape_of_a_question_is_checked_before_anything_is_sent(self):
        cases = {
            "no type": {"kind": "noul", "instructions": "The deploy is blocked"},
            "unknown type": {"type": "maybe", "instructions": "Is the deploy blocked?"},
            "no instructions": {"type": "noul"},
            "blank instructions": {"type": "noul", "instructions": "   "},
            "instructions that are the id": {"type": "noul", "instructions": "deploy blocked"},
            "choice with one option": {"type": "choice", "instructions": "Which lane?",
                                       "criteria": {"spam": "Junk"}},
            "choice with a list": {"type": "choice", "instructions": "Which lane?", "criteria": ["a", "b"]},
            "score with one level": {"type": "score", "instructions": "How urgent?", "criteria": ["today"]},
            "noul with criteria": {"type": "noul", "instructions": "The deploy is blocked",
                                   "criteria": ["yes", "no"]},
        }
        for why, question in cases.items():
            with self.subTest(why=why):
                self.ask({"deploy blocked": question})

    def test_instructions_that_repeat_the_id_are_refused_not_renamed(self):
        """The id is not a question. A one-word 'question' is scored against the state as a fact."""
        sent = Recording()
        with self.assertRaises(ValueError) as raised:
            client.ask("state", {"Blocked_On_Review": {"type": "noul", "instructions": "blocked on review?"}},
                       api_key=KEY, transport=sent, timeout=1)
        self.assertIn("asks nothing", str(raised.exception))
        self.assertEqual(sent.questions, [])

    def test_instructions_in_a_non_latin_script_are_a_question_not_a_repeat(self):
        """A CJK question that mentions its own id asks something; it must not be read as bare repeat.

        Regression (2026-09-23, N150): `[^a-z0-9]` stripped every CJK character, so
        "【i5】该候选的判定档位？" collapsed to "i5" and was refused as repeating the name —
        no Chinese question could be asked, and three production features that ask in Chinese
        with ASCII ids (batch classify, collect triage, framework triage) all failed with rc=2.
        """
        for name, text in (("i5", "【i5】该候选的判定档位？"),
                           ("pair0", "【pair0】这两条记忆条目是否应当合并（保留一条）？"),
                           ("gh1234", "判定采集候选【gh1234】的处置。")):
            with self.subTest(name=name):
                checked = client.check_question(name, {"type": "choice", "instructions": text,
                                                       "criteria": {"a": "one", "b": "two"}})
                self.assertEqual(checked["instructions"], text)

    def test_the_identifier_still_distinguishes_punctuation_and_case(self):
        """The letter rule is what the check is about: underscores and case still fold together."""
        self.assertEqual(client._identifier("Blocked_On-Review"), client._identifier("blocked on review"))
        self.assertEqual(client._identifier("blocked on review?"), client._identifier("blocked on review"))
        # The bug in one line: a CJK question keeps its letters, so it is no longer "i5".
        self.assertNotEqual(client._identifier("【i5】该候选的判定档位？"), client._identifier("i5"))
        self.assertIn("该候选", client._identifier("【i5】该候选的判定档位？"))

    def test_a_question_named_after_its_own_answer_is_still_a_question(self):
        """The rule is about a missing question, not about a short one: 'Is it blocked?' is asked."""
        sent = Recording()

        def unreachable(body, headers, timeout):
            sent.questions.append(json.loads(body)["questions"])
            raise client.JevError("timeout")

        with self.assertRaises(client.JevError):
            client.ask("state", {"blocked": {"type": "noul", "instructions": "Is the work stopped?"}},
                       api_key=KEY, transport=unreachable, timeout=1)


class TestTheStateLimitIsLocal(unittest.TestCase):
    def test_at_the_limit_the_request_is_sent(self):
        sent = Recording()

        def capture(body, headers, timeout):
            sent.states.append(body)
            raise client.JevError("timeout")

        with self.assertRaises(client.JevError):
            client.ask("x" * client.MAX_STATE_CHARS, {"q": client.noul("Is the work blocked?")},
                       api_key=KEY, transport=capture, timeout=1)
        self.assertEqual(len(sent.states), 1)

    def test_over_the_limit_is_refused_before_the_request(self):
        sent = Recording()
        with self.assertRaises(client.JevError) as raised:
            client.ask("x" * (client.MAX_STATE_CHARS + 1), {"q": client.noul("Is the work blocked?")},
                       api_key=KEY, transport=sent, timeout=1)
        self.assertEqual(raised.exception.code, "state_too_large")
        self.assertEqual(sent.questions, [], "an oversized state reached the wire")

    def test_a_structured_state_is_measured_as_it_is_encoded(self):
        """The limit is on the JSON that goes out, not on the size of the object's parts."""
        sent = Recording()
        state = {"passages": ["y" * 2_000 for _ in range(client.MAX_STATE_CHARS // 2_000 + 1)]}
        encoded = len(json.dumps(state, separators=(",", ":"), default=str))
        self.assertGreater(encoded, client.MAX_STATE_CHARS)
        with self.assertRaises(client.JevError) as raised:
            client.ask(state, {"q": client.noul("Is the work blocked?")}, api_key=KEY,
                       transport=sent, timeout=1)
        self.assertEqual(raised.exception.code, "state_too_large")
        self.assertEqual(sent.questions, [])


if __name__ == "__main__":
    unittest.main()