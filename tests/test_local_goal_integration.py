"""Fresh reconciliation checks through the real chooser/client validator, offline."""
import json
import unittest
from unittest.mock import patch

from jevkit import client, local_goal
from _wire import choice_answer


class LocalGoalIntegrationTests(unittest.TestCase):
    def test_bounded_type_then_click_loop_requires_observed_postcondition(self):
        original = client.ask
        sent = []
        def transport(body, headers, timeout):
            payload = json.loads(body); sent.append(payload)
            answers = {name: choice_answer(q, 'a0', confidence=0.95)
                       for name, q in payload['questions'].items()}
            return json.dumps({'model': 'offline-fixture', 'answers': answers}).encode()
        def ask(state, questions, **kwargs):
            kwargs['transport'] = transport
            return original(state, questions, api_key='offline-fixture', provider='typesafe', **kwargs)
        field = {'id': 'f1', 'name': 'Display name', 'role': 'textbox', 'actions': ['TYPE_TEXT'],
                 'input_type': 'text', 'value': '', 'visible': True, 'enabled': True}
        button = {'id': 'b1', 'name': 'Continue', 'role': 'button', 'actions': ['CLICK'],
                  'visible': True, 'enabled': True}
        actual_completed = False
        with patch.dict('os.environ', {'TYPESAFE_BASE_URL': ''}), patch.object(client, 'ask', new=ask):
            first = local_goal.select('Fill display name then continue', 'o1', [field, button],
                bindings={'Display name': 'name'}, inputs={'name': 'Ada Test'})
            self.assertEqual(first['action'], {'kind': 'TYPE_TEXT', 'target_id': 'f1', 'input_key': 'name'})
            # Owned fixture executor applies the binding, then observes a new revision.
            field['value'] = 'Ada Test'
            second = local_goal.select('Continue', 'o2', [field, button],
                bindings={'Display name': 'name'}, inputs={'name': 'Ada Test'})
            self.assertEqual(second['action'], {'kind': 'CLICK', 'target_id': 'b1'})
            self.assertFalse(actual_completed, 'selection is not an observed effect')
            actual_completed = second['action']['target_id'] == 'b1' and field['value'] == 'Ada Test'
        self.assertTrue(actual_completed)
        self.assertEqual(len(sent), 2)

    def test_malformed_real_client_reply_never_returns_executable_action(self):
        original = client.ask
        def ask(state, questions, **kwargs):
            kwargs['transport'] = lambda *_: b'not json'
            return original(state, questions, api_key='offline-fixture', provider='typesafe', **kwargs)
        element = {'id': 'b1', 'name': 'Continue', 'role': 'button', 'actions': ['CLICK']}
        with patch.dict('os.environ', {'TYPESAFE_BASE_URL': ''}), patch.object(client, 'ask', new=ask):
            self.assertIsNone(local_goal.select('Continue', 'o1', [element])['action'])


if __name__ == '__main__':
    unittest.main()
