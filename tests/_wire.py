"""Well-formed Jev wire answers, in one place, for the offline fakes in this suite.

Every fake transport here has to emit shapes `client.ask` accepts. When a fake emits a
distribution that is incomplete or does not sum to one, the test stops measuring the feature
and starts measuring the validator's refusal path — which is how a suite ends up green while
nobody notices that the shape it asserts against is one the API cannot actually produce.

Two rules, taken from what the API does rather than from taste:

* a choice's probabilities cover exactly the options that were offered, and sum to 1;
* a score's distribution sums to 1 and its point estimate is the expected value of that
  distribution.

`picked` keeps the bulk of the mass and the remainder is split evenly over the other options,
so the argmax is unambiguous and the runner-up gap is a parameter (`rest`) instead of an
accident.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


def distribution(keys: Iterable[str], picked: str, rest: float = 0.1) -> Dict[str, float]:
    """Mass over exactly `keys`: `picked` takes 1 - rest, the others share `rest`."""
    keys = list(keys)
    if picked not in keys:
        raise ValueError(f"{picked!r} is not one of {keys!r}")
    others = [key for key in keys if key != picked]
    share = rest / len(others) if others else 0.0
    return {key: (1.0 - rest if key == picked else share) for key in keys}


def choice_answer(question: Mapping[str, Any], picked: str, confidence: float = 0.95,
                  rest: float = 0.1) -> Dict[str, Any]:
    return {"type": "choice", "choice": picked, "confidence": confidence,
            "probabilities": distribution(question["criteria"], picked, rest)}


def score_answer(question: Mapping[str, Any], level: float, confidence: float = 0.95,
                 spread: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """A score whose point estimate is the expected value of its own distribution.

    `spread`, when given, is the probability of each level from 0 up and must sum to 1: the
    score is then its expected value, because a wire pair that disagrees with itself is
    refused by `client.ask` and would silently turn the test into a refusal test.
    """
    levels = len(question["criteria"])
    if spread is not None:
        spread = list(spread)
    else:
        # An integer level is one-hot; a fractional one is split between the two levels it
        # sits between, so the point estimate and the distribution cannot disagree — the API
        # sums to exactly 1 and matches its own mean (measured 3/3 on 2026-09-21).
        low = min(int(level), levels - 1)
        high = min(low + 1, levels - 1)
        weight = float(level) - low
        spread = [0.0] * levels
        spread[low] += 1.0 - weight
        spread[high] += weight
    probabilities = {str(index): float(weight) for index, weight in enumerate(spread)}
    mean = sum(index * weight for index, weight in enumerate(spread))
    return {"type": "score", "score": mean, "confidence": confidence, "probabilities": probabilities}


def noul_answer(value: float) -> Dict[str, Any]:
    return {"type": "noul", "noul": value}