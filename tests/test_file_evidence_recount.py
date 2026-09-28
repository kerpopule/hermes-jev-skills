"""Offline receipt integrity and summary regressions, not new model validation."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import shutil
import unittest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evals/file-evidence/independent-v2'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


summary = module('summarize_file_evidence_independent')
replay = module('reconstruct_file_evidence_requests')


class RecountTests(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertEqual(summary.percentile([3, 1, 2, 4], .5), 2)
        self.assertEqual(summary.percentile(list(range(1, 21)), .95), 19)
        self.assertIsNone(summary.percentile([], .95))

    def test_incomplete_results_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                summary.summarize(Path(folder))

    def test_duplicate_receipt_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'results'
            shutil.copytree(DATA / 'results', target)
            path = next(target.glob('*-requests.jsonl'))
            text = path.read_text()
            path.write_text(text + text.splitlines()[0] + '\n')
            with self.assertRaisesRegex(ValueError, 'Duplicate receipt'):
                summary.summarize(target)

    def test_missing_response_keeps_reservation_and_unknown_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'results'
            shutil.copytree(DATA / 'results', target)
            path = next(target.glob('*-requests.jsonl'))
            events = [json.loads(line) for line in path.read_text().splitlines()]
            removed = next(r for r in events if r['kind'] == 'response')
            events.remove(removed)
            path.write_text('\n'.join(json.dumps(r) for r in events) + '\n')
            result = summary.summarize(target)
            self.assertEqual(result['provider_total']['requests'], 318)
            self.assertEqual(result['provider_total']['usage_missing'], 1)
            self.assertEqual(result['provider_total']['reported_input_tokens'], 194941-removed['provider_input_tokens'])

    def test_committed_summary_reproduces(self):
        actual = summary.summarize(DATA / 'results')
        self.assertEqual(actual, json.loads((DATA / 'summary.json').read_text()))
        self.assertEqual(actual['provider_total']['requests'], 318)
        self.assertEqual(actual['provider_total']['reported_input_tokens'], 194941)
        self.assertEqual(actual['provider_total']['usage_missing'], 0)
        self.assertTrue(actual['triage_exit_preservation'])
        self.assertEqual(actual['triage']['extended']['accepted_correct'], 40)
        self.assertEqual(actual['triage']['enhanced']['accepted'], 42)
        self.assertLess(actual['scouting']['enhanced_batch']['net_input_tokens_saved_vs_exhaustive'], 0)
        self.assertIsNone(actual['scouting']['enhanced_batch']['break_even_frontier_usd_per_million_vs_bm25_8'])

    def test_every_original_wire_hash_reconstructs_offline(self):
        actual = replay.reconstruct()
        retained = [json.loads(line) for line in (DATA / 'reconstructed-requests.jsonl').read_text().splitlines()]
        self.assertEqual(actual, retained)
        self.assertEqual(len(actual), 318)
        for row in actual:
            body = row['body_utf8'].encode()
            self.assertEqual(hashlib.sha256(body).hexdigest(), row['wire_sha256'])
            self.assertEqual(len(body), row['wire_bytes'])
            self.assertNotIn('Authorization', json.loads(row['body_utf8']))


if __name__ == '__main__':
    unittest.main()
