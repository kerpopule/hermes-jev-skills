"""The plugin's new seams stay inert until switched on, and never wait on Jev when they are.

* Default install: no approval hooks, no policy tools — a gateway restart changes nothing.
* `/jev gate shadow` before a start: two observer hooks that queue work and return at once.
* The gate log holds hashes, ids and probabilities: never the command.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _decide_fakes import TempHome  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1] / "hermes" / "plugin" / "hermes-jev"
REPO = Path(__file__).resolve().parents[1]


def load_plugin():
    name = "hermes_jev_policy_under_test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN), str(REPO)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.tools, self.hooks, self.commands = {}, {}, {}

    def register_tool(self, name, toolset, handler, schema):
        self.tools[name] = handler

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_middleware(self, name, fn):
        pass

    def register_command(self, name, fn, **_):
        self.commands[name] = fn

    def register_system_prompt_section(self, *args, **kwargs):
        pass

    def get_config(self, name, default=None):
        return default


class Registration(TempHome):
    def test_nothing_new_is_registered_by_default(self):
        plugin = load_plugin()
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertNotIn("pre_approval_request", ctx.hooks)
        self.assertNotIn("post_approval_response", ctx.hooks)
        for tool in ("jev_decide", "jev_score", "jev_route_to"):
            self.assertNotIn(tool, ctx.tools)
        self.assertIn("jev_memory_filter", ctx.tools)   # the existing tools are untouched

    def test_switches_register_the_hooks_and_tools(self):
        plugin = load_plugin()
        plugin.switches.set_mode("gate", "shadow", shared=True)
        plugin.switches.set_mode("decide_tools", "on", shared=True)
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertIn("pre_approval_request", ctx.hooks)
        self.assertIn("post_approval_response", ctx.hooks)
        self.assertIn("jev_decide", ctx.tools)

    def test_the_kill_switch_keeps_the_hooks_away(self):
        plugin = load_plugin()
        plugin.switches.set_mode("gate", "shadow", shared=True)
        plugin.switches.kill_switch("gate").touch()
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertNotIn("pre_approval_request", ctx.hooks)


class Observers(TempHome):
    def setUp(self):
        super().setUp()
        self.plugin = load_plugin()
        self.plugin.switches.set_mode("gate", "shadow", shared=True)
        self.plugin._GATE_SEEN.clear()
        self.calls = []

        def slow_check(tool, **kwargs):
            time.sleep(0.2)
            self.calls.append(kwargs)
            return {"action": "ask_human", "policy": "gate-strict@1#test", "answers": {"risky": {"kind": "noul", "p": 0.5}},
                    "status": "ok", "command_sha256": "abc", "latency_ms": 200}

        patcher = mock.patch.object(self.plugin.gate, "check", side_effect=slow_check)
        patcher.start()
        self.addCleanup(patcher.stop)

    def log_rows(self):
        path = self.home / "logs" / "jev-gate.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_the_pre_hook_returns_at_once_and_checks_later(self):
        started = time.perf_counter()
        result = self.plugin._on_pre_approval_request(command="make deploy-preview", pattern_key="deploy",
                                                      pattern_keys=["deploy"], session_key="s1", surface="smart",
                                                      tool_call_id="c1")
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.assertIsNone(result)
        self.assertLess(elapsed_ms, 20)
        self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["mode"], "shadow")
        rows = self.log_rows()
        self.assertEqual(rows[0]["kind"], "gate")
        self.assertNotIn("deploy-preview", json.dumps(rows))

    def test_one_command_is_checked_once(self):
        for surface in ("smart", "gateway"):
            self.plugin._on_pre_approval_request(command="make deploy-preview", session_key="s1", surface=surface,
                                                 tool_call_id="c1")
        self.plugin._on_pre_approval_request(command="make deploy-preview", session_key="s1", surface="gateway",
                                             tool_call_id="c1", coalesced=True)
        self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        self.assertEqual(len(self.calls), 1)

    def test_the_outcome_is_logged_without_the_command(self):
        self.plugin._on_post_approval_response(command="make deploy-preview", choice="deny", session_key="s1",
                                               surface="gateway", tool_call_id="c1")
        self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        row = self.log_rows()[0]
        self.assertEqual(row["kind"], "gate_outcome")
        self.assertEqual(row["truth"], "deny")
        self.assertEqual(row["decided_by"], "person")
        self.assertNotIn("deploy-preview", json.dumps(row))

    def test_switched_off_the_hooks_do_nothing(self):
        self.plugin.switches.kill_switch("gate").touch()
        self.plugin._on_pre_approval_request(command="make x", session_key="s2", tool_call_id="c2")
        self.plugin._on_post_approval_response(command="make x", choice="once", session_key="s2")
        self.assertTrue(self.plugin.shadowq._QUEUE.drain(3))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.log_rows(), [])

    def test_a_broken_observer_never_raises(self):
        with mock.patch.object(self.plugin.shadowq, "submit", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.plugin._on_pre_approval_request(command="make x", session_key="s3"))
            self.assertIsNone(self.plugin._on_post_approval_response(command="make x", choice="once"))


class JevCommand(TempHome):
    def test_set_and_show(self):
        plugin = load_plugin()
        reply = plugin._jev_command("gate shadow")
        self.assertIn("gate = shadow", reply)
        self.assertEqual(plugin.switches.mode("gate"), "shadow")
        self.assertIn("gate off|shadow", plugin._jev_command(""))
        self.assertIn("takes", plugin._jev_command("gate on"))
        self.assertEqual(plugin.switches.mode("gate"), "shadow")
        plugin._jev_command("decide_tools on all")
        self.assertEqual(plugin.switches.mode("decide_tools"), "on")

    def test_the_old_switches_still_work(self):
        plugin = load_plugin()
        self.assertIn("routing = shadow", plugin._jev_command("routing shadow"))


if __name__ == "__main__":
    unittest.main()
