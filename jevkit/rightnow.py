"""Explicit, opt-in RightNow public-key adapter. Never selected by Jev routing.

Targets /v1/decisions, NOT the earlier rnsk_ API. No SDK, retries, stored
answers or fallback provider. Local protocol tests do not qualify model quality.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

from . import client

ENDPOINT = "https://api.rightnowai.co/v1/decisions"
MODEL = "rightnow-decision"
MAX_BODY = 3_665_920
MAX_FILES = 2_621_440


def _encode(value: Any) -> bytes:
    try:
        return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError):
        raise client.JevError("invalid_request", "not finite JSON") from None


def request(state: Any, questions: Mapping[str, Any]) -> dict:
    """Translate Jev rubrics without discarding descriptions or changing labels.

    More than 10 options, >4 score levels, box and structured instructions
    are deliberately refused, not silently truncated. Media uses prepared
    inline state; caller must first inspect and validate its original bytes.
    """
    checked = client.check_questions(questions)
    if not 1 <= len(checked) <= 32:
        raise client.JevError("unsupported_questions", "RightNow allows 1..32")
    if not isinstance(state, (str, dict, list)):
        raise client.JevError("invalid_request", "state must be text, object or array")
    wire = {}
    for name, q in checked.items():
        kind = q["type"]
        if kind == "box":
            raise client.JevError("unsupported_question", "box is not a RightNow public question")
        instructions = q["instructions"]
        if not isinstance(instructions, str):
            raise client.JevError("unsupported_question", "use explicit text instructions")
        row = {"type": kind, "instructions": instructions}
        if kind == "choice":
            if len(q["criteria"]) > 10:
                raise client.JevError("unsupported_question", "public adapter allows at most 10 options")
            row["options"] = list(q["criteria"])
            row["instructions"] += "\nOption definitions (data): " + _encode(q["criteria"]).decode()
        elif kind == "score":
            if len(q["criteria"]) > 4:
                raise client.JevError("unsupported_question", "public adapter allows at most 4 levels")
            row["levels"] = q["criteria"]
        elif q.get("criteria") is not None:
            row["instructions"] += "\nYes/no definitions (data): " + _encode(q["criteria"]).decode()
        wire[name] = row
    out = {"model": MODEL, "state": state, "questions": wire}
    if len(_encode(out)) > MAX_BODY:
        raise client.JevError("request_too_large")
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise client.JevError("redirect_refused")


def _send(body: bytes, headers: dict, timeout: float) -> bytes:
    req = urllib.request.Request(ENDPOINT, data=body, headers=headers, method="POST")
    try:
        # No redirect can forward the API key to another origin.
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(client.MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        # Never echo a vendor body, URL or exception string: it can contain inputs.
        code = {401: "auth", 402: "credits", 429: "rate_limit", 503: "unavailable",
                504: "timeout"}.get(error.code, "http_error")
        raise client.JevError(code, "RightNow HTTP " + str(error.code)) from None
    except (TimeoutError, OSError):
        raise client.JevError("transport", "RightNow request failed") from None
    if len(raw) > client.MAX_RESPONSE_BYTES:
        raise client.JevError("malformed", "reply too large")
    return raw


def validate(raw: bytes, questions: Mapping[str, Any]) -> dict:
    """Use existing strict Jev answer validation, plus abstention refusal.

    Public /decisions returns Jev-style noul/choice/score. Do not guess a
    legacy boolean mapping or manufacture probabilities from confidence.
    """
    try:
        if not isinstance(raw, bytes) or len(raw) > client.MAX_RESPONSE_BYTES:
            raise ValueError()
        payload = json.loads(raw, object_pairs_hook=client._unique_object)
        answers = payload["answers"]
        if not isinstance(answers, dict):
            raise ValueError()
    except (ValueError, KeyError, TypeError, RecursionError):
        raise client.JevError("malformed", "invalid RightNow envelope") from None
    for answer in answers.values():
        if isinstance(answer, dict) and ("error" in answer or answer.get("abstained") is True):
            raise client.JevError("question_refused", "do not act on partial or abstained answers")
    _, checked = client.validate_reply(raw, questions)
    for name, answer in checked.items():
        if answer["type"] == "choice":
            mass = answer["probabilities"][answer["choice"]]
            if abs(mass - answer["confidence"]) > 0.011:
                raise client.JevError("invalid_response", "choice confidence contradicts distribution")
        if answer["type"] == "score" and not answer["spread_reported"]:
            raise client.JevError("invalid_response", "RightNow score requires a distribution")
    model = payload.get("model")
    if not isinstance(model, str) or not model.startswith(MODEL):
        raise client.JevError("invalid_response", "missing or unexpected RightNow model")
    usage = payload.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        raise client.JevError("invalid_response", "missing charge; stop evaluation")
    try:
        finite = math.isfinite(cost)
    except OverflowError:
        finite = False
    if not finite or cost < 0:
        raise client.JevError("invalid_response", "invalid charge; stop evaluation")
    return {"answers": checked, "usage": usage, "provider": "rightnow",
            "jev_model": model, "input_tokens": usage.get("input_tokens"),
            "calibration": "unqualified", "request_id": payload.get("id")}


def ask(state: Any, questions: Mapping[str, Any], *, api_key: Optional[str] = None,
        timeout: float = 4.0, authorized: bool = False, transport=None) -> dict:
    """Explicit evaluation only. authorized means caller checked spend AND data scope.

    No retries, no idempotency retention, no key persistence, no automatic
    Jev fallback. transport is an offline test seam, never a quality claim.
    """
    body = _encode(request(state, questions))
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 130:
        raise client.JevError("invalid_request", "timeout must be 0..130 seconds")
    if not authorized:
        raise client.JevError("authorization_required", "check spending and data scope first")
    key = api_key or os.environ.get("RIGHTNOW_API_KEY", "")
    if not isinstance(key, str) or not key.startswith("rp_") or not 20 <= len(key) <= 512 or any(c.isspace() for c in key):
        raise client.JevError("auth", "requires a public API-page key from a secret store")
    started = time.monotonic()
    raw = (transport or _send)(body, {"Content-Type": "application/json", "Accept": "application/json",
                                   "Authorization": "Bearer " + key}, timeout)
    if time.monotonic() - started > timeout:
        raise client.JevError("timeout", "late answer discarded")
    out = validate(raw, questions)
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    return out
