"""One Jev request for two decisions: how to route this turn, and which skill it needs.

A Hermes turn pays two round trips before the model runs — skill selection on the pre-call
hook, routing on the request hook. Jev charges per request, not per question, so the two are
asked together here and each answer is handed back to the module that owns its policy:
`route.decide(answers=...)` and `skillpick.pick(stage_one=...)`. Neither module's thresholds,
floors or fail-open paths change; only the number of round trips does.

Measured on 2026-09-21 against the live API with a 379-skill catalog, one turn:

| request | latency |
|---|---|
| routing's three questions alone | ~540 ms |
| skill-pick stage 1 alone (120 options) | ~647 ms |
| both in one request | ~620 ms |

What is deliberately **not** here: stage 2, the per-finalist verification. Its questions
depend on the stage-1 answer, so it stays a second request and its own decision.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import client, privacy, route, skillpick

DEFAULT_TIMEOUT = 4.0


def mergeable(turn: str, *, profile: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Why this turn must not be asked as one request, or None when it may be.

    The rule is the privacy boundary, not convenience. Routing sends coarse features instead
    of text for a private profile or a turn that looks sensitive; skill selection refuses such
    a turn outright. A merged request cannot be half-private, so those turns keep the two
    separate calls they have today.
    """
    if not turn.strip():
        return "empty turn"
    config = config or route.load_config()
    if config.get("mode") == "features":
        return "routing is configured to send features only"
    if profile and profile in (config.get("private_profiles") or []):
        return f"profile {profile!r} is on the private list"
    if privacy.is_sensitive(turn):
        return "the turn looks sensitive"
    return None


def decide_turn(
    turn: str, skills: List[Dict[str, str]], *, profile: Optional[str] = None, context_tokens: int = 0,
    config: Optional[Dict[str, Any]] = None, timeout: float = DEFAULT_TIMEOUT,
    transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    """Ask routing's questions and skill selection's stage 1 in one request.

    Returns one of:
      ``{"status": "ok", "route_answers": ..., "stage_one": ..., "latency_ms": ...}``
      ``{"status": "not_mergeable", "reason": ...}`` — the caller runs its usual path
      ``{"status": "fail_open", "reason": ...}`` — Jev did not answer; nothing is invented
    """
    config = config or route.load_config()
    if not skills:
        return {"status": "not_mergeable", "reason": "no skills to rank"}
    reason = mergeable(turn, profile=profile, config=config)
    if reason:
        return {"status": "not_mergeable", "reason": reason}
    # Route's own clipping: a long turn keeps its opening and its end, which is where the ask is.
    limit = int(config.get("ask_chars", 2500))
    inner = route.unwrap(turn, config)
    ask = inner if len(inner) <= limit else inner[:limit // 4] + "\n[…]\n" + inner[-(limit - limit // 4):]
    # The turn goes in **once**. Sending it twice (routing's `user_turn` plus a `turn` field for
    # skill selection) was measured to cost routing its calibration on this same turn:
    # difficulty confidence 0.50-0.62 against 0.70-0.75 for the standalone call, which is the
    # difference between routing a borderline turn and leaving it on whatever model it had.
    # Both question sets read the same field; the pick instruction says "this turn" and the
    # state has exactly one.
    state: Dict[str, Any] = dict(route.state_for(ask, context_tokens=context_tokens, limit=limit))
    questions = dict(route.questions())
    questions.update(skillpick.stage_one_questions(skills))
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return {"status": "fail_open", "reason": f"Jev unavailable ({error.code})"}
    return {
        "status": "ok",
        "latency_ms": reply["latency_ms"],
        "route_answers": {name: reply["answers"][name] for name in route.questions()},
        "stage_one": skillpick.stage_one_answers(reply, skills),
        "usage": reply.get("usage") or {},
    }