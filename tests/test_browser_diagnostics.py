"""Offline executor fixtures; no browser, credentials or paid model calls."""
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/jev-browser-use/scripts/jev_browser_agent.py'
spec = importlib.util.spec_from_file_location('diagnostic_runner', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ProgressTests(unittest.TestCase):
    def test_checkbox_state_change_counts_without_navigation(self):
        progress = runner.BrowserProgress(max_stagnant=3)
        page = {'url': 'https://example.com', 'text': 'Settings', 'controls': [{'checked': False}]}
        progress.observe(page, proposed={'choice': 'a'}, executed=False)
        page['controls'][0]['checked'] = True
        row = progress.observe(page, proposed={'choice': 'a', 'confidence': .9}, executed=True)
        self.assertTrue(row['progress'])
        self.assertTrue(row['executed'])
        self.assertEqual(progress.stagnant, 0)
        self.assertNotIn('Settings', str(progress.trace))


class FakeAgent:
    def __init__(self, choices, *, change=False, fail=False, url='https://example.com'):
        self.choices = iter(choices)
        self.change = change
        self.fail = fail
        self.closed = False
        self.browser = self
        self.state = {'page': {'url': url, 'fingerprint': 'a'}, 'history': [],
                      'text_calls': [], 'status': 'ready'}
        self.controls = False

    def evaluate(self, script):
        if script == runner.SEMANTIC_PAGE:
            return {'url': self.state['page']['url'], 'text': 'Settings',
                    'controls': [{'checked': self.controls}]}
        return 'Settings'

    def command(self, name, body=None):
        if name == 'predict':
            self.state['decision'] = {'choice': next(self.choices), 'confidence': .9}
        else:
            if self.fail:
                raise RuntimeError('synthetic execution failure')
            choice = self.state['decision']['choice']
            if choice == 'DONE':
                self.state['status'] = 'done'
            elif choice == 'BLOCKED':
                self.state['status'] = 'blocked'
            else:
                self.controls = not self.controls if self.change else self.controls
                self.state['history'].append({'kind': 'click'})


class RunTests(unittest.TestCase):
    def run_fixture(self, agent, ticks=5):
        return runner.run_agent(agent, allow=['example.com'], expect='Settings', max_ticks=ticks)

    def test_model_done_without_page_match_is_not_success(self):
        result = runner.run_agent(FakeAgent(['DONE']), allow=['example.com'], expect='Not present', max_ticks=2)
        self.assertFalse(result['verified'])
        self.assertEqual(result['outcome'], 'degraded')

    def test_executed_checkboxes_advance_and_done_is_verified(self):
        result = self.run_fixture(FakeAgent(['click', 'click', 'DONE'], change=True))
        self.assertTrue(result['verified'])
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual([r['executed'] for r in result['trace']], [False, True, True, False])
        self.assertTrue(result['trace'][1]['progress'])

    def test_no_progress_stops_before_budget_even_if_title_matches(self):
        result = self.run_fixture(FakeAgent(['click'] * 5))
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(result['stop_reason'], 'no_progress')
        self.assertEqual(result['ticks'], 3)
        self.assertFalse(result['verified'])

    def test_execution_error_preserves_proposal_not_false_execution(self):
        result = self.run_fixture(FakeAgent(['click'], fail=True))
        self.assertEqual(result['outcome'], 'degraded')
        self.assertTrue(result['trace'][-1]['proposed'])
        self.assertFalse(result['trace'][-1]['executed'])
        self.assertFalse(result['verified'])

    def test_execution_receipt_survives_failed_post_action_observation(self):
        agent = FakeAgent(['click'])
        evaluate = agent.evaluate
        def fail_after_execution(script):
            if script == runner.SEMANTIC_PAGE and agent.state['history']:
                raise RuntimeError('synthetic DOM unavailable')
            return evaluate(script)
        agent.evaluate = fail_after_execution
        result = self.run_fixture(agent)
        self.assertEqual(result['outcome'], 'degraded')
        self.assertTrue(result['trace'][-1]['executed'])
        self.assertIsNone(result['trace'][-1]['observation_sha256'])
        self.assertIsNone(result['trace'][-1]['progress'])
        self.assertFalse(result['verified'])

    def test_allowlist_denial_never_predicts(self):
        result = self.run_fixture(FakeAgent([], url='https://other.example'))
        self.assertTrue(result['left_allowlist'])
        self.assertEqual(result['ticks'], 0)
        self.assertFalse(result['verified'])

    def test_vendor_stagnation_can_be_corrected_but_explicit_block_cannot(self):
        agent = FakeAgent(['click', 'click', 'click', 'DONE'], change=True)
        command = agent.command
        def vendor_stagnation(name, body=None):
            command(name, body)
            if (name == 'act' and len(agent.state['history']) == 3
                    and agent.state['decision']['choice'] not in ('DONE', 'BLOCKED')):
                agent.state['status'] = 'blocked'
                for row in agent.state['history']:
                    row['page_changed'] = False
        agent.command = vendor_stagnation
        self.assertTrue(self.run_fixture(agent)['verified'])
        blocked = self.run_fixture(FakeAgent(['BLOCKED']))
        self.assertEqual(blocked['stop_reason'], 'agent_blocked')
        self.assertFalse(blocked['verified'])

    def test_budget_is_hard_and_invalid_limits_are_rejected(self):
        result = self.run_fixture(FakeAgent(['click'] * 5, change=True), ticks=2)
        self.assertEqual(result['ticks'], 2)
        self.assertEqual(result['stop_reason'], 'tick_budget')
        with self.assertRaises(ValueError):
            self.run_fixture(FakeAgent([]), ticks=0)


if __name__ == '__main__':
    unittest.main()
