#!/usr/bin/env python3
"""Bounded browser-use + Jev runner for Jev Ultrafast.

Jev Ultrafast chooses an operation and an observed target; the executor performs
exactly that one action. Nothing here lets the model emit selectors, code, or
free-form text: the operation and target must map to elements that were observed
on the live page.

Safety properties enforced by this runner
-----------------------------------------
* Credentials come from the process environment, else macOS Keychain. They are
  never written to disk, never placed in config, and never printed.
* Live runs require an explicit ``--allow-hosts`` allowlist. The loop aborts
  mid-run if the page's host leaves the allowlist, so an agent cannot wander off
  the approved site.
* ``--max-ticks`` hard-caps the number of Jev model calls.
* Exactly one browser context is used, and it is always closed.
* Success is an independently verified page/postcondition check, never the
  agent's own DONE choice.
* Refuses to start without a TypeSafe credential.

Self-locating: if ``jev_ultrafast`` is not importable in the current interpreter,
this script re-execs itself with the vendored repo's virtualenv python.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import hashlib
import math
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(os.environ.get("JEV_ULTRAFAST_REPO") or Path.home() / "jev-ultrafast").expanduser()
VENV_PY = REPO / ".venv" / "bin" / "python"
KEYCHAIN_TYPESAFE = ("Hermes TypeSafe API", "TYPESAFE_API_KEY")
DEFAULT_TEXT_MODEL = "google/gemini-2.5-flash"
DEFAULT_TEXT_BASE = "https://openrouter.ai/api/v1"

# Chrome binaries we are willing to launch ourselves, in preference order.
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


# ─ credentials ──────────────────────────────────────────────────────────────

def keychain_get(service: str, account: str) -> str | None:
    """Read one generic-password item. Returns None when absent."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


TEXT_SETTINGS = ("TEXT_MODEL_PROVIDER", "TEXT_MODEL", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_RESPONSE_FORMAT",
                 "TEXT_MODEL_REASONING")


def text_model_config(env: dict) -> dict:
    """The machine's text helper, from ``JEV_BROWSER_CONFIG`` or ``~/.config/jev/browser.json``.

    One file every agent on the machine reads (Claude Code, Codex, each Hermes profile), so
    the helper is chosen once. Keys are the TEXT_* names; it never holds a credential.
    """
    path = Path(env.get("JEV_BROWSER_CONFIG")
                or Path(env.get("XDG_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
                        or Path.home() / ".config") / "jev" / "browser.json")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items() if k in TEXT_SETTINGS and v} if isinstance(data, dict) else {}


def _is_local(base: str) -> bool:
    return (urlparse(base).hostname or "") in ("127.0.0.1", "localhost", "::1")


def resolve_credentials(env: dict, lookup=None) -> dict:
    """Environment first, then the machine's browser config, then Keychain.

    ``lookup`` is resolved at call time, so tests (and any caller) can
    substitute a deterministic lookup without patching the default binding.
    """
    if lookup is None:
        lookup = keychain_get
    typesafe = env.get("TYPESAFE_API_KEY") or lookup(*KEYCHAIN_TYPESAFE)
    settings = {**text_model_config(env), **{k: env[k] for k in TEXT_SETTINGS if env.get(k)}}
    base = settings.get("TEXT_MODEL_BASE_URL", DEFAULT_TEXT_BASE)
    # The TypeSafe key already fell back to the secret store; the text key did not. An
    # agent's environment carries neither, so the loop started fine and then died the
    # first time Jev chose to TYPE - which on most sites is the very first action. It
    # only ever worked from a shell where someone had exported the key by hand.
    cli = settings.get("TEXT_MODEL_PROVIDER") == "claude-cli"   # Claude Code's own sign-in; no key
    text_key = None if cli else (
        env.get("TEXT_MODEL_API_KEY") or env.get("OPENROUTER_API_KEY")
        or ("local" if _is_local(base) else None)   # a local server needs none
        or lookup("OPENROUTER_API_KEY", env.get("USER") or os.environ.get("USER", "")))
    resolved = {}
    if cli:
        # Agents often run without ~/.local/bin on PATH, where the Claude Code installer puts it.
        found = env.get("CLAUDE_CLI") or shutil.which("claude", path=env.get("PATH")) or next(
            (str(p) for p in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude")) if p.exists()), None)
        if found:
            resolved["CLAUDE_CLI"] = found
    if typesafe:
        resolved["TYPESAFE_API_KEY"] = typesafe
    if text_key:
        resolved["TEXT_MODEL_API_KEY"] = text_key
    resolved["TYPESAFE_MODEL"] = env.get("TYPESAFE_MODEL", "jev-latest")
    resolved["TEXT_MODEL"] = settings.get("TEXT_MODEL", DEFAULT_TEXT_MODEL)
    resolved["TEXT_MODEL_BASE_URL"] = base
    for k in ("TEXT_MODEL_PROVIDER", "TEXT_MODEL_RESPONSE_FORMAT", "TEXT_MODEL_REASONING"):
        if k in settings:
            resolved[k] = settings[k]
    return resolved


def redact(secret: str | None) -> str:
    if not secret:
        return "(absent)"
    return f"present(len={len(secret)})"


# ── host allowlist ───────────────────────────────────────────────────────────

def host_allowed(url: str, allow_hosts: list[str]) -> bool:
    """True when the URL's host is the allowlisted host or a subdomain of it."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    for allowed in allow_hosts:
        a = allowed.strip().lower().lstrip(".")
        if a and (host == a or host.endswith("." + a)):
            return True
    return False


# ── outcome verification ─────────────────────────────────────────────────────

def outcome_verified(title: str, heading: str, url: str, expect: str) -> bool:
    """Independent check against live page state, not the agent's DONE choice."""
    if not expect:
        return False
    haystack = " ".join([title or "", heading or "", url or ""]).lower()
    return expect.lower() in haystack


class BrowserProgress:
    """Local semantic progress, not model confidence or an authorization verdict.

    Only digests and numeric/boolean metadata enter the trace. Raw DOM, labels,
    URLs and typed values remain in the executor and are not forwarded to Jev.
    """
    def __init__(self, max_stagnant=3):
        self.max_stagnant = max_stagnant
        self.stagnant = 0
        self.previous = None
        self.trace = []

    def observe(self, page, *, proposed=None, executed=False):
        digest = hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest() if page is not None else None
        changed = (self.previous is not None and digest != self.previous) if digest is not None else None
        if digest is not None:
            self.stagnant = 0 if changed else self.stagnant + int(executed)
            self.previous = digest
        proposed = proposed or {}
        confidence = proposed.get('confidence')
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            confidence = None
        row = {'observation_sha256': digest, 'progress': changed,
               'proposed': bool(proposed), 'executed': bool(executed),
               'confidence': confidence, 'stagnant': self.stagnant}
        self.trace.append(row)
        return row

    @property
    def blocked(self):
        return self.stagnant >= self.max_stagnant


# Include controls that can change without URL/title/body-copy changes. No input
# values are read; checkbox/selected/expanded state is sufficient for this probe.
SEMANTIC_PAGE = """(() => ({url: location.href, title: document.title,
  text: (document.body?.innerText || '').slice(0, 20000), scroll: [scrollX, scrollY],
  controls: [...document.querySelectorAll('input,select,option,button,[aria-expanded],[aria-checked]')]
    .slice(0, 128).map(e => ({tag:e.tagName, type:e.type || '', checked:!!e.checked,
      selected:!!e.selected, index:e.selectedIndex ?? null,
      expanded:e.getAttribute('aria-expanded'), ariaChecked:e.getAttribute('aria-checked'),
      disabled:!!e.disabled}))}))()"""


# ── browser ownership ────────────────────────────────────────────────────────

def find_chrome(env: Mapping[str, str] | None = None) -> str | None:
    """Locate a Chrome/Chromium binary without ever touching the person's profile."""
    env = env if env is not None else os.environ
    for key in ("BH_CHROME_PATH", "CHROME_PATH"):
        candidate = env.get(key)
        if candidate and Path(candidate).exists():
            return candidate
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def chrome_args(port: int, profile_dir: str, url: str = "about:blank") -> list[str]:
    """Flags for a throwaway, headless, isolation-friendly browser we own."""
    return [
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--metrics-recording-only",
        "--enable-unsafe-swiftshader",
        "--window-size=1280,900",
        url,
    ]


def wait_for_cdp(port: int, timeout: float = 30.0, opener=None) -> str | None:
    """Poll the DevTools endpoint until it hands us a websocket URL."""
    opener = opener or (lambda url: urllib.request.urlopen(url, timeout=2))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            payload = json.loads(opener(f"http://127.0.0.1:{port}/json/version").read().decode())
            websocket = payload.get("webSocketDebuggerUrl")
            if websocket:
                return websocket
        except Exception:  # noqa: BLE001  (not up yet, or not a CDP port)
            pass
        time.sleep(0.5)
    return None


class OwnedChrome:
    """A Chrome this process launched, on a throwaway profile, closed on exit.

    The person's everyday browser is never attached to, and never has remote
    debugging enabled. browser-harness needs a CDP endpoint, so when the caller
    has not supplied one we bring our own.
    """

    def __init__(self, chrome_path: str, url: str = "about:blank", port: int | None = None,
                 startup_timeout: float = 30.0):
        self.chrome_path = chrome_path
        self.url = url
        self.port = port or free_port()
        self.startup_timeout = startup_timeout
        self.log_path: Path | None = None
        self.profile_dir: str | None = None
        self.process: subprocess.Popen | None = None
        self.websocket: str | None = None

    def start(self) -> str:
        self.profile_dir = tempfile.mkdtemp(prefix="jev-browser-profile-")
        handle, log_name = tempfile.mkstemp(prefix="jev-browser-chrome-", suffix=".log")
        os.close(handle)
        self.log_path = Path(log_name)
        with open(self.log_path, "wb") as log:  # the child keeps its own descriptor
            self.process = subprocess.Popen(
                [self.chrome_path, *chrome_args(self.port, self.profile_dir, self.url)],
                stdout=log, stderr=subprocess.STDOUT,
            )
        websocket = wait_for_cdp(self.port, timeout=self.startup_timeout)
        if not websocket:
            tail = ""
            with contextlib.suppress(Exception):
                tail = self.log_path.read_text(errors="replace")[-400:]
            self.stop()
            raise RuntimeError(f"owned Chrome did not expose CDP on port {self.port}. {tail}".strip())
        self.websocket = websocket
        return websocket

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            for _ in range(20):
                if self.process.poll() is not None:
                    break
                time.sleep(0.5)
            if self.process.poll() is None:
                self.process.kill()
        if self.profile_dir:
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.profile_dir = None

    def __enter__(self) -> "OwnedChrome":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


@contextlib.contextmanager
def browser_endpoint(args):  # retained for callers that prefer explicit scoping
    if args.cdp:
        os.environ["BU_CDP_WS"] = args.cdp
        yield args.cdp
        return
    owned = start_owned_browser(args)
    try:
        yield owned.websocket
    finally:
        owned.stop()


def start_owned_browser(args) -> "OwnedChrome":
    """Launch our own Chrome when the caller did not supply a CDP endpoint."""
    chrome = args.chrome_path or find_chrome()
    if not chrome:
        raise SystemExit(
            "FAIL: no CDP endpoint was supplied and no Chrome/Chromium binary was found. "
            "Pass --cdp ws://…, set BU_CDP_WS, install Chrome, or point BH_CHROME_PATH at one. "
            "(Use --no-launch-chrome to require an attached browser instead.)"
        )
    owned = OwnedChrome(chrome, url="about:blank")
    try:
        os.environ["BU_CDP_WS"] = owned.start()
    except RuntimeError as error:
        raise SystemExit(f"FAIL: {error}") from error
    print(f"  browser: our own Chrome (pid={owned.process.pid if owned.process else '?'}, "
          f"port={owned.port}, throwaway profile) — closed on exit")
    return owned


# ── runner ───────────────────────────────────────────────────────────────────

def ensure_importable(argv: list[str] | None = None) -> None:
    """Re-exec into the vendored venv when jev_ultrafast is not importable.

    Forwards the arguments this run was actually invoked with. Never uses
    ``sys.argv`` when the caller supplied explicit arguments, because a
    programmatic caller's ``sys.argv`` belongs to the host process and would
    re-exec with the wrong flags.
    """
    try:
        import jev_ultrafast  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
        forwarded = list(argv) if argv is not None else sys.argv[1:]
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *forwarded])
    raise SystemExit(
        "jev_ultrafast is not importable and no vendored venv was found at "
        f"{VENV_PY}. Clone https://github.com/browser-use/jev-ultrafast, run `uv sync` in it, "
        "and set JEV_ULTRAFAST_REPO to that folder."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bounded browser-use + Jev runner")
    p.add_argument("--url", required=True, help="Starting URL.")
    p.add_argument("--goal", required=True, help="One narrow, non-sensitive goal.")
    p.add_argument("--allow-hosts", required=True,
                   help="Comma-separated host allowlist. The run aborts if the page leaves it.")
    p.add_argument("--max-ticks", type=int, default=10, help="Hard cap on Jev calls (default 10).")
    p.add_argument("--expect", default="", help="Substring that must appear in title/h1/url to PASS.")
    p.add_argument("--cdp", default=os.environ.get("BU_CDP_WS", ""),
                   help="Existing CDP websocket. Defaults to BU_CDP_WS / the attached browser.")
    p.add_argument("--chrome-path", default=os.environ.get("BH_CHROME_PATH", ""),
                   help="Chrome/Chromium binary to launch when no CDP endpoint is given.")
    p.add_argument("--launch-chrome", dest="launch_chrome", action="store_true", default=True,
                   help="Launch our own throwaway Chrome when no CDP endpoint is given (default).")
    p.add_argument("--no-launch-chrome", dest="launch_chrome", action="store_false",
                   help="Require an already-attached browser instead of launching one.")
    p.add_argument("--json", action="store_true", help="Emit a machine-readable result as the last line.")
    return p


def run_agent(agent, *, allow, expect, max_ticks):
    """Exercise the real predict/act boundary, retaining failed execution evidence.

    Caller owns the context and its cleanup. No recovery guesses or extra model
    calls: stale observations consume a tick; all terminal paths are explicit.
    """
    if type(max_ticks) is not int or not 1 <= max_ticks <= 100:
        raise ValueError('max_ticks must be an integer between 1 and 100')
    progress = BrowserProgress()
    ticks = 0
    left_allowlist = False
    stop_reason = 'tick_budget'
    error = None
    title = heading = ''
    try:
        progress.observe(agent.browser.evaluate(SEMANTIC_PAGE))
        while ticks < max_ticks:
            if not host_allowed(agent.state['page'].get('url', ''), allow):
                left_allowlist = True
                stop_reason = 'allowlist'
                break
            before = len(agent.state['history'])
            proposed = None
            ticks += 1
            try:
                agent.command('predict')
                proposed = dict(agent.state.get('decision') or {})
                # predict may refresh a stale page; recheck before any input.
                if not host_allowed(agent.state['page'].get('url', ''), allow):
                    left_allowlist = True
                    stop_reason = 'allowlist'
                    break
                agent.command('act', {'fingerprint': agent.state['page']['fingerprint']})
            except Exception as exc:
                if type(exc).__name__ != 'StalePage':
                    raise
                agent.state['page'] = agent.browser.observe(screenshot=False)
                agent.state['status'] = 'ready'
            finally:
                executed = len(agent.state['history']) > before
                try:
                    observation = agent.browser.evaluate(SEMANTIC_PAGE)
                except Exception:
                    # Mutation may have happened before observation failed. Keep
                    # that receipt, but never fabricate a successful observation.
                    progress.observe(None, proposed=proposed, executed=executed)
                    raise
                progress.observe(observation, proposed=proposed, executed=executed)
            if not host_allowed(agent.state['page'].get('url', ''), allow):
                left_allowlist = True
                stop_reason = 'allowlist'
                break
            if progress.blocked:
                stop_reason = 'no_progress'
                break
            # The vendor's older text fingerprint misses checkbox/menu changes.
            # Override only its post-execution stagnation heuristic, never an
            # explicit BLOCKED decision or an exception/budget failure.
            repeated = agent.state['history'][-3:]
            vendor_stagnation = (len(repeated) == 3 and
                                 all(row.get('page_changed') is False and row.get('kind') != 'wait'
                                     for row in repeated))
            if (agent.state['status'] == 'blocked' and vendor_stagnation and proposed
                    and proposed.get('choice') not in ('BLOCKED', 'DONE')
                    and progress.trace[-1]['executed'] and progress.trace[-1]['progress']):
                agent.state['status'] = 'ready'
            if agent.state['status'] in ('done', 'blocked'):
                stop_reason = 'model_done' if agent.state['status'] == 'done' else 'agent_blocked'
                break
    except Exception as exc:
        error = type(exc).__name__  # never log error text containing a URL or typed value
        stop_reason = 'execution_or_observation_error'
    final_url = agent.state['page'].get('url', '')
    try:
        title = agent.browser.evaluate('document.title') or ''
        heading = agent.browser.evaluate("(document.querySelector('h1')||{}).textContent||''") or ''
    except Exception as exc:
        error = type(exc).__name__
        stop_reason = 'verification_error'
    # A matched title cannot overrule a failed executor, exhausted budget or host escape.
    verified = (stop_reason == 'model_done' and not error and not left_allowlist
                and outcome_verified(title, heading, final_url, expect))
    outcome = 'completed' if verified else ('degraded' if error or stop_reason == 'model_done' else 'blocked')
    return {'schema': 'hermes.browser_use_jev_run_v1', 'final_url': final_url, 'title': title,
            'heading': heading, 'steps': len(agent.state['history']),
            'text_calls': len(agent.state['text_calls']), 'ticks': ticks,
            'left_allowlist': left_allowlist, 'expected': expect, 'verified': bool(verified),
            'outcome': outcome, 'stop_reason': stop_reason, 'error': error, 'trace': progress.trace}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.max_ticks <= 100:
        raise SystemExit('--max-ticks must be between 1 and 100')

    creds = resolve_credentials(dict(os.environ))
    print(f"typesafe: {redact(creds.get('TYPESAFE_API_KEY'))} model={creds['TYPESAFE_MODEL']}")
    if creds.get("TEXT_MODEL_PROVIDER") == "claude-cli":
        print(f"text helper: claude-cli model={creds['TEXT_MODEL']} cli={creds.get('CLAUDE_CLI', '(not found)')}")
    else:
        print(f"text helper: {redact(creds.get('TEXT_MODEL_API_KEY'))} model={creds['TEXT_MODEL']} "
              f"base={creds['TEXT_MODEL_BASE_URL']}")
    if "TYPESAFE_API_KEY" not in creds:
        print("FAIL: no TypeSafe credential. Run `jev setup-key`.")
        return 2
    for k, v in creds.items():
        os.environ[k] = v

    allow = [h for h in (args.allow_hosts or "").split(",") if h.strip()]
    if not allow:
        print("FAIL: --allow-hosts is required for a live run.")
        return 2
    if not host_allowed(args.url, allow):
        print(f"FAIL: start URL host is not in the allowlist {allow}.")
        return 2
    # Bring up the browser AFTER ensure_importable, because that call may re-exec this
    # script into the vendored venv. A browser started before the exec is orphaned:
    # the replacement process never runs the parent's atexit handler, so it leaks.
    ensure_importable(argv)

    owned = None
    if args.cdp:
        os.environ["BU_CDP_WS"] = args.cdp
    elif not args.launch_chrome:
        print("FAIL: --no-launch-chrome was set but no CDP endpoint was supplied.")
        return 2
    else:
        owned = start_owned_browser(args)  # sets BU_CDP_WS for browser-harness
        atexit.register(owned.stop)
        for _signal in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                signal.signal(_signal, lambda *_: (owned.stop(), sys.exit(130)))

    try:
        from jev_ultrafast import Agent
        with Agent(args.url, args.goal) as agent:
            result = run_agent(agent, allow=allow, expect=args.expect, max_ticks=args.max_ticks)
    finally:
        if owned is not None:
            owned.stop()
    result.update(goal=args.goal, browser='attached' if args.cdp else 'owned')
    print(f"  outcome: {result['outcome']} ({result['stop_reason']})")
    print(f"  independent check: {'PASS' if result['verified'] else 'FAIL'}")
    if args.json:
        print(json.dumps(result))
    if result['left_allowlist']:
        return 5
    return 0 if result['verified'] else 4


if __name__ == "__main__":
    sys.exit(main())