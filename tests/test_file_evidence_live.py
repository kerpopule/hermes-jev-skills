"""Regressions promoted from initial live panel A, not fresh heldout."""
import json
from pathlib import Path
from unittest.mock import patch

from _decide_fakes import Scripted, TempHome
from jevkit import file_evidence as ev, test_triage


class LiveFailureRegressions(TempHome):
    def test_non_observed_error_mentions_never_claim_symptom(self):
        cases = [
            'TimeoutError was not raised. The runner vanished.',
            'Expected exception: ModuleNotFoundError. The actual failure is not recorded.',
            'A previous run had MemoryError. This run has no diagnostic.',
            'The assertion mentions the string SyntaxError but no parsing failure occurred.',
        ]
        fake = Scripted({'symptom': 'timeout'})
        for text in cases:
            with self.subTest(text=text):
                out = test_triage.classify({'error': text, 'exit_code': 7,
                    'semantic': True, 'approved_public': True}, transport=fake)
                self.assertEqual(out['category'], 'unknown')
                self.assertEqual(out['exit_code'], 7)
                self.assertFalse(out['proven_root_cause'])
                self.assertFalse(out['sent_to_jev'])
        self.assertEqual(fake.bodies, [])

    def test_normal_observations_and_expected_values_not_blanket_blocked(self):
        for text, label in [('AssertionError: expected ten but got three', 'assertion'),
                            ('TimeoutError: no response arrived', 'timeout'),
                            ('ModuleNotFoundError: absent package', 'dependency')]:
            self.assertEqual(test_triage.classify({'error': text, 'exit_code': 1})['category'], label)

    def test_empty_file_reference_remains_recoverable(self):
        root = self.home.resolve() / 'fixture'
        root.mkdir()
        (root / 'empty.txt').write_text('')
        ref = ev.scout(str(root), ['empty.txt'], 'cache')['evidence'][0]
        self.assertEqual(ev.recover(str(root), ref)['text'], '')

    def test_unknown_failure_outages_preserve_evidence(self):
        state = {'error': 'The returned value differs from the expected value', 'exit_code': 7}
        local = test_triage.classify(state)
        for fake in (Scripted(fail='timeout'), lambda *args: b'not json',
                     Scripted({'symptom': 'assertion'}, confidence=0.4)):
            out = test_triage.classify({**state, 'semantic': True, 'approved_public': True}, transport=fake)
            self.assertEqual(out['category'], 'unknown')
            self.assertEqual(out['evidence'], local['evidence'])
            self.assertEqual(out['exit_code'], 7)
            self.assertTrue(out['test_failed'])

    def test_benchmark_freeze_and_panels_disjoint(self):
        root = Path(__file__).resolve().parents[1]
        import hashlib
        folder = root / 'evals/file-evidence'
        freeze = json.loads((folder / 'live-v1-freeze.json').read_text())
        for kind, name in [('corpus', 'live-v1-corpus.json'), ('protocol', 'live-v1-protocol.md')]:
            self.assertEqual(freeze[kind + '_sha256'], hashlib.sha256((folder / name).read_bytes()).hexdigest())
        panels = json.loads((folder / 'live-v1-corpus.json').read_text())['panels']
        a = {r['text'] for r in panels['A']['failures']}
        b = {r['text'] for r in panels['B']['failures']}
        self.assertFalse(a & b)
        self.assertEqual(len(a), 24)
        self.assertEqual(len(b), 24)
