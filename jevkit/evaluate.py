"""Scores and thresholds in place of an LLM judge (the article's Step 6).

An agent loop often pays a second frontier call only to grade the first one. Here the grade
is a handful of Jev readings — quality, relevance, completeness and risk on rubrics, plus two
yes/no checks — and the loop's next step is plain code over those numbers:

    risk > 0.80            -> human_review      (the article's thresholds, on 0..1)
    quality < 0.70         -> retry
    relevance > 0.90       -> continue

The model no longer both judges and decides what happens next; the probability becomes an
input to deterministic software. ``evaluator-default`` is the shipped policy; a caller can
pass its own rubric (rubric levels written for the task, lowest first) or its own policy.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Optional

from . import client, decide as engine, policy as policies

DEFAULT_POLICY = "evaluator-default"
RUBRIC_KEYS = ("quality", "relevance", "completeness", "risk")


def lint_rubric(rubric: Mapping[str, Any]) -> List[str]:
    """A rubric is {name: {"instructions": str, "levels": [lowest, ..., highest]}}."""
    problems = []
    if not isinstance(rubric, Mapping) or not rubric:
        return ["a rubric is an object of {name: {instructions, levels}}"]
    for name, spec in rubric.items():
        if not isinstance(spec, Mapping):
            problems.append(f"{name}: must be an object")
            continue
        levels = spec.get("levels")
        if not isinstance(levels, list) or not 2 <= len(levels) <= client.MAX_SCORE_LEVELS:
            problems.append(f"{name}: levels must be a list of 2 to {client.MAX_SCORE_LEVELS}, lowest first")
        if not isinstance(spec.get("instructions"), (str, Mapping, list)) or not spec.get("instructions"):
            problems.append(f"{name}: needs instructions")
    return problems


def with_rubric(base: Mapping[str, Any], rubric: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The policy with some of its score questions rewritten for this task. Rules are unchanged."""
    out = copy.deepcopy(dict(base))
    for key in [k for k in out if k.startswith("_")]:
        out.pop(key)
    if not rubric:
        return out
    problems = lint_rubric(rubric)
    if problems:
        raise policies.PolicyError("; ".join(problems))
    for name, spec in rubric.items():
        question = out["questions"].get(name)
        if question is None or question.get("type") != "score":
            raise policies.PolicyError(f"{name!r} is not a score question of {out['name']}; "
                                       f"rubric names must be one of {sorted(q for q, v in out['questions'].items() if v['type'] == 'score')}")
        out["questions"][name] = client.score(spec["instructions"], list(spec["levels"]))
    return out


def score(state: Any, *, rubric: Optional[Mapping[str, Any]] = None, policy: Any = DEFAULT_POLICY,
          timeout: float = 4.0, transport: Optional[client.Transport] = None, mode: str = "live",
          record: bool = True, llm_avoided_est: Optional[int] = 1_500) -> Dict[str, Any]:
    """Grade ``state`` (typically ``{"task": ..., "output": ...}``) and say what the loop does next.

    ``llm_avoided_est`` defaults to a judge call's typical size, labelled as an estimate.
    On any failure the action is the policy's ``on_error`` (``caller_default``): do what the
    loop would have done with no grade at all.
    """
    base = policies.load(policy)
    rules = policies.load(with_rubric(base, rubric)) if rubric else base
    result = engine.decide(state, rules, mode=mode, feature="score", timeout=timeout, transport=transport,
                           record=record, llm_avoided_est=llm_avoided_est)
    result["scores"] = {name: reading.get("norm", reading.get("p"))
                        for name, reading in result.get("answers", {}).items()}
    return result
