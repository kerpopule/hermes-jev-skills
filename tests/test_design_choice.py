"""Offline contract tests for the bounded design-choice example."""
import importlib
import importlib.util
import json
import unittest
from unittest.mock import patch

from jevkit import client


class DesignChoiceTests(unittest.TestCase):
    def test_offline_default_renders_without_asking_jev(self):
        self.assertIsNotNone(importlib.util.find_spec('jevkit.design_choice'),
                             'bounded design-choice example is missing')
        design = importlib.import_module('jevkit.design_choice')
        result = design.select('product')
        self.assertEqual(result, {'preset': 'product', 'source': 'code', 'reason': 'offline'})
        html = design.render(result['preset'], '<Local title>', 'Local copy')
        self.assertIn('&lt;Local title&gt;', html)
        self.assertIn('Local copy', html)
        self.assertNotIn('<Local title>', html)

    def test_live_pick_uses_validated_client_and_enum_only_state(self):
        from jevkit import design_choice as design
        sent = []
        def transport(body, headers, timeout):
            sent.append(json.loads(body))
            return json.dumps({'answers': {'preset': {
                'type': 'choice', 'choice': 'editorial', 'confidence': 0.95,
                'probabilities': {'editorial': 0.95, 'product': 0.05}}}}).encode()
        with patch.dict('os.environ', {'TYPESAFE_BASE_URL': ''}), \
                patch('jevkit.keystore.resolve', return_value='offline-fixture'), \
                patch('jevkit.keystore.provider', return_value='typesafe'):
            out = design.select('editorial', live=True, transport=transport)
        self.assertEqual(out, {'preset': 'editorial', 'source': 'jev', 'reason': 'selected'})
        self.assertEqual(sent[0]['state'], {'intent': 'editorial'})
        self.assertEqual(set(sent[0]['questions']['preset']['criteria']), set(design.PRESETS))

    def test_render_checks_contrast_and_bounds_before_writing(self):
        from jevkit import design_choice as design
        for preset in design.PRESETS:
            self.assertGreaterEqual(design.contrast(preset), 4.5)
        for title, body in [('x' * 201, 'ok'), ('ok', 'x' * 4001), (None, 'ok')]:
            with self.assertRaises(ValueError):
                design.render('product', title, body)
        with self.assertRaises(ValueError):
            design.render('unknown', 'ok', 'ok')
        with patch.dict(design.PRESETS['product'], {'foreground': '#101827'}):
            with self.assertRaises(ValueError):
                design.render('product', 'ok', 'ok')

    def test_unavailable_uncertain_and_invalid_replies_fall_back(self):
        from jevkit import design_choice as design
        def reply(choice='product', confidence=0.95):
            return json.dumps({'answers': {'preset': {'type': 'choice', 'choice': choice,
                'confidence': confidence, 'probabilities': {}}}}).encode()
        cases = [b'not json', b'{}', reply('invented'), reply(confidence=float('nan')),
                 reply(confidence=0.84)]
        with patch.dict('os.environ', {'TYPESAFE_BASE_URL': ''}), \
                patch('jevkit.keystore.resolve', return_value='offline-fixture'), \
                patch('jevkit.keystore.provider', return_value='typesafe'):
            for raw in cases:
                with self.subTest(raw=raw):
                    out = design.select('editorial', live=True, transport=lambda *_: raw)
                    self.assertEqual(out['preset'], 'editorial')
                    self.assertEqual(out['source'], 'fallback')
            def timeout(*_):
                raise client.JevError('timeout', 'must not be echoed')
            self.assertEqual(design.select('product', live=True, transport=timeout),
                             {'preset': 'product', 'source': 'fallback', 'reason': 'timeout'})
        with patch.dict('os.environ', {'TYPESAFE_BASE_URL': ''}), \
                patch('jevkit.keystore.resolve', return_value=None), \
                patch('jevkit.keystore.provider', return_value='typesafe'):
            self.assertEqual(design.select('product', live=True)['reason'], 'no_key')

    def test_offline_or_invalid_intent_never_resolves_credentials(self):
        from jevkit import design_choice as design
        with patch('jevkit.client.ask', side_effect=AssertionError('must stay local')):
            self.assertEqual(design.select('editorial')['source'], 'code')
            for intent in ['api_key=private', 'arbitrary brief', [], None]:
                with self.assertRaises(ValueError):
                    design.select(intent, live=True)

    def test_cli_creates_html_without_overwriting_existing_file(self):
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'preview.html'
            env = dict(os.environ, TYPESAFE_BASE_URL='')
            cmd = [sys.executable, '-m', 'jevkit.design_choice', '--out', str(out)]
            run = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['source'], 'code')
            self.assertIn('<html lang="en">', out.read_text())
            before = out.read_bytes()
            again = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(out.read_bytes(), before)

    def test_existing_output_is_refused_before_live_provider_call(self):
        import sys
        import tempfile
        from pathlib import Path
        from jevkit import design_choice as design
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'preview.html'
            out.write_text('keep')
            with patch.object(sys, 'argv', ['example', '--live', '--out', str(out)]), \
                    patch.object(design, 'select', side_effect=AssertionError('unexpected provider call')):
                with self.assertRaises(SystemExit) as raised:
                    design.main()
                self.assertEqual(raised.exception.code, 2)
            self.assertEqual(out.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
