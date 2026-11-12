"""Offline public companion regressions; no upstream runtime or credentials needed."""
import pathlib
import shutil
import subprocess
import unittest

class PomeradoCompanionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "optional companion requires Node")
    def test_companion_boundaries(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), "--test", "--test-reporter=tap", "companions/pomerado.test.mjs"], cwd=root, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# fail 0", result.stdout)

    def test_installed_skill_runner_matches_companion(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        self.assertEqual((root / "companions/pomerado.mjs").read_bytes(),
                         (root / "skills/jev-pomerado/scripts/pomerado.mjs").read_bytes())
