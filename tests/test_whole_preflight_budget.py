"""All pre-call I/O shares one deadline; late diagnostics cannot clobber a turn."""
import contextlib
import threading
import time
import unittest
from unittest.mock import patch

from test_plugin_policy_features import load_plugin
from _decide_fakes import TempHome


class WholePreflightBudgetTests(TempHome):
    def exercise(self, boundary):
        p = load_plugin()
        release, entered, finished = threading.Event(), threading.Event(), threading.Event()
        original_feedback = p._feedback_update
        def held(value=None):
            entered.set()
            release.wait(4)
            finished.set()
            return value
        def setting(key, default):
            if boundary == 'settings':
                held()
            return {'skills':'on', 'routing':'off', 'skill_feedback':'on'}.get(key, default)
        def discover(*args, **kwargs):
            if boundary == 'selection':
                held()
            return []
        def feedback(change):
            if boundary == 'feedback_write':
                held()
            return original_feedback(change)
        stack = contextlib.ExitStack()
        with stack:
            for obj, key, value in [
                (p, '_setting', setting), (p, '_skill_roots', lambda: []),
                (p, '_disabled_skills', lambda: set()), (p, '_reachable_skill', lambda n:n),
                (p.skillpick, 'discover', discover),
                (p.skillpick, 'pick', lambda *a, **k: {'status':'ok', 'skills':[{'name':'synthetic','match':.99}]}),
                (p, '_log', lambda entry: held() if boundary == 'logging' else None),
                (p, '_feedback_suppressed', lambda name: held(None) if boundary == 'feedback_read' else None),
                (p, '_feedback_update', feedback),
            ]:
                stack.enter_context(patch.object(obj, key, value))
            start = time.monotonic()
            try:
                result = p._on_pre_llm_call(session_id='synthetic', turn_id=1, user_message='synthetic fixture')
                elapsed = time.monotonic()-start
                # Set a newer same-ID turn before releasing optional bookkeeping.
                newer = {'turn_id':1, 'text':'newer', 'decision':None, 'route_answers':None}
                p._TURNS['synthetic'] = newer
                p._PENDING['synthetic'] = {'skill':'newer','token':'newer'}
            finally:
                release.set()
                if boundary != 'baseline':
                    self.assertTrue(finished.wait(1))
                # Wait for admitted work to settle, not merely its held sub-call.
                for _ in range(100):
                    slots = p.preflight._SLOTS
                    if slots._value == 6:
                        break
                    time.sleep(.01)
                self.assertEqual(p.preflight._SLOTS._value, 6)
            self.assertLess(elapsed, 1.9, boundary)
            if boundary in ('settings','selection','feedback_read'):
                self.assertIsNone(result)
            else:
                self.assertIn('context', result)
            self.assertEqual(p._PENDING['synthetic'], {'skill':'newer','token':'newer'})
            self.assertIs(p._TURNS['synthetic'], newer)
            if boundary != 'baseline':
                self.assertTrue(entered.is_set())

    def test_fast_path_still_suggests(self): self.exercise('baseline')
    def test_settings_cannot_stall_hook(self): self.exercise('settings')
    def test_selection_cannot_stall_hook(self): self.exercise('selection')
    def test_logging_cannot_stall_hook(self): self.exercise('logging')
    def test_feedback_read_cannot_stall_hook(self): self.exercise('feedback_read')
    def test_late_feedback_write_cannot_stall_or_replace_newer_pending(self): self.exercise('feedback_write')

    def test_late_feedback_records_a_load_that_arrived_before_disk_commit(self):
        p = load_plugin()
        current = {'turn_id':1}
        p._TURNS['synthetic'] = current
        entered, release = threading.Event(), threading.Event()
        original = p._feedback_update
        def held(change):
            entered.set()
            release.wait(2)
            return original(change)
        with patch.object(p, '_feedback_update', held):
            worker = threading.Thread(target=lambda: p._feedback_shown('synthetic', 'synthetic', turn_record=current))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                p._on_post_tool_call(tool_name='skill_view', args={'name':'synthetic'}, session_id='synthetic')
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
        entry = p._read(p._feedback_path())['skills']['synthetic']
        self.assertEqual(entry['outcomes'][0][1], 1)
        self.assertNotIn('synthetic', p._PENDING)


if __name__ == '__main__':
    unittest.main()
