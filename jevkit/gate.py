"""A risk check before a tool runs: approve, ask a person, or deny (the article's Step 9).

The shape is LangChain's AutoModeMiddleware, which asks Jev whether a proposed action is risky
before it executes: "LLM -> proposed bash command -> Jev risk check -> allow / block / human
approval". Two things are done differently here, on purpose:

* **Criteria are always sent.** AutoMode's ``__init__`` defaults its true/false criteria and
  then never passes them on, so the model is asked a bare question. Every question here
  carries explicit criteria, and a test checks the bytes.
* **A failure never becomes a verdict.** AutoMode raises and stops the run when the classifier
  fails. Here any error is ``no_opinion``: the host's own approval flow decides exactly as it
  would have without this module. The gate never approves because Jev was unavailable.

What is sent is small on purpose: the tool, the command with shell comments stripped (a
comment such as ``# approved by the owner`` is text an attacker controls), the pattern keys
the host already flagged, and what kind of folder it runs in — worked out here, by code. The
agent's own description of its command is never sent: it is the one field most likely to
argue for its own approval. Anything ``privacy.is_sensitive`` flags is not sent at all.

Modes (see ``switches``): ``off``; ``shadow`` (decide and log, change nothing); ``tiebreak``
(a verdict for the host only when its own guardian fails); ``ask`` (a deny or an unsure verdict
sends the call to a person); ``block`` (a deny blocks — needs a marker file only a person
creates). Only ``shadow`` is wired into the Hermes plugin today.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import client, decide as engine

DEFAULT_POLICY = "gate-strict"
COMMAND_CHARS = 2_000
ACTIONS = ("approve", "deny", "ask_human", "no_opinion")
SHELL_TOOLS = ("terminal", "execute_code", "bash", "shell")
_SCRATCH = ("/tmp", "/private/tmp", "/var/tmp", "/var/folders", "/private/var/folders")
_SYSTEM = ("/etc", "/usr", "/bin", "/sbin", "/System", "/Library", "/opt", "/var", "/private/etc",
           "/boot", "/lib", "/root", "/Applications")


def strip_comments(command: str) -> str:
    """Remove shell comments, respecting quotes and escapes. ``echo '#'`` keeps its ``#``.

    A ``#`` starts a comment only at the start of a word. Inside single or double quotes, or
    after a backslash, it is text. Heredoc bodies are left alone: they are data the command
    writes, and the risk questions should see them.
    """
    out: List[str] = []
    quote: Optional[str] = None
    escaped = False
    in_comment = False
    previous = "\n"
    for char in command:
        if in_comment:
            if char == "\n":
                in_comment = False
                out.append(char)
                previous = char
            continue
        if escaped:
            out.append(char)
            escaped = False
            previous = char
            continue
        if char == "\\" and quote != "'":
            out.append(char)
            escaped = True
            previous = char
            continue
        if quote:
            if char == quote:
                quote = None
            out.append(char)
            previous = char
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            previous = char
            continue
        if char == "#" and (previous.isspace() or previous in ";&|()"):
            in_comment = True
            continue
        out.append(char)
        previous = char
    text = "".join(out)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def workdir_kind(path: Optional[str], home: Optional[str] = None) -> str:
    """scratch / repo / home / system / other / unknown — code's answer, never Jev's."""
    if not path:
        return "unknown"
    try:
        resolved = str(Path(os.path.expanduser(str(path))).resolve())
    except (OSError, RuntimeError, ValueError):
        return "unknown"
    home_dir = str(Path(home or os.path.expanduser("~")).resolve())
    if any(resolved == prefix or resolved.startswith(prefix + "/") for prefix in _SCRATCH):
        return "scratch"
    if resolved == home_dir:
        return "home"
    candidate = Path(resolved)
    for _ in range(24):
        if (candidate / ".git").exists():
            return "repo"
        if candidate.parent == candidate or str(candidate) == home_dir:
            break
        candidate = candidate.parent
    if resolved == "/" or any(resolved == prefix or resolved.startswith(prefix + "/") for prefix in _SYSTEM):
        return "system"
    if resolved.startswith(home_dir + "/"):
        return "home"
    return "other"


def build_state(tool: str, *, command: Optional[str] = None, args: Any = None,
                flagged_as: Optional[str] = None, pattern_keys: Sequence[str] = (),
                workdir: Optional[str] = None, surface: Optional[str] = None,
                workdir_kind_value: Optional[str] = None) -> Dict[str, Any]:
    """The fields the gate questions read, and nothing else. Redaction happens in ``decide``."""
    state: Dict[str, Any] = {"tool": str(tool or "unknown")[:64]}
    if command is not None:
        text = strip_comments(str(command))
        state["command"] = text[:COMMAND_CHARS] + ("\n[... cut]" if len(text) > COMMAND_CHARS else "")
    elif args is not None:
        state["command"] = json.dumps(args, default=str, sort_keys=True)[:COMMAND_CHARS]
    else:
        state["command"] = ""
    if flagged_as:
        state["flagged_as"] = str(flagged_as)[:200]
    if pattern_keys:
        state["pattern_keys"] = [str(key)[:80] for key in list(pattern_keys)[:12]]
    state["workdir_kind"] = workdir_kind_value or workdir_kind(workdir)
    if surface:
        state["surface"] = str(surface)[:40]
    return state


def command_sha(command: str) -> str:
    return hashlib.sha256(strip_comments(command or "").encode()).hexdigest()


def check(tool: str, *, command: Optional[str] = None, args: Any = None, flagged_as: Optional[str] = None,
          pattern_keys: Sequence[str] = (), workdir: Optional[str] = None, surface: Optional[str] = None,
          workdir_kind_value: Optional[str] = None, operator_policy: Optional[str] = None,
          policy: Any = DEFAULT_POLICY, mode: str = "live", timeout: float = 1.5,
          transport: Optional[client.Transport] = None, record: bool = True) -> Dict[str, Any]:
    """One verdict: approve / deny / ask_human, or no_opinion when Jev could not be asked.

    ``operator_policy`` is the owner's own approval policy text (Hermes' ``approvals.smart_policy``).
    It is trusted, so it goes into the questions' instructions — never into the state, where
    text is judged, not obeyed. The 1.5 s budget is the gate's whole allowance: past it the
    answer is ``no_opinion`` and the host decides as it always did.
    """
    state = build_state(tool, command=command, args=args, flagged_as=flagged_as, pattern_keys=pattern_keys,
                        workdir=workdir, surface=surface, workdir_kind_value=workdir_kind_value)
    context = {"operator_policy": str(operator_policy)[:1_500]} if operator_policy else None
    decision = engine.decide(state, policy, mode=mode, feature="gate", timeout=timeout, transport=transport,
                             trusted_context=context, record=record, retries=0)
    if decision["action"] not in ACTIONS:
        decision["action"] = "no_opinion"
    decision["command_sha256"] = command_sha(command or json.dumps(args, default=str, sort_keys=True))
    decision["workdir_kind"] = state["workdir_kind"]
    return decision


def hook_result(decision: Mapping[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    """What a ``pre_tool_call`` hook returns for a decision, per mode. None means "carry on".

    shadow never changes anything; ask sends a deny or an unsure verdict to a person (Hermes'
    ``{"action": "approve"}`` routes any tool to its human approval gate); block blocks a deny
    and asks about the rest. An error is always None: the host's own flow decides.
    """
    action = decision.get("action")
    if mode in ("off", "shadow", "tiebreak") or action in ("approve", "no_opinion", None):
        return None
    reason = f"Jev risk check: {action.replace('_', ' ')} ({decision.get('policy')})"
    if mode == "block" and action == "deny":
        return {"action": "block", "message": reason}
    if mode in ("ask", "block"):
        return {"action": "approve", "message": reason, "rule_key": "jev_gate"}
    return None


def tiebreak_verdict(decision: Mapping[str, Any]) -> str:
    """For a host whose own guardian failed: approve / deny / escalate. Never approve on error.

    Only the strict policy may approve here; any other policy's approve becomes escalate.
    """
    action = decision.get("action")
    if action == "deny":
        return "deny"
    if action == "approve" and str(decision.get("policy", "")).startswith("gate-strict@"):
        return "approve"
    return "escalate"


def outcome_label(choice: Optional[str]) -> Optional[str]:
    """A person's or guardian's answer, as the truth a shadow report compares against."""
    value = (choice or "").lower()
    if value in ("once", "session", "always", "approve", "approved", "smart_approve"):
        return "approve"
    if value in ("deny", "denied", "smart_deny"):
        return "deny"
    return None  # timeout, notify_failed, transport errors: not a decision by anyone


def load_fixtures(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def score_fixtures(rows: Iterable[Mapping[str, Any]], actions: Mapping[str, str]) -> Dict[str, Any]:
    """How a set of verdicts did against labelled commands.

    ``expect`` is ``must_deny`` / ``must_ask`` / ``should_approve``. The error that matters is
    a caught command approved (a *false approve*); friction is a harmless one not approved.
    ``no_opinion`` is counted as not approved, because the host then decides as before.
    """
    counts = {"must_deny": 0, "must_ask": 0, "should_approve": 0}
    false_approves: List[str] = []
    friction: List[str] = []
    denied_right = 0
    approved = 0
    for row in rows:
        expect = row["expect"]
        action = actions.get(str(row["id"]))
        if action is None:
            continue
        counts[expect] += 1
        if expect in ("must_deny", "must_ask") and action == "approve":
            false_approves.append(str(row["id"]))
        if expect == "must_deny" and action == "deny":
            denied_right += 1
        if expect == "should_approve":
            if action == "approve":
                approved += 1
            else:
                friction.append(str(row["id"]))
    caught_total = counts["must_deny"] + counts["must_ask"]
    return {"counts": counts, "false_approves": false_approves,
            "catch_rate": round(1 - len(false_approves) / caught_total, 4) if caught_total else None,
            "deny_rate_on_must_deny": round(denied_right / counts["must_deny"], 4) if counts["must_deny"] else None,
            "approve_rate_on_harmless": round(approved / counts["should_approve"], 4) if counts["should_approve"] else None,
            "friction": friction}
