"""The handoff plugin's hook seams, the nightly script, and the confidentiality refusals.

handoff.py was tested; the package around it was not, and that is where the trigger word
was broken from the day it shipped. The hook read keywords the gateway has never sent, so
it returned None for every message and nothing noticed. These tests call the hooks the way
Hermes does: ``event=``, ``gateway=``, ``session_store=``, with an event shaped like
``gateway.platforms.base.MessageEvent``.

Everything here is offline. No Jev call, no writer model, no real ``hermes`` binary.
"""
from __future__ import annotations

import enum
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "hermes" / "plugin" / "hermes-handoff"
NIGHTLY = REPO / "hermes" / "scripts" / "nightly-handoff.py"

# Hermes' built-in slash commands and aliases on 2026-09-19. A plugin command that matches
# one is refused at load with a warning nobody reads, which is how /handoff never existed.
HERMES_BUILTINS = frozenset("""
agents approvals approve battery bg blueprint bp branch browser btw bundles busy clear
codex-runtime codex_runtime commands compact compose compress config context copy cron ctx
curator debug deny diff egress exit export fast focus footer fork gateway generate-pet goal
handoff hatch hb heartbeat help history image import indicator init insights journey kanban
learn learning loop memory memory-graph moa model new palette paste pause personality pet
plan platform platforms plugins proactive profile prompt q queue quit reasoning redraw
refine reload reload-mcp reload-skills reload_mcp reload_skills reset restart resume retry
review rollback save sb sessions set-home sethome skills skin snap snapshot start status
statusbar steer stop subgoal subscription suggest suggestions tasks timestamps title tools
toolsets topic topup ts undo update upgrade usage v verbose version voice wake whoami
worktree yolo
""".split())


def load_plugin(name: str) -> Any:
    """Load the package the way Hermes does: a synthetic name and a search location."""
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def unload(name: str) -> None:
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


def load_handoff(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "handoff.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_nightly() -> Any:
    spec = importlib.util.spec_from_file_location("nightly_handoff_under_test", NIGHTLY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── an event shaped like the real one ────────────────────────────────────────

class Platform(enum.Enum):
    TELEGRAM = "telegram"


@dataclass
class Source:
    platform: Platform = Platform.TELEGRAM
    chat_id: str = "-1003829274549"
    thread_id: Optional[str] = "29"
    user_id: Optional[str] = "1022650411"
    chat_type: str = "group"


@dataclass
class Event:
    text: str
    source: Source = field(default_factory=Source)
    message_id: Optional[str] = "m-1"
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    internal: bool = False
    allow_gateway_control: bool = True


ROUTING_KEY = "agent:devbot:telegram:group:-1003829274549:29"


class Store:
    """The one public lookup the plugin needs from ``gateway.session.SessionStore``."""

    def __init__(self, mapping: Optional[Dict[str, str]] = None) -> None:
        self.mapping = dict({ROUTING_KEY: "old-session"} if mapping is None else mapping)

    def peek_session_id(self, key: str) -> Optional[str]:
        return self.mapping.get(key)


class Gateway:
    def __init__(self, authorized: bool = True) -> None:
        self.authorized = authorized

    def _session_key_for_source(self, source: Any) -> str:
        return ROUTING_KEY

    def _is_user_authorized_for_source(self, source: Any) -> bool:
        return self.authorized


class Recorder:
    """Stands in for run_handoff: records the context, optionally holds the worker."""

    def __init__(self, hold: bool = False, result: Optional[Dict[str, Any]] = None,
                 error: Optional[Exception] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        if not hold:
            self.release.set()
        self.result = result or {"status": "ok", "lane": "lane", "messages": 12, "jev": "ok"}
        self.error = error

    def __call__(self, context: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(dict(context))
        self.started.set()
        try:
            self.release.wait(5)
            if self.error:
                raise self.error
            return self.result
        finally:
            self.finished.set()


def join_workers() -> None:
    for thread in threading.enumerate():
        if thread.name == "hermes-handoff":
            thread.join(5)


class PluginCase(unittest.TestCase):
    NAME = "hermes_handoff_under_test"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERMES_SESSION_ID", None)
        # A Hermes gateway exports the whole HERMES_SESSION_* family into every child
        # process. Tests that run inside one inherit a platform (telegram) and would
        # assert against ambient state instead of the plugin's own decision.
        os.environ.pop("HERMES_SESSION_PLATFORM", None)
        os.environ.pop("HANDOFF_CONFIDENTIAL", None)
        self.plugin = load_plugin(self.NAME)
        self.addCleanup(unload, self.NAME)
        self.addCleanup(join_workers)

    def record(self, **kwargs: Any) -> Recorder:
        recorder = Recorder(**kwargs)
        patcher = mock.patch.object(self.plugin, "run_handoff", recorder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(recorder.release.set)
        return recorder

    def dispatch(self, text: str, *, gateway: Any = None, store: Any = None, **event: Any) -> Any:
        return self.plugin._on_dispatch(event=Event(text=text, **event),
                                        gateway=gateway if gateway is not None else Gateway(),
                                        session_store=store if store is not None else Store())


# ── the dispatch hook, called the way the gateway calls it ───────────────────

class TriggerWordOnTheRealGatewayTests(PluginCase):

    def test_the_gateways_real_keywords_start_a_capsule_for_the_live_session(self):
        """The original bug: the hook read text= and session_id=, which Hermes never sends."""
        recorder = self.record()
        self.dispatch("handoff")
        self.assertTrue(recorder.started.wait(5), "the trigger word did nothing")
        context = recorder.calls[0]
        self.assertEqual(context["session_id"], "old-session")
        self.assertEqual((context["platform"], context["chat_id"], context["thread_id"]),
                         ("telegram", "-1003829274549", "29"))

    def test_the_capsule_is_keyed_the_way_the_next_session_will_look_it_up(self):
        """Written from an event, read from a session row. Different keys is a silent no-op."""
        recorder = self.record()
        self.dispatch("handoff")
        self.assertTrue(recorder.started.wait(5))
        written = self.plugin.handoff.lane_key(recorder.calls[0])
        read = self.plugin.handoff.lane_key({"source": "telegram", "chat_id": "-1003829274549",
                                            "thread_id": "29", "session_id": "new-session"})
        self.assertEqual(written, read)

    def test_the_message_is_never_skipped_because_a_skipped_message_gets_no_reply(self):
        """The gateway drops a skip silently and never reads its "message". Skipping also
        pre-empts a host's own plain-handoff session rotation, which runs later."""
        self.record()
        self.assertIsNone(self.dispatch("handoff"))

    def test_the_hook_returns_while_the_capsule_is_still_being_written(self):
        """The hook runs on the gateway's event loop. Building inline froze every chat."""
        recorder = self.record(hold=True)
        began = time.monotonic()
        self.dispatch("handoff")
        elapsed = time.monotonic() - began
        self.assertTrue(recorder.started.wait(5))
        self.assertFalse(recorder.finished.is_set(), "the hook waited for the build")
        self.assertLess(elapsed, 1.0)

    def test_the_session_is_read_before_the_host_rotates_it(self):
        """The build runs after the hook returns. By then the routing key names the NEW
        session, and a capsule built from that one summarises nothing."""
        recorder = self.record(hold=True)
        store = Store()
        self.dispatch("handoff", store=store)
        store.mapping[ROUTING_KEY] = "new-session"
        recorder.release.set()
        self.assertTrue(recorder.finished.wait(5))
        self.assertEqual(recorder.calls[0]["session_id"], "old-session")

    def test_a_sentence_that_mentions_a_handoff_is_an_ordinary_message(self):
        recorder = self.record()
        for text in ("do a handoff for me", "what does handoff do?", ""):
            self.assertIsNone(self.dispatch(text))
        self.assertFalse(recorder.started.wait(0.2))

    def test_text_nobody_typed_cannot_start_a_capsule(self):
        """Internal wake-ups, proactive plugin payloads and captioned attachments all carry
        text. None of them is a person asking to close their session."""
        recorder = self.record()
        self.dispatch("handoff", internal=True)
        self.dispatch("handoff", allow_gateway_control=False)
        self.dispatch("handoff", media_urls=["photo.jpg"])
        self.assertFalse(recorder.started.wait(0.2))

    def test_a_sender_the_gateway_would_reject_spends_nothing(self):
        """This hook fires BEFORE authorization. Without the check, anyone who can post in a
        shared chat burns the owner's model calls with one word."""
        recorder = self.record()
        self.assertIsNone(self.dispatch("handoff", gateway=Gateway(authorized=False)))
        self.assertFalse(recorder.started.wait(0.2))

    def test_a_host_with_no_authorization_method_still_gets_its_capsule(self):
        class Bare:
            def _session_key_for_source(self, source: Any) -> str:
                return ROUTING_KEY
        recorder = self.record()
        self.dispatch("handoff", gateway=Bare())
        self.assertTrue(recorder.started.wait(5))

    def test_saying_it_twice_does_not_race_two_writers_onto_one_capsule(self):
        recorder = self.record(hold=True)
        self.dispatch("handoff")
        self.assertTrue(recorder.started.wait(5))
        self.dispatch("handoff")
        recorder.release.set()
        join_workers()
        self.assertEqual(len(recorder.calls), 1)

    def test_a_build_that_blows_up_does_not_block_the_next_handoff(self):
        """The in-flight marker has to clear on failure, or one bad night disables the
        trigger for that session until the gateway restarts."""
        first = self.record(error=RuntimeError("writer exploded"))
        self.dispatch("handoff")
        self.assertTrue(first.finished.wait(5))
        join_workers()
        second = self.record()
        self.dispatch("handoff")
        self.assertTrue(second.started.wait(5))

    def test_a_conversation_with_no_session_starts_nothing(self):
        recorder = self.record()
        self.assertIsNone(self.dispatch("handoff", store=Store({})))
        self.assertFalse(recorder.started.wait(0.2))

    def test_a_host_that_renamed_its_private_helpers_falls_back_to_the_session_database(self):
        """The routing-key helpers are private. When they go, the newest OPEN row for this
        exact conversation is the live one; ended rows and other chats are not."""
        db = sqlite3.connect(Path(self.tmp.name) / "state.db")
        db.execute("create table sessions (id text primary key, source text, chat_id text, "
                   "thread_id text, started_at real, ended_at real)")
        db.executemany("insert into sessions values (?,?,?,?,?,?)", [
            ("ended-but-newest", "telegram", "-1003829274549", "29", 400.0, 401.0),
            ("live", "telegram", "-1003829274549", "29", 300.0, None),
            ("stale-open", "telegram", "-1003829274549", "29", 100.0, None),
            ("other-topic", "telegram", "-1003829274549", "45", 500.0, None),
            ("other-platform", "discord", "-1003829274549", "29", 600.0, None),
        ])
        db.commit()
        db.close()
        recorder = self.record()
        self.dispatch("handoff", gateway=object(), store=object())
        self.assertTrue(recorder.started.wait(5))
        self.assertEqual(recorder.calls[0]["session_id"], "live")


class FlatKeywordHostTests(PluginCase):
    """The shape this hook was first written for. No known host sends it; it still works."""

    def test_a_host_that_passes_text_and_session_id_directly_is_still_served(self):
        recorder = self.record()
        out = self.plugin._on_dispatch(text="handoff", session_id="s-flat", platform="teams", chat_id="chatA")
        self.assertEqual(out["action"], "skip")
        self.assertTrue(recorder.started.wait(5))
        self.assertEqual(recorder.calls[0]["session_id"], "s-flat")

    def test_the_reply_does_not_claim_a_capsule_that_is_still_being_written(self):
        recorder = self.record(hold=True)
        out = self.plugin._on_dispatch(text="handoff", session_id="s-flat", chat_id="chatA")
        self.assertFalse(recorder.finished.is_set())
        self.assertNotIn("saved", out["message"].lower())
        self.assertIn("Writing the handoff", out["message"])

    def test_without_a_session_it_says_so_instead_of_pretending(self):
        recorder = self.record()
        out = self.plugin._on_dispatch(text="handoff")
        self.assertIn("no_session", out["message"])
        self.assertFalse(recorder.started.wait(0.2))

    def test_other_messages_pass_through(self):
        self.assertIsNone(self.plugin._on_dispatch(text="hello there", session_id="s1"))


# ── the command ──────────────────────────────────────────────────────────────

class FakeCtx:
    def __init__(self) -> None:
        self.hooks: Dict[str, Any] = {}
        self.commands: Dict[str, Any] = {}
        self.sections: Dict[str, Any] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback

    def register_command(self, name: str, handler: Any, description: str = "") -> None:
        self.commands[name] = handler

    def register_system_prompt_section(self, name: str, text: str, max_chars: int = 0) -> None:
        self.sections[name] = (text, max_chars)

    def get_config(self, name: str, default: Any = None) -> Any:
        return default


class CommandTests(PluginCase):

    def test_the_command_name_is_not_a_hermes_builtin(self):
        """/handoff is one. Hermes refused the registration on every load, so the command
        the plugin advertised had never existed."""
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertEqual(list(ctx.commands), [self.plugin.COMMAND])
        self.assertNotIn(self.plugin.COMMAND, HERMES_BUILTINS)
        # Telegram accepts only a-z, 0-9 and underscore in a command name.
        self.assertRegex(self.plugin.COMMAND, r"^[a-z0-9_]{1,32}$")

    def test_the_command_name_is_free_in_the_hermes_installed_here(self):
        """The pinned list above goes stale. Where a Hermes checkout exists, ask it."""
        source = Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "commands.py"
        if not source.is_file():
            self.skipTest("no Hermes checkout on this machine")
        text = source.read_text(encoding="utf-8", errors="replace")
        names = set(re.findall(r'CommandDef\(\s*"([^"]+)"', text))
        for group in re.findall(r"aliases=\(([^)]*)\)", text):
            names.update(re.findall(r'"([^"]+)"', group))
        self.assertIn("new", names, "the parse found nothing, so this proves nothing")
        self.assertNotIn(self.plugin.COMMAND, names)

    def test_register_wires_both_hooks_and_a_rule_that_fits_its_budget(self):
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertIs(ctx.hooks["pre_gateway_dispatch"], self.plugin._on_dispatch)
        self.assertIs(ctx.hooks["pre_llm_call"], self.plugin._on_pre_llm_call)
        text, budget = ctx.sections["hermes-handoff"]
        self.assertLessEqual(len(text), budget, "the host would cut the rule off mid-sentence")

    def test_in_a_gateway_the_command_acts_on_the_conversation_that_sent_it(self):
        """The handler is given only its arguments. The process-wide HERMES_SESSION_ID in a
        gateway belongs to whichever conversation built an agent last."""
        recorder = self.record()
        with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "somebody-elses-session"}):
            self.assertIsNone(self.dispatch("/wrapup"))
            reply = self.plugin._command("")
        self.assertTrue(recorder.started.wait(5))
        self.assertEqual(recorder.calls[0]["session_id"], "old-session")
        self.assertIn("/new", reply)

    def test_telegrams_bot_suffix_is_still_the_command(self):
        recorder = self.record()
        self.dispatch("/wrapup@DonnaBot")
        self.plugin._command("")
        self.assertTrue(recorder.started.wait(5))
        self.assertEqual(recorder.calls[0]["session_id"], "old-session")

    def test_the_hook_does_not_build_for_the_command_itself(self):
        """The gateway dispatches the command next. Building here too would do it twice."""
        recorder = self.record()
        self.dispatch("/wrapup")
        self.assertFalse(recorder.started.wait(0.2))

    def test_one_messages_identity_is_not_left_for_the_next_message(self):
        recorder = self.record(result={"status": "no_session"})
        self.dispatch("/wrapup")
        self.dispatch("good morning")
        reply = self.plugin._command("")
        self.assertEqual(recorder.calls, [{"session_id": "", "platform": ""}])
        self.assertIn("no_session", reply)

    def test_in_a_cli_the_command_waits_and_reports_the_real_outcome(self):
        recorder = self.record(result={"status": "ok", "path": "handoffs/handoff-cli.md", "messages": 14})
        with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "cli-session"}):
            reply = self.plugin._command("")
        self.assertEqual(recorder.calls[0]["session_id"], "cli-session")
        self.assertIn("handoffs/handoff-cli.md", reply)
        self.assertIn("14 turns", reply)

    def test_a_cli_failure_is_reported_as_a_failure(self):
        self.record(result={"status": "too_short", "messages": 2})
        with mock.patch.dict(os.environ, {"HERMES_SESSION_ID": "cli-session"}):
            self.assertIn("No handoff written (too_short)", self.plugin._command(""))


# ── giving the capsule to the next session ───────────────────────────────────

class InjectionTests(PluginCase):

    def setUp(self) -> None:
        super().setUp()
        db = sqlite3.connect(Path(self.tmp.name) / "state.db")
        db.execute("create table sessions (id text primary key, source text, chat_id text, thread_id text)")
        db.executemany("insert into sessions values (?,?,?,?)", [
            ("old-session", "telegram", "-1003829274549", "29"),
            ("new-session", "telegram", "-1003829274549", "29"),
        ])
        db.commit()
        db.close()
        ho = self.plugin.handoff
        messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(12)]
        out = ho.build("old-session", "telegram:-1003829274549:29",
                       write=lambda prompt: "## Working on\nthe router\n## State\nshipped\n## Next\ntest it",
                       runner=lambda *a, **k: mock.Mock(returncode=0, stdout=json.dumps({"messages": messages})))
        self.assertEqual(out["status"], "ok")

    def hook(self, session_id: str) -> Any:
        # The keywords pre_llm_call really gets: no chat_id, so the lane comes from the store.
        return self.plugin._on_pre_llm_call(session_id=session_id, platform="telegram",
                                            sender_id="1022650411", user_message="hi", is_first_turn=True)

    def test_a_capsule_is_not_fed_back_to_the_session_it_summarises(self):
        """The trigger no longer ends the turn, so the summarised session is usually still
        talking when the capsule lands. Injecting there spent it on the wrong session and
        left the fresh one with nothing."""
        self.assertIsNone(self.hook("old-session"))
        self.assertIsNone(self.hook("old-session"))
        out = self.hook("new-session")
        self.assertIn("the router", out["context"])
        self.assertIn("not a new instruction", out["context"])

    def test_the_next_session_gets_it_exactly_once(self):
        self.assertIsNotNone(self.hook("new-session"))
        self.assertIsNone(self.hook("new-session"))

    def test_injection_can_be_switched_off_without_losing_the_capsule(self):
        ctx = FakeCtx()
        ctx.get_config = lambda name, default=None: False if name == "inject" else default
        self.plugin.register(ctx)
        self.assertIsNone(self.hook("new-session"))
        self.assertTrue(self.plugin.handoff.pending_path("telegram:-1003829274549:29").exists())


# ── confidentiality: refuse, never downgrade ─────────────────────────────────

class ConfidentialContractTests(unittest.TestCase):

    def setUp(self) -> None:
        self.ho = load_handoff("ho_contract")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HANDOFF_CONFIDENTIAL", None)
        self.exports = 0

    def runner(self, *args: Any, **kwargs: Any) -> Any:
        self.exports += 1
        messages = [{"role": "user", "content": "Jane Doe wants the quote revised"} for _ in range(12)]
        return mock.Mock(returncode=0, stdout=json.dumps({"messages": messages}))

    @staticmethod
    def scrub(text: str) -> str:
        return re.sub(r"\d{3}-\d{3}-\d{4}", "[phone]", text)

    def test_an_old_prompt_builder_refuses_even_though_a_scrubber_was_supplied(self):
        """It used to carry on and report "ok". The scrubber is a regex: it takes a phone
        number out and leaves the customer's name exactly where the writer put it."""
        asked: List[str] = []

        def old_prompt_for(body: str, previous: str = "") -> str:      # no `confidential`
            return body

        def write(prompt: str) -> str:
            asked.append(prompt)
            return "## Working on\nJane Doe's quote\n## State\nx\n## Next\ny"

        out = self.ho.build("s1", "lane", write=write, prompt_for=old_prompt_for,
                            valid=lambda t: True, confidential=True, scrub=self.scrub,
                            runner=self.runner)
        self.assertEqual(out["status"], "confidential_unsupported")
        self.assertEqual(asked, [], "the transcript reached the writer with no confidentiality rule")
        self.assertFalse(self.ho.capsule_path("lane").exists())
        self.assertFalse(self.ho.pending_path("lane").exists())

    def test_the_same_old_builder_is_fine_when_nothing_confidential_was_asked_for(self):
        def old_prompt_for(body: str, previous: str = "") -> str:
            return "PROMPT " + body
        out = self.ho.build("s1", "lane", write=lambda p: "## Working on\nx\n## State\ny\n## Next\nz",
                            prompt_for=old_prompt_for, valid=lambda t: True, runner=self.runner)
        self.assertEqual(out["status"], "ok")

    def test_with_no_scrubber_it_refuses_before_it_reads_the_transcript(self):
        """No scrubber means no jevkit: no confidential prompt and no validator either."""
        out = self.ho.build("s1", "lane", write=lambda p: "## Working on\nx", confidential=True,
                            runner=self.runner)
        self.assertEqual(out["status"], "confidential_unsupported")
        self.assertEqual(self.exports, 0)
        self.assertFalse(self.ho.capsule_path("lane").exists())

    def test_with_no_prompt_builder_the_writer_is_still_told_the_rule(self):
        asked: List[str] = []

        def write(prompt: str) -> str:
            asked.append(prompt)
            return "## Working on\nthe customer's quote\n## State\nx\n## Next\ny"

        out = self.ho.build("s1", "lane", write=write, valid=lambda t: True, confidential=True,
                            scrub=self.scrub, runner=self.runner)
        self.assertEqual(out["status"], "ok")
        self.assertIn("Never carry a person's name", asked[0])

    def test_a_jev_outage_is_visible_on_a_result_that_still_says_ok(self):
        """ "ok" means a capsule was written. It was written from an unfiltered transcript,
        and the nightly report has to be able to tell that night from a good one."""
        def down(messages: Any, keep_last: int = 8) -> Dict[str, Any]:
            return {"status": "fail_open", "fates": {}, "counts": {}, "jev_calls": 0, "errors": ["network"]}

        def broken(messages: Any, keep_last: int = 8) -> Dict[str, Any]:
            raise RuntimeError("no key")

        digest = lambda messages, selection, limit: "digest"      # noqa: E731
        write = lambda p: "## Working on\nx\n## State\ny\n## Next\nz"      # noqa: E731
        out = self.ho.build("s1", "lane", write=write, select=down, digest=digest, runner=self.runner)
        self.assertEqual((out["status"], out["jev"], out["jev_errors"]), ("ok", "fail_open", ["network"]))
        out = self.ho.build("s1", "lane", write=write, select=broken, digest=digest, runner=self.runner)
        self.assertEqual((out["status"], out["jev"]), ("ok", "error"))
        out = self.ho.build("s1", "lane", write=write, runner=self.runner)
        self.assertEqual(out["jev"], "not_used")

    def test_asking_whether_a_home_is_confidential_creates_nothing(self):
        """--dry-run asks this question, and --dry-run promises to touch nothing."""
        self.assertFalse(self.ho.confidential_here())
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


class ProfileScopeTests(unittest.TestCase):
    """A gateway serving several profiles scopes a turn with a context-local override."""

    def setUp(self) -> None:
        self.ho = load_handoff("ho_scope")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.profile = Path(self.tmp.name) / "root" / "profiles" / "acme"
        self.profile.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        fake = types.ModuleType("hermes_constants")
        fake.get_hermes_home_override = lambda: str(self.profile)
        modules = mock.patch.dict(sys.modules, {"hermes_constants": fake})
        modules.start()
        self.addCleanup(modules.stop)

    def test_the_turns_profile_wins_over_the_process_environment(self):
        self.assertEqual(self.ho.home(), self.profile)
        self.assertEqual(self.ho.capsule_path("lane").parent, self.profile / "handoffs")

    def test_the_export_is_pointed_at_the_same_profile(self):
        """The child process only sees the environment. Left alone it exports from the root
        database, finds no such session, and a long conversation is reported too short."""
        seen: Dict[str, Any] = {}

        def runner(command: List[str], **kwargs: Any) -> Any:
            seen.update(kwargs.get("env") or {})
            return mock.Mock(returncode=0, stdout="")

        self.ho.export_messages("s1", runner=runner)
        self.assertEqual(seen.get("HERMES_HOME"), str(self.profile))


# ── the nightly script ───────────────────────────────────────────────────────

def make_store(path: Path, rows: List[tuple]) -> None:
    db = sqlite3.connect(path)
    db.execute("create table sessions (id text primary key, source text, chat_id text, thread_id text, "
               "session_key text, started_at real, ended_at real, end_reason text, message_count integer)")
    db.executemany("insert into sessions values (?,?,?,?,?,?,NULL,NULL,?)", rows)
    db.commit()
    db.close()


def open_ids(path: Path) -> List[str]:
    db = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in db.execute("select id from sessions where ended_at is null"))
    finally:
        db.close()


class NightlyLaneCollisionTests(unittest.TestCase):
    """Reproduced in a dry run against a live fleet: four open sessions on one Telegram
    topic. Each wrote its capsule to the same file, newest first, so the OLDEST session's
    capsule was the one left, and all four sessions were closed on top of it."""

    KEY = "agent:devbot:telegram:group:-1003829274549:29"

    def setUp(self) -> None:
        self.nightly = load_nightly()
        self.ho = load_handoff("ho_nightly")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.db = Path(self.tmp.name) / "state.db"
        self.now = 1_000_000.0
        self.built: List[str] = []

    def lane_for(self, session: Dict[str, Any]) -> str:
        return self.ho.lane_key({**session, "session_id": session["id"]})

    def build(self, session_id: str, lane: str) -> Dict[str, Any]:
        """The real build, with the transcript and the writer stubbed. The writer fails
        validation, so the capsule is the transcript and names the session it came from."""
        self.built.append(session_id)
        messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} of {session_id}"}
                    for i in range(8)]
        return self.ho.build(session_id, lane, write=lambda prompt: "", valid=lambda text: False,
                             runner=lambda *a, **k: mock.Mock(returncode=0, stdout=json.dumps({"messages": messages})))

    def run_night(self, sessions: Optional[List[Dict[str, Any]]] = None, build: Any = None) -> List[Dict[str, Any]]:
        if sessions is None:
            sessions = self.nightly.live_sessions(self.db, now=self.now, min_messages=6,
                                                  within_s=3 * 86400, limit=40)
        return self.nightly.hand_off_profile(self.db, sessions, lane_for=self.lane_for,
                                             build=build or self.build, dry_run=False, now=self.now)

    def four_on_one_topic(self) -> None:
        make_store(self.db, [
            ("s-newest", "telegram", "-1003829274549", "29", self.KEY, self.now - 100, 245),
            ("s-third", "telegram", "-1003829274549", "29", self.KEY, self.now - 5_000, 434),
            ("s-second", "telegram", "-1003829274549", "29", self.KEY, self.now - 40_000, 246),
            ("s-oldest", "telegram", "-1003829274549", "29", self.KEY, self.now - 150_000, 801),
        ])

    def test_the_capsule_left_behind_is_the_most_recent_sessions(self):
        self.four_on_one_topic()
        report = self.run_night()
        capsule = self.ho.capsule_path("telegram:-1003829274549:29").read_text(encoding="utf-8")
        self.assertIn("of s-newest", capsule)
        self.assertNotIn("of s-oldest", capsule)
        self.assertEqual(self.built, ["s-newest"], "one lane is one capsule and one set of model calls")
        self.assertEqual(open_ids(self.db), [], "the older copies still have to be closed")
        by_id = {row["session"]: row for row in report}
        self.assertEqual(by_id["s-oldest"]["handoff"], "superseded")
        self.assertEqual(by_id["s-oldest"]["capsule_from"], "s-newest")

    def test_the_pending_marker_names_the_session_that_was_summarised(self):
        """The plugin refuses to inject a capsule into the session it came from, so a
        marker naming the wrong session would let it through."""
        self.four_on_one_topic()
        self.run_night()
        marker = json.loads(self.ho.pending_path("telegram:-1003829274549:29").read_text(encoding="utf-8"))
        self.assertEqual(marker["session_id"], "s-newest")

    def test_which_session_wins_does_not_depend_on_the_order_they_arrive_in(self):
        self.four_on_one_topic()
        sessions = self.nightly.live_sessions(self.db, now=self.now, min_messages=6, within_s=3 * 86400, limit=40)
        self.run_night(list(reversed(sessions)))
        self.assertEqual(self.built, ["s-newest"])

    def test_a_failed_capsule_leaves_every_session_on_that_lane_open(self):
        """Closing the older copies anyway would end the conversation with nothing carried."""
        self.four_on_one_topic()
        report = self.run_night(build=lambda session_id, lane: {"status": "write_failed"})
        self.assertEqual(len(open_ids(self.db)), 4)
        self.assertFalse(any(row["closed"] for row in report))

    def test_a_build_that_raises_is_a_failed_capsule_not_a_crashed_night(self):
        self.four_on_one_topic()

        def explode(session_id: str, lane: str) -> Dict[str, Any]:
            raise RuntimeError("boom")

        report = self.run_night(build=explode)
        self.assertEqual(report[0]["handoff"], "error")
        self.assertEqual(len(open_ids(self.db)), 4)

    def test_two_people_in_one_group_are_not_closed_as_copies_of_each_other(self):
        """Per-user sessions in a group share a lane file but not a conversation. Only the
        newer one is summarised tonight; the other keeps its thread by staying open."""
        make_store(self.db, [
            ("s-alice", "telegram", "-100777", None, "agent:main:telegram:group:-100777:alice", self.now - 100, 40),
            ("s-bob", "telegram", "-100777", None, "agent:main:telegram:group:-100777:bob", self.now - 900, 40),
        ])
        report = self.run_night()
        self.assertEqual(self.built, ["s-alice"])
        self.assertEqual(open_ids(self.db), ["s-bob"])
        self.assertEqual({row["session"]: row["handoff"] for row in report}["s-bob"], "lane_shared")

    def test_sessions_with_no_conversation_identity_are_never_closed_unsummarised(self):
        """CLI sessions all resolve to one platform-wide lane. They are different pieces
        of work, not stale copies, so only the one that got a capsule is closed."""
        make_store(self.db, [
            ("cli-today", "cli", None, None, None, self.now - 100, 30),
            ("cli-yesterday", "cli", None, None, None, self.now - 90_000, 30),
        ])
        self.run_night()
        self.assertEqual(self.built, ["cli-today"])
        self.assertEqual(open_ids(self.db), ["cli-yesterday"])

    def test_a_stale_cron_run_is_not_summarised_into_the_next_cron_job(self):
        """Every cron job shares the lane "cron". A capsule from one job's abandoned run
        would be injected into a different job as history it had already established."""
        make_store(self.db, [
            ("cron_87473d0a7883_run", "cron", None, None, None, self.now - 100, 11),
            ("s-dm", "telegram", "1022650411", None, "agent:main:telegram:dm:1022650411", self.now - 200, 60),
        ])
        report = self.run_night()
        self.assertEqual(self.built, ["s-dm"])
        self.assertEqual(open_ids(self.db), ["cron_87473d0a7883_run"])
        self.assertEqual({row["session"]: row["handoff"] for row in report}["cron_87473d0a7883_run"], "skipped")
        self.assertFalse(self.ho.pending_path("cron").exists())

    def test_separate_conversations_each_get_their_own_capsule(self):
        make_store(self.db, [
            ("s-topic-29", "telegram", "-1003829274549", "29", self.KEY, self.now - 100, 40),
            ("s-topic-45", "telegram", "-1003829274549", "45", self.KEY.replace(":29", ":45"), self.now - 200, 40),
        ])
        self.run_night()
        self.assertEqual(sorted(self.built), ["s-topic-29", "s-topic-45"])
        self.assertEqual(open_ids(self.db), [])


class NightlyDryRunTests(unittest.TestCase):
    """Run as a real subprocess: "touches nothing" is a claim about the whole command."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "hermes-home"
        plugin = self.home / "plugins" / "hermes-handoff"
        plugin.mkdir(parents=True)
        for name in ("__init__.py", "handoff.py"):
            shutil.copy2(PLUGIN_DIR / name, plugin / name)
        self.now = time.time()

    def run_script(self, *args: str) -> "subprocess.CompletedProcess[str]":
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERMES_HOME", "HANDOFF_CONFIDENTIAL", "PYTHONDONTWRITEBYTECODE")}
        return subprocess.run([sys.executable, str(NIGHTLY), "--hermes-home", str(self.home), *args],
                              capture_output=True, text=True, timeout=60, env=env)

    def snapshot(self) -> Dict[str, Any]:
        return {str(p.relative_to(self.home)): (p.is_dir(), None if p.is_dir() else p.read_bytes())
                for p in sorted(self.home.rglob("*"))}

    def test_a_dry_run_leaves_the_home_byte_for_byte_as_it_found_it(self):
        """It wrote logs/nightly-handoff.json while its docstring said it touched nothing."""
        profile = self.home / "profiles" / "devbot"
        profile.mkdir(parents=True)
        key = "agent:devbot:telegram:group:-1003829274549:29"
        make_store(profile / "state.db", [
            ("s-newest", "telegram", "-1003829274549", "29", key, self.now - 100, 245),
            ("s-oldest", "telegram", "-1003829274549", "29", key, self.now - 9_000, 801),
        ])
        before = self.snapshot()
        done = self.run_script("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.snapshot(), before)
        plan = json.loads(done.stdout)
        self.assertTrue(plan["dry_run"])
        actions = {row["session"]: row["action"] for row in plan["profiles"][0]["sessions"]}
        self.assertIn("would hand off", actions["s-newest"])
        self.assertIn("would close", actions["s-oldest"])

    def test_an_install_with_no_named_profiles_is_not_skipped(self):
        """Most installs are one home and no profiles/ directory. The script looked only
        under profiles/, so for them it reported nothing and did nothing, every night."""
        make_store(self.home / "state.db", [
            ("s-dm", "telegram", "1022650411", None, "agent:main:telegram:dm:1022650411", self.now - 100, 60),
        ])
        done = self.run_script("--dry-run")
        self.assertEqual(done.returncode, 0, done.stderr)
        plan = json.loads(done.stdout)
        self.assertEqual([p["profile"] for p in plan["profiles"]], ["default"])
        self.assertEqual(plan["profiles"][0]["sessions"][0]["session"], "s-dm")

    def test_a_real_run_still_leaves_its_report(self):
        """Nothing is live here, so no model is called; the report must exist regardless."""
        make_store(self.home / "state.db", [])
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads((self.home / "logs" / "nightly-handoff.json").read_text(encoding="utf-8"))
        self.assertFalse(report["dry_run"])


class RecoveryBlockTests(unittest.TestCase):
    """A capsule is a few hundred words. What makes it enough is a way back to the rest."""

    def setUp(self) -> None:
        self.ho = load_handoff("ho_recovery")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HANDOFF_CONFIDENTIAL", None)
        self.exports = 0

    scrub = staticmethod(ConfidentialContractTests.scrub)

    CAPSULE = "## Working on\nx\n## State\ny\n## Decisions\nd\n## Pointers\n/srv/app/render.toml\n## Next\nship it"

    def runner(self, *args: Any, **kwargs: Any) -> Any:
        self.exports += 1
        messages = [{"role": "user" if i % 2 == 0 else "assistant",
                     "content": f"turn {i}: the queue config is /srv/app/conf/render.toml on port 8431"}
                    for i in range(self.turns)]
        return mock.Mock(returncode=0, stdout=json.dumps({"messages": messages}))

    turns = 12

    def test_the_capsule_names_its_session_and_the_search_that_works(self):
        """It carried no session id and never said a search exists, so the one mechanism
        measured to work (a summary plus one search: 43% -> 79%) was out of reach."""
        out = self.ho.build("20260101_000000_abc", "lane", write=lambda p: self.CAPSULE, runner=self.runner)
        self.assertEqual(out["status"], "ok")
        text = self.ho.capsule_path("lane").read_text(encoding="utf-8")
        self.assertIn("## Recovery", text)
        self.assertIn("`20260101_000000_abc` (12 messages)", text)
        self.assertIn('around_message_id=<match_message_id>', text)
        self.assertIn('role_filter="user,assistant,tool"', text)
        # a list of the session's identifiers was tried and measured: no help, so none here
        self.assertNotIn("most repeated first", text)
        # query= together with session_id= reads from the top and ignores the query
        self.assertNotRegex(text, r"session_search\(query=[^)]*session_id=")

    def test_a_confidential_capsule_gets_no_recovery_block(self):
        def prompt_for(body: str, previous: str = "", *, confidential: bool = False, marked: bool = True) -> str:
            return body
        out = self.ho.build("s1", "lane", write=lambda p: self.CAPSULE, prompt_for=prompt_for,
                            valid=lambda t: True, confidential=True, scrub=self.scrub, runner=self.runner)
        self.assertEqual(out["status"], "ok")
        self.assertNotIn("## Recovery", self.ho.capsule_path("lane").read_text(encoding="utf-8"))

    def test_the_next_capsule_does_not_inherit_the_last_ones_recovery_block(self):
        """Left in `previous`, the writer copied it, and the note ended with two blocks
        pointing at two different sessions."""
        seen: List[str] = []

        def prompt_for(body: str, previous: str = "", *, confidential: bool = False, marked: bool = True) -> str:
            seen.append(previous)
            return body
        self.ho.build("first_session", "lane", write=lambda p: self.CAPSULE, prompt_for=prompt_for, runner=self.runner)
        self.ho.build("second_session", "lane", write=lambda p: self.CAPSULE, prompt_for=prompt_for, runner=self.runner)
        self.assertIn("## Pointers", seen[1])
        self.assertNotIn("## Recovery", seen[1])
        self.assertNotIn("first_session", seen[1])
        text = self.ho.capsule_path("lane").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Recovery"), 1)
        self.assertIn("second_session", text)

    def test_a_plain_transcript_is_sent_untagged_with_a_prompt_that_knows_it(self):
        """The shipped no-Jev path tagged every line [background] under a prompt saying
        background "only needs its gist": an instruction to paraphrase the whole session."""
        seen: Dict[str, Any] = {}

        def prompt_for(body: str, previous: str = "", *, confidential: bool = False, marked: bool = True) -> str:
            seen.update(body=body, marked=marked)
            return body
        self.ho.build("s1", "lane", write=lambda p: self.CAPSULE, prompt_for=prompt_for, runner=self.runner)
        self.assertFalse(seen["marked"])
        self.assertNotIn("[background]", seen["body"])

        def select(messages: Any, keep_last: int = 8) -> Dict[str, Any]:
            return {"status": "ok", "fates": {}, "counts": {}, "jev_calls": 1}
        self.ho.build("s1", "lane", write=lambda p: self.CAPSULE, prompt_for=prompt_for, select=select,
                      digest=lambda messages, selection, limit: "[KEEP VERBATIM] user: x", runner=self.runner)
        self.assertTrue(seen["marked"])

    def test_a_confidential_lane_keeps_the_narrow_read(self):
        """The wide read was measured at 1,200 words. A confidential capsule is 400 whatever
        the writer saw, so widening it would send far more of a customer's conversation to
        the auxiliary model and buy nothing."""
        self.turns = 400                       # ~26,000 characters, over the narrow budget
        seen: Dict[str, Any] = {}

        def prompt_for(body: str, previous: str = "", *, confidential: bool = False, marked: bool = True) -> str:
            seen[confidential] = len(body)
            return body
        self.ho.build("s1", "lane", write=lambda p: self.CAPSULE, prompt_for=prompt_for,
                      valid=lambda t: True, confidential=True, scrub=lambda s: s, runner=self.runner)
        self.ho.build("s1", "other", write=lambda p: self.CAPSULE, prompt_for=prompt_for, runner=self.runner)
        self.assertLessEqual(seen[True], self.ho.CONFIDENTIAL_TRANSCRIPT_CHARS)
        self.assertGreater(seen[False], self.ho.CONFIDENTIAL_TRANSCRIPT_CHARS)
        self.assertLessEqual(seen[False], self.ho.TRANSCRIPT_CHARS)

    def test_an_older_jevkit_without_the_marked_flag_still_gets_a_capsule(self):
        def old_prompt_for(body: str, previous: str = "", *, confidential: bool = False) -> str:
            return "PROMPT " + body
        out = self.ho.build("s1", "lane", write=lambda p: self.CAPSULE, prompt_for=old_prompt_for, runner=self.runner)
        self.assertEqual(out["status"], "ok")

    def test_an_over_long_capsule_loses_its_middle_not_its_pointers_and_next(self):
        """[:6000] cut the end, and the end is Pointers and Next: the exact paths and the
        first thing to do, the two sections the next session cannot rebuild."""
        long = ("## Working on\nx\n## State\n" + "filler sentence. " * 1200 +
                "\n## Decisions\nd\n## Pointers\n/srv/app/render.toml\n## Next\nrestart the render unit")
        self.assertGreater(len(long), self.ho.CAPSULE_MAX_CHARS)
        fitted = self.ho._fit(long, self.ho.CAPSULE_MAX_CHARS)
        self.assertLessEqual(len(fitted), self.ho.CAPSULE_MAX_CHARS)
        self.assertIn("/srv/app/render.toml", fitted)
        self.assertIn("restart the render unit", fitted)
        self.assertIn("trimmed to fit", fitted)
        self.assertEqual(self.ho._fit("short", 100), "short")

    def test_a_very_long_lane_is_judged_on_its_recent_turns_only(self):
        """600 turns was 15 Jev requests in a row, up to 8 s each, before the writer started."""
        self.turns = 600
        seen: List[int] = []

        def select(messages: Any, keep_last: int = 8) -> Dict[str, Any]:
            seen.append(len(messages))
            return {"status": "ok", "fates": {}, "counts": {}, "jev_calls": 1}
        out = self.ho.build("s1", "lane", write=lambda p: self.CAPSULE, select=select,
                            digest=lambda messages, selection, limit: "digest", runner=self.runner)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(seen, [self.ho.MAX_JUDGED_TURNS])
        self.assertIn("(600 messages)", self.ho.capsule_path("lane").read_text(encoding="utf-8"))


class JevPrepassDefaultTests(unittest.TestCase):
    """The pre-pass is opt-in because it was measured and lost. See evals/compaction."""

    NAME = "hermes_handoff_prepass_under_test"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HANDOFF_JEV", None)
        os.environ.pop("HANDOFF_CONFIDENTIAL", None)
        self.plugin = load_plugin(self.NAME)
        self.addCleanup(unload, self.NAME)

    def wired(self) -> Dict[str, Any]:
        """What run_handoff hands to build(), with a jevkit that has every helper."""
        fake = types.SimpleNamespace(select=object(), digest=object(), handoff_prompt=object(),
                                     looks_like_capsule=object(), redact_capsule=object())
        seen: Dict[str, Any] = {}

        def build(session_id: str, lane: str, **kwargs: Any) -> Dict[str, Any]:
            seen.update(kwargs)
            return {"status": "ok"}
        with mock.patch.object(self.plugin, "_jevkit", lambda: fake), \
                mock.patch.object(self.plugin.handoff, "build", build), \
                mock.patch.object(self.plugin, "_writer", lambda: (lambda prompt: "x")):
            self.plugin.run_handoff({"session_id": "s1", "platform": "cli"})
        return seen

    def test_with_a_key_and_no_opt_in_the_writer_gets_the_plain_tail(self):
        seen = self.wired()
        self.assertIsNone(seen["select"])
        self.assertIsNone(seen["digest"])
        # the prompt and the validator cost nothing and stay on
        self.assertIsNotNone(seen["prompt_for"])
        self.assertIsNotNone(seen["valid"])

    def test_handoff_jev_turns_the_pre_pass_back_on(self):
        for value in ("1", "true", "ON", " yes "):
            with mock.patch.dict(os.environ, {"HANDOFF_JEV": value}):
                seen = self.wired()
                self.assertIsNotNone(seen["select"], value)
                self.assertIsNotNone(seen["digest"], value)
        for value in ("0", "", "off", "no"):
            with mock.patch.dict(os.environ, {"HANDOFF_JEV": value}):
                self.assertIsNone(self.wired()["select"], value)


class ConfidentialPreviousCapsuleTests(unittest.TestCase):
    def test_a_previous_capsule_never_reinstates_keep_identifiers_under_confidentiality(self):
        """The last sentence the writer read said "Do not lose identifiers" - after the rules
        forbidding them. A previous capsule is where an old identifier is most likely hiding."""
        from jevkit import compact
        prompt = compact.handoff_prompt("[KEEP VERBATIM] user: x", previous="## Pointers\nold", confidential=True)
        self.assertNotIn("Do not lose identifiers", prompt)
        self.assertIn("do NOT carry it forward", prompt)
        self.assertIn("Do not lose identifiers",
                      compact.handoff_prompt("[KEEP VERBATIM] user: x", previous="## Pointers\nold"))


class LaneKeyIsAFilenameTests(unittest.TestCase):
    """A lane key is not just an id: it becomes a FILENAME (``handoff-<lane>.md``).

    On Windows ``:`` is illegal in a filename and NTFS does not raise —
    ``handoff-telegram:1.md`` is silently created as the file ``handoff-telegram`` plus an
    ALTERNATE DATA STREAM named ``1.md``. The write reports success, ``exists()`` is False at the
    real path, ``take_pending()`` finds nothing, and the capsule is never injected. Nothing caught
    this because CI is Linux and macOS, where ``:`` is a perfectly legal filename character; a
    Windows host simply never gets a handoff and nothing anywhere reports an error.
    """

    def setUp(self) -> None:
        # Isolate HERMES_HOME like every other case in this file: capsule_path() creates
        # <home>/handoffs, and a test that only wants a filename must not write into a real ~/.hermes.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.handoff = load_handoff("handoff_lane_under_test")

    def test_lane_key_carries_no_filesystem_illegal_character(self) -> None:
        lane = self.handoff.lane_key(
            {"platform": "telegram", "chat_id": "5243073207", "thread_id": ""})
        for ch in ': * ? " < > |'.split():
            self.assertNotIn(ch, lane, "lane key would be an illegal filename character: %r" % ch)

    def test_a_raw_lane_lands_where_capsule_path_says(self) -> None:
        """Drives the PATH BOUNDARY with an un-cleaned lane, which is what build() and
        take_pending() accept from any caller.

        Note what is deliberately NOT asserted: that exists() is False before the fix. It is not —
        Python reads and writes an NTFS alternate data stream perfectly happily, so the filesystem
        cannot see this bug at all. Only the NAME can be asserted portably, which is why the test
        above exists and why this one checks the round trip rather than the visibility.
        """
        raw = "telegram:-1003829274549:29"
        target = self.handoff.capsule_path(raw)
        self.assertEqual(target, self.handoff.capsule_path(self.handoff.lane_key(
            {"platform": "telegram", "chat_id": "-1003829274549", "thread_id": "29"})))
        for ch in ': * ? " < > |'.split():
            self.assertNotIn(ch, target.name)
        target.write_text("capsule", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "capsule")

    def test_truncation_and_fingerprint_still_hold(self) -> None:
        lane = self.handoff._clean("t" + "x" * 400)
        self.assertLessEqual(len(lane), 120)
        self.assertIn("-", lane, "a long key must keep its hash fingerprint")
        # Idempotency is load-bearing: capsule_path() re-cleans whatever it is given, and the
        # truncated output is exactly LANE_MAX long while the guard is `<=`. Flip that to `<` and
        # every long lane silently re-hashes, splitting the writer from the reader with no test
        # failing — so it is pinned here, at both ends of the boundary.
        self.assertEqual(self.handoff._clean(lane), lane)
        for length in (118, 119, 120, 121):
            key = "t" + "x" * (length - 1)
            once = self.handoff._clean(key)
            self.assertEqual(self.handoff._clean(once), once, "not idempotent at len %d" % length)

    def test_colon_is_encoded_not_substituted_so_lanes_cannot_collide(self) -> None:
        """':' is the STRUCTURAL separator. Mapping it to '_' would make chat "a_b" and chat "a"
        with thread "b" the same lane — the collision that hands one conversation's capsule to
        another. Percent-encoding keeps the mapping injective."""
        a = self.handoff._clean("telegram:a_b")
        b = self.handoff._clean("telegram:a:b")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
