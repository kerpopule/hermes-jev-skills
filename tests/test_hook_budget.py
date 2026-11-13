"""Exercise the actual hook with held dependency work and isolated feedback."""
import functools
import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('jev_budget_hook_fixture',
    ROOT / 'hermes/plugin/hermes-jev/__init__.py',
    submodule_search_locations=[str(ROOT / 'hermes/plugin/hermes-jev'), str(ROOT)])
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)


class HookBudgetTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.patchers = [
            mock.patch.object(plugin, '_setting', side_effect=lambda n, d: 'off' if n == 'skill_feedback' else 'on'),
            mock.patch.object(plugin, '_skill_roots', return_value=[]),
            mock.patch.object(plugin, '_disabled_skills', return_value=set()),
            mock.patch.object(plugin.skillpick, 'discover', return_value=[]),
            mock.patch.object(plugin, '_log', side_effect=self.logs.append),
            mock.patch.object(plugin, '_reachable_skill', return_value='synthetic-skill'),
            mock.patch.object(plugin.preflight, 'run',
                side_effect=functools.partial(plugin.preflight.run, timeout=.03)),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        plugin._TURNS.clear()
        plugin._LOADED.clear()

    def test_fast_merged_success_preserves_suggestion_and_route_answers(self):
        merged = {'status': 'ok', 'route_answers': {'fixture': 1}, 'stage_one': {}}
        picked = {'status': 'ok', 'skills': [{'name': 'synthetic-skill', 'match': .8}]}
        with mock.patch.object(plugin.turn, 'decide_turn', return_value=merged), \
             mock.patch.object(plugin.skillpick, 'pick', return_value=picked):
            result = plugin._on_pre_llm_call(session_id='fixture', turn_id='first', user_message='synthetic task')
        self.assertIn('synthetic-skill', result['context'])
        self.assertEqual(plugin._TURNS['fixture']['route_answers'], {'fixture': 1})

    def test_timeout_never_applies_late_route_or_feedback(self):
        release, finished = threading.Event(), threading.Event()
        def held(*args, **kwargs):
            release.wait(2)
            finished.set()
            return {'status': 'fail_open'}
        with mock.patch.object(plugin.turn, 'decide_turn', side_effect=held), \
             mock.patch.object(plugin, '_feedback_shown') as shown:
            try:
                start = time.monotonic()
                self.assertIsNone(plugin._on_pre_llm_call(session_id='fixture', turn_id='old', user_message='synthetic task'))
                self.assertLess(time.monotonic() - start, .5)
                plugin._TURNS['fixture'] = {'turn_id': 'new', 'route_answers': None}
            finally:
                release.set()
            self.assertTrue(finished.wait(1))
            shown.assert_not_called()
        self.assertEqual(plugin._TURNS['fixture'], {'turn_id': 'new', 'route_answers': None})
        self.assertEqual(self.logs, [{'kind': 'preflight', 'status': 'fail_open', 'reason_code': 'timeout'}])

    @unittest.skipIf(sys.platform == 'win32', 'fcntl lock semantics')
    def test_feedback_lock_contention_does_not_wait(self):
        import fcntl
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'feedback.json'
            with mock.patch.object(plugin, '_feedback_path', return_value=path):
                with open(str(path) + '.lock', 'a') as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    start = time.monotonic()
                    self.assertIsNone(plugin._feedback_update(lambda state, now: True))
                    self.assertLess(time.monotonic() - start, .5)
                    self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
