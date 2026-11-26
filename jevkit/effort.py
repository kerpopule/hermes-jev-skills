"""Map a routing difficulty answer onto a reasoning-effort level for that turn.

Reasoning effort is the knob that buys thinking tokens: a low level answers fast and cheap, a
high one spends more compute before replying. It is a per-request choice, and "how hard is this
question" is exactly the answer routing already paid Jev for — so the effort pick reuses that
difficulty score instead of buying a second judgement. This module owns the table that turns the
score into a level; the plugin owns writing the level onto the request.

The table is a config value (`effort.levels` in routing.json), not code, because the right trade
between speed and quality is the operator's: a fleet on a fast model may never want xhigh, a
frontier one may want it for anything above routine. Defaults map trivial/routine down and
substantial/expert up, which is the balance the name of the ladder implies.

Everything here fails open: an unconfigured or malformed table, a missing answer, or a score
outside the rubric all return None, and None means the request goes out with whatever effort
Hermes had already resolved for it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

# The levels Hermes' agent.reasoning_effort.EFFORT_LADDER defines, without importing it (the
# plugin also runs on Hermes versions where the module layout differs). "ultra" is deliberately
# omitted: it is internal-only (clamped to max everywhere). "minimal" is named so a config can
# use it — it is in Hermes' canonical ladder and the widest OpenAI-compatible wire vocabulary;
# Codex/Responses wires reject it and Hermes clamps it to low at the transport.
KNOWN_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Difficulty buckets 0..3, matching jevkit.route.DIFFICULTY (trivial / routine / substantial /
# expert). Index 0 answers the easiest turns, index 3 the hardest.
DEFAULT_LEVELS = ("low", "medium", "high", "xhigh")

_LEVEL_RE = re.compile(r"^[a-z]+$")

# Routing's tier is a probability-MASS judgement; the effort pick reads the single largest
# bucket. They can disagree: a turn routing called medium can still have trivial as its
# argmax bucket. When they do, the effort must not undercut the tier — otherwise a turn
# routing considered real work gets the cheapest thinking budget. Measured on a live fleet:
# a tool-needing turn landed on effort "none" and the model answered in one line without
# calling a single tool.
TIER_FLOORS = {"simple": 0, "medium": 1, "hard": 2}


def tier_floor(tier: Any) -> int:
    """The lowest difficulty bucket an effort pick may use for a routed tier."""
    return TIER_FLOORS.get(tier, 0) if isinstance(tier, str) else 0


def min_bucket_for(decision: Optional[Mapping[str, Any]], config: Optional[Mapping[str, Any]] = None) -> int:
    """The effort floor for one routing decision: the tier floor, plus a floor for doubt.

    A kept decision (tier None) still carries answers, and an unsure difficulty answer —
    the same low confidence that made routing refuse to switch models — must not buy the
    OFF switch for thinking either: "unsure is not hard" cuts both ways, and a flat
    spread can argmax onto trivial by a hair. A confident trivial turn still gets bucket 0.
    """
    if not isinstance(decision, Mapping):
        return 0                       # anything but a decision object floors nothing
    floor = max(tier_floor(decision.get("tier")), tier_floor(decision.get("effort_tier")))
    if floor >= 1:
        return floor
    try:
        confidence = float(decision.get("answers", {}).get("difficulty", {}).get("confidence", 1.0))
    except (TypeError, ValueError, AttributeError):
        return floor
    threshold = 0.6
    if isinstance(config, Mapping):
        try:
            threshold = float(config.get("min_confidence", threshold))
        except (TypeError, ValueError):
            pass
    return 1 if confidence < threshold else floor


def levels_from_config(config: Optional[Mapping[str, Any]]) -> Optional[tuple]:
    """The configured difficulty→level table, or None when effort routing is off/invalid.

    ``effort.levels`` may be a four-element list (one level per difficulty bucket) or a dict
    naming the buckets (``trivial``, ``routine``, ``substantial``, ``expert``) — missing buckets
    in a dict fall back to DEFAULT_LEVELS so a partial override works. Unknown level names are
    dropped at the call site by the clamp, but a bucket that names no known level is a config
    error and must not silently buy the wrong effort, so the whole table is rejected.
    """
    if not isinstance(config, Mapping):
        return None
    effort = config.get("effort")
    if not isinstance(effort, Mapping) or not effort.get("enabled"):
        return None
    raw = effort.get("levels")
    names = ("trivial", "routine", "substantial", "expert")
    if isinstance(raw, Mapping):
        table = tuple(str(raw.get(name, DEFAULT_LEVELS[i]) or "").strip().lower()
                      for i, name in enumerate(names))
    elif isinstance(raw, (list, tuple)) and len(raw) == len(names):
        table = tuple(str(v or "").strip().lower() for v in raw)
    elif raw is None:
        table = DEFAULT_LEVELS
    else:
        return None
    for level in table:
        if level not in KNOWN_LEVELS:
            return None
    return table


def pick(answers: Optional[Mapping[str, Any]], *, levels: Optional[tuple] = None,
         min_bucket: int = 0) -> Optional[str]:
    """The effort level this turn's difficulty answer buys, or None to leave the request alone.

    ``answers`` is the same object ``route.decide`` consumed: a mapping with a ``difficulty``
    score answer (0..3 rubric with per-bucket probabilities when Jev returned them). The pick
    follows the same "unsure is not hard" rule as routing: a low-confidence answer is not
    evidence for the expensive end of the table, so the score is read conservatively — the
    probability-weighted bucket when a spread exists, the plain score otherwise, rounded toward
    the middle rather than extrapolated past the rubric.
    """
    table = levels or DEFAULT_LEVELS
    if len(table) != 4:
        return None
    if not isinstance(answers, Mapping):
        return None
    diff = answers.get("difficulty")
    if not isinstance(diff, Mapping):
        return None
    try:
        raw_score = diff.get("score")
        score = float(raw_score) if isinstance(raw_score, (int, float)) else float(str(raw_score))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= score <= 3.0:
        return None
    spread = diff.get("probabilities")
    if isinstance(spread, Mapping) and spread:
        try:
            bucket = max(range(4), key=lambda i: float(spread.get(i, spread.get(str(i), 0.0))))
        except (TypeError, ValueError):
            bucket = int(round(score))
    else:
        bucket = int(round(score))
    try:
        floor = int(min_bucket)
    except (TypeError, ValueError):
        floor = 0
    bucket = min(3, max(0, max(floor, bucket)))
    level = table[bucket]
    return level if _LEVEL_RE.match(level) else None
