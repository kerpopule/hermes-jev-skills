"""Tests for routing_store: safe, comment-preserving, verified config edits."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing_store as rs  # noqa: E402

FIXTURE = """\
# Hermes profile config (fixture)
model:
  default: deepseek/deepseek-v4.1-flash
  provider: openrouter
  context_length: 1048576
  aliases:
    qwen27: local-qwen/pocketaihub-qwen3.8-27b

auxiliary:
  compression:
    provider: openrouter
    model: deepseek/deepseek-v4-flash-0731
    timeout: 60
  vision:
    provider: openrouter
    model: qwen/qwen3-vl-235b-a22b-instruct
  approval:
    provider: auto
    model: ''

agent:
  max_turns: 120
plugins:
  enabled:
  - resource-lifecycle
"""


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "profiles", "wiki"), exist_ok=True)
        self.root_cfg = os.path.join(self.home, "config.yaml")
        self.wiki_cfg = os.path.join(self.home, "profiles", "wiki", "config.yaml")
        for p in (self.root_cfg, self.wiki_cfg):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_targets_default_first(self):
        names = [t.name for t in rs.discover_targets(self.home)]
        self.assertEqual(names, ["default", "wiki"])

    def test_read_config_reports_main_and_slots(self):
        cfg = rs.read_config(self.root_cfg)
        self.assertEqual(cfg["main"]["provider"], "openrouter")
        self.assertEqual(cfg["main"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(cfg["slots"]["compression"]["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(cfg["slots"]["approval"]["provider"], "auto")

    def test_edit_preserves_comments_order_and_unrelated_keys(self):
        text = open(self.root_cfg, encoding="utf-8").read()
        out = rs._set_scalar(text, "model", "default", "z-ai/glm-5.3")
        self.assertIn("# Hermes profile config (fixture)", out)
        self.assertIn("    qwen27: local-qwen/pocketaihub-qwen3.8-27b", out)
        self.assertIn("  default: z-ai/glm-5.3\n", out)
        self.assertNotIn("deepseek/deepseek-v4.1-flash", out)
        self.assertIn("agent:\n  max_turns: 120", out)

    def test_edit_nested_slot_scalar(self):
        text = open(self.root_cfg, encoding="utf-8").read()
        out = rs._set_scalar(text, "auxiliary", "model", "google/gemini-2.5-flash", sub="compression")
        self.assertIn("  compression:\n    provider: openrouter\n    model: google/gemini-2.5-flash\n", out)
        # untouched sibling
        self.assertIn("    model: qwen/qwen3-vl-235b-a22b-instruct", out)

    def test_edit_adds_missing_slot_without_disturbing_others(self):
        text = open(self.root_cfg, encoding="utf-8").read()
        out = rs._set_scalar(text, "auxiliary", "provider", "openrouter", sub="web_extract")
        out = rs._set_scalar(out, "auxiliary", "model", "deepseek/deepseek-v4-flash-0731",
                             sub="web_extract")
        self.assertIn("  web_extract:\n    provider: openrouter\n"
                      "    model: deepseek/deepseek-v4-flash-0731\n", out)
        self.assertIn("  approval:\n    provider: auto\n    model: ''\n", out)

    def test_edit_appends_section_when_absent(self):
        text = "model:\n  provider: openrouter\n  default: x\n"
        out = rs._set_scalar(text, "auxiliary", "model", "m", sub="compression")
        self.assertIn("auxiliary:\n  compression:\n    model: m\n", out)

    def test_plan_reports_diffs_and_rejects_bad_input(self):
        rows = rs.plan(self.root_cfg, {"compression": {"model": "google/gemini-2.5-flash"}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["before"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(rows[0]["after"], "google/gemini-2.5-flash")
        with self.assertRaises(ValueError):
            rs.plan(self.root_cfg, {"not_a_slot": {"model": "m"}})
        with self.assertRaises(ValueError):
            rs.plan(self.root_cfg, {"compression": {"model": "evil\n  injected: true"}})
        with self.assertRaises(ValueError):
            rs.plan(self.root_cfg, {"compression": {"model": "bad'quote"}})

    def test_apply_writes_backs_up_and_verifies(self):
        rec = rs.apply_changes(self.home, self.root_cfg,
                               {"__main__": {"model": "z-ai/glm-5.3", "provider": "openrouter"},
                                "compression": {"model": "google/gemini-2.5-flash"}})
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["changed"], 2)
        self.assertTrue(os.path.isfile(rec["backup"]))
        self.assertTrue(rec["reload_required"])
        after = rs.read_config(self.root_cfg)
        self.assertEqual(after["main"]["model"], "z-ai/glm-5.3")
        self.assertEqual(after["slots"]["compression"]["model"], "google/gemini-2.5-flash")
        # backup holds the pre-change content
        self.assertIn("deepseek/deepseek-v4.1-flash", open(rec["backup"], encoding="utf-8").read())

    def test_apply_noop_is_honest(self):
        rec = rs.apply_changes(self.home, self.root_cfg, {"compression": {"provider": "openrouter"}})
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["changed"], 0)
        self.assertFalse(rec["reload_required"])

    def test_apply_refuses_foreign_path(self):
        with self.assertRaises(ValueError):
            rs.apply_changes(self.home, "/etc/passwd", {"compression": {"model": "m"}})

    def test_apply_leaves_valid_yaml_for_every_profile(self):
        for cfg in (self.root_cfg, self.wiki_cfg):
            rec = rs.apply_changes(self.home, cfg, {"web_extract": {"provider": "openrouter",
                                                                   "model": "deepseek/deepseek-v4-flash-0731"}})
            self.assertTrue(rec["verified"], rec)

    def test_snapshot_shape(self):
        snap = rs.snapshot(self.home)
        self.assertEqual([p["name"] for p in snap["profiles"]], ["default", "wiki"])
        keys = [u["key"] for u in snap["use_cases"]]
        self.assertEqual(keys[0], "__main__")
        self.assertIn("compression", keys)
        self.assertEqual(snap["jev_mode"]["desired_default"], "shadow")
        self.assertIn("intent", snap["jev_mode"])

    def test_model_catalog_includes_configured_ids(self):
        ids = {m["id"] for m in rs.model_catalog(self.home)}
        self.assertIn("deepseek/deepseek-v4.1-flash", ids)
        self.assertIn("qwen/qwen3-vl-235b-a22b-instruct", ids)

    def test_jev_state_reports_not_configured_then_value(self):
        self.assertEqual(rs.read_config(self.root_cfg)["jev"]["mode"], "not-configured")
        rec = rs.apply_changes(self.home, self.root_cfg, {})
        self.assertTrue(rec["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class JevSwitchTests(unittest.TestCase):
    def test_profile_override_and_all_resets_everyone(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("alpha", "beta"):
                os.makedirs(os.path.join(tmp, "profiles", name))
            rs.set_jev_switch(tmp, "__all__", "routing", "shadow")
            rs.set_jev_switch(tmp, "alpha", "routing", "on")
            state = rs.jev_switch_state(tmp)
            self.assertEqual(state["profiles"]["alpha"]["effective"]["routing"], "on")
            self.assertEqual(state["profiles"]["beta"]["effective"]["routing"], "shadow")
            with open(os.path.join(tmp, "profiles", "alpha", "jev", "state.json"), "w") as fh:
                json.dump({"routing": "on", "notice": "on"}, fh)
            state = rs.set_jev_switch(tmp, "__all__", "routing", "off")
            self.assertTrue(all(p["effective"]["routing"] == "off" for p in state["profiles"].values()))
            with open(os.path.join(tmp, "profiles", "alpha", "jev", "state.json")) as fh:
                self.assertEqual(json.load(fh), {"notice": "on"})  # only the routing override was cleared

    def test_rejects_bad_values_and_unknown_profiles(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for scope, name, value in (("__all__", "routing", "maybe"), ("__all__", "model", "on"), ("ghost", "routing", "on")):
                with self.assertRaises(ValueError):
                    rs.set_jev_switch(tmp, scope, name, value)


class JevPoolTests(unittest.TestCase):
    """The live view has to say which pool a model came from; the decision log records
    the decision, not the pool, so the dashboard resolves it against routing.json."""

    def setUp(self):
        import json
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.home, "xdg")})
        env.start()
        os.environ.pop("JEV_ROUTING_CONFIG", None)
        self.addCleanup(env.stop)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.home, "jev"))
        with open(os.path.join(self.home, "jev", "routing.json"), "w", encoding="utf-8") as fh:
            json.dump({"tiers": {"medium": {"general": ["openrouter:mid/one"],
                                            "coding": ["openrouter:codes/well"]}}}, fh)

    def log(self, *rows):
        import json
        os.makedirs(os.path.join(self.home, "logs"), exist_ok=True)
        with open(os.path.join(self.home, "logs", "jev-decisions.jsonl"), "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_pools_are_reported_for_every_profile(self):
        os.makedirs(os.path.join(self.home, "profiles", "wiki"))
        out = rs.jev_pools(self.home)
        self.assertEqual([p["name"] for p in out["profiles"]], ["default", "wiki"])
        self.assertEqual(out["profiles"][0]["tiers"]["medium"]["coding"]["usable"], 1)

    def test_live_route_says_the_specialty_pool_chose_the_model(self):
        self.log({"ts": 10, "kind": "route", "tier": "medium", "specialty": "coding",
                  "model": "openrouter:codes/well", "routed": True})
        event = rs.jev_live(self.home)["events"][0]
        self.assertEqual(event["pool"]["specialty"], "coding")
        self.assertIs(event["pool"]["earned"], True)

    def test_live_route_says_when_the_specialty_answer_was_discarded(self):
        self.log({"ts": 11, "kind": "route", "tier": "medium", "specialty": "writing",
                  "model": "openrouter:mid/one", "routed": True})
        event = rs.jev_live(self.home)["events"][0]
        self.assertEqual(event["pool"]["specialty"], "general")
        self.assertIs(event["pool"]["earned"], False)

    def test_effective_counts_use_applied_first_requests_not_shadow_predictions(self):
        self.log({"ts": 1, "kind": "route", "model": "openrouter:would/route", "routed": True, "mode": "shadow"},
                 {"ts": 2, "kind": "route_effective", "effective_request_model": "current/one",
                  "first_request": True, "applied": False, "mode": "shadow"},
                 {"ts": 3, "kind": "route_effective", "effective_request_model": "routed/two",
                  "first_request": True, "applied": True, "mode": "on"},
                 {"ts": 4, "kind": "route_effective", "effective_request_model": "routed/two",
                  "first_request": False, "applied": True, "mode": "on"})
        live = rs.jev_live(self.home)
        self.assertEqual(dict(live["by_model"]), {"current/one": 1, "routed/two": 1})
        self.assertEqual((live["effective_turns"], live["applied_turns"]), (2, 1))

    def test_a_kept_model_decision_carries_no_pool(self):
        self.log({"ts": 12, "kind": "route", "routed": False, "from": "openrouter:mid/one",
                  "reason": "low confidence 0.41"})
        self.assertNotIn("pool", rs.jev_live(self.home)["events"][0])
