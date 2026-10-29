"""Provider failure must never become permission for a protected action."""
import unittest
from jevkit import gate


class FailureBoundaryTests(unittest.TestCase):
    def test_even_an_approve_label_cannot_overrule_explicit_failure(self):
        for failure in ({'fallback_used': True}, {'error': 'timeout'}, {'drift': True}):
            with self.subTest(failure=failure):
                decision = {'action': 'approve', 'policy': 'gate-strict@1#fixture', **failure}
                self.assertEqual(gate.tiebreak_verdict(decision), 'escalate')

    def test_deny_is_not_weakened_by_failed_provider_metadata(self):
        self.assertEqual(gate.tiebreak_verdict({'action': 'deny', 'error': 'timeout'}), 'deny')

    def test_advisory_approve_and_no_opinion_do_not_bypass_host_gate(self):
        for mode in ('off', 'shadow', 'ask', 'block', 'tiebreak'):
            for action in ('approve', 'no_opinion'):
                self.assertIsNone(gate.hook_result({'action': action}, mode))


if __name__ == '__main__':
    unittest.main()
