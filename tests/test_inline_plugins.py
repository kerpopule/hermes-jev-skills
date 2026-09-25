"""Inline plugin lists stay valid and retain pre-existing plugins."""
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer_inline_test", ROOT / "install.py")
assert spec is not None and spec.loader is not None
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class InlinePluginListTests(unittest.TestCase):
    def test_existing_inline_names_survive_enable_and_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.yaml"
            path.write_text("plugins:\n  enabled: ['hermes-lcm', 'other-plugin']\n  disabled: []\n")
            install.enable_plugins(path, ["hermes-jev"], True)
            self.assertEqual(path.read_text(), "plugins:\n  enabled:\n    - hermes-jev\n    - hermes-lcm\n    - other-plugin\n  disabled: []\n")
            install.enable_plugins(path, ["hermes-jev"], False)
            self.assertIn("    - hermes-lcm\n", path.read_text())
            self.assertNotIn("hermes-jev", path.read_text())

    def test_unsupported_yaml_is_rejected_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.yaml"
            original = "plugins:\n  enabled: [hermes-lcm, other-plugin]\n"
            path.write_text(original)
            with self.assertRaisesRegex(ValueError, "unsupported inline"):
                install.enable_plugins(path, ["hermes-jev"], True)
            self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
