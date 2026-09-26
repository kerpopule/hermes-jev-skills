"""A decision policy, as data: the questions to ask Jev, and the rules that turn answers into an action.

"LLMs think, Jev decides, tools act" (@0xMorlex, "Jev Engineering", 2026-09-19). The decision
layer in that sentence is two things, and this module keeps them apart on purpose:

* **Jev supplies readings.** A noul is a probability, a choice is a distribution over closed
  options, a score is a position on a rubric. Nothing Jev returns is an action.
* **Code applies the policy.** Thresholds live in a JSON file a person can read, diff and argue
  with. The file is data only: no expressions are evaluated, and the loader refuses anything
  it cannot check.

A policy file::

    {"name": "gate-strict", "version": 1, "feature": "gate", "tuned_on": "jev-1.13.0",
     "questions": {"risky": {"type": "noul", "instructions": "...", "criteria": {...}}, ...},
     "rules": [{"then": "deny", "any": [["secrets_or_exfil", ">=", 0.85], ...]}, ...],
     "otherwise": "ask_human", "on_error": "no_opinion", "on_drift": "no_opinion",
     "uncertain_band": [0.3, 0.7], "promotion": {...}}

Rules are checked in order and the first match wins. A condition is ``[operand, op, value...]``
or ``{"all"|"any": [conditions]}``. Operands:

* ``q`` — a noul's probability of yes; ``q.unsure`` — True when it sits inside the band;
* ``q.choice``, ``q.confidence``, ``q.margin`` (top probability minus the runner-up) and
  ``q.p.<option>`` for a choice;
* ``q.norm`` (``score / (levels - 1)``, so every threshold is on 0..1, the scale the article's
  numbers use), ``q.score``, ``q.confidence`` and ``q.p.<level>`` for a score.

Ops: ``>=``, ``>``, ``<=``, ``<``, ``==``, ``!=``, ``in``, ``not_in``, ``between`` (inclusive).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import client

SHIPPED = Path(__file__).resolve().parent / "policies"
NUMERIC_OPS = (">=", ">", "<=", "<")
OPS = NUMERIC_OPS + ("==", "!=", "in", "not_in", "between")
DEFAULT_BAND = (0.30, 0.70)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# Keys a policy may carry. Anything else is refused: a misspelt "otherwize" must not silently
# leave the fallback at its default.
KNOWN_KEYS = {
    "name", "version", "feature", "tuned_on", "description", "questions", "rules", "otherwise",
    "on_error", "on_drift", "uncertain_band", "annotations", "promotion", "state_fields",
    "field_limits", "trusted_instruction_fields", "code_first", "precedence", "actions",
    "dynamic_choices", "_note", "source",
}


class PolicyError(ValueError):
    """A policy that cannot be trusted to mean what it says."""


# ── where policies live ──────────────────────────────────────────────────────

def hermes_root() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home.parent.parent if home.parent.name == "profiles" else home


def override_dir() -> Path:
    """Local overrides, shared by every profile. An override is logged under its own sha."""
    return hermes_root() / "jev" / "policies"


def shipped_names() -> List[str]:
    return sorted(path.stem for path in SHIPPED.glob("*.json"))


def locate(name_or_path: str) -> Path:
    """A file path if one is given, else a local override, else the policy this package ships."""
    raw = str(name_or_path)
    candidate = Path(raw).expanduser()
    if raw.endswith(".json") or os.sep in raw or "/" in raw:
        if candidate.is_file():
            return candidate
        raise PolicyError(f"no policy file at {raw}")
    if not _NAME.match(raw):
        raise PolicyError(f"{raw!r} is not a policy name (lowercase letters, digits, - _ .)")
    for folder in (override_dir(), SHIPPED):
        path = folder / f"{raw}.json"
        if path.is_file():
            return path
    raise PolicyError(f"no policy named {raw!r}; shipped: {', '.join(shipped_names())}")


def load(name_or_path: Any, *, choices: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Read, bind and lint one policy. Raises ``PolicyError`` naming every problem found."""
    if isinstance(name_or_path, Mapping):
        raw = copy.deepcopy(dict(name_or_path))
        origin = "inline"
    else:
        path = locate(str(name_or_path))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PolicyError(f"{path.name} could not be read as JSON: {error}") from None
        if not isinstance(raw, dict):
            raise PolicyError(f"{path.name} must hold one JSON object")
        raw.setdefault("name", path.stem)
        origin = "override" if path.parent == override_dir() else "shipped" if path.parent == SHIPPED else "file"
    if choices:
        raw = bind(raw, choices)
    problems = lint(raw)
    if problems:
        raise PolicyError(f"policy {raw.get('name', '?')!r}: " + "; ".join(problems))
    raw["_origin"] = origin
    raw["_sha"] = sha(raw)
    return raw


def sha(policy: Mapping[str, Any]) -> str:
    """A short hash of what the policy decides with, so a log row names the exact thresholds."""
    body = {key: value for key, value in policy.items() if not key.startswith("_")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()[:8]


def label(policy: Mapping[str, Any]) -> str:
    return f"{policy.get('name', 'inline')}@{policy.get('version', 0)}#{policy.get('_sha') or sha(policy)}"


def bind(policy: Mapping[str, Any], choices: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Fill a choice whose options are only known at run time (profiles, destinations).

    ``dynamic_choices`` names the question and whether ``none_fits`` is added. The options are
    the caller's; the rules can still refer to ``none_fits`` because the policy guarantees it.
    """
    out = copy.deepcopy(dict(policy))
    dynamic = out.get("dynamic_choices") or {}
    for name, options in choices.items():
        if name not in dynamic:
            raise PolicyError(f"question {name!r} does not take options at run time")
        if name not in (out.get("questions") or {}):
            raise PolicyError(f"dynamic choice {name!r} is not one of the questions")
        criteria = dict(options)
        spec = dynamic[name] if isinstance(dynamic[name], Mapping) else {}
        if spec.get("none_fits", True):
            criteria.setdefault("none_fits", spec.get("none_fits_means")
                                or "None of the other options fits this; do not guess")
        out["questions"][name] = {**out["questions"][name], "criteria": criteria}
    out["dynamic_choices"] = {key: value for key, value in dynamic.items() if key not in choices}
    return out


# ── reading answers ──────────────────────────────────────────────────────────

def margin(probabilities: Mapping[Any, float]) -> float:
    """Top probability minus the runner-up: how clearly the pick beat the next option."""
    values = sorted((float(v) for v in probabilities.values()), reverse=True)
    if not values:
        return 0.0
    return round(values[0] - (values[1] if len(values) > 1 else 0.0), 6)


def readings(questions: Mapping[str, Any], answers: Mapping[str, Any],
             band: Sequence[float] = DEFAULT_BAND) -> Dict[str, Dict[str, Any]]:
    """Every validated answer reduced to the numbers a rule can name. No text survives this.

    A reading says its ``kind`` (noul / choice / score); it is not a question and is never sent.
    """
    low, high = float(band[0]), float(band[1])
    out: Dict[str, Dict[str, Any]] = {}
    for name, question in questions.items():
        answer = answers[name]
        kind = question["type"]
        if kind == "noul":
            p = float(answer["noul"])
            out[name] = {"kind": "noul", "p": round(p, 6), "unsure": low <= p <= high}
        elif kind == "choice":
            probabilities = {str(k): float(v) for k, v in answer["probabilities"].items()}
            out[name] = {"kind": "choice", "choice": answer["choice"],
                         "confidence": round(float(answer["confidence"]), 6),
                         "margin": margin(probabilities),
                         "p": {k: round(v, 6) for k, v in probabilities.items()}}
        else:
            levels = len(question["criteria"])
            value = float(answer["score"])
            out[name] = {"kind": "score", "score": round(value, 6),
                         "norm": round(min(1.0, max(0.0, value / (levels - 1))), 6),
                         "confidence": round(float(answer.get("confidence", 1.0)), 6),
                         "p": {str(k): round(float(v), 6) for k, v in (answer.get("probabilities") or {}).items()}}
    return out


def operand_value(operand: str, values: Mapping[str, Mapping[str, Any]]) -> Any:
    """The number, label or flag an operand names. ``KeyError`` for anything unknown."""
    name, _, field = operand.partition(".")
    reading = values[name]
    kind = reading["kind"]
    if not field:
        if kind != "noul":
            raise KeyError(operand)
        return reading["p"]
    if field.startswith("p."):
        return reading["p"].get(field[2:], 0.0)
    if field == "unsure" and kind == "noul":
        return reading["unsure"]
    if field in ("choice", "confidence", "margin") and kind == "choice":
        return reading[field]
    if field in ("norm", "score", "confidence") and kind == "score":
        return reading[field]
    raise KeyError(operand)


def _compare(left: Any, op: str, args: Sequence[Any]) -> bool:
    if op == "between":
        return float(args[0]) <= float(left) <= float(args[1])
    right = args[0]
    if op == "in":
        return left in right
    if op == "not_in":
        return left not in right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    left_f, right_f = float(left), float(right)
    return {">=": left_f >= right_f, ">": left_f > right_f,
            "<=": left_f <= right_f, "<": left_f < right_f}[op]


def holds(condition: Any, values: Mapping[str, Mapping[str, Any]]) -> bool:
    if isinstance(condition, Mapping):
        if "all" in condition:
            return all(holds(item, values) for item in condition["all"])
        return any(holds(item, values) for item in condition["any"])
    operand, op, *args = condition
    return _compare(operand_value(operand, values), op, args)


def rule_holds(rule: Mapping[str, Any], values: Mapping[str, Mapping[str, Any]]) -> bool:
    if "all" in rule:
        return all(holds(item, values) for item in rule["all"])
    return any(holds(item, values) for item in rule["any"])


def apply(policy: Mapping[str, Any], values: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """First matching rule wins. Also reports every rule that matched, for ``--explain``."""
    fired: List[int] = [index for index, rule in enumerate(policy["rules"]) if rule_holds(rule, values)]
    action = policy["rules"][fired[0]]["then"] if fired else policy["otherwise"]
    notes = [str(item["add"]) for item in policy.get("annotations") or () if rule_holds(item, values)]
    unsure = sorted(name for name, reading in values.items() if reading.get("unsure"))
    return {"action": action, "matched_rule": fired[0] if fired else None, "fired_rules": fired,
            "annotations": notes, "unsure": unsure}


def explain(policy: Mapping[str, Any], values: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """One plain line per rule: what it would do and whether it matched."""
    lines = []
    for index, rule in enumerate(policy["rules"]):
        joiner = "all" if "all" in rule else "any"
        parts = []
        for item in rule[joiner]:
            if isinstance(item, Mapping):
                parts.append(("yes " if holds(item, values) else "no  ") + json.dumps(item))
            else:
                operand = item[0]
                parts.append(f"{'yes' if holds(item, values) else 'no '} {operand}={operand_value(operand, values)!r} "
                             f"{item[1]} {' '.join(json.dumps(a) for a in item[2:])}")
        lines.append(f"rule {index} -> {rule['then']} ({joiner}): {'MATCH' if rule_holds(rule, values) else 'no match'}")
        lines.extend("    " + part for part in parts)
    lines.append(f"otherwise -> {policy['otherwise']}")
    return lines


# ── lint ─────────────────────────────────────────────────────────────────────

def _operand_problem(operand: Any, questions: Mapping[str, Any]) -> Optional[str]:
    if not isinstance(operand, str) or not operand:
        return f"operand {operand!r} is not a string"
    name, _, field = operand.partition(".")
    question = questions.get(name)
    if not isinstance(question, Mapping):
        return f"operand {operand!r} names no question"
    kind = question.get("type")
    if field.startswith("p."):
        option = field[2:]
        if kind == "choice":
            if question.get("criteria") is not None and option not in question["criteria"]:
                return f"operand {operand!r}: {option!r} is not an option of {name}"
            return None
        if kind == "score":
            levels = len(question.get("criteria") or ())
            if not option.isdigit() or int(option) >= levels:
                return f"operand {operand!r}: level {option!r} is not 0..{levels - 1}"
            return None
        return f"operand {operand!r}: a noul has no .p.<option>"
    allowed = {"noul": {"", "unsure"}, "choice": {"choice", "confidence", "margin"},
               "score": {"norm", "score", "confidence"}}.get(kind, set())
    if field not in allowed:
        return f"operand {operand!r}: a {kind} offers {sorted(f or '(bare)' for f in allowed)}"
    return None


def _condition_problems(condition: Any, questions: Mapping[str, Any], where: str) -> List[str]:
    if isinstance(condition, Mapping):
        keys = set(condition) & {"all", "any"}
        if len(keys) != 1 or set(condition) - keys:
            return [f"{where}: a nested condition is {{\"all\": [...]}} or {{\"any\": [...]}}"]
        items = condition[keys.pop()]
        if not isinstance(items, list) or not items:
            return [f"{where}: an empty all/any"]
        return [p for i, item in enumerate(items) for p in _condition_problems(item, questions, f"{where}.{i}")]
    if not isinstance(condition, list) or len(condition) < 3:
        return [f"{where}: a condition is [operand, op, value...]"]
    operand, op, *args = condition
    problem = _operand_problem(operand, questions)
    if problem:
        return [f"{where}: {problem}"]
    if op not in OPS:
        return [f"{where}: unknown op {op!r}; use one of {', '.join(OPS)}"]
    name, _, field = operand.partition(".")
    kind = questions[name]["type"]
    categorical = kind == "choice" and field == "choice"
    boolean = field == "unsure"
    if op == "between":
        if len(args) != 2 or categorical or boolean:
            return [f"{where}: between takes two numbers"]
    elif len(args) != 1:
        return [f"{where}: {op} takes one value"]
    if categorical:
        if op in NUMERIC_OPS or op == "between":
            return [f"{where}: a choice label cannot be compared with {op}"]
        options = set(questions[name].get("criteria") or ())
        wanted = args[0] if op in ("in", "not_in") else [args[0]]
        if op in ("in", "not_in") and not isinstance(args[0], list):
            return [f"{where}: {op} takes a list of options"]
        unknown = [w for w in wanted if options and w not in options]
        if unknown:
            return [f"{where}: {unknown} are not options of {name}"]
        return []
    if boolean:
        if op not in ("==", "!=") or not isinstance(args[0], bool):
            return [f"{where}: .unsure is compared with == true or == false"]
        return []
    if op in ("in", "not_in"):
        return [f"{where}: {op} is for choice labels"]
    top = (len(questions[name].get("criteria") or ()) - 1) if field == "score" else 1.0
    for value in args:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return [f"{where}: {value!r} is not a number"]
        if not 0.0 <= float(value) <= top:
            return [f"{where}: {value!r} is outside 0..{top:g}; thresholds sit on the 0..1 scale "
                    f"(use .norm for a score)"]
    if op == "between" and float(args[0]) > float(args[1]):
        return [f"{where}: between {args[0]} and {args[1]} is empty"]
    return []


def _rule_problems(rule: Any, questions: Mapping[str, Any], where: str, needs_then: bool) -> List[str]:
    if not isinstance(rule, Mapping):
        return [f"{where}: a rule is an object"]
    joiners = [key for key in ("all", "any") if key in rule]
    if len(joiners) != 1:
        return [f"{where}: a rule has exactly one of \"all\" or \"any\""]
    problems = []
    if needs_then and (not isinstance(rule.get("then"), str) or not rule.get("then")):
        problems.append(f"{where}: no \"then\" action")
    items = rule[joiners[0]]
    if not isinstance(items, list) or not items:
        return problems + [f"{where}: an empty {joiners[0]}"]
    for index, item in enumerate(items):
        problems += _condition_problems(item, questions, f"{where}.{joiners[0]}.{index}")
    return problems


def lint(policy: Mapping[str, Any]) -> List[str]:
    """Every problem with a policy, so one run lists them all."""
    problems: List[str] = []
    unknown = sorted(set(policy) - KNOWN_KEYS - {k for k in policy if str(k).startswith("_")})
    if unknown:
        problems.append(f"unknown keys {unknown}")
    name = policy.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        problems.append("name must be lowercase letters, digits, - _ .")
    version = policy.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        problems.append("version must be a whole number from 1")
    questions = policy.get("questions")
    if not isinstance(questions, Mapping) or not questions:
        return problems + ["questions must be a non-empty object"]
    if len(questions) > 32:
        problems.append("more than 32 questions in one request; split the policy")
    dynamic = policy.get("dynamic_choices") or {}
    for qname, question in questions.items():
        if qname in dynamic:
            continue  # bound at run time; checked again after binding
        try:
            client.check_question(str(qname), question)
        except ValueError as error:
            problems.append(str(error))
    rules = policy.get("rules")
    if not isinstance(rules, list):
        problems.append("rules must be a list (it may be empty)")
        rules = []
    checkable = {q: v for q, v in questions.items() if isinstance(v, Mapping)}
    for index, rule in enumerate(rules):
        problems += _rule_problems(rule, checkable, f"rule {index}", True)
    for index, note in enumerate(policy.get("annotations") or ()):
        if not isinstance(note, Mapping) or not isinstance(note.get("add"), str):
            problems.append(f"annotation {index}: needs \"add\"")
            continue
        problems += _rule_problems({k: v for k, v in note.items() if k != "add"}, checkable,
                                   f"annotation {index}", False)
    for key in ("otherwise", "on_error"):
        if not isinstance(policy.get(key), str) or not policy.get(key):
            problems.append(f"{key} must name an action")
    if "on_drift" in policy and (not isinstance(policy["on_drift"], str) or not policy["on_drift"]):
        problems.append("on_drift must name an action")
    band = policy.get("uncertain_band", list(DEFAULT_BAND))
    if (not isinstance(band, (list, tuple)) or len(band) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in band)
            or not 0 <= band[0] < band[1] <= 1):
        problems.append("uncertain_band must be [low, high] with 0 <= low < high <= 1")
    produced = [rule.get("then") for rule in rules if isinstance(rule, Mapping)]
    declared = policy.get("actions")
    everything = set(filter(None, produced + [policy.get("otherwise"), policy.get("on_error"),
                                                policy.get("on_drift")]))
    if declared is not None:
        if not isinstance(declared, list) or not all(isinstance(a, str) for a in declared):
            problems.append("actions must be a list of names")
        elif everything - set(declared):
            problems.append(f"actions {sorted(everything - set(declared))} are produced but not declared")
    precedence = policy.get("precedence")
    if precedence is not None:
        order: List[str] = []
        for action in produced:
            if action not in order:
                order.append(action)
        if list(precedence) != order:
            problems.append(f"precedence {list(precedence)} does not match the rule order {order}; "
                            f"rules are checked in order and the first match wins")
    fields = policy.get("state_fields")
    if fields is not None and (not isinstance(fields, list) or not all(isinstance(f, str) for f in fields)):
        problems.append("state_fields must be a list of field names")
    tuned = policy.get("tuned_on")
    if tuned is not None and (not isinstance(tuned, str) or not version_of(tuned)):
        problems.append("tuned_on must name a Jev version, like jev-1.13.0")
    return problems


# ── drift ────────────────────────────────────────────────────────────────────

def version_of(model: Optional[str]) -> Tuple[int, ...]:
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", model or "")
    return tuple(int(part) for part in found.groups() if part is not None) if found else ()


def drifted(tuned_on: Optional[str], jev_model: Optional[str]) -> Optional[bool]:
    """True when the answering model is not the version the thresholds were tuned on.

    None when either side is unknown: an unnamed version is not evidence of a change. Versions
    compare on the parts both name, so ``jev-1.13-free`` (Zen's id) matches ``jev-1.13.0``.
    """
    tuned, seen = version_of(tuned_on), version_of(jev_model)
    if not tuned or not seen:
        return None
    width = min(len(tuned), len(seen))
    return tuned[:width] != seen[:width]


def action_set(policy: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for action in [rule["then"] for rule in policy["rules"]] + [policy["otherwise"], policy["on_error"],
                                                               policy.get("on_drift") or policy["on_error"]]:
        if action not in out:
            out.append(action)
    return out


def describe(names: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """One line per policy for `jev policies`: name, feature, actions, where it came from."""
    out = []
    for name in list(names) or shipped_names():
        try:
            loaded = load(name)
        except PolicyError as error:
            out.append({"name": name, "error": str(error)})
            continue
        out.append({"name": loaded["name"], "label": label(loaded), "feature": loaded.get("feature"),
                    "origin": loaded["_origin"], "questions": list(loaded["questions"]),
                    "actions": action_set(loaded), "tuned_on": loaded.get("tuned_on"),
                    "runtime_choices": sorted(loaded.get("dynamic_choices") or {})})
    return out
