"""Hermetic tests for the plugin's routing middleware.

The plugin module is loaded from the repo and its Jev call is replaced with a spy,
so these tests never touch the network, the real decision log or the real key.
"""
import importlib.util
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
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

    def test_custom_endpoint_bare_id_matches_vendor_qualified_default(self):
        plugin._on_pre_llm_call(session_id="custom-default", turn_id="t1", user_message=HARD)
        with mock.patch.object(plugin.route, "load_config", return_value={"provider_aliases": {"custom": "tokenharbor"}}):
            plugin._on_llm_request(request={"model": "deepseek-v4.1-flash", "messages": []},
                                   session_id="custom-default", turn_id="t1",
                                   model="custom:deepseek-v4.1-flash", provider="custom")
        self.assertEqual(self.decisions[0]["current"], "tokenharbor:deepseek-v4.1-flash")
        self.assertFalse(self.decisions[0]["pinned"])

    def test_vendor_qualified_different_vendor_remains_pinned(self):
        self.turn("different-vendor", "t1", "other/deepseek-v4.1-flash")
        self.assertTrue(self.decisions[0]["pinned"])

    def test_different_bare_id_remains_pinned_on_custom_endpoint(self):
        plugin._on_pre_llm_call(session_id="custom-pin", turn_id="t1", user_message=HARD)
        with mock.patch.object(plugin.route, "load_config", return_value={"provider_aliases": {"custom": "tokenharbor"}}):
            plugin._on_llm_request(request={"model": "another-model", "messages": []},
                                   session_id="custom-pin", turn_id="t1",
                                   model="another-model", provider="custom")
        self.assertTrue(self.decisions[0]["pinned"])

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

    def test_cached_decision_cannot_cross_provider_endpoint_or_model(self):
        for changed in ({"provider": "custom"}, {"base_url": "http://other/v1"},
                        {"model": "user-pin"}, {"request_model": "user-pin"}):
            with self.subTest(changed=changed):
                self.turn("binding", "t1", DEFAULT)
                args = {"provider": "openrouter", "model": DEFAULT, "base_url": ""}
                args.update({k: v for k, v in changed.items() if k != "request_model"})
                request = {"model": changed.get("request_model", DEFAULT), "messages": []}
                original = dict(request)
                before = len(self.decisions)
                self.assertIsNone(plugin._on_llm_request(
                    request=request, session_id="binding", turn_id="t1", **args))
                self.assertEqual(request, original)
                self.assertEqual(len(self.decisions), before)
                self.assertEqual(self.logs[-1]["compatibility"], "request_context_changed")

    def test_unchanged_model_is_not_counted_as_applied(self):
        with mock.patch.object(plugin.route, "decide", return_value={
                "routed": True, "model": f"openrouter:{DEFAULT}", "model_id": DEFAULT}):
            self.assertIsNone(self.turn("same", "t1", DEFAULT))
        self.assertFalse(self.logs[-1]["applied"])

    def test_context_change_invalidates_routing_until_next_turn(self):
        for mode in ("on", "shadow"):
            for changed in ({"provider": "custom"}, {"base_url": "http://other/v1"},
                            {"model": "user-pin"}, {"request_model": "user-pin"}):
                with self.subTest(mode=mode, changed=changed), mock.patch.object(
                        plugin, "_setting", lambda name, default: mode if name == "routing" else default):
                    self.turn("sticky", "t1", DEFAULT)
                    plugin._TURNS["sticky"]["effort"] = "high"
                    before = len(self.decisions)
                    changed_args = {"provider": "openrouter", "model": DEFAULT, "base_url": ""}
                    changed_args.update({k: v for k, v in changed.items() if k != "request_model"})
                    for args, request_model in [
                        (changed_args, changed.get("request_model", DEFAULT)),
                        ({"provider": "openrouter", "model": DEFAULT}, DEFAULT),
                        (changed_args, changed.get("request_model", DEFAULT)),
                    ]:
                        request = {"model": request_model, "messages": []}
                        original = dict(request)
                        self.assertIsNone(plugin._on_llm_request(
                            request=request, session_id="sticky", turn_id="t1", **args))
                        self.assertEqual(request, original)
                        self.assertFalse(self.logs[-1]["applied"])
                    self.assertEqual(len(self.decisions), before)
                    self.assertNotIn("effort", plugin._TURNS["sticky"])
                    with mock.patch.object(plugin, "_setting", return_value="on"):
                        self.assertIsNone(plugin._on_transform_output("answer", session_id="sticky"))
                    # Invalidating one session must not poison another, or the next turn.
                    self.turn("other-session", "t1", DEFAULT)
                    self.turn("sticky", "t2", DEFAULT)
                    self.assertEqual(len(self.decisions), before + 2)

    def test_endpoint_binding_is_not_logged(self):
        plugin._on_pre_llm_call(session_id="url", turn_id="t1", user_message=HARD)
        plugin._on_llm_request(request={"model": DEFAULT}, model=DEFAULT,
                               provider="openrouter", session_id="url", turn_id="t1",
                               base_url="http://user:private-sentinel@localhost/v1")
        self.assertNotIn("private-sentinel", repr(self.logs))

    def test_malformed_named_alias_fails_closed(self):
        for alias in ({"dracarys": "openrouter"}, ["openrouter"], None, "bad:prefix"):
            with self.subTest(alias=alias):
                plugin._on_pre_llm_call(session_id="invalid", turn_id="t1", user_message=HARD)
                with mock.patch.object(plugin.route, "load_config", return_value={
                        "provider_aliases": {"custom": alias}}):
                    self.assertIsNone(plugin._on_llm_request(
                        request={"model": DEFAULT}, model=DEFAULT, provider="custom",
                        session_id="invalid", turn_id="t1"))

    def test_custom_provider_without_mapping_is_not_routed_across_providers(self):
        plugin._on_pre_llm_call(session_id="custom", turn_id="t1", user_message=HARD)
        with mock.patch.object(plugin.route, "load_config", return_value={}):
            result = plugin._on_llm_request(
                request={"model": "deepseek-v4-1-flash", "messages": []},
                session_id="custom", turn_id="t1", model="deepseek-v4-1-flash", provider="custom")
        self.assertIsNone(result)
        self.assertEqual(self.decisions, [])
        self.assertIn("provider_aliases.custom", [x for x in self.logs if x["kind"] == "route"][0]["reason"])

    def test_explicit_custom_provider_alias_limits_the_pool(self):
        plugin._on_pre_llm_call(session_id="custom-alias", turn_id="t1", user_message=HARD)
        with mock.patch.object(plugin.route, "load_config", return_value={"provider_aliases": {"custom": "venice"}}):
            plugin._on_llm_request(
                request={"model": "deepseek-v4-1-flash", "messages": []},
                session_id="custom-alias", turn_id="t1", model="deepseek-v4-1-flash", provider="custom")
        self.assertEqual(self.decisions[0]["only_provider"], "venice")
        self.assertEqual(self.decisions[0]["current"], "venice:deepseek-v4-1-flash")

    def test_custom_prefixed_model_is_normalised_with_explicit_alias(self):
        plugin._on_pre_llm_call(session_id="custom-prefixed", turn_id="t1", user_message=HARD)
        with mock.patch.object(plugin.route, "load_config", return_value={"provider_aliases": {"custom": "venice"}}):
            plugin._on_llm_request(request={"model": "custom:deepseek-v4-1-flash", "messages": []},
                                   session_id="custom-prefixed", turn_id="t1",
                                   model="custom:deepseek-v4-1-flash", provider="custom")
        self.assertEqual(self.decisions[0]["current"], "venice:deepseek-v4-1-flash")


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

    def enable_for(self, model="moonshotai/kimi-k3", levels=None):
        self.effort_config = {"effort": {"enabled": True,
            "models": {f"openrouter:{model}": levels or ["low", "medium", "high", "xhigh"]}}}

    def test_enabled_maps_expert_only_for_declared_capability(self):
        self.enable_for()
        result = self.turn()
        self.assertEqual(result["request"]["reasoning_effort"], "xhigh")
        self.assertNotIn("extra_body", result["request"])

    def test_configured_effort_isolated_after_every_context_change(self):
        self.enable_for()
        for responses in (False, True):
            for changed in ({"provider": "custom"}, {"base_url": "http://other/v1"},
                            {"model": "user-pin"}, {"request_model": "user-pin"}):
                with self.subTest(responses=responses, changed=changed):
                    extra = {"input": [], "reasoning": {"summary": "auto"}} if responses else {}
                    if responses:
                        first = self.codex_turn(session="isolation", reasoning={"summary": "auto"})
                    else:
                        first = self.turn(session="isolation")
                    outgoing = first["request"]
                    self.assertEqual(outgoing["reasoning"]["effort"] if responses
                                     else outgoing["reasoning_effort"], "xhigh")
                    for restore in (False, True, False):
                        args = {"model": DEFAULT, "provider": "openrouter", "base_url": ""}
                        if not restore:
                            args.update({k: v for k, v in changed.items() if k != "request_model"})
                        request = {"model": DEFAULT if restore else changed.get("request_model", DEFAULT), **extra}
                        if not responses:
                            request["messages"] = []
                        original = deepcopy(request)
                        self.assertIsNone(plugin._on_llm_request(
                            request=request, session_id="isolation", turn_id="t1", **args))
                        self.assertEqual(request, original)
                        self.assertNotIn("effort", plugin._TURNS["isolation"])
                    with mock.patch.object(plugin, "_setting", return_value="on"):
                        self.assertIsNone(plugin._on_transform_output("answer", session_id="isolation"))

    def paused_callback(self, pause_target):
        """Run one request with a deterministic RPC/effort pause, never a sleep."""
        self.enable_for()
        plugin._on_pre_llm_call(session_id="race", turn_id="t1", user_message=HARD)
        entered, release = threading.Event(), threading.Event()
        original = getattr(*pause_target)

        def paused(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise AssertionError("test did not release paused callback")
            return original(*args, **kwargs)

        return entered, release, mock.patch.object(*pause_target, side_effect=paused)

    def race_request(self, **changes):
        args = {"model": DEFAULT, "provider": "openrouter", "base_url": ""}
        args.update({k: v for k, v in changes.items() if k not in ("request_model", "turn_id")})
        request = {"model": changes.get("request_model", DEFAULT), "messages": []}
        return plugin._on_llm_request(request=request, session_id="race",
                                      turn_id=changes.get("turn_id", "t1"), **args)

    def test_inflight_router_cannot_publish_after_context_change(self):
        for changed in ({"provider": "custom"}, {"base_url": "http://other/v1"},
                        {"model": "user-pin"}, {"request_model": "user-pin"}):
            with self.subTest(changed=changed):
                entered, release, pause = self.paused_callback((plugin.route, "decide"))
                logs = []
                with pause as router, mock.patch.object(plugin, "_log", logs.append), ThreadPoolExecutor(1) as pool:
                    first = pool.submit(self.race_request)
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertIsNone(self.race_request(**changed))
                    finally:
                        release.set()
                    self.assertIsNone(first.result(timeout=5))
                    self.assertEqual(router.call_count, 1)
                self.assertTrue(plugin._TURNS["race"]["route_invalidated"])
                self.assertIsNone(plugin._TURNS["race"]["decision"])
                self.assertNotIn("effort", plugin._TURNS["race"])
                self.assertFalse(any(row.get("applied") for row in logs))
                self.assertFalse(any(row.get("kind") == "effort" for row in logs))

    def test_same_context_inflight_callback_keeps_request_without_duplicate_rpc(self):
        entered, release, pause = self.paused_callback((plugin.route, "decide"))
        with pause as router, ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.race_request)
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(self.race_request())
            finally:
                release.set()
            result = first.result(timeout=5)
            self.assertEqual(result["request"]["reasoning_effort"], "xhigh")
            self.assertEqual(router.call_count, 1)
        self.assertFalse(plugin._TURNS["race"].get("route_invalidated", False))

    def test_inflight_router_cannot_overwrite_replacement_turn(self):
        for new_id in ("t2", "t1"):
            with self.subTest(new_turn_id=new_id):
                entered, release, pause = self.paused_callback((plugin.route, "decide"))
                with pause, ThreadPoolExecutor(1) as pool:
                    first = pool.submit(self.race_request)
                    try:
                        self.assertTrue(entered.wait(5))
                        plugin._on_pre_llm_call(session_id="race", turn_id=new_id, user_message=HARD)
                        replacement = plugin._TURNS["race"]
                        new_request = self.race_request(turn_id=new_id)
                        self.assertEqual(new_request["request"]["reasoning_effort"], "xhigh")
                    finally:
                        release.set()
                    self.assertIsNone(first.result(timeout=5))
                self.assertIs(plugin._TURNS["race"], replacement)
                self.assertEqual(replacement["effort"], "xhigh")

    def test_inflight_effort_pick_cannot_apply_after_invalidation(self):
        entered, release, pause = self.paused_callback((plugin.effort, "pick"))
        logs = []
        with pause, mock.patch.object(plugin, "_log", logs.append), ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.race_request)
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(self.race_request(base_url="http://other/v1"))
            finally:
                release.set()
            self.assertIsNone(first.result(timeout=5))
        self.assertNotIn("effort", plugin._TURNS["race"])
        self.assertFalse(any(row.get("applied") for row in logs))
        self.assertFalse(any(row.get("kind") == "effort" for row in logs))

    def test_opt_in_without_model_capability_does_not_mutate(self):
        self.effort_config = {"effort": {"enabled": True}}
        result = self.turn()
        self.assertNotIn("reasoning_effort", result["request"])

    def test_unsupported_level_does_not_mutate(self):
        self.enable_for(levels=["low", "medium", "high"])
        self.assertNotIn("reasoning_effort", self.turn()["request"])

    def test_a_custom_table_is_honored_end_to_end(self):
        self.enable_for(levels=["low", "medium", "high", "max"])
        self.effort_config["effort"]["levels"] = {"expert": "max"}
        self.assertEqual(self.turn()["request"]["reasoning_effort"], "max")

    def test_unrelated_extra_body_is_preserved(self):
        self.enable_for()
        extra = {"provider": {"order": ["fireworks"]}}
        result = self.turn(request_extra={"extra_body": extra})
        self.assertEqual(result["request"]["extra_body"], extra)

    def test_existing_reasoning_block_or_manual_effort_wins(self):
        self.enable_for()
        extra = {"reasoning": {"effort": "low", "max_tokens": 512}}
        result = self.turn(request_extra={"extra_body": extra})
        self.assertEqual(result["request"]["extra_body"], extra)
        self.assertNotIn("reasoning_effort", result["request"])
        result = self.turn(session="manual", request_extra={"reasoning_effort": "low"})
        self.assertEqual(result["request"]["reasoning_effort"], "low")

    def codex_turn(self, session="responses", **extra):
        plugin._on_pre_llm_call(session_id=session, turn_id="t1", user_message=HARD)
        request = {"input": [{"role": "user", "content": HARD}], "model": DEFAULT}
        request.update(extra)
        return plugin._on_llm_request(request=request, session_id=session, turn_id="t1",
                                      model=DEFAULT, provider="openrouter")

    def test_responses_effort_uses_enabled_nested_object(self):
        self.enable_for()
        result = self.codex_turn(reasoning={"summary": "auto"})
        self.assertNotIn("reasoning_effort", result["request"])
        self.assertEqual(result["request"]["reasoning"], {"summary": "auto", "effort": "xhigh"})
        self.assertEqual(plugin._TURNS["responses"]["effort"], "xhigh")

    def test_responses_manual_effort_is_not_overwritten_or_claimed(self):
        self.enable_for()
        with mock.patch.object(plugin, "_log") as log:
            result = self.codex_turn(reasoning={"effort": "medium", "summary": "auto"})
        self.assertEqual(result["request"]["reasoning"], {"effort": "medium", "summary": "auto"})
        self.assertNotIn("reasoning_effort", result["request"])
        self.assertNotIn("effort", plugin._TURNS["responses"])
        self.assertFalse(any(c.args[0].get("kind") == "effort" for c in log.call_args_list))

    def test_responses_disabled_reasoning_has_no_effort_telemetry_or_notice(self):
        self.enable_for()
        for i, value in enumerate([None, False, "disabled"]):
            session = "disabled-" + str(i)
            with mock.patch.object(plugin, "_log") as log:
                result = self.codex_turn(session=session, reasoning=value)
            self.assertIs(result["request"]["reasoning"], value)
            self.assertNotIn("reasoning_effort", result["request"])
            self.assertNotIn("effort", plugin._TURNS[session])
            self.assertFalse(any(c.args[0].get("kind") == "effort" for c in log.call_args_list))
            with mock.patch.object(plugin, "_setting", lambda name, default: "on"):
                notice = plugin._on_transform_output(response_text="reply", session_id=session)
            self.assertNotIn("effort xhigh", notice or "")
        result = self.codex_turn(session="absent")
        self.assertNotIn("reasoning_effort", result["request"])
        self.assertNotIn("reasoning", result["request"])
        self.assertNotIn("effort", plugin._TURNS["absent"])

    def test_effort_helper_preserves_manual_and_disabled_responses(self):
        for request in [{"input": []}, {"input": [], "reasoning": None},
                        {"input": [], "reasoning": {"effort": "low"}}]:
            self.assertEqual(plugin._with_effort(request, "xhigh"), request)

    def test_followup_disabled_request_does_not_reuse_effort_notice(self):
        self.enable_for()
        self.codex_turn(reasoning={"summary": "auto"})
        result = plugin._on_llm_request(request={"input": [], "model": DEFAULT},
            session_id="responses", turn_id="t1", model=DEFAULT, provider="openrouter")
        self.assertNotIn("reasoning_effort", result["request"])
        self.assertNotIn("effort", plugin._TURNS["responses"])

    def test_shadow_and_off_do_not_mutate(self):
        self.enable_for()
        with mock.patch.object(plugin, "_setting",
                               lambda name, default: "shadow" if name == "routing" else default):
            self.assertIsNone(self.turn())
        with mock.patch.object(plugin, "_setting",
                               lambda name, default: "off" if name == "routing" else default):
            self.assertIsNone(self.turn(session="off"))

    def test_a_malformed_table_fails_open(self):
        self.effort_config = {"effort": {"enabled": True, "levels": ["low", "medium", "high"]}}
        result = self.turn()
        self.assertNotIn("reasoning_effort", result["request"])

    def test_notice_names_the_effort_on_a_routed_turn(self):
        self.enable_for()
        self.turn()
        with mock.patch.object(plugin, "_setting", lambda name, default: "on"):
            out = plugin._on_transform_output(response_text="reply", session_id="eff")
        self.assertIn("effort xhigh", out)

    def test_shadow_notice_does_not_claim_effort_applied(self):
        self.enable_for()
        with mock.patch.object(plugin, "_setting",
                               lambda name, default: "shadow" if name == "routing" else "off" if name in ("skills", "merge_requests") else "on"):
            self.turn()
            out = plugin._on_transform_output(response_text="reply", session_id="eff")
        self.assertIsNone(out)

    def test_no_tiers_still_buys_the_difficulty_for_effort(self):
        self.enable_for(model=DEFAULT)
        self.tiers_override = {}
        result = self.turn()
        self.assertEqual(result["request"]["model"], DEFAULT)
        self.assertEqual(result["request"]["reasoning_effort"], "xhigh")

    def test_pinned_model_requires_its_own_capability(self):
        self.enable_for(model="other/model")
        plugin._on_pre_llm_call(session_id="pin", turn_id="t1", user_message=HARD)
        result = plugin._on_llm_request(
            request={"messages": [{"role": "user", "content": HARD}], "model": "other/model"},
            session_id="pin", turn_id="t1", model="other/model", provider="openrouter")
        self.assertEqual(result["request"]["model"], "other/model")
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
