import json
from pathlib import Path
import subprocess
import sys
import unittest


class OfflineWorkflowDemoTests(unittest.TestCase):
    def test_documented_command_shows_selection_and_fallback(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, '-m', 'scripts.offline_workflow_demo'], cwd=root,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['model_calls'], 0)
        self.assertEqual(output['source'], 'canned responses')
        self.assertEqual(output['accepted']['preset'], 'editorial')
        self.assertEqual(output['fallback'], {
            'preset': 'product', 'source': 'fallback', 'reason': 'uncertain',
        })
