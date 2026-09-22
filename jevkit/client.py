"""One small, strict client for TypeSafe Jev (POST /v1/systemone).

Jev answers typed questions about a state: ``choice`` (one of a closed set),
``score`` (a position on an ordered rubric) and ``noul`` (probability of yes).
It never writes text. Every helper here validates the reply against the question
that was asked, so a malformed or surprising answer becomes a ``JevError`` and
the caller takes its fail-open path rather than acting on junk.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import threading
import time
import urllib.parse
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


# ── the shape of a question ──────────────────────────────────────────────────
#
# The builders above only guard the callers that use them. A dict written by hand went
# straight to the wire, and `_check_answer` then read `question["criteria"]` and died on a
# KeyError, or read an answer against a question nobody had actually asked. The rules live
# here, once, and `ask` applies them to everything it is handed — so a feature cannot ship a
# question Jev cannot answer, whoever built it.

QUESTION_TYPES = ("choice", "score", "noul")
MIN_CRITERIA = 2


def _identifier(text: Any) -> str:
    """What a string says once punctuation, case and underscores stop distinguishing it."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _shown(value: Any) -> str:
    """A refusal quotes part of the caller's own input back, never all of it.

    A 1 MB type came back as a 1 MB error with the actual complaint at the far end of it.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= 80 else text[:77] + "..."


def check_question(name: str, question: Any) -> Dict[str, Any]:
    """One question in the shape Jev answers, or ``ValueError`` saying what is wrong.

    The rules, and why each one exists:

    * the ``type`` decides how the reply is read, so an unknown one is refused;
    * ``instructions`` is the question. The name is an identifier — a question whose
      instructions repeat its own name was never written, and Jev scores the state against
      that name;
    * a choice needs a mapping of at least two options and a score a list of at least two
      levels, because one option is not a choice and the reply check reads the criteria;
    * a noul has no criteria, so criteria written here would never be sent — refuse them
      rather than let a caller believe they did something.
    """
    if not isinstance(question, Mapping):
        raise ValueError(f'question "{_shown(name)}" must be an object like {{"type": ..., "instructions": ...}}')
    kind = question.get("type")
    if kind not in QUESTION_TYPES:
        found = "has no type" if kind is None else f"has unknown type {_shown(json.dumps(kind, default=str))}"
        raise ValueError(f'question "{_shown(name)}" {found}; use one of {", ".join(QUESTION_TYPES)}')
    text = question.get("instructions")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f'question "{_shown(name)}" has no instructions: the text of the question, as a string')
    if _identifier(text) == _identifier(name):
        raise ValueError(f'question "{_shown(name)}" asks nothing: its instructions only repeat its own name. '
                         f'The id names the question, "instructions" asks it')
    criteria = question.get("criteria")
    if kind == "noul":
        if criteria is not None:
            raise ValueError(f'question "{_shown(name)}" is a noul: it has no criteria, so criteria written here '
                             f'are never sent and never answered')
        return {"type": "noul", "instructions": text}
    wanted, shape = ((dict, 'an object of at least two options, {"option": "what it means"}')
                     if kind == "choice" else (list, "a list of at least two levels, lowest first"))
    if criteria is None:
        raise ValueError(f'question "{_shown(name)}": criteria are required for a {kind}, as {shape}')
    if not isinstance(criteria, wanted) or len(criteria) < MIN_CRITERIA:
        raise ValueError(f'question "{_shown(name)}": criteria for a {kind} must be {shape}')
    return {"type": kind, "instructions": text, "criteria": dict(criteria) if kind == "choice" else list(criteria)}


def check_questions(questions: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Every question named and shaped, in the order and under the names it was handed."""
    if not isinstance(questions, Mapping):
        raise ValueError("questions must be a mapping of {name: question}")
    if not questions:
        raise ValueError("no questions")
    return {str(name): check_question(str(name), question) for name, question in questions.items()}


# ── transport ────────────────────────────────────────────────────────────────

# A Jev call is one POST, and until 2026-09-22 each one opened its own TLS session. Measured
# on the same question against api.typesafe.ai: urllib with a fresh opener 522 ms, one
# `http.client` connection reused 245 ms, httpx (what the official SDK pools) 189 ms. The
# handshake was over half of what a decision cost, and a Hermes turn pays for two of them.
#
# The pool is bounded and lends one connection to one caller at a time: skill selection fans
# out its batches on threads, and a shared connection would interleave two responses on one
# socket. A server that closes an idle keep-alive socket is the ordinary failure — one fresh
# connection, then the caller's own retry policy.
MAX_POOLED_CONNECTIONS = 8


class _ConnectionPool:
    def __init__(self, limit: int = MAX_POOLED_CONNECTIONS) -> None:
        self._free: List[Any] = []          # [(key, connection)], newest last
        self._limit = limit
        self._lock = threading.Lock()

    def borrow(self, key: Any, timeout: float) -> Any:
        with self._lock:
            for index in range(len(self._free) - 1, -1, -1):
                if self._free[index][0] == key:
                    connection = self._free.pop(index)[1]
                    _reuse(connection, timeout)
                    return connection
        return _connect(key, timeout)

    def release(self, key: Any, connection: Any) -> None:
        """Give a healthy connection back, or close it when the pool is already full."""
        with self._lock:
            if _is_open(connection) and len(self._free) < self._limit:
                self._free.append((key, connection))
                return
        _close(connection)

    def drop(self, connection: Any) -> None:
        """A connection whose state is unknown: never handed out again."""
        _close(connection)

    def size(self) -> int:
        with self._lock:
            return len(self._free)


_POOL = _ConnectionPool()


def _origin(url: str) -> Any:
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "https").lower()
    return (scheme, parsed.hostname or "", parsed.port), (parsed.path or "/") + (
        f"?{parsed.query}" if parsed.query else "")


def _connect(key: Any, timeout: float) -> Any:
    scheme, host, port = key
    if scheme == "http":
        return http.client.HTTPConnection(host, port, timeout=timeout)
    return http.client.HTTPSConnection(host, port, timeout=timeout)


def _reuse(connection: Any, timeout: float) -> None:
    """A pooled connection carries the timeout of whoever borrowed it last."""
    connection.timeout = timeout
    sock = getattr(connection, "sock", None)
    if sock is not None:
        try:
            sock.settimeout(timeout)
        except OSError:
            pass


def _is_open(connection: Any) -> bool:
    """Worth keeping: either it has no socket yet (never used) or its socket is still live."""
    sock = getattr(connection, "sock", None)
    if sock is None:
        return True
    try:
        return sock.fileno() != -1
    except OSError:
        return False


def _close(connection: Any) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - closing a broken socket must never raise at a caller
        pass


def _http_transport(body: bytes, headers: Dict[str, str], timeout: float, url: str = ENDPOINT,
                    max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """POST one request over a pooled connection. Redirects are never followed.

    A redirect would carry the bearer token to another origin, so a 3xx is an error here, not a
    hop — the same guarantee the previous opener gave, without a new TLS session per call.
    ``max_bytes`` is the caller's own ceiling on the reply: a feature that plans one small
    object should not be able to pull a megabyte because the client's default is higher.
    """
    key, path = _origin(url)
    for attempt in (0, 1):
        connection = _POOL.borrow(key, timeout)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read(max_bytes + 1)
        except JevError:
            _POOL.drop(connection)
            raise
        except (http.client.HTTPException, OSError):
            # A keep-alive socket the server closed while it was idle: the request never
            # reached it. One fresh connection; if that fails too, the caller retries.
            _POOL.drop(connection)
            if attempt:
                raise JevError("network") from None
            continue
        if len(raw) > max_bytes:
            _POOL.drop(connection)
            raise JevError("response_too_large")
        if status != 200:
            _POOL.drop(connection)
            code = {301: "http_301", 302: "http_302", 303: "http_303", 307: "http_307", 308: "http_308",
                    401: "auth_failed", 403: "auth_failed", 402: "credits_exhausted",
                    429: "rate_limited", 529: "overloaded"}.get(status, f"http_{status}")
            raise JevError(code)
        _POOL.release(key, connection)
        return raw
    raise JevError("network")


def _openrouter_transport(body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
    return _http_transport(body, headers, timeout, OPENROUTER_ENDPOINT)


def post(url: str, body: bytes, headers: Dict[str, str], timeout: float,
         max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """POST one request to ``url`` over the pooled connection.

    For the features that call a provider directly instead of asking Jev a question — `jev plan`
    is the one today. It used to build its own opener, which meant its own TLS session: fine for
    a once-per-task call, and a second handshake the pool already knows how to avoid. Raises
    ``JevError`` with a code; a caller maps that to its own error type.
    """
    return _http_transport(body, headers, timeout, url, max_bytes)


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
    # Where the shape is enforced. A caller that hand-builds a question dict used to reach the
    # wire with it; now it is refused here, before any request is made or any key resolved.
    questions = check_questions(questions)
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
