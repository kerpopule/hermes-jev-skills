"""Exercise the existing scoped installer in a disposable home, never the live fleet."""
import json
import os
from pathlib import Path
import subprocess
import sys
from _decide_fakes import TempHome


class EvidenceInstallSmoke(TempHome):
    def test_preview_install_and_installed_cli(self):
        repo = Path(__file__).resolve().parents[1]
        config = self.home / 'config.yaml'
        config.write_text('plugins:\n  enabled: []\n', encoding='utf-8')
        before = config.read_bytes()
        child = self.home / 'profiles' / 'sentinel'
        child.mkdir(parents=True)
        (child / 'config.yaml').write_bytes(before)
        command = [sys.executable, str(repo / 'install.py'), '--hermes-root-only',
                   '--hermes-home', str(self.home)]
        preview = subprocess.run(command + ['--check'], capture_output=True, text=True, timeout=30)
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        report = json.loads(preview.stdout)
        self.assertNotIn('warning', report)
        self.assertEqual(report['hermes']['would_enable_in'], [])
        self.assertFalse((self.home / 'plugins').exists())
        installed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        report = json.loads(installed.stdout)
        self.assertNotIn('warning', report)
        self.assertEqual(report['scope'], 'hermes-root-only')
        self.assertEqual(report['hermes']['enabled_in'], {})
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual((child / 'config.yaml').read_bytes(), before)
        self.assertFalse((child / 'plugins').exists())
        plugin = self.home / 'plugins' / 'hermes-jev'
        for name in ['file_evidence.py', 'cli_evidence.py', 'test_triage.py']:
            self.assertEqual((plugin / 'jevkit' / name).read_bytes(), (repo / 'jevkit' / name).read_bytes())
        env = dict(os.environ, PYTHONPATH=str(plugin))
        smoke = subprocess.run([sys.executable, '-m', 'jevkit', 'triage', '--preset', 'test-failure'],
                               input=json.dumps({'error': 'AssertionError: fixture', 'exit_code': 7}),
                               env=env, cwd=str(self.home), capture_output=True, text=True, timeout=15)
        self.assertEqual(smoke.returncode, 1, smoke.stdout + smoke.stderr)
        result = json.loads(smoke.stdout)['results'][0]
        self.assertEqual(result['category'], 'assertion')
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse(result['sent_to_jev'])
