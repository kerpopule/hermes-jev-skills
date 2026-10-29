"""Opt-in, model-free wire-contract trace/replay. Not an execution receipt.

Inspired by Switch's typed choices and replay, implemented without its runtime.
Only caller-supplied public/synthetic fixtures belong here. Nothing is auto-logged.
"""
import hashlib
import json
from typing import Any, Mapping, Sequence

from . import client

SCHEMA = "jev-wire-trace-v1"
MAX_CASES = 4096


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record(cases: Sequence[Mapping[str, Any]]) -> dict:
    """Run actual validation, retaining only IDs, definition hashes and outcomes.

    No fixture bodies, labels, response content, credentials or state are persisted.
    A digest is an integrity binding, not proof of secrecy or authenticity.
    """
    if not isinstance(cases, (list, tuple)) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("trace requires a bounded non-empty fixture sequence")
    seen = set()
    rows = []
    for case in cases:
        name = case.get("id")
        if (not isinstance(name, str) or not name.isascii() or not name
                or len(name) > 80 or not all(c.isalnum() or c in "_-" for c in name)
                or name in seen):
            raise ValueError("trace requires unique short opaque fixture IDs")
        seen.add(name)
        questions = client.check_questions(case["questions"])
        wire = case["response"]
        if not isinstance(wire, str):
            raise ValueError("fixture response must be a JSON wire string")
        # Refuse oversized fixtures before canonicalizing them into a trace digest.
        raw = wire.encode("utf-8")
        if len(raw) > client.MAX_RESPONSE_BYTES:
            raise ValueError("fixture exceeds response byte bound")
        binding = fingerprint({"questions": questions, "response": wire})
        try:
            _, answers = client.validate_reply(raw, questions)
            outcome = {"status": "validated", "answers_sha256": fingerprint(answers)}
        except client.JevError as error:
            outcome = {"status": "refused", "error": error.code}
        rows.append({"id": name, "definition_sha256": binding, "outcome": outcome})
    return {"schema": SCHEMA, "rows": rows, "rows_sha256": fingerprint(rows)}


def replay(cases: Sequence[Mapping[str, Any]], trace: Mapping[str, Any]) -> dict:
    """Re-exercise current validator; refuse edited/reordered/missing definitions.

    Does not trust recorded success or replay any external action/transition.
    Contract behavior changes are explicit diffs, never automatic promotion.
    """
    if not isinstance(trace, Mapping) or trace.get("schema") != SCHEMA:
        raise ValueError("unsupported trace schema")
    old = trace.get("rows")
    if not isinstance(old, list) or trace.get("rows_sha256") != fingerprint(old):
        raise ValueError("trace integrity mismatch")
    current = record(cases)["rows"]
    if len(old) != len(current):
        raise ValueError("trace fixture count drift")
    changes = []
    for before, after in zip(old, current):
        if not isinstance(before, dict) or any(before.get(k) != after[k]
                                               for k in ("id", "definition_sha256")):
            raise ValueError("trace fixture definition drift")
        if before.get("outcome") != after["outcome"]:
            changes.append({"id": after["id"], "before": before.get("outcome"),
                            "after": after["outcome"]})
    return {"cases": len(current), "matches": len(current) - len(changes), "changes": changes,
            "execution_verified": False, "model_calls": 0}
