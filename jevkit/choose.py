"""Computer use and browser use: Jev picks the next action from a table you built.

The agent observes the screen or page and builds a short table of complete,
prevalidated actions, each with an opaque id. Jev sees only the goal, a compact
description of what is on screen, and the id + description of each action. It
returns one id. It cannot invent coordinates, text, selectors or tool calls, so
a wrong answer can only ever be one of the actions you already judged safe.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Mapping, Optional

from . import client, privacy

REQUEST_SCHEMA = "jev.action_choice_request_v1"
RESPONSE_SCHEMA = "jev.action_choice_v1"
MAX_CANDIDATES = 32
MAX_REGIONS = 100
MAX_HISTORY = 16
def _floor() -> float:
    """The confidence below which `choose` declines to act. Measured, not guessed.

    It was 0.80, chosen by feel, and it was throwing away answers Jev had right: a live
    run lost "open the Sound pane" at 0.74. scripts/calibrate_choose.py replays labelled
    cases - including traps and screens where the only right move is not to act - at
    every threshold. With on-screen regions supplied, as the real runner supplies them:

      * correct answers scored 0.93-0.99 synthetic, 0.74-0.90 on a live 26-row table
      * the ONE wrong answer ("cancel without losing my work" -> Save) was wrong 9 runs
        in 10 but never above 0.58. Jev knows when it is unsure; the floor just has to
        sit above where the wrong answers live.

    0.60 blocks that case by 0.02, which is inside the +-0.08 run-to-run noise. 0.65 keeps
    every real-world correct answer and leaves actual margin. JEV_MIN_CONFIDENCE overrides
    it, clamped so it can never be set down into the band where wrong answers were seen.

    A second question - "does any candidate match the goal at all", the way Stagehand's Act
    primitive gates its own Jev pick - was measured against these same cases and NOT adopted
    (2026-09-21). It does separate cleanly: `no_answer` cases topped out at 0.45 across five
    live runs while every other case started at 0.61. It buys nothing all the same, because
    this floor already produced zero wrong actions in every run - the known wrong answer sits
    at 0.45-0.60, under it, and slipped through exactly once at a 0.60 floor - while used
    alone at any useful threshold the match question acts on that same case. Tables, limits
    and the script: evals/choose-match/ and scripts/calibrate_choose_match.py.

    Stakes and margin - a higher bar where the caller marked a candidate irreversible, and a
    minimum gap between the top choice and the runner-up - were priced the same way and NOT
    adopted either (2026-09-22). On the same cases over three live runs, all 18 gate families
    reproduced this floor exactly. On a screen with nothing irreversible the correct answers came
    back at 0.92+ and the declines at 0.72 or below, so no floor between 0.55 and 0.70 changes a
    decision there, and the one case that ever sits in the band is the irreversible trap - wrong
    at 0.51-0.53, right at 0.42, with confidence not separating the two. Script, numbers and the
    honest gap in the harness: evals/choose-match/SCORECARD-2026-09-22-stakes-and-margin.md.
    """
    try:
        value = float(os.environ.get("JEV_MIN_CONFIDENCE", "") or 0.65)
    except ValueError:
        value = 0.65
    return min(0.95, max(0.60, value))


MIN_CONFIDENCE = _floor()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
    if privacy.is_sensitive(value):
        raise ValueError(f"{name} looks sensitive and will not be sent")
    return value


def validate(request: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(request, Mapping) or request.get("schema") != REQUEST_SCHEMA:
        raise ValueError(f"request.schema must be {REQUEST_SCHEMA}")
    unknown = set(request) - {"schema", "goal", "observation_id", "regions", "history", "candidates"}
    if unknown:
        raise ValueError(f"unsupported request fields: {sorted(unknown)}")
    regions = request.get("regions") or []
    if not isinstance(regions, list) or len(regions) > MAX_REGIONS:
        raise ValueError(f"regions must be a list of at most {MAX_REGIONS}")
    clean_regions: List[Dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, Mapping) or not set(region) <= {"id", "role", "label", "interactive"}:
            raise ValueError("a region may contain only id, role, label and interactive")
        clean_regions.append({"id": _text(region.get("id"), "region id", 128),
                              "role": _text(region.get("role", "element"), "region role", 64),
                              "label": _text(region.get("label"), "region label", 300),
                              "interactive": bool(region.get("interactive", False))})
    history = request.get("history") or []
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise ValueError(f"history must be a list of at most {MAX_HISTORY}")
    clean_history = []
    for entry in history:
        if not isinstance(entry, Mapping) or not set(entry) <= {"selected_id", "outcome"}:
            raise ValueError("a history item may contain only selected_id and outcome")
        clean_history.append({k: _text(v, f"history {k}", 160) for k, v in entry.items()})
    candidates = request.get("candidates")
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError(f"candidates must hold between 2 and {MAX_CANDIDATES} actions")
    table: Dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {"id", "description"}:
            raise ValueError("a candidate must contain exactly id and description")
        identifier = candidate["id"]
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in table:
            raise ValueError("candidate ids must be unique and use only letters, digits and . _ : -")
        table[identifier] = _text(candidate["description"], "candidate description", 600)
    if not {"reobserve", "abstain"} <= set(table):
        raise ValueError("candidates must include `reobserve` and `abstain`")
    return {"goal": _text(request.get("goal"), "goal", 2000), "observation_id": str(request.get("observation_id", ""))[:256],
            "regions": clean_regions, "history": clean_history, "table": table}


def choose(request: Mapping[str, Any], *, timeout: float = 3.0, mock: bool = False,
           transport: Optional[client.Transport] = None) -> Dict[str, Any]:
    """Raises ValueError for a bad request. A Jev failure returns ``reobserve``, never a guess."""
    valid = validate(request)
    table = valid["table"]

    def answer(selected: str, confidence: float, reason: str, probabilities: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        return {"schema": RESPONSE_SCHEMA, "selected_id": selected, "confidence": round(confidence, 3), "reason": reason,
                "observation_id": valid["observation_id"], "probabilities": probabilities or {}}

    if mock:
        return answer("reobserve", 1.0, "mock")
    state = {"goal": valid["goal"], "on_screen": valid["regions"], "already_tried": valid["history"]}
    question = client.choice("Which single action should be taken next to move toward the goal?", table)
    try:
        reply = client.ask(state, {"next_action": question}, timeout=timeout, transport=transport)
    except client.JevError as error:
        return answer("reobserve", 0.0, f"Jev unavailable ({error.code})")
    picked = reply["answers"]["next_action"]
    if picked["confidence"] < MIN_CONFIDENCE:
        return answer("reobserve", picked["confidence"], "low confidence", picked["probabilities"])
    return answer(picked["choice"], picked["confidence"], "chosen", picked["probabilities"])
