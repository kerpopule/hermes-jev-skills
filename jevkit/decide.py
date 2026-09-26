"""One engine for every policy decision: ask Jev the policy's questions, apply its rules, log it.

This is the article's Steps 3, 4 and 6 as a single building block (@0xMorlex, "Jev
Engineering: How to Make AI Agent Loops 200x Faster in 10 Steps", 2026-09-19): one yes/no
question returns a probability; several questions about one state go in one request; the
surrounding code, not the model, turns the numbers into what happens next.

``decide`` never raises for anything Jev or the network does. Every way it can fail — no key,
a timeout, a malformed reply, a state that looks like it holds a secret, the daily budget
spent — returns the policy's ``on_error`` action with ``fallback_used: true``, and that action
is by construction what the caller would have done without Jev. A reply from a Jev version
other than the one the thresholds were tuned on is marked ``drift``; a live caller then gets
``on_drift`` (a shadow caller still gets the rule's answer, flagged, so the log keeps
measuring).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional

from . import client, ledger, limits, policy as policies, privacy

DEFAULT_FIELD_CHARS = 1_500
MAX_DEPTH = 4
MAX_ADHOC = 8
MODES = ("live", "shadow")


def _redact_value(value: Any, limit: int, depth: int = 0) -> Any:
    if isinstance(value, str):
        return privacy.redact(value, limit)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= MAX_DEPTH:
        return privacy.redact(json.dumps(value, default=str), limit)
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v, limit, depth + 1) for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, limit, depth + 1) for v in list(value)[:64]]
    return privacy.redact(str(value), limit)


def prepare_state(state: Any, rules: Mapping[str, Any]) -> Any:
    """Only the fields the policy names, each redacted and capped. A long state makes Jev worse.

    Jev 1.13 is measurably worse when the state is full of text the question does not need
    (TypeSafe's jaggedness page), and every field sent is a field that can leak. So a policy
    that lists ``state_fields`` gets exactly those; ``field_limits`` caps each one.
    """
    limits_by_field = rules.get("field_limits") or {}
    if isinstance(state, Mapping):
        wanted = rules.get("state_fields")
        picked = {key: state[key] for key in (wanted or state) if key in state}
        return {key: _redact_value(value, int(limits_by_field.get(key, DEFAULT_FIELD_CHARS)))
                for key, value in picked.items()}
    return _redact_value(state, int(limits_by_field.get("_", 4_000)))


def state_digest(state: Any) -> str:
    """sha256 of the redacted state, for joining log rows to outcomes without keeping the text."""
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _with_context(question: Mapping[str, Any], context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Put trusted operator text in the question, never in the state.

    The state is data the policy judges; text in it that argues for its own answer can move
    the result (TypeSafe, Jev 1.13 jaggedness). An operator's own rules are not that, so they
    ride in ``instructions`` as a named field the question points at.
    """
    if not context:
        return dict(question)
    text = question["instructions"]
    base = dict(text) if isinstance(text, Mapping) else {"question": text}
    return {**question, "instructions": {**base, **{str(k): v for k, v in context.items()}}}


def _fallback(rules: Mapping[str, Any], *, error: str, mode: str, feature: str, started_label: str,
              status: str = "error", sent: bool = False) -> Dict[str, Any]:
    return {"action": rules.get("on_error", "fallback"), "rule_action": None, "matched_rule": None,
            "fired_rules": [], "annotations": [], "unsure": [], "answers": {}, "jev_model": None,
            "drift": None, "latency_ms": None, "input_tokens": None, "cost_usd": 0.0, "list_usd": 0.0,
            "policy": started_label, "feature": feature, "mode": mode, "error": error, "status": status,
            "fallback_used": True, "sent_to_jev": sent}


def decide(
    state: Any,
    policy: Any,
    *,
    mode: str = "live",
    feature: Optional[str] = None,
    timeout: float = 4.0,
    retries: int = 1,
    transport: Optional[client.Transport] = None,
    choices: Optional[Mapping[str, Mapping[str, Any]]] = None,
    trusted_context: Optional[Mapping[str, Any]] = None,
    patient: bool = False,
    record: bool = True,
    use_limits: bool = True,
    llm_avoided_est: Optional[int] = None,
    queue_dropped: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one policy against one state. Returns a decision dict; never raises for Jev's sake.

    A bad *policy* is a caller bug and does raise ``PolicyError`` — silently falling back on a
    policy nobody can read would hide the bug behind the fail-open path.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    rules = policies.load(policy, choices=choices) if not (isinstance(policy, Mapping) and policy.get("_sha")) \
        else dict(policy)
    if rules.get("dynamic_choices"):
        raise policies.PolicyError(f"policy {rules['name']!r} needs its options at run time: "
                                   f"{sorted(rules['dynamic_choices'])}")
    label = policies.label(rules)
    feature = feature or str(rules.get("feature") or rules["name"])
    shadow = mode == "shadow"
    row_extra = {"llm_avoided_est": llm_avoided_est, "queue_dropped": queue_dropped}

    def finish(result: Dict[str, Any]) -> Dict[str, Any]:
        if record:
            ledger.append({**result, "n_questions": len(rules["questions"]), "shadow": shadow,
                           "provider": result.get("provider"), **row_extra})
        return result

    raw_probe = state if isinstance(state, str) else json.dumps(
        {k: state[k] for k in (rules.get("state_fields") or state) if k in state}
        if isinstance(state, Mapping) else state, default=str)
    if privacy.is_sensitive(raw_probe):
        # Never sent. The caller gets exactly what it would have done without Jev.
        return finish(_fallback(rules, error="sensitive_not_sent", mode=mode, feature=feature,
                                started_label=label, status="skipped"))
    prepared = prepare_state(state, rules)
    digest = state_digest(prepared)

    if use_limits:
        allowed, reason = limits.admit(shadow=shadow)
        if not allowed:
            out = _fallback(rules, error=reason, mode=mode, feature=feature, started_label=label,
                            status="skipped")
            out["state_sha256"] = digest
            return finish(out)

    questions = {name: _with_context(question, trusted_context) for name, question in rules["questions"].items()}
    try:
        reply = client.ask(prepared, questions, timeout=timeout, retries=retries, transport=transport,
                           patient=patient)
    except client.JevError as error:
        out = _fallback(rules, error=error.code, mode=mode, feature=feature, started_label=label,
                        sent=error.code not in ("no_key", "state_too_large", "invalid_endpoint"))
        out["state_sha256"] = digest
        return finish(out)
    except ValueError as error:  # a question the client refused: a policy bug, surfaced loudly
        raise policies.PolicyError(str(error)) from None

    band = rules.get("uncertain_band") or policies.DEFAULT_BAND
    values = policies.readings(rules["questions"], reply["answers"], band)
    applied = policies.apply(rules, values)
    drift = policies.drifted(rules.get("tuned_on"), reply.get("jev_model"))
    action = applied["action"]
    fallback_used = False
    if drift and not shadow:
        action = rules.get("on_drift") or rules["on_error"]
        fallback_used = True
    spend = ledger.cost(reply.get("input_tokens"), reply.get("provider"), reply.get("jev_model"))
    if use_limits:
        limits.charge(spend["cost_usd"])
    result = {
        "action": action, "rule_action": applied["action"], "matched_rule": applied["matched_rule"],
        "fired_rules": applied["fired_rules"], "annotations": applied["annotations"],
        "unsure": applied["unsure"], "answers": values, "jev_model": reply.get("jev_model"),
        "drift": drift, "latency_ms": reply.get("latency_ms"), "input_tokens": reply.get("input_tokens"),
        **spend, "policy": label, "feature": feature, "mode": mode, "error": "drift" if fallback_used else None,
        "status": "ok", "fallback_used": fallback_used, "sent_to_jev": True, "provider": reply.get("provider"),
        "state_sha256": digest,
    }
    return finish(result)


def rescore(policy: Any, answers: Mapping[str, Any], *, choices: Optional[Mapping[str, Mapping[str, Any]]] = None,
            jev_model: Optional[str] = None, mode: str = "live") -> Dict[str, Any]:
    """Apply a policy to answers already recorded — new thresholds on old readings, for free.

    ``answers`` may be raw validated answers (as ``client.ask`` returns them) or the reduced
    readings a decision logged. Nothing is sent anywhere.
    """
    rules = policies.load(policy, choices=choices) if not (isinstance(policy, Mapping) and policy.get("_sha")) \
        else dict(policy)
    band = rules.get("uncertain_band") or policies.DEFAULT_BAND
    low, high = band
    values: Dict[str, Dict[str, Any]] = {}
    for name, question in rules["questions"].items():
        if name not in answers:
            raise ValueError(f"no recorded answer for {name!r}")
        answer = answers[name]
        if isinstance(answer, Mapping) and ("noul" in answer or "probabilities" in answer):
            # A reply off the wire: validated exactly as a live one would be, then reduced.
            checked = client._check_answer(name, client.check_question(name, question), dict(answer))
            values[name] = policies.readings({name: question}, {name: checked}, band)[name]
        else:
            reading = dict(answer)
            if reading.get("kind") == "noul":
                reading["unsure"] = low <= float(reading["p"]) <= high
            values[name] = reading
    applied = policies.apply(rules, values)
    drift = policies.drifted(rules.get("tuned_on"), jev_model)
    action = applied["action"]
    if drift and mode == "live":
        action = rules.get("on_drift") or rules["on_error"]
    return {"action": action, "rule_action": applied["action"], "matched_rule": applied["matched_rule"],
            "fired_rules": applied["fired_rules"], "annotations": applied["annotations"],
            "unsure": applied["unsure"], "answers": values, "drift": drift, "policy": policies.label(rules)}


def adhoc(state: Any, questions: Mapping[str, Any], *, timeout: float = 4.0,
          transport: Optional[client.Transport] = None, band: Any = policies.DEFAULT_BAND,
          record: bool = True, use_limits: bool = True) -> Dict[str, Any]:
    """Up to eight yes/no questions about one state, in one request, with the default band.

    Each question is a string (the instructions) or ``{"instructions", "criteria"}``. The
    verdict per question is ``yes`` above the band, ``no`` below it and ``unsure`` inside it —
    TypeSafe's own advice for a Noul between 0.30 and 0.70 is to send it to a person rather
    than ask again, because repeats barely move it (self-consistency cookbook).
    """
    if not isinstance(questions, Mapping) or not questions:
        raise ValueError("give at least one question")
    if len(questions) > MAX_ADHOC:
        raise ValueError(f"at most {MAX_ADHOC} ad-hoc questions; write a policy for more")
    built = {}
    for name, spec in questions.items():
        if isinstance(spec, str):
            built[str(name)] = client.noul(spec)
        elif isinstance(spec, Mapping):
            built[str(name)] = client.noul(spec.get("instructions"), spec.get("criteria"))
        else:
            raise ValueError(f"question {name!r} must be text or {{instructions, criteria}}")
    inline = {"name": "adhoc", "version": 1, "feature": "adhoc", "questions": built, "rules": [],
              "otherwise": "answered", "on_error": "no_answer", "uncertain_band": list(band)}
    result = decide(state, inline, timeout=timeout, transport=transport, record=record, use_limits=use_limits)
    low, high = band
    result["verdicts"] = {name: ("unsure" if low <= reading["p"] <= high else "yes" if reading["p"] > high else "no")
                          for name, reading in result.get("answers", {}).items()}
    return result
