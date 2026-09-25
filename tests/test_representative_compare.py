"""Paired-evaluation scoring must never silently invent or omit an outcome."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "evals" / "representative" / "compare.py"
spec = importlib.util.spec_from_file_location("representative_compare", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CompareTests(unittest.TestCase):
    def score(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "results.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            return module.compare(path)

    def row(self, arm, **changes):
        return {**{"task_id": "public-fixture", "arm": arm, "completed": False,
                   "latency_ms": 100, "usage_tokens": 20, "spurious_skills": 1,
                   "needed_context_missed": 0}, **changes}

    def test_scores_actual_pairs(self):
        result = self.score([self.row("baseline"), self.row("jev", completed=True,
                            latency_ms=80, usage_tokens=10, spurious_skills=0)])
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["arms"]["jev"]["completed"], 1)
        self.assertEqual(result["arms"]["baseline"]["spurious_skills"], 1)

    def test_no_missing_arm_or_duplicate(self):
        for rows in ([], [self.row("baseline")],
                     [self.row("jev"), self.row("jev"), self.row("baseline")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.score(rows)

    def test_no_fake_unknown_or_negative_metric(self):
        for key, value in (("completed", "unknown"), ("latency_ms", -1),
                           ("usage_tokens", None), ("needed_context_missed", float("inf"))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.score([self.row("baseline"), self.row("jev", **{key: value})])


if __name__ == "__main__":
    unittest.main()
