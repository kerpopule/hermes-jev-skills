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
VENICE_ENDPOINT = "https://api.venice.ai/api/v1/decisions"
VENICE_MODEL = "jev-latest"
# Jev through OpenCode Zen. Same request, same answers, same `{"answers": {...}}` reply as
# TypeSafe — Zen serves the same model — with a free tier for the small model id below.
# TypeSafe is not accepting new signups, so this is the door a new install can actually open.
ZEN_ENDPOINT = "https://opencode.ai/zen/v1/systemone"
ZEN_MODEL = "jev-1.13-free"
MAX_RESPONSE_BYTES = 1_000_000
MAX_STATE_CHARS = 60_000
USER_AGENT = "hermes-jev-skills/0.1"

State = Union[str, Mapping[str, Any], Sequence[Any]]
Transport = Callable[[bytes, Dict[str, str], float], bytes]


class JevError(RuntimeError):
    """Anything that means "do not trust or use this Jev result"."""

    def __init__(self, code: str, detail: str = "", retry_after: Optional[float] = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        # Set only by a contradiction check (`client._invalid`): which invariant the reply broke.
        self.invariant: Optional[str] = None
        # Seconds the server asked us to wait (`retry-after-ms` / `retry-after`), when it said.
        # Only a patient caller (a batch job) waits that long; a live caller keeps its budget.
        self.retry_after = retry_after


# ── question builders ────────────────────────────────────────────────────────

def choice(instructions: str, criteria: Mapping[str, str]) -> Dict[str, Any]:
    if len(criteria) < 2:
        raise ValueError("a choice needs at least two options")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, levels: Sequence[str]) -> Dict[str, Any]:
    if len(levels) < 2:
        raise ValueError("a score needs at least two levels")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def noul(instructions: Any, criteria: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A yes/no question. ``criteria`` optionally says what a yes and a no mean.

    ``{"true": ..., "false": ...}`` is part of the API's Noul (the guardrails cookbook and
    LangChain's AutoMode both depend on it). Written criteria are always sent: LangChain's
    AutoModeMiddleware defaults them in ``__init__`` and then never sends them, which is the
    bug a caller of this builder cannot reproduce.
    """
    question: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        question["criteria"] = dict(criteria)
    return question


# ── the shape of a question ──────────────────────────────────────────────────
#
# The builders above only guard the callers that use them. A dict written by hand went
# straight to the wire, and `_check_answer` then read `question["criteria"]` and died on a
# KeyError, or read an answer against a question nobody had actually asked. The rules live
# here, once, and `ask` applies them to everything it is handed — so a feature cannot ship a
# question Jev cannot answer, whoever built it.

QUESTION_TYPES = ("choice", "score", "noul")
MIN_CRITERIA = 2
# The API's own limits (TypeSafe API reference, read 2026-09-26): at most 255 options in a
# Choice, and a Score of 2 to 10 levels. Refused here, because the API answers either with a
# 422 that every feature reads as "Jev had no opinion" — a silent fail-open on a caller bug.
MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10
NOUL_CRITERIA_KEYS = ("true", "false")


def _structured(value: Any) -> bool:
    """A string with words in it, or a non-empty object or array: what the API takes as text.

    ``instructions`` and every criterion may be an object or an array as well as a string,
    so a long question can keep its data in named fields and point at them in backticks.
    """
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (Mapping, list, tuple)) and bool(value)


def _identifier(text: Any) -> str:
    """Fold punctuation/case/underscores, keeping letters and digits in every script."""
    return re.sub(r"[\W_]+", " ", str(text).lower()).strip()


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
    * a choice needs a mapping of 2 to 255 options and a score a list of 2 to 10 levels,
      because one option is not a choice, the reply check reads the criteria, and the API
      refuses anything past its limits;
    * a noul's criteria, when written, are exactly ``{"true": ..., "false": ...}`` (either or
      both). Anything else would be sent and silently ignored, so it is refused.

    ``instructions`` and each criterion may be a string, or an object or array (the API's
    structured form). Only a string can "repeat its own name", so only a string is checked
    for that.
    """
    if not isinstance(question, Mapping):
        raise ValueError(f'question "{_shown(name)}" must be an object like {{"type": ..., "instructions": ...}}')
    kind = question.get("type")
    if kind not in QUESTION_TYPES:
        found = "has no type" if kind is None else f"has unknown type {_shown(json.dumps(kind, default=str))}"
        raise ValueError(f'question "{_shown(name)}" {found}; use one of {", ".join(QUESTION_TYPES)}')
    text = question.get("instructions")
    if not _structured(text):
        raise ValueError(f'question "{_shown(name)}" has no instructions: the text of the question, as a string '
                         f'(or a non-empty object or array holding it)')
    if isinstance(text, str) and _identifier(text) == _identifier(name):
        raise ValueError(f'question "{_shown(name)}" asks nothing: its instructions only repeat its own name. '
                         f'The id names the question, "instructions" asks it')
    text = text if isinstance(text, str) else json.loads(json.dumps(text, default=str))
    criteria = question.get("criteria")
    if kind == "noul":
        if criteria is None:
            return {"type": "noul", "instructions": text}
        if (not isinstance(criteria, Mapping) or not criteria
                or not set(map(str, criteria)) <= set(NOUL_CRITERIA_KEYS)):
            raise ValueError(f'question "{_shown(name)}" is a noul: its criteria, if any, must be '
                             f'{{"true": "what a yes means", "false": "what a no means"}}; anything else '
                             f'would be sent and never read')
        for side, meaning in criteria.items():
            if not _structured(meaning):
                raise ValueError(f'question "{_shown(name)}": the noul criterion "{side}" is empty')
        return {"type": "noul", "instructions": text,
                "criteria": {str(side): criteria[side] for side in criteria}}
    wanted, shape = ((Mapping, 'an object of 2 to 255 options, {"option": "what it means"}')
                     if kind == "choice" else (list, "a list of 2 to 10 levels, lowest first"))
    if criteria is None:
        raise ValueError(f'question "{_shown(name)}": criteria are required for a {kind}, as {shape}')
    if not isinstance(criteria, wanted) or len(criteria) < MIN_CRITERIA:
        raise ValueError(f'question "{_shown(name)}": criteria for a {kind} must be {shape}')
    if kind == "choice":
        if len(criteria) > MAX_CHOICE_OPTIONS:
            raise ValueError(f'question "{_shown(name)}": {len(criteria)} options; a choice takes at most '
                             f'{MAX_CHOICE_OPTIONS}. Pick a group first, then a member')
        return {"type": kind, "instructions": text, "criteria": dict(criteria)}
    if len(criteria) > MAX_SCORE_LEVELS:
        raise ValueError(f'question "{_shown(name)}": {len(criteria)} levels; a score takes at most '
                         f'{MAX_SCORE_LEVELS}')
    for position, level in enumerate(criteria):
        if not _structured(level):
            raise ValueError(f'question "{_shown(name)}": score level {position} is empty')
    return {"type": kind, "instructions": text, "criteria": list(criteria)}


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
            raise JevError(code, retry_after=retry_after_seconds(response))
        _POOL.release(key, connection)
        return raw
    raise JevError("network")


MAX_RETRY_AFTER = 60.0


def retry_after_seconds(response: Any) -> Optional[float]:
    """What the server asked for, in seconds: ``retry-after-ms`` first, then ``retry-after``.

    Only the numeric form of ``retry-after`` is read; an HTTP date needs a clock comparison
    and a wrong clock would turn it into a zero or an hour. Capped at a minute: a batch that
    is told to wait longer than that is better stopped and resumed.
    """
    try:
        header = getattr(response, "getheader", None)
        if header is None:
            return None
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            raw = header(name)
            if raw is None:
                continue
            value = float(str(raw).strip()) * scale
            if math.isfinite(value) and value >= 0:
                return min(value, MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return None
    return None


def _openrouter_transport(body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
    return _http_transport(body, headers, timeout, OPENROUTER_ENDPOINT)

def _venice_transport(body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
    return _http_transport(body, headers, timeout, VENICE_ENDPOINT)

def _custom_typesafe_endpoint(base: str) -> str:
    """Explicit compatible server: never send provider credentials to an override host.

    The server may mount the API under a path prefix (a gateway's ``/jev``), which becomes
    part of the endpoint: ``https://gw.example/jev`` -> ``https://gw.example/jev/v1/systemone``.
    Contributed in github.com/kerpopule/hermes-jev-skills/pull/24 by Timo Goetzken (@HearthCore).
    """
    try:
        parsed = urllib.parse.urlsplit(base)
        port = parsed.port
    except ValueError:
        raise JevError("invalid_endpoint") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1")) or
            not _PATH_PREFIX.fullmatch(parsed.path or "") or not parsed.netloc or port == 0):
        raise JevError("invalid_endpoint")
    return base.rstrip("/") + "/v1/systemone"


# A path prefix is plain segments: no spaces, controls, dot segments, backslashes or
# percent-escapes, so the endpoint that is validated is the endpoint that is requested.
_PATH_PREFIX = re.compile(r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._~!$&'()*+,;=:@-]+)*/?")

# The one credential an override endpoint can ever receive. It is its own variable on
# purpose: TYPESAFE_API_KEY in the environment is where most installs keep their real
# TypeSafe key (keystore.resolve reads it first), so forwarding that variable would send a
# provider key to whatever host TYPESAFE_BASE_URL names, on every existing install that has
# both set, without anyone opting in. A gateway that wants a bearer gets this one, set by
# the operator for that gateway and nothing else. Never read from a keychain or a file.
PROXY_KEY_ENV = "JEV_PROXY_API_KEY"


def _proxy_key() -> Optional[str]:
    return (os.environ.get(PROXY_KEY_ENV) or "").strip() or None


def _zen_transport(body: bytes, headers: Dict[str, str], timeout: float) -> bytes:
    """One POST to Zen's systemone path. No extra headers: Zen asks callers for none, so a
    request here is the TypeSafe one with a different URL and model id."""
    return _http_transport(body, headers, timeout, ZEN_ENDPOINT)


# Which model id and which door each provider speaks. Kept as lookups rather than an
# if/elif chain so a provider added to `keystore.PROVIDERS` is one entry in each, and a
# provider this file does not know (an older/newer kit) falls back to TypeSafe's own values.
# Both read the module's names at call time: a table built at import would freeze the
# transport functions, and patching `client._openrouter_transport` (or any other door) is
# how callers and the offline tests keep a request off the real network.
def _provider_model(via: str) -> str:
    return {"openrouter": OPENROUTER_MODEL, "venice": VENICE_MODEL,
            "zen": ZEN_MODEL}.get(via, DEFAULT_MODEL)


def _provider_transport(via: str) -> Transport:
    return {"openrouter": _openrouter_transport, "venice": _venice_transport,
            "zen": _zen_transport}.get(via, _http_transport)


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
    patient: bool = False,
) -> Dict[str, Any]:
    """Ask Jev every question against one state, in a single request.

    Returns ``{"answers": {...validated...}, "usage": {...}, "latency_ms": int,
    "jev_model": str|None, "input_tokens": int|None, "provider": str}``. ``jev_model`` is
    the exact version that answered (the reply's ``model``): ``jev-latest`` is an alias that
    can move, and thresholds tuned on one version are only known to hold on that version.
    Raises ``JevError`` for anything the caller should not act on. ``timeout`` is a
    total wall-clock budget across retries, not a per-attempt one.

    ``patient=True`` is for batch jobs: a 429/529 that names a ``retry-after`` is waited out
    (within ``timeout``) instead of retried after a quarter second. A live caller never passes
    it, so a slow provider costs a live feature its short budget and nothing more.
    """
    if not questions:
        raise ValueError("no questions")
    # Where the shape is enforced. A caller that hand-builds a question dict used to reach the
    # wire with it; now it is refused here, before any request is made or any key resolved.
    questions = check_questions(questions)
    base = os.environ.get("TYPESAFE_BASE_URL", "").strip()
    if base.rstrip("/") == "https://api.typesafe.ai":
        base = ""  # the official URL still uses the official credential flow
    via = provider or ("typesafe" if api_key or base else keystore.provider())
    if via not in keystore.PROVIDERS:
        via = "typesafe"
    endpoint = _custom_typesafe_endpoint(base) if base and via == "typesafe" else None
    # A compatible endpoint may be a local mock or an explicit HTTPS proxy. Never forward
    # either an explicit api_key or an automatically discovered provider key to it. The only
    # bearer it can get is JEV_PROXY_API_KEY, which an operator sets for that gateway alone.
    key = _proxy_key() if endpoint else api_key or keystore.resolve(via)
    if not key and not endpoint:
        raise JevError("no_key", "run `jev setup-key`")
    encoded_state = state if isinstance(state, str) else json.dumps(state, separators=(",", ":"), default=str)
    if len(encoded_state) > MAX_STATE_CHARS:
        raise JevError("state_too_large")
    default_model = _provider_model(via)
    body = json.dumps(
        {"state": state, "model": model or os.environ.get("TYPESAFE_MODEL") or default_model,
         "questions": {name: dict(q) for name, q in questions.items()}},
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if via == "openrouter":
        # OpenRouter asks callers to identify themselves; neither header carries anything
        # about the person or the decision.
        headers["HTTP-Referer"] = "https://github.com/kerpopule/hermes-jev-skills"
        headers["X-Title"] = "Hermes Jev Skills"
    send = transport or ((lambda body, headers, timeout: _http_transport(body, headers, timeout, endpoint))
                         if endpoint else _provider_transport(via))

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
            left = max(0.0, timeout - (time.monotonic() - started) - 0.1)
            wait = min(0.25 * attempt, left)
            if patient and error.retry_after is not None:
                if error.retry_after > left:
                    raise  # told to wait past the budget: stop now rather than sleep and fail
                wait = error.retry_after
            time.sleep(wait)

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JevError("malformed", "reply is not JSON") from None
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise JevError("malformed", "reply has no answers")
    checked = {name: _check_answer(name, question, answers.get(name)) for name, question in questions.items()}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    model_seen = payload.get("model")
    tokens = usage.get("input_tokens")
    return {"answers": checked, "usage": usage, "latency_ms": int((time.monotonic() - started) * 1000),
            "jev_model": model_seen if isinstance(model_seen, str) and model_seen else None,
            "input_tokens": int(tokens) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool)
            and math.isfinite(float(tokens)) and tokens >= 0 else None,
            "provider": "custom" if endpoint else via}


def verify_key(api_key: str, timeout: float = 10.0, provider: str = "typesafe") -> bool:
    """One tiny synthetic call. True means the key is accepted by that provider."""
    if provider == "typesafe" and os.environ.get("TYPESAFE_BASE_URL", "").strip().rstrip("/") not in ("", "https://api.typesafe.ai"):
        return False  # a compatible server cannot authenticate a TypeSafe credential
    try:
        ask("The build finished and all tests passed.",
            {"ok": noul("The text reports a successful outcome")},
            api_key=api_key, provider=provider, timeout=timeout)
        return True
    except JevError:
        return False


def batches(items: Sequence[Any], size: int) -> List[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
