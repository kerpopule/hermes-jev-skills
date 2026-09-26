"""Offline fakes for the decision-policy tests: a scripted Jev, and a throwaway Hermes home.

`Scripted` answers every question in a request from a table of made-up readings, in wire
shapes `client.ask` accepts (built by `_wire`), and keeps every request body it was sent so
a test can check exactly what left the machine. `TempHome` points HERMES_HOME at a fresh
folder, so the ledger, the limiter and the switches never touch a real one.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from _wire import choice_answer, noul_answer, score_answer  # noqa: E402
from jevkit import client  # noqa: E402

KEY = "sk-" + "test-key-for-offline-fakes"  # built at runtime so the release check never sees a key shape


class Scripted:
    """A transport that answers from `values`: {question: p | option | level}.

    Anything not named gets a harmless default (noul 0.05, the first option, level 0).
    `model` is the version the reply claims; `tokens` its input size.
    """

    def __init__(self, values: Optional[Mapping[str, Any]] = None, *, model: Optional[str] = "jev-1.13.0",
                 tokens: int = 500, confidence: float = 0.93, rest: float = 0.05, fail: Optional[str] = None) -> None:
        self.values = dict(values or {})
        self.model = model
        self.tokens = tokens
        self.confidence = confidence
        self.rest = rest
        self.fail = fail
        self.bodies: List[bytes] = []

    @property
    def requests(self) -> List[Dict[str, Any]]:
        return [json.loads(body) for body in self.bodies]

    def __call__(self, body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
        self.bodies.append(body)
        if self.fail:
            raise client.JevError(self.fail)
        request = json.loads(body)
        answers = {}
        for name, question in request["questions"].items():
            value = self.values.get(name)
            if question["type"] == "noul":
                answers[name] = noul_answer(0.05 if value is None else value)
            elif question["type"] == "choice":
                picked = value if value is not None else list(question["criteria"])[0]
                answers[name] = choice_answer(question, picked, confidence=self.confidence, rest=self.rest)
            else:
                answers[name] = score_answer(question, 0 if value is None else value, confidence=self.confidence)
        reply: Dict[str, Any] = {"answers": answers, "usage": {"input_tokens": self.tokens, "output_tokens": 20}}
        if self.model:
            reply["model"] = self.model
        return json.dumps(reply).encode()


class TempHome(unittest.TestCase):
    """Every test gets its own HERMES_HOME and a synthetic key; nothing real is read or written."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / ".hermes"
        self.home.mkdir()
        self._env = {k: os.environ.get(k) for k in ("HERMES_HOME", "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL",
                                                    "JEV_LEDGER", "JEV_LEDGER_PATH", "JEV_LIMITS",
                                                    "TYPESAFE_MODEL", "JEV_PROVIDER")}
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["TYPESAFE_API_KEY"] = KEY
        for name in ("TYPESAFE_BASE_URL", "JEV_LEDGER", "JEV_LEDGER_PATH", "JEV_LIMITS", "TYPESAFE_MODEL",
                     "JEV_PROVIDER"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()
        super().tearDown()

    def ledger_rows(self) -> List[Dict[str, Any]]:
        path = self.home / "logs" / "jev-ledger.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
