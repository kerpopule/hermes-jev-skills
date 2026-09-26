"""Many states, one policy: JSONL in, decisions out (the article's Step 8, and every backtest).

A backtest that runs different code from the live feature measures the wrong thing. So every
offline replay here goes through ``decide`` — the same function, the same policy file, the
same redaction — and a live shadow later reads the same numbers.

* **Input**: one JSON object per line: ``{"id": ..., "state": {...}}`` plus any label fields a
  report will need (``truth``, ``current``, ``baseline``, ``expect``, ...). Labels are copied
  to the output; the state is not (it is history, and it already lives in the input file).
* **Facts**: a row's ``facts`` object is handed to the policy's ``pre_rules`` and ``fact.*``
  operands, exactly as a live caller hands them in. A row a pre-rule decides is never sent;
  ``--jev-only`` (``code_first=False``) skips the pre-rules to measure Jev alone.
* **Rescore**: a row that already carries ``answers`` (a recorded reply, or the readings a
  decision logged) is scored locally with ``--rescore`` — new thresholds on old readings,
  nothing sent, nothing paid. A row a pre-rule decides needs no answers.
* **Paid runs** print their estimate and wait for ``--yes``. A run resumes where it stopped:
  ids already in the output file are skipped.
* ``retry-after`` is honoured (``patient=True``), and the fleet limiter applies.
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import client, decide as engine, ledger, policy as policies

CHARS_PER_TOKEN = 3.5
KEEP_FIELDS = ("action", "rule_action", "matched_rule", "matched_pre_rule", "source", "fired_rules",
               "annotations", "unsure", "answers",
               "jev_model", "drift", "latency_ms", "input_tokens", "cost_usd", "list_usd", "policy", "error",
               "status", "fallback_used", "state_sha256")


def read_rows(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"line {number} is not JSON: {error}") from None
            if not isinstance(row, dict) or "id" not in row:
                raise ValueError(f"line {number} needs an object with an \"id\"")
            rows.append(row)
    ids = [str(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("ids must be unique: a resumed run skips an id it has already written")
    return rows


def estimate(rows: Iterable[Mapping[str, Any]], policy: Mapping[str, Any], code_first: bool = True) -> Dict[str, Any]:
    """A rough upper estimate: characters of prepared state plus questions, at ~3.5 per token.

    Rows a pre-rule decides from their facts cost nothing and are counted apart.
    """
    questions = len(json.dumps(policy["questions"]))
    count = tokens = by_code = 0
    for row in rows:
        count += 1
        if code_first and policies.pre_decide(policy, row.get("facts")) is not None:
            by_code += 1
            continue
        prepared = engine.prepare_state(row.get("state"), policy)
        tokens += int((len(json.dumps(prepared, default=str)) + questions) / CHARS_PER_TOKEN) + 60
    return {"rows": count, "decided_by_code": by_code, "est_input_tokens": tokens,
            "est_usd": round(tokens * ledger.USD_PER_INPUT_TOKEN, 4)}


def _done_ids(out_path: Path) -> set:
    done = set()
    try:
        with open(out_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    done.add(str(json.loads(line)["id"]))
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return done


def run(policy: Any, rows: List[Mapping[str, Any]], out_path: Path, *, yes: bool = False,
        rescore: bool = False, workers: int = 4, timeout: float = 10.0,
        transport: Optional[client.Transport] = None,
        choices: Optional[Mapping[str, Mapping[str, Any]]] = None, feature: Optional[str] = None,
        limit: int = 0, code_first: bool = True) -> Dict[str, Any]:
    """Decide every row not already in ``out_path``. Returns a summary; writes one line per row."""
    rules = policies.load(policy, choices=choices)
    done = _done_ids(out_path)
    todo = [row for row in rows if str(row["id"]) not in done]
    if limit:
        todo = todo[:limit]
    summary: Dict[str, Any] = {"policy": policies.label(rules), "rows": len(rows), "already_done": len(done),
                               "to_run": len(todo), "out": str(out_path), "code_first": code_first}
    if rescore:
        missing = [str(row["id"]) for row in todo if not isinstance(row.get("answers"), Mapping)
                   and not (code_first and policies.pre_decide(rules, row.get("facts")) is not None)]
        if missing:
            raise ValueError(f"--rescore needs recorded answers on every row; {len(missing)} have none "
                             f"(first: {missing[0]})")
    else:
        summary["estimate"] = estimate(todo, rules, code_first=code_first)
        if not yes and transport is None and summary["estimate"]["decided_by_code"] < len(todo):
            summary["status"] = "needs_yes"
            return summary

    lock = threading.Lock()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    actions: Dict[str, int] = {}
    errors = 0

    def one(row: Mapping[str, Any]) -> None:
        nonlocal errors
        if rescore:
            decision = engine.rescore(rules, row.get("answers") or {}, jev_model=row.get("jev_model"), mode="shadow",
                                      facts=row.get("facts"), code_first=code_first)
            decision.setdefault("status", "rescored")
        else:
            decision = engine.decide(row.get("state"), rules, mode="shadow", feature=feature, timeout=timeout,
                                     transport=transport, patient=True, retries=4, facts=row.get("facts"),
                                     code_first=code_first)
        kept = {key: decision.get(key) for key in KEEP_FIELDS if key in decision}
        labels = {key: value for key, value in row.items() if key not in ("state", "answers")}
        line = json.dumps({**labels, "decision": kept}, separators=(",", ":"), default=str) + "\n"
        with lock:
            os.write(fd, line.encode("utf-8"))
            actions[str(decision.get("action"))] = actions.get(str(decision.get("action")), 0) + 1
            if decision.get("status") == "error":
                errors += 1

    try:
        if workers <= 1 or rescore:
            for row in todo:
                one(row)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(one, todo))
    finally:
        os.close(fd)
    summary.update({"status": "done", "actions": dict(sorted(actions.items())), "errors": errors})
    return summary
