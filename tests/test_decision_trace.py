"""Offline integrity/drift tests, with no provider or execution dependencies."""
import copy
import json
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from jevkit import client
from jevkit.decision_trace import fingerprint, record, replay

ROOT = Path(__file__).resolve().parents[1]


def cases():
    return [{"id": "opaque_1", "questions": {"q": client.noul("Synthetic fact")},
             "response": '{"answers":{"q":{"type":"noul","noul":1}}}'}]


class DecisionTraceTests(unittest.TestCase):
    def test_replay_revalidates_without_network_or_key(self):
        with patch.object(client.keystore, "resolve", side_effect=AssertionError("key access")), \
             patch.object(client, "_http_transport", side_effect=AssertionError("network")):
            trace = record(cases())
            out = replay(cases(), trace)
        self.assertEqual(out["matches"], 1)
        self.assertEqual(out["model_calls"], 0)
        self.assertFalse(out["execution_verified"])
        encoded = json.dumps(trace)
        self.assertNotIn("Synthetic fact", encoded)
        self.assertNotIn('"response"', encoded)

    def test_outcome_change_reported_not_trusted(self):
        trace = record(cases())
        trace["rows"][0]["outcome"] = {"status": "refused", "error": "malformed"}
        trace["rows_sha256"] = fingerprint(trace["rows"])
        self.assertEqual(len(replay(cases(), trace)["changes"]), 1)

    def test_payload_drift_is_not_a_replay(self):
        original = cases()
        trace = record(original)
        changed = copy.deepcopy(original)
        changed[0]["questions"]["q"]["instructions"] = "Changed definition"
        with self.assertRaisesRegex(ValueError, "definition drift"):
            replay(changed, trace)

    def test_tamper_count_and_schema_refused(self):
        trace = record(cases())
        for bad in ({}, {**trace, "schema": "unknown"}, {**trace, "rows_sha256": "0" * 64},
                    {**trace, "rows": [], "rows_sha256": fingerprint([])}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    replay(cases(), bad)

    def test_duplicate_id_and_fixture_bounds_refused(self):
        for bad in ([], cases() * 2, cases() * 4097,
                    [{**cases()[0], "id": "private name not an opaque ID"}],
                    [{**cases()[0], "response": " " * (client.MAX_RESPONSE_BYTES + 1)}]):
            with self.assertRaises(ValueError):
                record(bad)

    def test_all_nightmare_corpus_outcomes_match_and_replay(self):
        corpus = runpy.run_path(str(ROOT / "scripts/nightmare_contract.py"))["corpus"]()
        with patch.object(client.keystore, "resolve", side_effect=AssertionError("key access")), \
             patch.object(client, "_http_transport", side_effect=AssertionError("network")):
            trace = record(corpus)
            for fixture, row in zip(corpus, trace["rows"]):
                with self.subTest(case=fixture["id"]):
                    self.assertEqual(fixture["expected"], row["outcome"]["status"])
            self.assertEqual(replay(corpus, trace)["changes"], [])
