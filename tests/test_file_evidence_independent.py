"""Offline validation provenance/contracts; never measure model accuracy in unit tests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evals/file-evidence/independent-v2'
spec = importlib.util.spec_from_file_location('comparators', ROOT / 'scripts/file_evidence_comparators.py')
assert spec is not None and spec.loader is not None
comparators = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparators)


class IndependentCorpusTests(unittest.TestCase):
    def test_frozen_manifest_hashes_and_counts(self):
        freeze = json.loads((DATA / 'freeze.json').read_text())
        for name, digest in freeze['sha256'].items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest, name)
        corpus = json.loads((DATA / 'corpus.json').read_text())
        self.assertEqual(len(corpus['files']), 32)
        self.assertEqual(len(corpus['scouting']), 8)
        self.assertEqual(len(corpus['failures']), 64)
        names = {row['id'] for row in corpus['files']}
        self.assertEqual(len(names), 32)
        self.assertEqual(len({row['id'] for row in corpus['failures']}), 64)
        for row in corpus['files']:
            self.assertEqual(hashlib.sha256(row['text'].encode()).hexdigest(), row['sha256'])
            self.assertGreater(row['source_end_line'], row['source_start_line'])
        for row in corpus['scouting']:
            self.assertEqual(len(row['relevant']), 2)
            self.assertTrue(set(row['critical']) <= names)
        self.assertEqual(sum('fixture_code' in row for row in corpus['failures']), 24)
        self.assertNotIn(str(Path.home()), (DATA / 'corpus.json').read_text())

    def test_executed_fixtures_actually_fail_without_network(self):
        corpus = json.loads((DATA / 'corpus.json').read_text())
        for row in corpus['failures']:
            if 'fixture_code' not in row:
                continue
            p = subprocess.run([sys.executable, '-I', '-S', '-c', row['fixture_code']],
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(p.returncode, row['exit_code'], row['id'])
            self.assertTrue(p.stderr, row['id'])

    def test_bm25_tokenization_and_stable_ties(self):
        files = [{'id': 'B', 'path': 'redBlue.py', 'text': 'irrelevant'},
                 {'id': 'A', 'path': 'red_blue.py', 'text': 'irrelevant'},
                 {'id': 'C', 'path': 'other.py', 'text': 'green'}]
        self.assertEqual(comparators.bm25(files, 'red blue'), ['A', 'B'])
        self.assertEqual(comparators.bm25(list(reversed(files)), 'red blue'), ['A', 'B'])
        self.assertEqual(comparators.bm25(files, 'nothing'), [])
        self.assertEqual(comparators.bm25(files, 'red', top_k=1), ['A'])

    def test_extended_comparator_preserves_guards_and_conflicts(self):
        base = {'category': 'unknown', 'test_failed': True}
        self.assertEqual(comparators.extended_failure('EAI_AGAIN', base), 'network')
        self.assertEqual(comparators.extended_failure('EAI_AGAIN', {**base, 'reason': 'negation'}), 'unknown')
        self.assertEqual(comparators.extended_failure('EAI_AGAIN', {**base, 'test_failed': False}), 'unknown')
        self.assertEqual(comparators.extended_failure('EAI_AGAIN OutOfMemoryError', base), 'unknown')
        self.assertEqual(comparators.extended_failure('EAI_AGAIN', {**base, 'category': 'assertion'}), 'unknown')


if __name__ == '__main__':
    unittest.main()
