#!/usr/bin/env python3
"""Bounded GUI computer-use loop: cua-driver (MCP) + Jev.

The counterpart to jev_browser_agent.py for desktop surfaces. You own the
planner role; Jev only ever picks one id from a table you built. It never
writes coordinates, selectors or text.

  observe (cua-driver) -> element table -> jev choose -> one action -> observe

Usage:
  python3 jev_gui_agent.py --pid 26955 --window-id 46041 \
    --goal 'Open the Library page in YouTube Music' \
    --expect 'Library' --max-steps 12 --json

With --plan, a multi-step command is split once by a small text model (jevkit/plan.py)
and each step is either run directly (open an app, open an address, press a key, invoke a
menu, scroll, wait) or handed to the same loop as a one-action goal. --pid and --window-id
become optional, because a command that opens an app cannot know its window beforehand:

  python3 jev_gui_agent.py --plan \
    --goal 'Open System Settings, go to General and then open Storage' \
    --expect 'Storage' --json

A plan can come from jevkit's plan cache instead of the model (JEV_MEMO=on; the default only
records whether it would have matched). Every step is still observed, chosen, executed and
verified here either way, and a run that fails a step or ends unverified forgets its plan.

Exit codes: 0 verified, 4 unverified, 2 refused to start, 6 abstained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- jevkit

def _repo_root() -> Path | None:
    """Find jevkit from wherever this file actually lives.

    The first version only walked its own parent directories, which works inside the
    repo checkout and NOWHERE ELSE. Installed as a skill under ~/.hermes/skills/ — the
    only place an agent ever runs it from — no parent holds jevkit, so every agent got
    "jevkit not importable" on the first call. It passed every test because every test
    ran from the checkout. The installer vendors jevkit inside the Hermes plugin, and
    the `jev` CLI symlink points into a checkout, so both are searched.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "jevkit" / "choose.py").is_file():
            return parent
    homes = [os.environ.get("HERMES_HOME"), str(Path.home() / ".hermes")]
    for home in filter(None, homes):
        root = Path(home)
        # HERMES_HOME is often a PROFILE dir (<root>/profiles/<name>); plugins live at the root.
        bases = [root] + ([root.parent.parent] if root.parent.name == "profiles" else [])
        for base in bases:
            candidate = base / "plugins" / "hermes-jev"
            if (candidate / "jevkit" / "choose.py").is_file():
                return candidate
    cli = shutil.which("jev")
    if cli:
        for parent in Path(cli).resolve().parents:
            if (parent / "jevkit" / "choose.py").is_file():
                return parent
    return None


ROOT = _repo_root()
if ROOT is not None and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from jevkit.choose import choose as jev_choose
    from jevkit.privacy import is_sensitive
except Exception:  # noqa: BLE001
    jev_choose = None

    def is_sensitive(text: str) -> bool:  # type: ignore[misc]
        return False

try:
    from jevkit.choose import MIN_CONFIDENCE as JEV_FLOOR
except Exception:  # noqa: BLE001
    JEV_FLOOR = 0.65

# Imported on its own. The installer vendors jevkit into the Hermes plugin, so a newer
# copy of this script can meet an older jevkit that has `choose` and no `plan`. That must
# cost the --plan speed-up and nothing else; folded into the import above it would have
# reported "jevkit not importable" and refused to run at all.
try:
    from jevkit import plan as jev_plan
except Exception:  # noqa: BLE001
    jev_plan = None


def _find_driver() -> str:
    """Locate cua-driver without baking anyone's home directory into the repo.

    A hardcoded /Users/<someone>/ path is both wrong on every other machine and blocked
    by scripts/check_release.py, which is the gate that keeps fleet-specific runtime out
    of the public skill. Order: explicit override, PATH, then the usual install sites.
    """
    override = os.environ.get("CUA_DRIVER_BIN")
    if override:
        return override
    found = shutil.which("cua-driver")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "cua-driver",
                      Path("/Applications/CuaDriver.app/Contents/MacOS/cua-driver"),
                      Path("/usr/local/bin/cua-driver")):
        if candidate.exists():
            return str(candidate)
    return "cua-driver"      # let the failure name the missing binary


CUA = _find_driver()

INTERACTIVE_ROLES = {
    "AXButton", "AXLink", "AXTextField", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXComboBox", "AXMenuItem", "AXSearchField",
    "AXTab", "AXSlider", "AXDisclosureTriangle", "AXSegmentedControl",
    # Linux/AT-SPI (cua-driver on X11) reports the bare, lowercase role, so a macOS-only
    # set filtered every candidate out and the loop reported "no interactive elements
    # observed" on a window whose tree held 35 labelled controls.
    "button", "push button", "toggle button", "link", "text box", "entry", "spin button",
    "check box", "checkbox", "radio button", "combo box", "menu item", "menu",
    "slider", "tab", "page tab", "search box", "text", "list item", "tree item",
}

# The chooser contract caps a table at 32 candidates. build_table always appends these,
# so the element budget is whatever is left. Stated here once rather than as a magic 26.
STANDARD_ACTIONS = (
    ("scroll-down", "Scroll the page down to reveal more elements."),
    ("scroll-up", "Scroll the page up."),
    ("wait", "Wait one second for the page to finish changing."),
    ("reobserve", "Take a fresh observation without changing anything."),
    ("done", "The goal is fully achieved and independently verified."),
    ("abstain", "Stop and ask the person for help."),
)
MAX_CANDIDATES = 32
MAX_REGIONS = MAX_CANDIDATES - len(STANDARD_ACTIONS)


# ---------------------------------------------------------------- MCP client


class DriverError(RuntimeError):
    """The driver could not be started. main() turns this into exit 2, not a traceback."""


class Driver:
    """Minimal MCP stdio client for cua-driver."""

    def __init__(self, binary: str = CUA) -> None:
        # On a machine without cua-driver this raised FileNotFoundError straight through
        # main(): a traceback and exit code 1, where the docstring promises "2 refused to
        # start". A caller that branches on the exit code read a missing install as a crash.
        try:
            self.proc = subprocess.Popen(
                [binary, "mcp", "--no-daemon-relaunch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError as exc:
            raise DriverError(f"cannot start {binary!r} ({exc.strerror or exc}). "
                              "Install cua-driver or point CUA_DRIVER_BIN at it.") from None
        self._id = 0
        hello = self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "jev-gui-agent", "version": "0.1.0"},
        }, timeout=20.0)
        # A binary that starts and is not an MCP server (wrong file, a build that exits on
        # launch) answers nothing. Every later call would fail the same way and the run
        # would end as "unverified", which blames the screen for a broken install.
        if "result" not in hello:
            self.stop()
            raise DriverError(f"{binary!r} started but did not answer the MCP handshake.")
        self._notify("notifications/initialized")

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method})
        except OSError:
            pass      # the driver has gone; the next call() reports it

    def call(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        self._id += 1
        mid = self._id
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        except OSError:
            # Writing to a process that has already exited is a BrokenPipeError. Without
            # this the handshake check above is a race: a binary that quits at once fails
            # here with a traceback before its missing reply can be noticed.
            return {"error": {"message": "driver exited"}}
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == mid:
                return msg
        return {"error": {"message": "timeout"}}

    def tool(self, name: str, args: dict, timeout: float = 90.0) -> dict:
        return self.call("tools/call", {"name": name, "arguments": args}, timeout)

    def stop(self) -> None:
        try:
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- observation


def observe(driver: Driver, pid: int, window_id: int, session: str) -> dict:
    args = {
        "pid": pid, "window_id": window_id,
        "include_screenshot": False, "max_elements": 6000,
    }
    if session:
        args["session"] = session
    res = driver.tool("get_window_state", args)
    result = res.get("result", {}) or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured.get("elements") is not None:
        return structured
    # cua-driver 0.23.x answers with content[0].text (a JSON string) and no
    # structuredContent, so reading only the structured side returned {} and every
    # run reported "no interactive elements observed".
    for part in (result.get("content") or []):
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            try:
                parsed = json.loads(part["text"])
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def with_session(args: dict, session: str) -> dict:
    if session:
        args["session"] = session
    return args


def window_bounds(state: dict) -> dict:
    return state.get("window_bounds") or {}


def content_bounds(state: dict) -> dict | None:
    """The web-content region of a browser window, so chrome buttons stay out of the table."""
    best = None
    for el in state.get("elements", []):
        if el.get("role") != "AXWebArea":
            continue
        frame = el.get("frame") or {}
        if float(frame.get("w", 0)) < 200 or float(frame.get("h", 0)) < 200:
            continue
        if best is None or float(frame.get("w", 0)) * float(frame.get("h", 0)) > \
                float(best.get("w", 0)) * float(best.get("h", 0)):
            best = frame
    return best


STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "into", "from", "then", "than",
    "click", "open", "page", "using", "left", "right", "sidebar", "button", "link",
    "so", "can", "will", "should", "make", "sure", "about", "must", "need",
}


def goal_tokens(goal: str) -> list[str]:
    out = []
    for word in goal.lower().replace("'s", "").split():
        word = word.strip(".,:;()\"'`[]!?")
        if len(word) > 3 and word not in STOPWORDS:
            out.append(word)
    return out


def relevance(label: str, tokens: list[str]) -> int:
    low = label.lower()
    return sum(1 for t in tokens if t in low)


def element_rows(state: dict, max_regions: int, tokens: list[str] | None = None) -> list[dict]:
    """Visible, labelled, interactive elements, ranked, deduplicated.

    When the window is a browser, only elements inside the web-content area are
    offered: the toolbar, omnibox and extension buttons are never the answer.
    Rows that share a meaningful word with the goal float to the top, because
    the 32-candidate contract cannot hold every control on a rich web app.
    """
    tokens = tokens or []
    bounds = window_bounds(state)
    bx, by = float(bounds.get("x", 0)), float(bounds.get("y", 0))
    bw, bh = float(bounds.get("width", 0)), float(bounds.get("height", 0))
    content = content_bounds(state)
    if content is not None:
        bx, by = float(content.get("x", 0)), float(content.get("y", 0))
        bw, bh = float(content.get("w", 0)), float(content.get("h", 0))
    rows: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    # macOS sidebars, lists and tables are AXOutline/AXTable -> AXRow -> AXStaticText. The
    # ROW is what you click and it carries no label; the LABEL is on a child static text
    # that is not interactive. Filtering on role alone dropped every sidebar item: on
    # System Settings "Displays" was in the tree and never offered, so Jev answered at
    # 0.35 because the right answer was not on the table.
    #
    # The driver reports `parent_index`, so this follows the real tree. An earlier
    # version guessed from frame overlap, which cannot work for a row scrolled out of
    # view - it has no frame - and that is exactly the row you most need to know about.
    by_index = {el.get("element_index"): el for el in state.get("elements", [])}

    def _row_of(el: dict) -> dict | None:
        """The enclosing row, preferring AXRow over AXCell.

        Stopping at the first AXCell looked right and lost the selection: the tree is
        AXRow -> AXCell -> AXStaticText and `selected` lives on the ROW, so every row
        read as unselected and arrival could never be proved.
        """
        node, hops, cell = el, 0, None
        while node is not None and hops < 5:
            node = by_index.get(node.get("parent_index"))
            if node is None:
                break
            if node.get("role") == "AXRow":
                return node
            if node.get("role") == "AXCell" and cell is None:
                cell = node
            hops += 1
        return cell

    for el in state.get("elements", []):
        label = (el.get("label") or "").strip()
        if not label:
            continue
        role = el.get("role") or ""
        acts = el.get("actions") or []
        row = _row_of(el) if role == "AXStaticText" else None
        if role not in INTERACTIVE_ROLES and "AXPress" not in acts and row is None:
            continue
        selected = bool(el.get("selected"))
        if row is not None:
            role = "AXRow"           # describe it to Jev as what it is: a selectable row
            selected = selected or bool(row.get("selected"))
        frame = el.get("frame") or {}
        x, y = float(frame.get("x", 0)), float(frame.get("y", 0))
        w, h = float(frame.get("w", 0)), float(frame.get("h", 0))
        if w < 8 or h < 8:
            continue
        if bw and bh and not (bx <= x <= bx + bw and by <= y <= by + bh):
            continue
        key = (label[:60], int(x), int(y))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "token": el.get("element_token"),
            "role": role,
            "label": label[:120],
            "x": x, "y": y, "w": w, "h": h,
            "local_x": x - bx, "local_y": y - by,
            "cx": x - bx + w / 2, "cy": y - by + h / 2,
            "index": el.get("element_index"),
            "selected": selected,
        })
    rows.sort(key=lambda r: (-relevance(r["label"], tokens), r["y"], r["x"]))
    return rows[:max_regions]


def single_line(text: str, limit: int = 78) -> str:
    return " ".join(text.split())[:limit]


def offscreen_matches(state: dict, tokens: list[str], limit: int = 3) -> list[str]:
    """Goal-relevant labels that are in the tree but scrolled out of view.

    A row below the fold has no frame, so it cannot be clicked and is rightly left off
    the table. But leaving Jev ignorant of it is a different mistake: asked to open
    "Sound" with Sound off-screen, it scored 0.33 and the run stalled, because from where
    it sat nothing on the table served the goal. It was right. The fix is not to lower
    the floor until it guesses - it is to say the thing exists further down, so that
    scrolling becomes the obviously correct move instead of a shot in the dark.
    """
    if not tokens:
        return []
    found: list[str] = []
    for el in state.get("elements", []):
        label = (el.get("label") or "").strip()
        if not label or el.get("frame"):
            continue
        if relevance(label, tokens) and label not in found:
            found.append(label[:40])
    return found[:limit]


def _stable_id(kind: str, label: str, used: set[str]) -> str:
    """An id for "this element", not for "this snapshot's handle to it"."""
    slug = re.sub(r"[^a-z0-9]+", "-", _fold(label))[:36].strip("-") or "item"
    base = f"{kind}:{slug}"
    cid, n = base, 2
    while cid in used:
        cid, n = f"{base}-{n}", n + 1
    used.add(cid)
    return cid


def build_table(rows: list[dict], below: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    regions: list[dict] = []
    candidates: list[dict] = []
    used: set[str] = set()
    for i, r in enumerate(rows):
        rid = f"r{i}"
        regions.append({
            "id": rid, "role": r["role"].replace("AX", "").lower(),
            "label": single_line(r["label"]), "interactive": True,
        })
        typing = r["role"] in ("AXTextField", "AXSearchField")
        verb = "Type into" if typing else "Click"
        # The id used to be `click:<element_token>`, and the driver reissues every token
        # on every observation. So no id in `history` was ever still on the table, and
        # Jev had no way to see it had already clicked something: given "open General,
        # then Storage" it clicked General ten times running. An id built from what the
        # element IS survives re-observation, so history means something.
        r["cid"] = _stable_id("type" if typing else "click", r["label"], used)
        state = " It is the currently selected item, so clicking it again changes nothing." \
            if r.get("selected") else ""
        candidates.append({
            "id": r["cid"],
            "description": f"{verb} [{i}] {r['role'].replace('AX','').lower()} "
                           f"\"{single_line(r['label'])}\".{state}",
        })
    for extra, desc in STANDARD_ACTIONS:
        if extra == "scroll-down" and below:
            names = ", ".join(f'"{single_line(b, 30)}"' for b in below)
            desc = f"Scroll down: {names} exists further down this list but is not visible yet."
        candidates.append({"id": extra, "description": desc})
    return regions, candidates


# ---------------------------------------------------------------- text helper

def text_helper(goal: str, field_label: str, values: list[str]) -> str:
    """Which of the ALLOWED values goes into this field. Only ever one of ``values``.

    Two faults lived here. With exactly one allowed value it still asked a text model to
    "choose the best one" from a list of one: a network call with a 30 s timeout, in the
    middle of a step, for an answer that was never in doubt. And whatever the model replied
    was typed as it came, so a reply that was not on the list put text into a field that
    the person never allowed. ``--values`` is the whole of what may be typed; the model
    only gets to pick from it.
    """
    key = os.environ.get("TEXT_MODEL_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("TEXT_MODEL", "google/gemini-2.5-flash")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    if not values:
        return ""
    if not key or len(values) == 1:
        return values[0]
    prompt = (
        "You write one short value to type into a GUI field. Reply as JSON "
        '{"text": "..."} and nothing else.\n'
        f"Goal: {goal}\nField: {field_label}\n"
        f"Allowed values (choose the best one, exactly as written): {values}\n"
    )
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as fh:
            payload = json.load(fh)
        raw = payload["choices"][0]["message"]["content"].strip()
        raw = raw.strip("`").removeprefix("json").strip()
        chosen = str(json.loads(raw).get("text", "")).strip()
        return chosen if chosen in values else values[0]
    except Exception:  # noqa: BLE001 - a reply shaped as a list, a reset socket: the first value, not a traceback
        return values[0]


# ---------------------------------------------------------------- execution


def execute(driver: Driver, pid: int, window_id: int, session: str,
            action: str, rows: list[dict], goal: str, values: list[str],
            literal: str = "") -> tuple[str, str]:
    """Run exactly one pre-validated action. Returns (op, detail).

    ``literal`` is the text of a planned type_text step. The person dictated it, so it is
    typed as given; asking the text model to choose a value would add a second model call
    to the step whose whole purpose is to save one.
    """
    if action.startswith("click:"):
        token = next((r["token"] for r in rows if r.get("cid") == action), None)
        for r in rows:
            if r["token"] == token:
                res = driver.tool("click", with_session({
                    "pid": pid, "window_id": window_id,
                    "element_token": token, "delivery_mode": "background",
                }, session))
                return "click", json.dumps(_brief(res))
        return "click", "target token no longer observed"
    if action.startswith("type:"):
        token = next((r["token"] for r in rows if r.get("cid") == action), None)
        for r in rows:
            if r["token"] == token:
                text = literal or text_helper(goal, r["label"], values)
                res = driver.tool("type_text", with_session({
                    "pid": pid, "window_id": window_id,
                    "element_token": token, "text": text,
                    "delivery_mode": "foreground",
                }, session), timeout=120)
                return "type", f"{text!r} -> {json.dumps(_brief(res))}"
        return "type", "target token no longer observed"
    if action in ("scroll-down", "scroll-up"):
        res = driver.tool("scroll", with_session({
            "pid": pid, "window_id": window_id,
            "direction": action.split("-", 1)[1], "amount": 6,
        }, session))
        return "scroll", json.dumps(_brief(res))
    if action == "wait":
        time.sleep(1.0)
        return "wait", "slept 1s"
    return action, "no-op"


def _failure(res: dict) -> str:
    """Empty when a driver call worked, otherwise the reason. The driver fails closed and
    says so in the result body, not as a JSON-RPC error, so both places are read."""
    if not isinstance(res, dict):
        return "no reply from the driver"
    if res.get("error"):
        err = res["error"]
        return str(err.get("message") if isinstance(err, dict) else err)[:160]
    out = res.get("result") or {}
    if out.get("isError"):
        text = next((c.get("text") for c in out.get("content") or []
                     if isinstance(c, dict) and c.get("text")), "")
        return (text or "the driver refused the call")[:160]
    return ""


def _brief(res: dict) -> dict:
    out = res.get("result", {}) if isinstance(res, dict) else {}
    sc = out.get("structuredContent") or {}
    # The message used to be read from `result.result`, a field the driver never sets, so
    # a refused click was logged as {"effect": "refused", "message": ""} and the one line
    # that said why was thrown away. The reason is in the content text.
    why = _failure(res)
    return {
        "effect": sc.get("effect") or ("refused" if why else out.get("status", "unknown")),
        "message": (why or str(out.get("result") or ""))[:120],
    }


def _landed(detail: str) -> bool:
    """Did the action reach the app? A refusal or a vanished element is not a completed step."""
    return "no longer observed" not in detail and '"effect": "refused"' not in detail


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212"), "-")


def _fold(text: str) -> str:
    """Compare what a person typed with what an app displays.

    macOS writes "Wi\u2011Fi" with a NON-BREAKING hyphen. `--expect Wi-Fi` never matched
    it, so a click that landed first time at 0.96 confidence was reported unverified and
    the runner clicked it five more times. Fold dash variants and width forms before
    comparing; nobody can see the difference, so the check must not depend on it.
    """
    import unicodedata
    return unicodedata.normalize("NFKC", text or "").translate(_DASHES).casefold().strip()


def verify(rows: list[dict], title: str, expect: str) -> bool:
    """Is the goal state actually reached? Deliberately strict about what counts.

    Two false passes lived here, and the skill's own documented example hit both.

    1. It matched `--expect` against any ELEMENT LABEL. `--expect 'Library'` passed on
       every page of YouTube Music, because the left nav carries a Library link
       everywhere. "A link named X exists" is not "page X is open", and the check ran
       before the first action, so the runner exited 0 having clicked nothing.
    2. An empty `--expect` returned True, and `--expect` defaults to "". Every run
       without it reported PASS regardless of what happened.

    The window title is the one signal that actually changes when you arrive somewhere,
    so it is the only thing accepted. No expectation now means "unverified", not "pass".
    """
    if not expect:
        return False
    needle = _fold(expect)
    if needle in _fold(title or ""):
        return True
    # Some apps never title their window: System Settings reports an empty title on the
    # General pane, so title-only checking could not pass there however right the click.
    # "The row named X is the SELECTED row" is real proof of arrival. It is not the old
    # bug in disguise: that accepted a row named X merely EXISTING, which is true on
    # every page. Selection is only true once you are there.
    return any(r.get("selected") and needle in _fold(r.get("label", "")) for r in rows)

def screen_digest(state: dict) -> str:
    """Local fingerprint of observable state, never sent to Jev or written to logs.

    Include field values and all observed controls, not only the candidate shortlist:
    otherwise typing or a change outside the first 26 controls appears ineffective.
    Exclude ephemeral driver tokens and indices so a recapture alone is not progress.
    """
    visible = [(el.get("role"), el.get("label"), el.get("value"), el.get("selected"),
                el.get("enabled"), el.get("frame")) for el in state.get("elements", [])]
    raw = json.dumps((state.get("window_title"), visible), sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- the loop


def run_goal(driver: Driver, pid: int, window_id: int, session: str, goal: str, *,
             expect: str, values: list[str], regions_cap: int, budget: int,
             first_step: int = 1, history: list[dict] | None = None,
             log: list[dict] | None = None, until_op: str = "", literal: str = "") -> dict:
    """observe -> choose -> act for ONE goal, until it ends or ``budget`` Jev calls are spent.

    Without --plan this is the whole run, called once. With --plan it is called for each
    step that needs an on-screen target, with ``until_op`` naming the operation that
    completes the step ("click" or "type"). A planned step is one action, so the loop
    returns as soon as that action is delivered instead of spending another observation
    and another Jev call on being told "done".
    """
    tokens = goal_tokens(goal)
    history = [] if history is None else history
    log = [] if log is None else log
    out: dict = {"used": 0, "abstained": False, "ended": "budget", "title": "", "rows": []}
    stalled = 0
    last_digest: str | None = None
    last_mutation: tuple[str, str] | None = None
    unchanged_mutations = 0
    for step in range(first_step, first_step + budget):
        state = observe(driver, pid, window_id, session)
        out["title"] = state.get("window_title", "")
        rows = out["rows"] = element_rows(state, regions_cap, tokens)
        if not rows:
            print(f"  step {step}: no interactive elements observed")
            out["ended"] = "nothing_observed"
            break
        safe_rows = [r for r in rows if not is_sensitive(r["label"])]
        withheld = len(rows) - len(safe_rows)
        if withheld:
            print(f"  step {step}: withheld {withheld} label(s) that look sensitive")
        rows = out["rows"] = safe_rows
        if not rows:
            print("  every observed label looks sensitive; stopping")
            out["abstained"] = True
            out["ended"] = "all_sensitive"
            break
        # A fresh, complete AX observation, not the driver's delivery acknowledgement.
        digest = screen_digest(state)
        stalled = stalled + 1 if digest == last_digest else 0
        if expect and verify(rows, out["title"], expect):
            print(f"  verified before step {step}; stopping")
            out["ended"] = "verified"
            break
        if last_mutation:
            action_id, before = last_mutation
            unchanged_mutations = unchanged_mutations + 1 if digest == before else 0
            last_mutation = None
            if unchanged_mutations >= 2:
                print(f"  step {step}: repeated {action_id} left the observed screen unchanged; stopping")
                out["ended"] = "stalled_action"
                break
        last_digest = digest
        regions, candidates = build_table(rows, offscreen_matches(state, tokens))
        request = {
            "schema": "jev.action_choice_request_v1",
            "goal": goal,
            "observation_id": f"obs-{step}",
            "regions": regions,
            "history": history[-6:],
            "candidates": candidates,
        }
        try:
            t_jev = time.time()
            reply = jev_choose(request)
            jev_ms = int((time.time() - t_jev) * 1000)
        except ValueError as exc:
            print(f"  step {step}: request rejected by the Jev contract: {exc}")
            out["ended"] = "contract_rejected"
            break
        action = reply.get("selected_id", "reobserve")
        confidence = reply.get("confidence")
        out["used"] = step - first_step + 1
        if action not in {candidate["id"] for candidate in candidates}:
            print(f"  step {step}: Jev returned an id outside this observation; stopping")
            out["ended"] = "invalid_choice"
            break
        if action in ("done", "abstain"):
            print(f"  step {step:>2}  Jev -> {action} "
                  f"(conf {confidence}) {reply.get('reason','')}")
            out["abstained"] = action == "abstain"
            out["ended"] = action
            history.append({"selected_id": action, "outcome": "loop ended"})
            break
        if until_op == "click" and action == "reobserve" and _already_there(reply, rows):
            print(f"  step {step:>2}  the target is already the selected item; nothing to do")
            out["ended"] = "done"
            history.append({"selected_id": "done", "outcome": "already selected"})
            break
        if action == "reobserve" and stalled >= 2:
            print(f"  step {step:>2}  nothing on screen is changing after "
                  f"{stalled} reobservations; stopping instead of spinning")
            out["ended"] = "stalled"
            break
        t1 = time.time()
        op, detail = execute(driver, pid, window_id, session, action, rows, goal, values, literal)
        if op in ("click", "type", "scroll") and _landed(detail):
            last_mutation = (action, digest)
            # A different action is an actual attempt to recover, not repetition.
            if not history or action != history[-1].get("selected_id"):
                unchanged_mutations = 0
        else:
            unchanged_mutations = 0
        action_ms = int((time.time() - t1) * 1000)
        print(f"  step {step:>2}  jev {jev_ms:>4} ms + act {action_ms:>4} ms  op={op}  "
              f"conf={confidence}  {detail[:100]}")
        # These were one field called `decision_ms` that actually timed the CLICK. It
        # made a 470 ms Jev decision look like 2.6 s and sent the latency hunt after
        # the wrong component: the time is the driver confirming the click's effect.
        log.append({"step": step, "action": action, "op": op,
                    "detail": detail, "confidence": confidence,
                    "decision_ms": jev_ms, "action_ms": action_ms})
        what = next((f'{r["role"].replace("AX", "").lower()} "{single_line(r["label"], 40)}"'
                     for r in rows if r.get("cid") == action), action)
        history.append({"selected_id": action,
                        "outcome": f"{op} {what} - {single_line(detail, 60)}"})
        if op == "no-op":
            time.sleep(0.3)
        if until_op and op == until_op and _landed(detail):
            out["ended"] = "acted"
            break
    return out


def _already_there(reply: dict, rows: list[dict]) -> bool:
    """A planned step whose target is already the selected item needs no action.

    Found live: "Open System Settings, go to General..." with General already showing.
    The table tells Jev the General row is selected and that clicking it changes nothing,
    so it split 0.66 on the click and 0.14 on `done`. Both answers are right, neither
    clears the floor alone, and the step stalled on a screen that was already correct.
    When the mass on "that selected row" plus `done` clears the floor, the step is over.
    This only ever resolves to NOT acting: if it is wrong, the next step cannot find its
    target and the plan stops, which is the same outcome as the stall it replaces.
    """
    probabilities = reply.get("probabilities") or {}
    if not probabilities:
        return False
    selected = {r.get("cid") for r in rows if r.get("selected")}
    top = max(probabilities, key=lambda cid: probabilities[cid])
    if top != "done" and top not in selected:
        return False
    mass = sum(p for cid, p in probabilities.items() if cid == "done" or cid in selected)
    return mass >= JEV_FLOOR


# ---------------------------------------------------------------- planned steps

OPEN = "/usr/bin/open"
DIRECT_KINDS = ("open_app", "open_url", "press_key", "menu", "scroll", "wait")
# type_text is Jev-driven on purpose. Typing at "wherever the focus happens to be" looks
# like a harmless shortcut and is not: with no field focused, a web app reads each letter
# as a keyboard command, and in a mail client those archive and delete. The text only
# ever goes into a field Jev picked from the table.
JEV_KINDS = {"click": "click", "type_text": "type"}
# The driver draws its agent cursor in a window of its own that is always on top, so
# "the frontmost window" would otherwise always be the driver's overlay.
_DRIVER_APPS = {"cua driver"}


def list_windows(driver: Driver) -> list[dict]:
    res = driver.tool("list_windows", {}, timeout=15)
    found = (res.get("result", {}).get("structuredContent") or {}).get("windows") or []
    return [w for w in found if _fold(w.get("app_name", "")) not in _DRIVER_APPS]


def _frontness(window: dict) -> tuple[bool, int]:
    visible = bool(window.get("is_on_screen")) and window.get("on_current_space") is not False
    z = window.get("z_index")
    return visible, z if isinstance(z, int) and not isinstance(z, bool) else -1


def front_window(windows: list[dict]) -> dict | None:
    return max(windows, key=_frontness, default=None)


def window_of(windows: list[dict], app_name: str) -> dict | None:
    """The app's frontmost window. "Chrome" finds "Google Chrome"; nothing finds nothing."""
    want = _fold(app_name)
    names = [(w, _fold(w.get("app_name", ""))) for w in windows]
    exact = [w for w, name in names if name == want]
    loose = [w for w, name in names if name and want and (want in name or name in want)]
    return front_window(exact or loose)


# A default argument is bound once, when the module is imported, so `opener=subprocess.run`
# could not be replaced afterwards. The tests thought they had faked it and had not: on macOS
# they ran the REAL /usr/bin/open and opened System Settings on whoever ran them, and on Linux
# the same call died with ENOENT and turned CI red. Resolve these at call time instead.
def _opener(given):
    return given or subprocess.run


def _sleeper(given):
    return given or time.sleep


def wait_for_window(driver: Driver, app_name: str, timeout: float = 8.0, sleep=None) -> dict | None:
    """The window of the app a direct op just opened, so the next step knows where to act."""
    sleep = _sleeper(sleep)
    found = None
    # Counted polls rather than a wall-clock deadline, so the wait is exactly as long as
    # the sleeps it was given. A listing costs about 3 ms on top.
    for attempt in range(max(1, int(timeout / 0.4)) + 1):
        if attempt:
            sleep(0.4)
        found = window_of(list_windows(driver), app_name)
        if found is not None and _frontness(found)[0]:
            return found
    return found


def default_browser() -> str:
    """The bundle id this Mac opens https links with, "" when it cannot be read."""
    prefs = (Path.home() / "Library" / "Preferences" / "com.apple.LaunchServices"
             / "com.apple.launchservices.secure.plist")
    try:
        with open(prefs, "rb") as handle:
            handlers = plistlib.load(handle).get("LSHandlers") or []
    except Exception:  # noqa: BLE001 - a missing or unreadable plist only costs the shortcut
        return ""
    for handler in handlers:
        if handler.get("LSHandlerURLScheme") in ("https", "http") and handler.get("LSHandlerRoleAll"):
            return str(handler["LSHandlerRoleAll"]).lower()
    return "com.apple.safari"      # nobody ever chose one


def window_after_open_url(driver: Driver, before: dict, browser: str | None = None,
                          timeout: float = 3.0, sleep=None) -> dict | None:
    """Which window did the address open in? Nobody said which browser.

    The first version took "whatever is in front afterwards". Live, `open` loaded the page
    in a browser window BEHIND the front app: a background process is not always allowed
    to take focus, so the front window was still System Settings and the next step would
    have clicked there. So ask the OS which app handles https and use that app's window.
    If that cannot be answered, accept only a window that visibly changed (it is new, or
    its title is not what it was before the open); never the front window on faith.
    """
    sleep = _sleeper(sleep)
    bundle = default_browser() if browser is None else browser
    pids: set = set()
    if bundle:
        # list_apps is the only call that maps a bundle id to a pid. It costs about 0.75 s
        # because it also scans installed apps, which is why it is asked once, here, and
        # nowhere else in a run.
        res = driver.tool("list_apps", {}, timeout=20)
        apps = (res.get("result", {}).get("structuredContent") or {}).get("apps") or []
        pids = {a.get("pid") for a in apps
                if str(a.get("bundle_id") or "").lower() == bundle and a.get("running") and a.get("pid")}
    for attempt in range(max(1, int(timeout / 0.4)) + 1):
        if attempt:
            sleep(0.4)
        windows = list_windows(driver)
        mine = [w for w in windows if w.get("pid") in pids]
        # A browser that was not running yet has no pid above; its new window shows up
        # here as one that did not exist before the open.
        changed = [w for w in windows if before.get(w.get("window_id")) != (w.get("title") or "")]
        if mine or changed:
            return front_window(mine or changed)
    return None


def _aim(driver: Driver, where: dict) -> bool:
    """Make sure there is a window to act on. A spoken-style command means the front one."""
    if where.get("pid") is not None and where.get("window_id") is not None:
        return True
    front = front_window(list_windows(driver))
    if front is None:
        return False
    where["pid"], where["window_id"] = front.get("pid"), front.get("window_id")
    return True


def run_direct(driver: Driver, where: dict, session: str, step: dict,
               opener=None, sleep=None, browser: str | None = None) -> tuple[bool, str]:
    """One step that needs no on-screen target. Returns (ok, detail).

    ``step`` has been through jevkit.plan.clean_step, and every value is checked again
    here at the point of use: this is the function that hands a model-written string to
    the operating system.
    """
    opener, sleep = _opener(opener), _sleeper(sleep)
    kind = step.get("kind")
    if kind in ("open_app", "open_url"):
        if sys.platform != "darwin":
            return False, f"{kind} is only wired for macOS"
        if kind == "open_app":
            name = jev_plan.safe_app_name(step.get("target"))
            if not name:
                return False, "refused: not an application name"
            command, before = [OPEN, "-a", name], {}
        else:
            url = jev_plan.safe_url(step.get("target"))
            if not url:
                return False, "refused: only http and https addresses are opened"
            before = {w.get("window_id"): w.get("title") or "" for w in list_windows(driver)}
            command, name = [OPEN, url], ""
        try:
            proc = opener(command, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"open failed: {exc}"
        if proc.returncode != 0:
            return False, f"open failed: {single_line(proc.stderr or '', 120)}"
        window = (wait_for_window(driver, name, sleep=sleep) if name
                  else window_after_open_url(driver, before, browser=browser, sleep=sleep))
        if window is None:
            return False, "opened, but no window appeared to act on"
        where["pid"], where["window_id"] = window.get("pid"), window.get("window_id")
        return True, f"window {window.get('window_id')} of {window.get('app_name', '')!r}"
    if kind == "wait":
        seconds = min(10, max(1, int(step.get("amount") or 1)))
        sleep(seconds)
        return True, f"slept {seconds}s"
    if not _aim(driver, where):
        return False, "no window to act on"
    base = {"pid": where["pid"], "window_id": where["window_id"]}
    if kind == "press_key":
        keys = jev_plan.parse_keys(step.get("target"))
        if not keys:
            return False, "refused: not a key on the allowlist"
        modifiers, key = keys
        # Foreground, like type_text: the key has to reach whatever field the previous
        # step focused, and a background post does not reach Chromium content or a
        # native menu equivalent.
        if modifiers:
            res = driver.tool("hotkey", with_session(
                dict(base, keys=modifiers + [key], delivery_mode="foreground"), session))
        else:
            res = driver.tool("press_key", with_session(
                dict(base, key=key, delivery_mode="foreground"), session))
    elif kind == "menu":
        path = jev_plan.menu_path(step.get("target"))
        if not path:
            return False, "refused: not a menu path"
        res = driver.tool("invoke_menu", with_session(dict(base, path=path), session))
    elif kind == "scroll":
        direction = str(step.get("target") or "")
        if direction not in ("up", "down", "left", "right"):
            return False, "refused: not a scroll direction"
        times = min(20, max(1, int(step.get("amount") or 1)))
        # 6 notches is what one scroll action moves in the loop above; the driver caps at 50.
        res = driver.tool("scroll", with_session(
            dict(base, direction=direction, amount=min(50, 6 * times)), session))
    else:
        return False, f"refused: {kind!r} is not a direct operation"
    why = _failure(res)
    return (False, why) if why else (True, json.dumps(_brief(res)))


def forget_plan(goal: str, outcome: dict) -> None:
    """Take this command's plan out of the plan cache, once, and never at the run's expense.

    A cached plan is served again for a week. One that led to a failed step or to an end
    state nobody could verify may be the reason it failed, and serving it again turns one
    bad run into every run. It costs one model call to be wrong about that.
    """
    context = outcome.get("planned_for")
    outcome["planned_for"] = None        # a failed step and an unverified end are one forget, not two
    # getattr: the installer vendors jevkit, so this script can meet one whose planner has
    # no cache. Then there is nothing to forget.
    forget = getattr(jev_plan, "forget", None)
    if not context or forget is None:
        return
    try:
        forget(goal, front_app=context["front_app"], running_apps=context["running_apps"])
        print("  plan cache: forgot this plan, so the next run asks the model again")
    except Exception:  # noqa: BLE001 - forget() promises not to raise; the exit code must not depend on it
        pass


def run_plan(driver: Driver, where: dict, args: argparse.Namespace, values: list[str],
             regions_cap: int, *, planner=None, opener=None, sleep=None) -> dict:
    """Plan once, then run each step directly or through the Jev loop.

    Returns what run_goal returns, plus ``report`` for the --json result and ``planned_for``,
    the context main() needs to forget the plan. ``--max-steps`` stays the ceiling on Jev
    calls for the WHOLE command, not per step, so a plan cannot turn a bounded run into
    steps x budget.
    """
    opener, sleep = _opener(opener), _sleeper(sleep)
    windows = list_windows(driver)
    front = front_window(windows)
    running: list[str] = []
    for window in sorted(windows, key=_frontness, reverse=True):
        name = str(window.get("app_name") or "")
        if name and name not in running:
            running.append(name)
    front_app = str((front or {}).get("app_name") or "")
    try:
        planned = (planner or jev_plan.plan)(args.goal, front_app=front_app, running_apps=running)
    except Exception as exc:  # noqa: BLE001 - plan() promises not to raise; the run must not depend on it
        planned = {"status": "fallback", "reason": f"planner_error:{type(exc).__name__}",
                   "steps": [{"kind": "goal"}]}
    if not isinstance(planned, dict) or not planned.get("steps"):
        planned = {"status": "fallback", "reason": "no_plan", "steps": [{"kind": "goal"}]}
    status = planned.get("status", "fallback")
    why = f" ({planned['reason']})" if planned.get("reason") else ""
    # "miss" is also what an older jevkit with no plan cache amounts to: the model was asked.
    cache = str(planned.get("cache", "miss"))
    print(f"  plan: {status}{why}, {len(planned.get('steps') or [])} step(s), "
          f"{planned.get('latency_ms', 0)} ms, cache {cache}")

    # The plan is checked again here, whoever produced it. A step outside the vocabulary
    # is ignored and reported, never executed.
    records: list[dict] = []
    runnable: list[dict] = []
    whole_goal = False
    for raw in planned.get("steps") or []:
        if isinstance(raw, dict) and raw.get("kind") == "goal":
            whole_goal = True
            continue
        step = jev_plan.clean_step(raw)
        if step is None:
            kind = str(raw.get("kind"))[:40] if isinstance(raw, dict) else "?"
            records.append({"kind": kind, "target": "", "mode": "ignored", "ok": False,
                            "duration_ms": 0,
                            "detail": "not a step this runner knows how to run safely"})
            print(f"  plan step ignored: {kind!r} is not in the vocabulary")
            continue
        runnable.append(step)
    dropped = list(planned.get("dropped") or [])
    if whole_goal:
        # The fallback always means "the goal the person gave", run as one loop exactly
        # as it would be without --plan. Its own text is never read: a plan that could
        # put words in the goal step could put a different goal in front of Jev.
        runnable = [{"kind": "goal"}]
    else:
        runnable, cut = jev_plan.enforce_never_send(args.goal, runnable)
        dropped += cut
        for item in cut:
            print(f"  plan step dropped: {item['step'].get('kind')} "
                  f"{item['step'].get('target', '')!r} - {item['reason']}")

    out: dict = {"used": 0, "abstained": False, "ended": "planned", "title": "", "rows": []}
    # What forget_plan() needs. Only a real plan is ever stored, so a fallback has nothing
    # to forget, and with JEV_MEMO=off nothing was stored either.
    out["planned_for"] = ({"front_app": front_app, "running_apps": running}
                          if status == "planned" and cache != "off" else None)
    history: list[dict] = []
    log: list[dict] = []
    stopped = ""
    try:
        for index, step in enumerate(runnable, 1):
            kind = step["kind"]
            record = {"kind": kind, "target": step.get("target", ""),
                      "mode": "direct" if kind in DIRECT_KINDS else "jev"}
            if step.get("risky"):
                record["risky"] = step["risky"]
            if stopped:
                records.append(dict(record, mode="not_run", ok=False, duration_ms=0, detail=stopped))
                continue
            t0 = time.time()
            if kind in DIRECT_KINDS:
                ok, detail = run_direct(driver, where, args.session, step, opener=opener, sleep=sleep)
            else:
                remaining = args.max_steps - out["used"]
                if remaining <= 0:
                    ok, detail = False, "the --max-steps budget is spent"
                elif not _aim(driver, where):
                    ok, detail = False, "no window to act on"
                else:
                    whole = kind == "goal"
                    part = run_goal(
                        driver, where["pid"], where["window_id"], args.session,
                        args.goal if whole else jev_plan.step_goal(step),
                        expect=args.expect, values=values, regions_cap=regions_cap,
                        budget=remaining, first_step=out["used"] + 1, history=history, log=log,
                        until_op="" if whole else JEV_KINDS[kind],
                        literal="" if whole else str(step.get("text") or ""))
                    out["used"] += part["used"]
                    out["abstained"] = out["abstained"] or part["abstained"]
                    out["title"], out["rows"] = part["title"], part["rows"]
                    record["jev_calls"] = part["used"]
                    ok = part["ended"] in ("acted", "done", "verified")
                    detail = part["ended"]
                    if part["ended"] == "verified":
                        stopped = "the expected end state was already reached"
            record.update(ok=ok, duration_ms=int((time.time() - t0) * 1000), detail=single_line(detail, 160))
            records.append(record)
            print(f"  plan step {index}/{len(runnable)}  {kind:<9} {record['mode']:<6} "
                  f"{record['duration_ms']:>5} ms  {'ok' if ok else 'FAILED'}  {record['detail'][:80]}")
            # Later steps were planned on the assumption that this one happened. Typing into
            # a window that failed to open is how text ends up somewhere it was never meant
            # to go, so a failed step ends the plan.
            if not ok and not stopped:
                stopped = f"step {index} ({kind}) did not complete"
    except BaseException:
        # Ctrl-C because the plan was doing the wrong thing, or a driver that died mid-step:
        # that run did not end well either, and main() never sees this plan's context.
        forget_plan(args.goal, out)
        raise
    if any(r["mode"] in ("direct", "jev") and not r["ok"] for r in records):
        forget_plan(args.goal, out)
    out["log"] = log
    out["report"] = {
        "status": status, "reason": planned.get("reason", ""),
        "latency_ms": planned.get("latency_ms", 0), "model": planned.get("model", ""),
        "cache": cache,
        "dropped": [{"kind": d.get("step", {}).get("kind"), "target": d.get("step", {}).get("target", ""),
                     "reason": d.get("reason", "")} for d in dropped if isinstance(d, dict)],
        "steps": records,
    }
    return out


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bounded GUI computer-use loop: cua-driver + Jev")
    p.add_argument("--pid", type=int, default=None)
    p.add_argument("--window-id", type=int, default=None)
    p.add_argument("--goal", required=True)
    p.add_argument("--plan", action="store_true",
                   help="Split a multi-step command into atomic steps with one text-model "
                        "call, run the deterministic ones directly and the rest through "
                        "Jev. Makes --pid and --window-id optional.")
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--max-regions", type=int, default=MAX_REGIONS,
                   help=f"Element rows offered to Jev (max {MAX_REGIONS}: build_table "
                        f"always appends {len(STANDARD_ACTIONS)} standard actions and the "
                        f"contract caps the table at {MAX_CANDIDATES}).")
    p.add_argument("--expect", default="")
    p.add_argument("--values", default="", help="Pipe-separated pool for typed fields.")
    p.add_argument("--session", default="",
                   help="Optional cua-driver session label (omit when the transport has none).")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    # Only a plan can find its own window (it may be the one that opens the app), so the
    # plain loop still refuses to start without both, exactly as when they were required.
    missing = [flag for flag, value in (("--pid", args.pid), ("--window-id", args.window_id))
               if value is None]
    if missing and not args.plan:
        p.error(f"the following arguments are required: {', '.join(missing)}")
    # Above the cap the table is rejected by the contract on EVERY step, at step 1,
    # every time - a flag that silently guarantees total failure is worse than no flag.
    regions_cap = max(1, min(args.max_regions, MAX_REGIONS))
    values = [v.strip() for v in args.values.split("|") if v.strip()]
    tokens = goal_tokens(args.goal)

    if jev_choose is None:
        print("FAIL: jevkit not importable from this checkout.")
        return 2
    if is_sensitive(args.goal):
        print("FAIL: the goal looks sensitive; refusing to send it to Jev.")
        return 2

    if not os.environ.get("TYPESAFE_API_KEY"):
        # Was a bare call to macOS `security`, which on Linux raised FileNotFoundError and
        # killed the run before jevkit's own credentials-file fallback could be consulted.
        # jevkit.keystore already resolves environment → OS secret store → credentials file.
        try:
            from jevkit.keystore import resolve as _resolve_credential
            _key = _resolve_credential()
        except Exception:  # noqa: BLE001 - a broken credential lookup must not crash the loop
            _key = None
        if _key:
            os.environ["TYPESAFE_API_KEY"] = _key
    if not os.environ.get("TEXT_MODEL_API_KEY") and not os.environ.get("OPENROUTER_API_KEY"):
        token = None
        if shutil.which("security"):  # macOS only; on Linux skip straight to the fallback
            proc = subprocess.run(
                # No hardcoded account: whoever runs this is the account. A name baked in
                # here works on exactly one machine and fails silently on every other.
                ["security", "find-generic-password", "-s", "OPENROUTER_API_KEY",
                 "-a", os.environ.get("USER", ""), "-w"],
                capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                token = proc.stdout.strip()
        if not token:
            try:
                from jevkit.keystore import resolve as _resolve_credential
                token = _resolve_credential("openrouter")
            except Exception:  # noqa: BLE001
                token = None
        if token:
            os.environ["OPENROUTER_API_KEY"] = token
    if not os.environ.get("TYPESAFE_API_KEY"):
        print("FAIL: no TypeSafe credential. Run `jev setup-key`.")
        return 2

    try:
        # Resolved now, not at import, so CUA_DRIVER_BIN set by the caller is honoured.
        driver = Driver(_find_driver())
    except DriverError as exc:
        print(f"FAIL: {exc}")
        return 2
    use_plan = args.plan
    if use_plan and jev_plan is None:
        # Fail open: an older jevkit costs the speed-up, never the run.
        print("  --plan: this jevkit has no planner; running the goal as one loop instead")
        use_plan = False
    where = {"pid": args.pid, "window_id": args.window_id}
    outcome: dict = {"used": 0, "abstained": False, "title": "", "rows": [], "log": []}
    t0 = time.time()
    try:
        if args.session:
            driver.tool("start_session", {"session": args.session})
        if use_plan:
            outcome = run_plan(driver, where, args, values, regions_cap)
        elif not _aim(driver, where):
            print("  no window to act on")
        else:
            log: list[dict] = []
            outcome = run_goal(driver, where["pid"], where["window_id"], args.session, args.goal,
                               expect=args.expect, values=values, regions_cap=regions_cap,
                               budget=args.max_steps, log=log)
            outcome["log"] = log
    finally:
        title, rows = outcome.get("title", ""), outcome.get("rows", [])
        try:
            if where["pid"] is not None and where["window_id"] is not None:
                final = observe(driver, where["pid"], where["window_id"], args.session)
                title = final.get("window_title", title)
                rows = element_rows(final, regions_cap, tokens)
        except Exception:  # noqa: BLE001
            pass
        driver.stop()

    steps, abstained = outcome["used"], outcome["abstained"]
    verified = verify(rows, title, args.expect)
    if not verified:
        # No --expect is unverified too, so a plan is only ever reused after a run that
        # proved it reached the end state. That is the point: reuse has to be earned.
        forget_plan(args.goal, outcome)
    result = {
        "schema": "hermes.computer_use_jev_run_v1",
        "goal": args.goal,
        "window_title": title,
        "steps": steps,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "expected": args.expect,
        "verified": verified,
        "abstained": abstained,
        "regions_last": len(rows),
        "actions": outcome.get("log", []),
    }
    if "report" in outcome:
        result["plan"] = outcome["report"]
    print(f"  window_title: {title!r}")
    print(f"  independent check for {args.expect!r}: {'PASS' if verified else 'FAIL'}")
    print(f"  {steps} steps, {result['elapsed_ms']} ms total")
    if args.json:
        print(json.dumps(result))
    if abstained:
        return 6
    return 0 if verified else 4


if __name__ == "__main__":
    sys.exit(main())
