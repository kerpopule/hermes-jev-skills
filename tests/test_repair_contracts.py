"""Regression contracts: protocol refusal, explicit skill tuning, local-only planning."""
import os
import unittest
from unittest import mock
from copy import deepcopy
from jevkit import skillpick, search
from test_plugin_middleware import plugin


class SkillThresholdTests(unittest.TestCase):
    def test_defaults_and_precedence(self):
        env = {'JEV_PICK_NEED_THRESHOLD': '.6', 'JEV_PICK_MATCH_THRESHOLD': '.7'}
        self.assertEqual(skillpick.resolve_thresholds({}, {}), {'need_threshold': .5, 'match_threshold': .5})
        self.assertEqual(skillpick.resolve_thresholds({}, env), {'need_threshold': .6, 'match_threshold': .7})
        self.assertEqual(skillpick.resolve_thresholds({'need_threshold': .8}, env), {'need_threshold': .8, 'match_threshold': .7})

    def test_bad_values_are_explicit_errors_not_clamped(self):
        for value in (True, None, -1, 2, float('nan'), float('inf'), 'bogus', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                skillpick.resolve_thresholds({'need_threshold': value}, {})
        # A valid setting overrides an invalid environment value without parsing it.
        self.assertEqual(skillpick.resolve_thresholds({'need_threshold': .5}, {'JEV_PICK_NEED_THRESHOLD':'bad'})['need_threshold'], .5)

    def test_direct_pick_honors_environment_before_network(self):
        with mock.patch.dict(os.environ, {'JEV_PICK_NEED_THRESHOLD': '.8','JEV_PICK_MATCH_THRESHOLD':'.9'}), mock.patch.object(skillpick, '_rank', return_value={'skills':[]}) as rank:
            skillpick.pick('Inspect the synthetic repository', [{'name':'code','description':'Code review'}])
        self.assertEqual(rank.call_args.kwargs['need_threshold'], .8)
        self.assertEqual(rank.call_args.kwargs['match_threshold'], .9)


class ProtocolMiddlewareTests(unittest.TestCase):
    def setUp(self):
        plugin._TURNS.clear()
        self.logs=[]
        for p in [mock.patch.object(plugin, '_setting', lambda name, default:'on' if name=='routing' else default),
                  mock.patch.object(plugin, '_default_model', return_value='fixture-current'),
                  mock.patch.object(plugin, '_log', self.logs.append),
                  mock.patch.object(plugin.route, 'decide', return_value={'routed':True,'model':'fixture:fixture-target','model_id':'fixture-target','notice':'Jev: synthetic rejected target'})]:
            p.start();self.addCleanup(p.stop)
        plugin._on_pre_llm_call(session_id='protocol-test', turn_id='1', user_message='Inspect synthetic scheduling code')

    def invoke(self, api_mode='chat_completions'):
        request={'model':'fixture-current','messages':[], 'reasoning_effort':'high'}
        before=deepcopy(request)
        result=plugin._on_llm_request(request=request,model='fixture-current',provider='fixture',session_id='protocol-test',turn_id='1',api_mode=api_mode)
        self.assertEqual(request,before)
        return result

    def test_unknown_capability_retains_request(self):
        # No helper/host availability is assumed by old clients.
        with mock.patch.object(plugin,'_model_api_mode',return_value=None,create=True):
            self.assertIsNone(self.invoke())
        self.assertEqual(self.logs[-1]['compatibility'],'protocol_unknown')

    def test_incompatible_candidate_retains_effort_and_model(self):
        with mock.patch.object(plugin,'_model_api_mode',return_value='codex_responses',create=True):
            self.assertIsNone(self.invoke())
        self.assertEqual(self.logs[-1]['compatibility'],'protocol_incompatible')

    def test_same_protocol_can_route_but_absent_active_wire_cannot(self):
        with mock.patch.object(plugin,'_model_api_mode',return_value='chat_completions',create=True):
            self.assertEqual(self.invoke()['request']['model'],'fixture-target')
            self.assertIsNone(self.invoke(''))

    def test_changed_protocol_cannot_revive_cached_decision(self):
        with mock.patch.object(plugin,'_model_api_mode',return_value='chat_completions',create=True):
            self.assertIsNotNone(self.invoke())
            self.assertIsNone(self.invoke('codex_responses'))
            self.assertIsNone(self.invoke())

    def test_protocol_refusal_cannot_advertise_the_rejected_model(self):
        with mock.patch.object(plugin,'_model_api_mode',return_value='codex_responses'):
            self.assertIsNone(self.invoke())
        with mock.patch.object(plugin,'_setting',return_value='on'):
            self.assertIsNone(plugin._on_transform_output('synthetic answer',session_id='protocol-test'))
        self.assertNotIn('effort',plugin._TURNS['protocol-test'])

    def test_resolver_failure_is_non_mutating(self):
        with mock.patch.object(plugin,'_model_api_mode',side_effect=RuntimeError('synthetic failure'),create=True):
            self.assertIsNone(self.invoke())


class PluginThresholdTests(unittest.TestCase):
    def test_settings_reach_both_merged_and_standalone_pick(self):
        for merged in (False, True):
            with self.subTest(merged=merged), mock.patch.object(plugin, '_CTX') as ctx:
                ctx.get_config.return_value={'need_threshold': .75, 'match_threshold': .85}
                with mock.patch.object(plugin, '_setting', side_effect=lambda name,default: 'on' if name in ('skills','merge_requests') or (name=='routing' and merged) else 'off'), mock.patch.object(plugin, '_skill_roots', return_value=[]), mock.patch.object(plugin, '_disabled_skills', return_value=set()), mock.patch.object(plugin, '_log'), mock.patch.object(plugin.skillpick, 'discover', return_value=[]), mock.patch.object(plugin.skillpick, 'pick', return_value={'skills':[]}) as pick, mock.patch.object(plugin.turn, 'decide_turn', return_value={'status':'ok','route_answers':{},'stage_one':{}}):
                    plugin._on_pre_llm_call(session_id='threshold-test',turn_id='1',user_message='Inspect synthetic source')
                self.assertEqual(pick.call_args.kwargs['need_threshold'], .75)
                self.assertEqual(pick.call_args.kwargs['match_threshold'], .85)

    def test_bad_settings_do_not_buy_a_model_decision(self):
        with mock.patch.object(plugin, '_CTX') as ctx, mock.patch.object(plugin, '_setting', return_value='on'), mock.patch.object(plugin,'_log') as log, mock.patch.object(plugin.turn, 'decide_turn') as ask:
            ctx.get_config.return_value={'need_threshold': float('nan')}
            self.assertIsNone(plugin._on_pre_llm_call(user_message='Inspect synthetic source'))
        ask.assert_not_called()
        self.assertEqual(log.call_args.args[0]['reason_code'],'invalid_threshold_settings')


class LocalPlannerTests(unittest.TestCase):
    def test_multiple_plans_never_send_caller_queries_or_descriptions(self):
        req={'question':'Synthetic research question','sources':[{'id':'docs','description':'Internal synthetic roadmap'}],
             'plans':[{'id':'one','query':'synthetic alpha','sources':['docs'],'time_range':'month'},
                      {'id':'two','query':'synthetic beta','sources':['docs'],'time_range':'year'}]}
        with mock.patch.object(search.client,'ask') as ask:
            result=search.plan_sources(req)
        ask.assert_not_called()
        self.assertFalse(result['jev_called'])
        self.assertIsNone(result['plan'])
        self.assertEqual(result['reason'],'local_planning_required')
