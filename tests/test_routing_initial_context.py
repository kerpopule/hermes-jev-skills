"""Request-context consistency regressions; no network or real configuration."""
import unittest
from unittest import mock
import test_plugin_middleware as helpers

plugin = helpers.plugin

class InitialRequestContext(unittest.TestCase):
    decisions: list
    logs: list
    setUp = helpers.RoutingMiddlewareTests.setUp
    turn = helpers.RoutingMiddlewareTests.turn

    def test_initial_outgoing_model_mismatch_never_routes_or_revives(self):
        for mode in ('on', 'shadow'):
            with self.subTest(mode=mode), mock.patch.object(
                    plugin, '_setting', lambda name, default: mode if name == 'routing' else default):
                plugin._on_pre_llm_call(session_id=mode, turn_id='t1', user_message=helpers.HARD)
                before = len(self.decisions)
                for outgoing in ('user-explicit-model', helpers.DEFAULT):
                    request = {'model': outgoing, 'messages': []}
                    original = dict(request)
                    result = helpers._fixture_request(request=request, model=helpers.DEFAULT,
                        provider='openrouter', session_id=mode, turn_id='t1')
                    self.assertIsNone(result)
                    self.assertEqual(request, original)
                self.assertEqual(len(self.decisions), before)
                self.assertFalse(self.logs[-1]['applied'])
                with mock.patch.object(plugin, '_setting', return_value='on'):
                    self.assertIsNone(plugin._on_transform_output('answer', session_id=mode))
                self.turn(mode, 't2', helpers.DEFAULT)
                self.assertEqual(len(self.decisions), before + 1)

    def test_equivalent_provider_prefix_does_not_block_a_valid_request(self):
        plugin._on_pre_llm_call(session_id='equivalent', turn_id='t1', user_message=helpers.HARD)
        result = helpers._fixture_request(request={'model': helpers.DEFAULT, 'messages': []},
            model='openrouter:' + helpers.DEFAULT, provider='openrouter', session_id='equivalent', turn_id='t1')
        self.assertEqual(result['request']['model'], 'moonshotai/kimi-k3')

if __name__ == '__main__':
    unittest.main()
