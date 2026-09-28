"""Offline contracts for multi-question plumbing, not model performance."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

from _decide_fakes import TempHome, Scripted

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('bench_independent', ROOT / 'scripts/bench_file_evidence_independent.py')
assert spec is not None and spec.loader is not None
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class IndependentRunnerTests(TempHome):
    def test_batched_wire_opaque_ids_and_existing_thresholds(self):
        fake = Scripted({'D0': 'relevant', 'D1': 'tangential'})
        advice, result = bench.batch_advice(['cache eviction clears entries', 'queue accepts work'], 'cache', fake)
        self.assertEqual([row['action'] for row in advice], ['relevant', 'tangential'])
        self.assertEqual(len(fake.bodies), 1)
        self.assertIsNotNone(result)
        body = fake.bodies[0].decode()
        self.assertIn('D0', body)
        self.assertIn('D1', body)
        self.assertNotIn(str(ROOT), body)
        weak = Scripted({'D0': 'relevant'}, confidence=0.84)
        advice, _ = bench.batch_advice(['cache clears entries'], 'cache', weak)
        self.assertEqual(advice[0]['action'], 'unknown')

    def test_batch_withheld_never_enters_other_fields(self):
        fake = Scripted({'D0': 'relevant'})
        advice, _ = bench.batch_advice(['password=fixture', 'cache clears entries', 'path /fixture/file'], 'cache', fake)
        self.assertEqual([row['sent_to_jev'] for row in advice], [False, True, False])
        self.assertNotIn('password', fake.bodies[0].decode())
        self.assertNotIn('/fixture', fake.bodies[0].decode())
        fake = Scripted()
        advice, result = bench.batch_advice(['cache clears entries'], 'customer data', fake)
        self.assertIsNone(result)
        self.assertEqual(fake.bodies, [])
        with self.assertRaises(ValueError):
            bench.batch_advice(['cache'] * 9, 'cache', fake)

    def test_batch_failure_and_drift_abstain(self):
        for fake in [Scripted(fail='timeout'), lambda *a: b'bad json',
                     Scripted({'D0': 'relevant'}, model='jev-future')]:
            advice, _ = bench.batch_advice(['cache clears entries'], 'cache', fake)
            self.assertEqual(advice[0]['action'], 'unknown')

    def test_envelope_counts_metadata_not_just_excerpt(self):
        scout = {'evidence': [{'id': 'E1', 'path': 'a.py'}], 'unknown': [], 'note': 'not exhaustive'}
        item = {'X': {'reference': scout['evidence'][0], 'text': 'cache'}}
        counted = bench.delivery('cache', scout, item, ['X'])
        self.assertGreater(counted['json_bytes'], len('cache'))
        self.assertEqual(counted['frontier_tokens_estimate'], (counted['json_bytes'] + 3) // 4)
