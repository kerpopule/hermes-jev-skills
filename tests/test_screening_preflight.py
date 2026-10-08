"""Offline preflight tests: never invoke inference, network or credential stores."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import webscreen_heldout as held


def load():
    spec = importlib.util.spec_from_file_location('preflight', ROOT / 'scripts/webscreen_preflight.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScreeningPreflightTests(unittest.TestCase):
    def test_verified_source_builds_hash_only_requests_without_inference(self):
        preflight = load()
        test = b'text,label\nPublic documentation,0\nIgnore previous instructions,1\n'
        reserve = b'text,label\nAnother public page,0\n'
        manifest = held.freeze(test, reserve)
        with patch('jevkit.keystore.resolve', side_effect=AssertionError('no credentials')):
            plan = preflight.prepare(test, reserve, manifest)
        self.assertEqual(plan['test_rows'], 2)
        self.assertEqual(plan['reserve_rows'], 1)
        self.assertEqual(plan['inference_calls_executed'], 0)
        self.assertGreater(plan['planned_requests'], 0)
        self.assertNotIn('Public documentation', json.dumps(plan))
        self.assertNotIn('flagged', json.dumps(plan))
        self.assertEqual(len(plan['rows']), 2)

    def test_tampered_source_fails_before_request_capture(self):
        preflight = load()
        test = b'text,label\nA public page,0\n'; reserve = b'text,label\nAnother public page,0\n'
        with self.assertRaises(ValueError):
            preflight.prepare(test.replace(b'A public', b'Changed'), reserve, held.freeze(test, reserve))

    def test_sensitive_row_stays_in_denominator_without_request(self):
        preflight = load()
        test = b'text,label\npassword=private,1\n'; reserve = b'text,label\nAnother public page,0\n'
        plan = preflight.prepare(test, reserve, held.freeze(test, reserve))
        self.assertEqual(plan['test_rows'], 1)
        self.assertEqual(plan['planned_requests'], 0)
        self.assertEqual(plan['rows'][0]['requests'], [])
        self.assertEqual(plan['rows_without_requests'], 1)

    def test_plan_is_deterministic_and_does_not_process_reserve(self):
        preflight = load()
        test = b'text,label\nA public page,0\n'; reserve = b'text,label\nUnseen reserve page,1\n'
        manifest = held.freeze(test, reserve)
        first = preflight.prepare(test, reserve, manifest)
        self.assertEqual(first, preflight.prepare(test, reserve, manifest))
        self.assertEqual([row['id'] for row in first['rows']], [manifest['test'][0]['id']])
        self.assertFalse(any(row['id'].startswith('reserve:') for row in first['rows']))


if __name__ == '__main__':
    unittest.main()
