"""The vision shadow stays inert until switched on, never waits, never changes a result, never logs content.

* Default install: a vision_analyze call queues nothing.
* `/jev vision shadow`: the hook snapshots and returns at once; the local Jev-Omni run happens on
  the shadow thread (here a fake runner, or a fake subprocess — nothing loads a model).
* The row holds ids, hashes, classes and numbers: never the picture, the path, the question or
  the helper model's answer.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import TempHome  # noqa: E402
from test_plugin_policy_features import FakeContext, load_plugin  # noqa: E402

from jevkit import omni_vision  # noqa: E402

QUESTION = "Strict QA for the knight tile: is the SECRETWORD logo clipped?"
ANSWER = "The tile shows a knight mascot and the SECRETWORD logo. Answer: No, nothing is clipped."
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def aux_result(text: str = ANSWER, success: bool = True) -> str:
    return json.dumps({"success": success, "analysis": text}, indent=2)


class Shapes(unittest.TestCase):
    def test_decision_shapes(self):
        cases = {
            "Final QA. Give PASS or FAIL.": "passfail",
            "Return a clear pass/fail verdict for this cover.": "passfail",
            "Is the logo clipped?": "yesno",
            "Look at the header. Does the menu overlap the title?": "yesno",
            "Confirm whether the QR code is fully visible.": "yesno",
            "Strict visual QA: no clipping, no stray text, knight mascot correct.": "qa",
            "Inspect this screenshot for overlapping controls or cut-off text.": "qa",
            "Transcribe all visible text.": "open",
            "Describe the image in detail.": "open",
            "Give exact screenshot coordinates of the Create button.": "open",
            "Score 1 to 5: facial fidelity, anatomy.": "open",
            "": "open",
        }
        for question, shape in cases.items():
            self.assertEqual(omni_vision.question_shape(question), shape, question)

    def test_the_qa_shape_is_asked_as_pass_fail(self):
        asked = omni_vision.omni_question("Strict QA: no   clipping.", "qa")
        self.assertIn("no clipping.", asked)
        self.assertTrue(asked.endswith("Does the image pass this check?"))
        self.assertEqual(omni_vision.OPTIONS["qa"], ("PASS", "FAIL"))

    def test_the_question_sent_is_bounded(self):
        self.assertLessEqual(len(omni_vision.omni_question("x " * 5000, "yesno")), omni_vision.MAX_QUESTION_CHARS)


class CurrentAnswer(unittest.TestCase):
    def test_classes_from_the_helper_models_prose(self):
        cls = omni_vision.answer_class
        self.assertEqual(cls("Long description... **Verdict: FAIL** because the logo is cut.", "passfail"), "FAIL")
        self.assertEqual(cls("Checks: text PASS, logo FAIL. Overall: PASS", "qa"), "PASS")
        self.assertEqual(cls("Everything looks PASS-worthy: PASS", "qa"), "PASS")
        self.assertEqual(cls("Items: A PASS, B FAIL.", "qa"), "unclear")
        self.assertEqual(cls("It looks fine to me.", "passfail"), "unclear")
        self.assertEqual(cls("Yes. The logo is fully visible.", "yesno"), "Yes")
        self.assertEqual(cls("The image shows a tile... Answer: No, it is not clipped.", "yesno"), "No")
        self.assertEqual(cls("Detailed description. **No** — the menu does not overlap.", "yesno"), "No")
        self.assertEqual(cls("A knight on a blue field.", "yesno"), "unclear")
        self.assertIsNone(cls(None, "yesno"))

    def test_paths(self):
        self.assertEqual(omni_vision.current_answer(aux_result())[0], "aux")
        self.assertEqual(omni_vision.current_answer(aux_result(success=False))[0], "error")
        self.assertEqual(omni_vision.current_answer({"_multimodal": True, "content": []}), ("native", None, 0))
        self.assertEqual(omni_vision.current_answer("not json")[0], "error")


class Base(TempHome):
    def setUp(self):
        super().setUp()
        self.plugin = load_plugin()
        self.ov = self.plugin.omni_vision
        self.image = self.home / "tile.png"
        self.image.write_bytes(PNG)
        self.calls = []

        def fake_runner(items, timeout=None):
            time.sleep(0.2)
            self.calls.append(items)
            return {"load_s": 3.1, "results": [{"id": "1", "prediction": "No", "confidence": 0.93,
                                                 "ms": 950.0, "peak_gb": 13.1}]}

        for name, value in (("run_omni", fake_runner), ("available_gb", lambda: 40.0)):
            patcher = mock.patch.object(self.ov, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def on(self):
        self.plugin.switches.set_mode("vision", "shadow", shared=True)

    def call(self, question=QUESTION, image=None, result=None, **extra):
        return self.plugin._on_post_tool_call(
            tool_name="vision_analyze", args={"image_url": str(image or self.image), "question": question},
            session_id="sess-1", status="ok", result=aux_result() if result is None else result,
            tool_call_id="call-1", turn_id="t1", **extra)

    def rows(self):
        path = self.home / "logs" / "jev-vision.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def settle(self):
        self.assertTrue(self.plugin.shadowq._QUEUE.drain(5))


class Switches(Base):
    def test_off_by_default(self):
        self.assertEqual(self.plugin.switches.mode("vision"), "off")
        with mock.patch.object(self.plugin.shadowq, "submit") as submit:
            self.assertIsNone(self.call())
        submit.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_registration_adds_nothing_new(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        self.assertIn("post_tool_call", ctx.hooks)   # the existing skill-feedback hook carries the seam

    def test_the_kill_switch_wins(self):
        self.on()
        self.plugin.switches.kill_switch("vision").touch()
        self.assertEqual(self.plugin.switches.kill_switch("vision").name, "VISION_OFF")
        with mock.patch.object(self.plugin.shadowq, "submit") as submit:
            self.call()
        submit.assert_not_called()

    def test_a_kill_switch_made_after_queueing_still_stops_the_run(self):
        self.on()
        with mock.patch.object(self.plugin.shadowq, "submit") as submit:
            self.call()
        job, args = submit.call_args[0][0], submit.call_args[0][1:]
        self.plugin.switches.kill_switch("vision").touch()
        job(*args)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows(), [])

    def test_command(self):
        self.assertIn("vision = shadow", self.plugin._jev_command("vision shadow"))
        self.assertIn("vision: shadow", self.plugin._jev_command(""))
        self.assertIn("/jev vision off|shadow", self.plugin._jev_command(""))
        self.assertIn("takes off|shadow", self.plugin._jev_command("vision on"))

    def test_other_tools_are_ignored(self):
        self.on()
        with mock.patch.object(self.plugin.shadowq, "submit") as submit:
            self.plugin._on_post_tool_call(tool_name="browser_vision", args={"question": "x"}, result="{}")
            self.plugin._on_post_tool_call(tool_name="terminal", args={"command": "ls"}, result="{}")
        submit.assert_not_called()


class Shadow(Base):
    def setUp(self):
        super().setUp()
        self.on()

    def test_the_hook_returns_at_once_and_runs_later(self):
        result = aux_result()
        started = time.perf_counter()
        self.assertIsNone(self.call(result=result))
        self.assertLess((time.perf_counter() - started) * 1000, 20)
        self.settle()
        self.assertEqual(len(self.calls), 1)
        item = self.calls[0][0]
        self.assertEqual(item["options"], ["Yes", "No"])
        self.assertEqual(item["image"], str(self.image))
        row = self.rows()[0]
        self.assertEqual((row["kind"], row["status"], row["shape"]), ("vision", "ok", "yesno"))
        self.assertEqual((row["omni_class"], row["current_class"], row["agree"]), ("No", "No", True))
        self.assertTrue(row["handled_locally"])
        self.assertEqual(row["hybrid_class"], "No")
        self.assertEqual(row["image_sha256"], __import__("hashlib").sha256(PNG).hexdigest())
        self.assertEqual(row["current_path"], "aux")
        self.assertEqual(row["tool_call_id"], "call-1")

    def test_the_log_holds_no_content(self):
        self.call()
        self.settle()
        text = (self.home / "logs" / "jev-vision.jsonl").read_text()
        for secret in ("SECRETWORD", "knight", str(self.image), "tile.png", "sess-1", "clipped"):
            self.assertNotIn(secret, text)
        self.assertEqual(oct(os.stat(self.home / "logs" / "jev-vision.jsonl").st_mode & 0o777), "0o600")

    def test_the_ledger_meters_it_at_zero_cost(self):
        self.call()
        self.settle()
        row = [r for r in self.ledger_rows() if r["feature"] == "vision"][0]
        self.assertEqual((row["provider"], row["cost_usd"], row["shadow"], row["source"]), ("local", 0.0, True, "local"))
        self.assertEqual(row["llm_avoided_est"], len(ANSWER) // 4)

    def test_disagreement_and_low_confidence(self):
        self.ov.run_omni = lambda items, timeout=None: {"results": [{"prediction": "Yes", "confidence": 0.55}]}
        self.call()
        self.settle()
        row = self.rows()[0]
        self.assertFalse(row["agree"])
        self.assertFalse(row["handled_locally"])
        self.assertEqual(row["hybrid_class"], "No")   # below the cut-off the current model's answer stands

    def test_the_result_is_never_changed(self):
        native = {"_multimodal": True, "content": [{"type": "text", "text": "loaded"}]}
        before = json.dumps(native)
        self.assertIsNone(self.call(result=native))
        self.settle()
        self.assertEqual(json.dumps(native), before)
        row = self.rows()[0]
        self.assertEqual(row["current_path"], "native")
        self.assertIsNone(row["current_class"])
        self.assertIsNone(row["agree"])
        self.assertEqual(row["status"], "ok")

    def test_open_questions_never_load_the_model(self):
        self.call(question="Transcribe every word on this slide.")
        self.settle()
        self.assertEqual(self.calls, [])
        self.assertEqual((self.rows()[0]["status"], self.rows()[0]["reason"]), ("skipped", "open_question"))

    def test_a_changed_image_is_skipped(self):
        self.call()
        self.image.write_bytes(PNG + b"changed")
        self.settle()
        self.assertEqual(self.rows()[0]["reason"], "image_changed")
        self.assertEqual(self.calls, [])

    def test_unreadable_images_are_skipped(self):
        self.call(image="https://example.com/a.png")
        self.call(image=str(self.home / "gone.png"))
        self.call(image="relative/tile.png")
        self.settle()
        self.assertEqual([r["reason"] for r in self.rows()], ["remote_url", "image_gone", "relative_path"])
        self.assertEqual(self.calls, [])

    def test_a_data_url_is_read_from_a_temp_file_that_is_removed(self):
        seen = []

        def runner(items, timeout=None):
            seen.append(items[0]["image"])
            self.assertTrue(Path(items[0]["image"]).exists())
            return {"results": [{"prediction": "No", "confidence": 0.9}]}

        self.ov.run_omni = runner
        self.call(image="data:image/png;base64," + base64.b64encode(PNG).decode())
        self.settle()
        self.assertEqual(self.rows()[0]["status"], "ok")
        self.assertFalse(Path(seen[0]).exists())

    def test_low_memory_skips(self):
        self.ov.available_gb = lambda: 4.0
        self.call()
        self.settle()
        self.assertEqual(self.rows()[0]["reason"], "low_memory")
        self.assertEqual(self.calls, [])

    def test_runner_failure_is_logged_not_raised(self):
        self.ov.run_omni = lambda items, timeout=None: {"error": "timeout"}
        self.call()
        self.settle()
        row = self.rows()[0]
        self.assertEqual((row["status"], row["error"]), ("error", "timeout"))

    def test_a_broken_observer_never_raises(self):
        with mock.patch.object(self.plugin.shadowq, "submit", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.call())

    def test_busy_when_another_process_holds_the_model(self):
        import fcntl

        lock = self.plugin.switches.jev_dir(True) / "vision-omni.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as held, mock.patch.object(self.ov, "LOCK_WAIT_S", 0.1):
            fcntl.flock(held, fcntl.LOCK_EX)
            self.call()
            self.settle()
        self.assertEqual(self.rows()[0]["reason"], "busy")


class Subprocess(TempHome):
    def test_the_runner_is_called_offline_with_no_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")
            out = json.dumps({"load_s": 3.0, "results": [{"id": "1", "prediction": "PASS", "confidence": 0.8}]})
            fake = subprocess.CompletedProcess([], 0, stdout="mlx warning\n" + out + "\n", stderr="")
            with mock.patch.dict(os.environ, {"JEV_OMNI_DIR": tmp, "OPENROUTER_API_KEY": "should-not-pass"}), \
                    mock.patch.object(omni_vision.subprocess, "run", return_value=fake) as run:
                got = omni_vision.run_omni([{"id": "1"}])
        self.assertEqual(got["results"][0]["prediction"], "PASS")
        cmd, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(cmd[0], str(python))
        self.assertTrue(cmd[1].endswith("omni_runner.py"))
        self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
        self.assertNotIn("OPENROUTER_API_KEY", kwargs["env"])
        self.assertNotIn("TYPESAFE_API_KEY", kwargs["env"])
        self.assertEqual(json.loads(kwargs["input"])["items"], [{"id": "1"}])

    def test_failures_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"JEV_OMNI_DIR": tmp}):
                self.assertEqual(omni_vision.run_omni([]), {"error": "omni_not_installed"})
            python = Path(tmp) / "venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")
            with mock.patch.dict(os.environ, {"JEV_OMNI_DIR": tmp}):
                with mock.patch.object(omni_vision.subprocess, "run",
                                       side_effect=subprocess.TimeoutExpired("x", 1)):
                    self.assertEqual(omni_vision.run_omni([]), {"error": "timeout"})
                with mock.patch.object(omni_vision.subprocess, "run",
                                       return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
                    self.assertEqual(omni_vision.run_omni([]), {"error": "runner_exit_1"})


if __name__ == "__main__":
    unittest.main()
