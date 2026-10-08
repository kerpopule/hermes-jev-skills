"""Provider-neutral manifest regression. No network or real secret stores."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jevkit import client, keystore

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'hermes/plugin/hermes-jev/plugin.yaml'


class ProviderManifestTests(unittest.TestCase):
    def test_manifest_does_not_require_one_provider_or_all_four(self):
        text = MANIFEST.read_text(encoding='utf-8')
        self.assertNotIn('\nrequires_env:', text)
        for name in keystore.PROVIDERS:
            self.assertIn(name, text)

    def test_openrouter_only_install_copies_provider_neutral_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'hermes'
            home.mkdir()
            (home / 'config.yaml').write_text('plugins:\n  enabled: []\n')
            env = {'HOME': folder, 'PATH': os.environ.get('PATH', os.defpath),
                   'OPENROUTER_API_KEY': 'offline-fixture'}
            result = subprocess.run([sys.executable, str(ROOT / 'install.py'),
                '--hermes-root-only', '--hermes-home', str(home)], env=env,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = home / 'plugins/hermes-jev/plugin.yaml'
            self.assertEqual(installed.read_bytes(), MANIFEST.read_bytes())
            self.assertNotIn('\nrequires_env:', installed.read_text())
            self.assertFalse((home / '.env').exists())
            self.assertEqual((home / 'config.yaml').read_text(), 'plugins:\n  enabled: []\n')

    def test_no_key_runtime_still_returns_clean_error(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(keystore, 'provider', return_value='typesafe'), \
                patch.object(keystore, 'resolve', return_value=None):
            with self.assertRaises(client.JevError) as caught:
                client.ask('public fixture', {'q': client.noul('Fixture?')},
                           transport=lambda *_: self.fail('must not call transport'))
        self.assertEqual(caught.exception.code, 'no_key')


if __name__ == '__main__':
    unittest.main()
