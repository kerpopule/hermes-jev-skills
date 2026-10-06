import unittest
from unittest.mock import patch
from jevkit import local_goal


def element(identifier='b1', name='Continue', actions=('CLICK',), **extra):
    return dict(id=identifier, name=name, role='button', actions=actions,
                enabled=True, visible=True, **extra)


class LocalGoalTests(unittest.TestCase):
    def test_empty_request_keeps_only_safe_controls(self):
        req, table = local_goal.build('Continue the workflow', 'o1', [])
        self.assertEqual([c['id'] for c in req['candidates']], ['reobserve', 'abstain'])
        self.assertEqual(table, {})

    def test_consequential_controls_are_not_offered(self):
        req, table = local_goal.build('Continue', 'o1',
                                     [element(name=n, identifier=str(i)) for i,n in enumerate(
                                      ['Publish', 'Buy now', 'Delete', 'Sign in', 'Accept terms', 'Continue'])])
        self.assertEqual(len(table), 1)
        self.assertEqual(next(iter(table.values()))['target_id'], '5')

    def test_model_never_gets_executable_arguments(self):
        req, table=local_goal.build('Continue', 'o1', [element()])
        self.assertEqual(set(req['candidates'][0]), {'id','description'})
        self.assertEqual(table[req['candidates'][0]['id']], {'kind':'CLICK','target_id':'b1'})

    def test_input_binding_is_exact_and_other_field_is_not_typed(self):
        fields=[element('f1','Display name',('CLICK','TYPE_TEXT'),value=''),
                element('f2','Other',('CLICK','TYPE_TEXT'),value=''),element()]
        req,table=local_goal.build('Fill Display name with Ada Test then Continue', 'o1',fields,
                                   bindings={'Display name':'name'},inputs={'name':'Ada Test'})
        self.assertEqual(list(table.values()),[{'kind':'TYPE_TEXT','target_id':'f1','input_key':'name'}])
        self.assertIn('Ada Test',req['candidates'][0]['description'])

    def test_verified_filled_input_is_not_clicked_again(self):
        fields=[element('f1','Display name',('CLICK','TYPE_TEXT'),value='Ada Test'),element()]
        req,table=local_goal.build('Continue','o1',fields,bindings={'Display name':'name'},inputs={'name':'Ada Test'})
        self.assertEqual(list(table.values()),[{'kind':'CLICK','target_id':'b1'}])
        self.assertTrue(any('filled' in r['label'] for r in req['regions']))

    def test_missing_binding_input_refuses(self):
        with self.assertRaises(ValueError):
            local_goal.build('Fill Display name','o1',[],bindings={'Display name':'name'},inputs={})

    def test_sensitive_input_refuses_before_chooser(self):
        with self.assertRaises(ValueError):
            local_goal.build('Fill field','o1',[],bindings={'Field':'value'},inputs={'value':'password=private'})

    def test_duplicate_element_ids_refuse(self):
        with self.assertRaises(ValueError):
            local_goal.build('Continue','o1',[element(),element()])

    def test_disabled_or_hidden_controls_never_offered(self):
        one=element();one['visible']=False
        two=element('b2');two['enabled']=False
        self.assertEqual(local_goal.build('Continue','o1',[one,two])[1],{})

    def test_candidate_overflow_refuses_instead_of_silent_truncation(self):
        with self.assertRaises(ValueError):
            local_goal.build('Continue','o1',[element(str(i)) for i in range(31)])

    def test_abstention_is_no_action(self):
        with patch('jevkit.local_goal.choose', return_value={'selected_id':'abstain','reason':'blocked'}):
            result=local_goal.select('Continue','o1',[element()])
        self.assertIsNone(result['action'])

    def test_unknown_selection_is_no_action(self):
        with patch('jevkit.local_goal.choose', return_value={'selected_id':'invented','reason':'bad'}):
            result=local_goal.select('Continue','o1',[element()])
        self.assertIsNone(result['action'])

    def test_live_observation_id_is_carried_to_choice(self):
        with patch('jevkit.local_goal.choose',return_value={'selected_id':'a0','confidence':.99,'observation_id':'fresh-2'}) as pick:
            result=local_goal.select('Continue','fresh-2',[element()])
        self.assertEqual(pick.call_args.args[0]['observation_id'],'fresh-2')
        self.assertEqual(result['action'],{'kind':'CLICK','target_id':'b1'})


class LocalGoalDefenseTests(unittest.TestCase):
    def test_stale_model_result_has_no_action(self):
        with patch('jevkit.local_goal.choose',return_value={'selected_id':'a0','confidence':.99,'observation_id':'old'}):
            self.assertIsNone(local_goal.select('Continue','r1',[element()])['action'])

    def test_low_confidence_and_nonfinite_results_have_no_action(self):
        for confidence in (.64,True,float('nan'),float('inf')):
            with patch('jevkit.local_goal.choose',return_value={'selected_id':'a0','confidence':confidence,'observation_id':'r1'}):
                self.assertIsNone(local_goal.select('Continue','r1',[element()])['action'])

    def test_unicode_consequential_label_is_not_offered(self):
        request,table=local_goal.build('Continue','r1',[element(name='Ｐｕｂｌｉｓｈ')])
        self.assertFalse(table)

    def test_ambiguous_bound_labels_are_refused(self):
        fields=[element('x','Display name',('TYPE_TEXT',)),element('y','Display name',('TYPE_TEXT',))]
        with self.assertRaises(ValueError):
            local_goal.build('Fill','r1',fields,bindings={'Display name':'name'},inputs={'name':'Ada Test'})

    def test_contact_input_is_refused(self):
        with self.assertRaises(ValueError):
            local_goal.build('Fill','r1',[element()],bindings={'Display name':'name'},inputs={'name':'test@example.invalid'})

    def test_secure_role_is_refused(self):
        field=element();field['role']='password'
        with self.assertRaises(ValueError):
            local_goal.build('Continue','r1',[field])

    def test_private_history_is_refused_before_model(self):
        with patch('jevkit.local_goal.choose') as chooser:
            with self.assertRaises(ValueError):
                local_goal.select('Continue','r1',[element()],history=[{'selected_id':'a0','outcome':'test@example.invalid'}])
            chooser.assert_not_called()


if __name__=='__main__':unittest.main()
