"""Hermetic tests for the plugin's routing middleware.

The plugin module is loaded from the repo and its Jev call is replaced with a spy,
so these tests never touch the network, the real decision log or the real key.
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1] / "hermes" / "plugin" / "hermes-jev"
REPO = Path(__file__).resolve().parents[1]
# The repo keeps jevkit at the root while the installed plugin bundles a copy inside
# its own directory, so the package path has to cover both layouts.
spec = importlib.util.spec_from_file_location(
    "hermes_jev_under_test", PLUGIN / "__init__.py",
    submodule_search_locations=[str(PLUGIN), str(REPO)])
plugin = importlib.util.module_from_spec(spec)
sys.modules["hermes_jev_under_test"] = plugin
spec.loader.exec_module(plugin)

DEFAULT = "deepseek/deepseek-v4.1-flash"
HARD = "The scheduler deadlocks under load. Find the race and propose a fix."


class RoutingMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.decisions = []
        self.logs = []
        plugin._TURNS.clear()

        def fake_decide(prompt, **kwargs):
            self.decisions.append(kwargs)
            if kwargs.get("pinned"):
                return {"routed": False, "model": kwargs.get("current"), "model_id": None,
                        "reason": "you pinned this model"}
            return {"routed": True, "model": "openrouter:moonshotai/kimi-k3",
                    "model_id": "moonshotai/kimi-k3", "reason": "hard coding", "notice": "Jev: kimi-k3"}

        for patch in (
            mock.patch.object(plugin, "_setting", lambda name, default: "on" if name == "routing" else default),
            mock.patch.object(plugin, "_default_model", lambda: DEFAULT),
            mock.patch.object(plugin, "_log", self.logs.append),
            mock.patch.object(plugin.route, "decide", side_effect=fake_decide),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def turn(self, session: str, turn_id: str, model: str):
        plugin._on_pre_llm_call(session_id=session, turn_id=turn_id, user_message=HARD)
        return plugin._on_llm_request(
            request={"messages": [{"role": "user", "content": HARD}], "model": model},
            session_id=session, turn_id=turn_id, model=model, provider="openrouter")

    def test_bare_default_model_is_not_treated_as_pinned(self):
        result = self.turn("s1", "t1", DEFAULT)
        self.assertEqual(self.decisions[0]["current"], f"openrouter:{DEFAULT}")
        self.assertFalse(self.decisions[0]["pinned"])
        self.assertEqual(result["request"]["model"], "moonshotai/kimi-k3")

    def test_prefixed_model_is_not_double_prefixed_and_still_routes(self):
        result = self.turn("s2", "t1", f"openrouter:{DEFAULT}")
        self.assertEqual(self.decisions[0]["current"], f"openrouter:{DEFAULT}")
        self.assertFalse(self.decisions[0]["pinned"])
        self.assertEqual(result["request"]["model"], "moonshotai/kimi-k3")

    def test_a_model_the_user_picked_is_never_overridden(self):
        result = self.turn("s3", "t1", "openrouter:z-ai/glm-5.3-flash")
        self.assertTrue(self.decisions[0]["pinned"])
        self.assertIsNone(result)

    def test_a_prefixed_pin_is_still_recognised_as_a_pin(self):
        result = self.turn("s4", "t1", "openrouter:z-ai/glm-5.3-flash")
        self.assertEqual(self.decisions[0]["current"], "openrouter:z-ai/glm-5.3-flash")
        self.assertIsNone(result)

    def test_off_mode_never_calls_jev(self):
        with mock.patch.object(plugin, "_setting", lambda name, default: "off" if name == "routing" else default):
            result = self.turn("s5", "t1", DEFAULT)
        self.assertIsNone(result)

    def test_a_stale_turn_id_is_ignored(self):
        plugin._on_pre_llm_call(session_id="s6", turn_id="t2", user_message=HARD)
        result = plugin._on_llm_request(
            request={"messages": [{"role": "user", "content": HARD}], "model": DEFAULT},
            session_id="s6", turn_id="t1", model=DEFAULT, provider="openrouter")
        self.assertIsNone(result)


    def test_effective_telemetry_tracks_applied_shadow_pin_and_repeated_requests(self):
        self.turn("live", "t1", DEFAULT)
        entry = [x for x in self.logs if x["kind"] == "route_effective"][-1]
        self.assertEqual((entry["applied"], entry["effective_request_model"], entry["first_request"]),
                         (True, "moonshotai/kimi-k3", True))
        self.assertEqual(entry["requested_model"], DEFAULT)
        self.turn("pinned", "t1", "custom/pin")
        self.assertEqual([x for x in self.logs if x["kind"] == "route_effective"][-1]["effective_request_model"],
                         "custom/pin")
        with mock.patch.object(plugin, "_setting", lambda name, default: "shadow" if name == "routing" else default):
            self.assertIsNone(self.turn("shadow", "t1", DEFAULT))
        shadow = [x for x in self.logs if x["kind"] == "route_effective"][-1]
        self.assertEqual((shadow["applied"], shadow["effective_request_model"], shadow["decision_model"]),
                         (False, DEFAULT, "openrouter:moonshotai/kimi-k3"))
        again = plugin._on_llm_request(request={"model": DEFAULT, "messages": []}, session_id="live",
                                       turn_id="t1", model=DEFAULT, provider="openrouter")
        self.assertEqual(again["request"]["model"], "moonshotai/kimi-k3")
        self.assertFalse([x for x in self.logs if x["kind"] == "route_effective"][-1]["first_request"])
        self.assertEqual(len([x for x in self.logs if x["kind"] == "route" and x.get("from")]), 3)


class EffortMiddlewareTests(unittest.TestCase):
    """The llm_request middleware writes a reasoning-effort level when effort routing is on.

    The level comes from the difficulty answer routing already paid for, so these tests never
    mock Jev: they hand ``route.decide`` the answers it would have received and let the real
    code walk its table. Everything fail-open is asserted — off by default, a missing answer,
    a malformed table — because a wrong effort is worse than no effort at all.
    """

    ANSWERS = {"difficulty": {"type": "score", "score": 3.0, "confidence": 0.95,
                              "probabilities": {0: 0.0, 1: 0.0, 2: 0.1, 3: 0.9}},
               "kind": {"type": "choice", "choice": "coding", "confidence": 0.9, "probabilities": {}},
               "costly_mistake": {"type": "noul", "noul": 0.4}}

    def setUp(self):
        plugin._TURNS.clear()
        self.configs = []

        def fake_load_config(path=None):
            cfg = dict(plugin.route.DEFAULT_CONFIG)
            override = getattr(self, "tiers_override", None)
            cfg["tiers"] = override if override is not None else {"hard": {"coding": ["openrouter:moonshotai/kimi-k3"]}}
            cfg["cache_repeat_asks"] = False
            cfg.update(self.effort_config or {})
            return cfg

        self.effort_config = None
        self.tiers_override = None
        for patch in (
            mock.patch.object(plugin, "_setting", lambda name, default: "on" if name == "routing" else default),
            mock.patch.object(plugin, "_default_model", lambda: DEFAULT),
            mock.patch.object(plugin, "_log", lambda entry: None),
            mock.patch.object(plugin.route, "load_config", side_effect=fake_load_config),
            mock.patch.object(plugin.route, "_by_ref", lambda rows: {
                "openrouter:moonshotai/kimi-k3": {"price": 1.0, "context": 256_000, "vision": False}}),
            mock.patch.object(plugin.route.client, "ask",
                              lambda state, questions, **kw: {"answers": self.ANSWERS, "latency_ms": 5}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def turn(self, session="eff", turn_id="t1", model=DEFAULT, request_extra=None):
        plugin._on_pre_llm_call(session_id=session, turn_id=turn_id, user_message=HARD)
        request = {"messages": [{"role": "user", "content": HARD}], "model": model}
        if request_extra:
            request.update(request_extra)
        return plugin._on_llm_request(request=request, session_id=session, turn_id=turn_id,
                                      model=model, provider="openrouter")

    def test_off_by_default_leaves_the_request_alone(self):
        result = self.turn()
        self.assertIsNotNone(result)  # routing still swapped the model
        self.assertNotIn("reasoning_effort", result["request"])
        self.assertNotIn("extra_body", result["request"])

    def test_enabled_maps_expert_to_xhigh(self):
        self.effort_config = {"effort": {"enabled": True}}
        result = self.turn()
        self.assertEqual(result["request"]["reasoning_effort"], "xhigh")
        self.assertEqual(result["request"]["extra_body"]["reasoning"]["effort"], "xhigh")
        self.assertTrue(result["request"]["extra_body"]["reasoning"]["enabled"])

    def test_a_custom_table_is_honored_end_to_end(self):
        self.effort_config = {"effort": {"enabled": True,
                                          "levels": {"expert": "max"}}}
        result = self.turn()
        self.assertEqual(result["request"]["reasoning_effort"], "max")

    def test_an_existing_extra_body_is_preserved(self):
        self.effort_config = {"effort": {"enabled": True}}
        result = self.turn(request_extra={"extra_body": {"provider": {"order": ["fireworks"]}}})
        self.assertEqual(result["request"]["extra_body"]["provider"], {"order": ["fireworks"]})
        self.assertEqual(result["request"]["extra_body"]["reasoning"]["effort"], "xhigh")

    def test_an_existing_reasoning_block_is_overridden_not_dropped(self):
        self.effort_config = {"effort": {"enabled": True}}
        result = self.turn(request_extra={"extra_body": {"reasoning": {"effort": "low", "max_tokens": 512}}})
        self.assertEqual(result["request"]["extra_body"]["reasoning"]["effort"], "xhigh")
        self.assertEqual(result["request"]["extra_body"]["reasoning"]["max_tokens"], 512)

    def test_off_mode_with_effort_still_applies_effort(self):
        # routing=shadow (no model swap) must not gate effort: the kept model wants its budget.
        self.effort_config = {"effort": {"enabled": True}}
        with mock.patch.object(plugin, "_setting",
                               lambda name, default: "shadow" if name == "routing" else default):
            result = self.turn()
        self.assertIsNotNone(result)
        # shadow mode never swaps the model: the request keeps the model the turn arrived with
        self.assertEqual(result["request"]["model"], DEFAULT)
        self.assertEqual(result["request"]["reasoning_effort"], "xhigh")

    def test_a_malformed_table_fails_open(self):
        self.effort_config = {"effort": {"enabled": True, "levels": ["low", "medium", "high"]}}
        result = self.turn()
        self.assertNotIn("reasoning_effort", result["request"])

    def test_notice_names_the_effort_on_a_routed_turn(self):
        self.effort_config = {"effort": {"enabled": True}}
        self.turn()
        with mock.patch.object(plugin, "_setting", lambda name, default: "on"):
            out = plugin._on_transform_output(response_text="reply", session_id="eff")
        self.assertIn("effort xhigh", out)

    def test_notice_names_the_effort_even_when_the_model_stays(self):
        self.effort_config = {"effort": {"enabled": True}}
        self.turn()
        with mock.patch.object(plugin, "_setting",
                               lambda name, default: "shadow" if name == "routing" else "on"):
            # Re-run the request leg in shadow mode: model stays, effort still applies.
            plugin._on_llm_request(
                request={"messages": [{"role": "user", "content": HARD}], "model": DEFAULT},
                session_id="eff", turn_id="t1", model=DEFAULT, provider="openrouter")
            out = plugin._on_transform_output(response_text="reply", session_id="eff")
        self.assertIn("effort xhigh", out)
        self.assertIn("reply", out)

    def test_no_tiers_still_buys_the_difficulty_for_effort(self):
        # The "model never moves" setup: no pools at all. The model must stay put AND the
        # effort pick must still happen — otherwise effort-only users get nothing.
        self.effort_config = {"effort": {"enabled": True}}
        self.tiers_override = {}
        result = self.turn()
        self.assertIsNotNone(result)
        self.assertEqual(result["request"]["model"], DEFAULT, "no pools: the model stays")
        self.assertEqual(result["request"]["reasoning_effort"], "xhigh")

    def test_a_pinned_model_still_buys_the_difficulty_for_effort(self):
        # /model pins the model, but the effort pick must still happen — the thinking budget
        # belongs on the model the person chose, not on a decision that was never asked.
        self.effort_config = {"effort": {"enabled": True}}
        plugin._on_pre_llm_call(session_id="pin", turn_id="t1", user_message=HARD)
        result = plugin._on_llm_request(
            request={"messages": [{"role": "user", "content": HARD}], "model": "other/model"},
            session_id="pin", turn_id="t1", model="other/model", provider="openrouter")
        self.assertIsNotNone(result)
        self.assertEqual(result["request"]["model"], "other/model", "pinned: the model stays")
        self.assertEqual(result["request"]["reasoning_effort"], "xhigh")


class MergedRequestTests(unittest.TestCase):
    """One request for both decisions, and the three ways it can go.

    The merge is only allowed when routing and skill selection are both on and the turn may
    carry text. Anything else — a private profile, a sensitive turn, features-only routing —
    must fall back to the two calls it had before, and a Jev outage mid-merge must not turn
    into a routing decision nobody made.
    """

    ANSWERS = {"difficulty": {"type": "score", "score": 2.0, "confidence": 0.9, "probabilities": {}},
               "kind": {"type": "choice", "choice": "coding", "confidence": 0.9, "probabilities": {}},
               "costly_mistake": {"type": "noul", "noul": 0.2}}

    def setUp(self):
        plugin._TURNS.clear()
        self.picks = []
        self.merges = []
        self.decisions = []

        def fake_merge(text, skills, **kwargs):
            self.merges.append({"text": text, "skills": skills, **kwargs})
            return self.merge_result

        def fake_pick(text, skills, **kwargs):
            self.picks.append(kwargs)
            return {"status": "ok", "needs_skill": 0.9, "skills": [], "latency_ms": 12}

        def fake_decide(prompt, **kwargs):
            self.decisions.append(kwargs)
            return {"routed": False, "model": kwargs.get("current"), "reason": "kept"}

        self.merge_result = {"status": "ok", "latency_ms": 620, "route_answers": self.ANSWERS,
                             "stage_one": {0: {"S0": 0.9, "none": 0.1}}}
        for patch in (
            mock.patch.object(plugin, "_setting", lambda name, default: "on"),
            mock.patch.object(plugin, "_log", lambda entry: None),
            mock.patch.object(plugin, "_skill_roots", lambda: []),
            mock.patch.object(plugin.skillpick, "discover", lambda roots, **kw: [
                {"name": "a", "description": "d", "path": "p"}]), 
            mock.patch.object(plugin.skillpick, "pick", side_effect=fake_pick),
            mock.patch.object(plugin.turn, "decide_turn", side_effect=fake_merge),
            mock.patch.object(plugin.route, "decide", side_effect=fake_decide),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, session="s-merge", turn_id="t1"):
        plugin._on_pre_llm_call(session_id=session, turn_id=turn_id, user_message=HARD)
        return plugin._on_llm_request(request={"messages": [{"role": "user", "content": HARD}]},
                                      session_id=session, turn_id=turn_id, model=DEFAULT, provider="openrouter")

    def test_the_merged_answers_are_what_routing_acts_on(self):
        self.call()
        self.assertEqual(len(self.merges), 1, "one merged request, not two")
        self.assertEqual(self.picks[0]["stage_one"], self.merge_result["stage_one"],
                         "skill selection must read the merged answer, not buy another one")
        self.assertEqual(self.decisions[0]["answers"], self.ANSWERS,
                         "routing must use the answers the merged request already paid for")

    def test_merging_is_off_when_the_switch_says_so(self):
        with mock.patch.object(plugin, "_setting", lambda name, default: "off" if name == "merge_requests" else "on"):
            self.call()
        self.assertEqual(self.merges, [], "the kill switch must prevent the merged request")
        self.assertEqual(self.decisions[0]["answers"], None)
        self.assertNotIn("stage_one", self.picks[0])

    def test_a_turn_that_cannot_be_merged_keeps_the_two_calls(self):
        self.merge_result = {"status": "not_mergeable", "reason": "profile 'billing' is on the private list"}
        self.call()
        self.assertNotIn("stage_one", self.picks[0], "the old path asks skill selection on its own")
        self.assertEqual(self.decisions[0]["answers"], None, "and routing asks for itself")

    def test_a_failed_merge_does_not_invent_a_skill_and_leaves_routing_its_own_call(self):
        self.merge_result = {"status": "fail_open", "reason": "Jev unavailable (network)"}
        out = self.call()
        self.assertIsNone(out, "no suggestion is made up when the request never answered")
        self.assertEqual(self.picks, [], "skill selection is not re-asked inside the same failure")
        self.assertEqual(self.decisions[0]["answers"], None)
        self.assertEqual(plugin._TURNS["s-merge"]["route_answers"], None)


if __name__ == "__main__":
    unittest.main()
