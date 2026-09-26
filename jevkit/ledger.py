"""What every policy decision cost and how long it took: one JSONL row per call, and a report.

The article's claim is economic — stop waking the most expensive brain for every small
decision — so the claim needs a meter. Each row holds numbers, ids and hashes only: never the
state, never a command, never a question's text.

Row fields: ``ts``, ``profile``, ``feature``, ``mode``, ``policy`` (name@version#sha),
``jev_model``, ``provider``, ``n_questions``, ``input_tokens``, ``cost_usd`` (billed),
``list_usd`` (what the same tokens cost at list price, so a free tier still shows its size),
``latency_ms``, ``status`` (ok / error / skipped), ``error``, ``action``, ``fallback_used``,
``shadow``, ``drift``, ``queue_dropped`` and ``llm_avoided_est`` — an *estimate*, labelled
as one, of the frontier-model tokens a caller said this decision replaces.

``JEV_LEDGER=off`` turns writing off; ``JEV_LEDGER_PATH`` moves the file.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

# TypeSafe list price (Models page, read 2026-09-26): $0.042 per million input tokens,
# output free. The same figure mailbox.py and triage.py already use for their estimates.
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000
FREE_MODELS = ("jev-1.13-free",)
FIELDS = ("ts", "profile", "feature", "mode", "policy", "jev_model", "provider", "n_questions",
          "input_tokens", "cost_usd", "list_usd", "latency_ms", "status", "error", "action",
          "fallback_used", "shadow", "drift", "queue_dropped", "llm_avoided_est")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def profile() -> str:
    home = hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def path() -> Path:
    override = os.environ.get("JEV_LEDGER_PATH", "").strip()
    return Path(override).expanduser() if override else hermes_home() / "logs" / "jev-ledger.jsonl"


def enabled() -> bool:
    return os.environ.get("JEV_LEDGER", "").strip().lower() not in ("off", "0", "false", "no")


def cost(input_tokens: Optional[int], provider: Optional[str] = None,
         jev_model: Optional[str] = None) -> Dict[str, float]:
    """Billed dollars and list-price dollars for one call. A free tier bills 0 at the same size."""
    tokens = int(input_tokens or 0)
    listed = round(tokens * USD_PER_INPUT_TOKEN, 8)
    free = (jev_model or "") in FREE_MODELS or (provider == "zen" and (jev_model or "").endswith("-free"))
    return {"cost_usd": 0.0 if free else listed, "list_usd": listed}


def append(row: Mapping[str, Any]) -> None:
    """Write one row, 0600. Never raises: a meter that breaks the thing it meters is worse than none."""
    if not enabled():
        return
    entry = {"ts": round(time.time(), 3), "profile": profile()}
    entry.update({key: row[key] for key in FIELDS if key in row and key not in ("ts",)})
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass


def read(source: Optional[Path] = None, since: Optional[float] = None) -> List[Dict[str, Any]]:
    target = source or path()
    rows: List[Dict[str, Any]] = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and (since is None or float(row.get("ts") or 0) >= since):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered))) - 1))
    return ordered[index]


def summarize(rows: Iterable[Mapping[str, Any]], by_day: bool = True) -> Dict[str, Any]:
    """Per feature (and per day): calls, latency percentiles, error rate, dollars, drift, LLM avoided."""
    groups: Dict[Any, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(float(row.get("ts") or 0))) if by_day else "all"
        groups[(str(row.get("feature") or "?"), day)].append(row)
    out = []
    for (feature, day), items in sorted(groups.items()):
        sent = [r for r in items if r.get("status") in ("ok", "error")]
        latencies = [float(r["latency_ms"]) for r in sent if isinstance(r.get("latency_ms"), (int, float))]
        errors = sum(1 for r in sent if r.get("status") == "error")
        out.append({
            "feature": feature, "day": day, "calls": len(items), "sent": len(sent),
            "skipped": sum(1 for r in items if r.get("status") == "skipped"),
            "shadow": sum(1 for r in items if r.get("shadow")),
            "p50_ms": _percentile(latencies, 0.50), "p90_ms": _percentile(latencies, 0.90),
            "p99_ms": _percentile(latencies, 0.99),
            "error_rate": round(errors / len(sent), 4) if sent else None,
            "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in items), 6),
            "list_usd": round(sum(float(r.get("list_usd") or 0) for r in items), 6),
            "input_tokens": sum(int(r.get("input_tokens") or 0) for r in items),
            "drift": sum(1 for r in items if r.get("drift")),
            "fallback_used": sum(1 for r in items if r.get("fallback_used")),
            "queue_dropped": max([int(r.get("queue_dropped") or 0) for r in items] or [0]),
            "llm_avoided_est_tokens": sum(int(r.get("llm_avoided_est") or 0) for r in items),
            "actions": dict(sorted(_count(r.get("action") for r in items).items())),
        })
    totals = {"calls": sum(g["calls"] for g in out), "cost_usd": round(sum(g["cost_usd"] for g in out), 6),
              "list_usd": round(sum(g["list_usd"] for g in out), 6),
              "llm_avoided_est_tokens": sum(g["llm_avoided_est_tokens"] for g in out)}
    return {"groups": out, "totals": totals,
            "note": "llm_avoided_est_tokens is an estimate supplied by callers, not a measurement"}


def _count(values: Iterable[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for value in values:
        counts[str(value)] += 1
    return counts


def spent_today(rows: Optional[Iterable[Mapping[str, Any]]] = None) -> float:
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    source = rows if rows is not None else read(since=start)
    return round(sum(float(r.get("cost_usd") or 0) for r in source if float(r.get("ts") or 0) >= start), 8)
