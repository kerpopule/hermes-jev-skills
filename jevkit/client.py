"""One small, strict client for TypeSafe Jev (POST /v1/systemone).

Jev answers typed questions about a state: ``choice`` (one of a closed set),
``score`` (a position on an ordered rubric) and ``noul`` (probability of yes).
It never writes text. Every helper here validates the reply against the question
that was asked, so a malformed or surprising answer becomes a ``JevError`` and
the caller takes its fail-open path rather than acting on junk.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from . import keystore

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
# Jev, reached through OpenRouter's Decisions API instead of TypeSafe directly: one key
# instead of two for anyone already on OpenRouter. Same request, same answers, same model -
# only the URL and the model id differ. Contributed as PR #1 by Lorenzo DZ (@Barba2k2),
# whose version prompted a chat model for JSON instead; that returns an LLM's guess with a
# made-up confidence, which is the one thing a decision model exists not to do.
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "~typesafe/jev-latest"
MAX_RESPONSE_BYTES = 1_000_000
MAX_STATE_CHARS = 60_000
USER_AGENT = "hermes-jev-skills/0.1"

State = Union[str, Mapping[str, Any], Sequence[Any]]
Transport = Callable[[bytes, Dict[str, str], float], bytes]


class JevError(RuntimeError):
    """Anything that means "do not trust or use this Jev result"."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        # Set only by a contradiction check (`client._invalid`): which invariant the reply broke.
        self.invariant: Optional[str] = None


# ── question builders ────────────────────────────────────────────────────────

def choice(instructions: str, criteria: Mapping[str, str]) -> Dict[str, Any]:
    if len(criteria) < 2:
        raise ValueError("a choice needs at least two options")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, levels: Sequence[str]) -> Dict[str, Any]:
    if len(levels) < 2:
        raise ValueError("a score needs at least two levels")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def noul(instructions: str) -> Dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


# ── transport ────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        # A redirect would carry the bearer token to another origin.
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _http_transport(body: bytes, headers: Dict[str, str], timeout: float, url: str = ENDPOINT) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = {401: "auth_failed", 403: "auth_failed", 402: "credits_exhausted",
                429: "rate_limited", 529: "overloaded"}.get(error.code, f"http_{error.code}")
        raise JevError(code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JevError("network") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JevError("response_too_large")
    return raw


def _openrouter_transport(body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
    return _http_transport(body, headers, timeout, OPENROUTER_ENDPOINT)


_RETRYABLE = {"rate_limited", "overloaded", "network", "http_500", "http_502", "http_503", "http_504"}


# ── validation ───────────────────────────────────────────────────────────────

# Two tolerances, taken from a validator that already runs against this same API rather than
# guessed here: jkudish/jev-mcp (MIT), `src/lib.ts:11` PROBABILITY_SUM_TOLERANCE = 0.01 + 1e-12
# and `src/lib.ts:258` SCORE_MEAN_TOLERANCE = 0.02 + 1e-12. jev-ultrafast derives the same two
# ideas independently (`model.py:38-39`, sum within 0.02, choice >= max - 1e-6). A live probe of
# api.typesafe.ai (jev-1.13.0, 2026-09-21) summed to exactly 1.0, chose the argmax, and matched
# its own expected value exactly, so these bands are slack for float noise, not a correction.
PROBABILITY_SUM_TOLERANCE = 0.01 + 1e-12
SCORE_MEAN_TOLERANCE = 0.02 + 1e-12
ARGMAX_TOLERANCE = 1e-9


def _invalid(name: str, invariant: str, detail: str = "") -> JevError:
    """A reply that parses but contradicts itself. Typed, named, and never acted on.

    The code is not ``malformed``: the JSON was fine, the *answer* was not. Callers fail open
    on both, but a log or a counter can tell "the wire broke" from "the model agreed with
    itself inconsistently" only if the two stay distinguishable.
    """
    error = JevError("invalid_response", f"answer {name} violated {invariant}" + (f" ({detail})" if detail else ""))
    error.invariant = invariant  # recorded, so an eval can count which rule fires and how often
    return error


def _unit(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError("malformed", f"{name} is not numeric")
    number = float(value)
    if not math.isfinite(number) or not -1e-6 <= number <= 1 + 1e-6:
        raise JevError("malformed", f"{name} is outside 0..1")
    return min(1.0, max(0.0, number))


def _distribution(name: str, raw: Any, keys: Sequence[str], invariant: str) -> Dict[str, float]:
    """A probability mass over exactly ``keys``: complete, finite, and summing to one.

    An incomplete key set is refused rather than tolerated. Every source that validates this
    API's replies — jev-mcp's ``validateChoiceAnswer`` (exact key count and membership) and
    jev-ultrafast's ``validate_choice`` (``set(probabilities) == set(ids)``) — requires the key
    set to *equal* the offered options, and ``probabilities: {}`` used to reach callers here as
    an all-zero distribution, which read as "no evidence" at best and as a confident gap of 1.0
    at worst (mailbox.py carries the scar). A missing entry is not a zero: it is a reply we
    cannot interpret, so it becomes a refusal the caller already knows how to survive.
    """
    if not isinstance(raw, dict):
        raise _invalid(name, invariant, "probabilities are not an object")
    expected, found = set(keys), set(raw)
    if found != expected:
        missing, extra = sorted(expected - found), sorted(found - expected)
        raise _invalid(name, invariant, f"missing {missing or 'none'}, unexpected {extra or 'none'}")
    values = {key: _unit(raw[key], f"{name}.p[{key}]") for key in keys}
    total = sum(values.values())
    if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise _invalid(name, invariant, f"mass sums to {total!r}")
    return values


def _check_answer(name: str, question: Mapping[str, Any], answer: Any) -> Dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        raise JevError("malformed", f"answer {name} has the wrong type")
    kind = question["type"]
    if kind == "noul":
        return {"type": "noul", "noul": _unit(answer.get("noul"), f"{name}.noul")}
    if kind == "choice":
        options = list(question["criteria"])
        picked = answer.get("choice")
        if not isinstance(picked, str) or picked not in set(options):
            raise JevError("malformed", f"answer {name} chose an option that was not offered")
        probabilities = _distribution(name, answer.get("probabilities"), options,
                                      "choice_probability_key_set")
        top = max(probabilities.values())
        if probabilities[picked] < top - ARGMAX_TOLERANCE:
            # A choice that is not the maximum is a contradiction in the reply, whatever the
            # confidence says: it means the ranking callers read ("the highest option") and the
            # label callers act on ("the chosen option") are two different answers.
            raise _invalid(name, "choice_is_argmax",
                           f"chose {picked} at {probabilities[picked]!r} against a maximum of {top!r}")
        return {"type": "choice", "choice": picked, "probabilities": probabilities,
                "confidence": _unit(answer.get("confidence"), f"{name}.confidence")}
    levels = len(question["criteria"])
    value = answer.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise JevError("malformed", f"answer {name} has no numeric score")
    if not -0.5 <= float(value) <= levels - 0.5:
        raise JevError("malformed", f"answer {name} scored off the rubric")
    # The per-level spread says far more than the averaged score: an unsure answer averages to
    # the middle of the rubric, which looks like a real "medium-hard" unless you read the spread.
    #
    # Unlike a choice, a score may legitimately arrive with no distribution at all (jev-mcp
    # keeps that answer valid and its distribution null). We keep it too, but we say so:
    # `spread_reported` is False, so a gate that needs the spread to be trustworthy can treat
    # the score as unverified instead of quietly averaging over nothing.
    raw = answer.get("probabilities")
    spread: Dict[int, float] = {}
    if isinstance(raw, dict) and raw:
        keys = []
        for key in raw:
            if not str(key).isdigit() or int(key) >= levels:
                raise _invalid(name, "score_distribution_on_rubric", f"level {key!r} is not 0..{levels - 1}")
            keys.append(str(key))
        spread = {int(key): value for key, value in
                  _distribution(name, raw, sorted(keys, key=int), "score_distribution_mass").items()}
        mean = sum(level * probability for level, probability in spread.items())
        if abs(mean - float(value)) > SCORE_MEAN_TOLERANCE:
            # The incident this rule exists for: a flat 0.2-each spread that averaged to 2.73 was
            # filed at level 4 of 5 by rounding. A score that disagrees with its own distribution
            # is not a reading of the rubric, it is two readings, and neither can be acted on.
            raise _invalid(name, "score_matches_its_distribution",
                           f"score {float(value)!r} against an expected value of {mean!r}")
    # The legend is how the model read the rubric it was handed. Barely redundant with our own
    # labels — but when it differs, the disagreement is worth seeing, and dropping it hid that.
    legend = {}
    if isinstance(answer.get("legend"), dict):
        legend = {int(key): str(text) for key, text in answer["legend"].items()
                  if str(key).isdigit() and int(key) < levels and isinstance(text, str)}
    out = {"type": "score", "score": float(value), "probabilities": spread,
           "spread_reported": bool(spread), "confidence": _unit(answer.get("confidence", 1.0),
                                                               f"{name}.confidence")}
    if legend:
        out["legend"] = legend
    return out


# ── public call ──────────────────────────────────────────────────────────────

def ask(
    state: State,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    timeout: float = 4.0,
    retries: int = 1,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    provider: Optional[str] = None,
    transport: Optional[Transport] = None,
) -> Dict[str, Any]:
    """Ask Jev every question against one state, in a single request.

    Returns ``{"answers": {...validated...}, "usage": {...}, "latency_ms": int}``.
    Raises ``JevError`` for anything the caller should not act on. ``timeout`` is a
    total wall-clock budget across retries, not a per-attempt one.
    """
    if not questions:
        raise ValueError("no questions")
    via = provider or ("typesafe" if api_key else keystore.provider())
    if via not in keystore.PROVIDERS:
        via = "typesafe"
    key = api_key or keystore.resolve(via)
    if not key:
        raise JevError("no_key", "run `jev setup-key`")
    encoded_state = state if isinstance(state, str) else json.dumps(state, separators=(",", ":"), default=str)
    if len(encoded_state) > MAX_STATE_CHARS:
        raise JevError("state_too_large")
    default_model = OPENROUTER_MODEL if via == "openrouter" else DEFAULT_MODEL
    body = json.dumps(
        {"state": state, "model": model or os.environ.get("TYPESAFE_MODEL") or default_model,
         "questions": {name: dict(q) for name, q in questions.items()}},
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": USER_AGENT}
    if via == "openrouter":
        # OpenRouter asks callers to identify themselves; neither header carries anything
        # about the person or the decision.
        headers["HTTP-Referer"] = "https://github.com/kerpopule/hermes-jev-skills"
        headers["X-Title"] = "Hermes Jev Skills"
    send = transport or (_openrouter_transport if via == "openrouter" else _http_transport)

    started = time.monotonic()
    attempt = 0
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0.05:
            raise JevError("timeout")
        try:
            raw = send(body, headers, remaining)
            break
        except JevError as error:
            attempt += 1
            if error.code not in _RETRYABLE or attempt > retries:
                raise
            time.sleep(min(0.25 * attempt, max(0.0, timeout - (time.monotonic() - started) - 0.1)))

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JevError("malformed", "reply is not JSON") from None
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise JevError("malformed", "reply has no answers")
    checked = {name: _check_answer(name, question, answers.get(name)) for name, question in questions.items()}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {"answers": checked, "usage": usage, "latency_ms": int((time.monotonic() - started) * 1000)}


def verify_key(api_key: str, timeout: float = 10.0, provider: str = "typesafe") -> bool:
    """One tiny synthetic call. True means the key is accepted by that provider."""
    try:
        ask("The build finished and all tests passed.",
            {"ok": noul("The text reports a successful outcome")},
            api_key=api_key, provider=provider, timeout=timeout)
        return True
    except JevError:
        return False


def batches(items: Sequence[Any], size: int) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
