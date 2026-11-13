"""Offline held-work checks: no provider, owner text, or real state."""
import contextvars
import threading
import time
import unittest

from jevkit import preflight


class PreflightBudgetTests(unittest.TestCase):
    def test_fast_result_and_context_are_preserved(self):
        marker = contextvars.ContextVar('fixture_profile', default='wrong')
        marker.set('synthetic')
        self.assertEqual(preflight.run(lambda: marker.get(), timeout=.2), ('ok', 'synthetic'))

    def test_held_work_returns_before_release_and_late_result_is_discarded(self):
        release = threading.Event()
        finished = threading.Event()
        def held():
            release.wait(2)
            finished.set()
            return 'late'
        start = time.monotonic()
        try:
            self.assertEqual(preflight.run(held, timeout=.03), ('timeout', None))
            self.assertLess(time.monotonic() - start, .5)
            self.assertFalse(finished.is_set())
        finally:
            release.set()
        self.assertTrue(finished.wait(1))

    def test_capacity_is_bounded_and_recovers(self):
        release = threading.Event()
        finished = threading.Event()
        slots = threading.BoundedSemaphore(1)
        def held():
            release.wait(2)
            finished.set()
        try:
            self.assertEqual(preflight.run(held, timeout=.02, slots=slots), ('timeout', None))
            self.assertEqual(preflight.run(lambda: self.fail('must not run'), slots=slots), ('busy', None))
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        # Completion signals just before the worker's finally releases admission.
        for _ in range(100):
            outcome = preflight.run(lambda: 'recovered', slots=slots)
            if outcome[0] != 'busy':
                break
            time.sleep(.001)
        self.assertEqual(outcome, ('ok', 'recovered'))

    def test_default_capacity_is_exactly_six_without_queueing(self):
        release = threading.Event()
        completed = []
        def held():
            release.wait(2)
            completed.append(True)
        try:
            for _ in range(6):
                self.assertEqual(preflight.run(held, timeout=.005), ('timeout', None))
            self.assertEqual(preflight.run(lambda: self.fail('seventh callback ran')), ('busy', None))
        finally:
            release.set()
        for _ in range(200):
            if len(completed) == 6:
                break
            time.sleep(.001)
        self.assertEqual(len(completed), 6)

    def test_errors_do_not_escape_or_export_exception_text(self):
        def broken():
            raise RuntimeError('synthetic-sensitive-text')
        self.assertEqual(preflight.run(broken), ('error', None))


if __name__ == '__main__':
    unittest.main()
