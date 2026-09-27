"""Replay lanes against a Hermes fleet's own Kanban history: which lane each task would get,
what it actually ran on, what it cost in tokens, and whether it finished.

    jev lane replay-build --kanban-db <root>/kanban.db --hermes-root <root> --out rows.jsonl
    jev batch --policy lane --in rows.jsonl --out decisions.jsonl --yes
    jev lane replay-report --rows decisions.jsonl

The build step opens every database read-only (``immutable=1``: a live fleet keeps writing to
them) and sends nothing. A row is one task: its title and body are the ``state`` Jev reads;
the runs it took, the models and reasoning effort they used, their tokens and the final outcome
are labels, never sent. Tasks on a profile listed in ``private_profiles`` are left out.

The report answers the two questions that decide promotion, per lane:

* **tokens**: what the tasks in each lane cost as they ran, and an estimate of what they would
  cost at the lane's own effort, from the fleet's own runs of the same lane at each effort;
* **success parity**: the finish rate of tasks in each lane that happened to run at low effort
  versus higher effort. History is a natural experiment here, not a controlled one: effort was
  set per profile, so the report shows sample sizes and 95% intervals and says when they are too
  small to judge.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import lanes

SUCCESS = {"completed", "success", "review_requested"}
FAILED = {"crashed", "timed_out", "gave_up", "spawn_failed", "reclaimed", "infrastructure_interrupted",
          "infrastructure_transient", "stale"}
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _effort(model_config: Optional[str]) -> Optional[str]:
    try:
        data = json.loads(model_config or "{}")
    except ValueError:
        return None
    config = data.get("reasoning_config") if isinstance(data, dict) else None
    if isinstance(config, dict):
        if config.get("enabled") is False:
            return "none"
        return str(config.get("effort") or "") or None
    return None


def _sessions(root: Path, profile: str, ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    db = root / "profiles" / profile / "state.db"
    if not db.is_file() or not ids:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    con = _ro(db)
    try:
        for start in range(0, len(ids), 500):
            chunk = list(ids[start:start + 500])
            marks = ",".join("?" * len(chunk))
            for row in con.execute(
                    f"SELECT id, model, model_config, input_tokens, output_tokens, cache_read_tokens, "
                    f"reasoning_tokens, estimated_cost_usd, api_call_count FROM sessions WHERE id IN ({marks})", chunk):
                out[row["id"]] = {
                    "model": row["model"], "effort": _effort(row["model_config"]),
                    "input": int(row["input_tokens"] or 0), "output": int(row["output_tokens"] or 0),
                    "cache_read": int(row["cache_read_tokens"] or 0), "reasoning": int(row["reasoning_tokens"] or 0),
                    "cost": float(row["estimated_cost_usd"] or 0.0), "calls": int(row["api_call_count"] or 0),
                }
    finally:
        con.close()
    return out


def _private_profiles(root: Path) -> List[str]:
    try:
        data = json.loads((root / "jev" / "routing.json").read_text(encoding="utf-8"))
        return [str(p) for p in data.get("private_profiles") or ()]
    except (OSError, ValueError, AttributeError):
        return []


def outcome_class(outcomes: Sequence[Optional[str]], status: Optional[str]) -> str:
    if status in ("done",) or any(o in SUCCESS for o in outcomes):
        return "success"
    last = next((o for o in reversed(outcomes) if o), None)
    if last == "blocked" or status == "blocked":
        return "blocked"
    if last in FAILED or status in ("failed",):
        return "failed"
    return "other"


def build_rows(kanban_db: Any, hermes_root: Any, *, since: Optional[float] = None,
               limit: int = 0, body_chars: int = 2_500) -> List[Dict[str, Any]]:
    """One row per task that has at least one run with a known session (so tokens are known)."""
    root = Path(hermes_root).expanduser()
    private = set(_private_profiles(root))
    con = _ro(Path(kanban_db).expanduser())
    try:
        tasks = {row["id"]: dict(row) for row in con.execute(
            "SELECT id, title, body, status, assignee, created_at, model_override, reasoning_effort FROM tasks")}
        runs = [dict(row) for row in con.execute(
            "SELECT id, task_id, profile, outcome, status, metadata, started_at, ended_at FROM task_runs ORDER BY id")]
    finally:
        con.close()
    by_profile: Dict[str, List[str]] = defaultdict(list)
    for run in runs:
        try:
            meta = json.loads(run.get("metadata") or "{}")
        except ValueError:
            meta = {}
        run["session"] = meta.get("worker_session_id") if isinstance(meta, dict) else None
        if run["session"] and run.get("profile"):
            by_profile[run["profile"]].append(run["session"])
    sessions: Dict[str, Dict[str, Any]] = {}
    for profile, ids in by_profile.items():
        sessions.update(_sessions(root, profile, ids))
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[run["task_id"]].append(run)
    rows: List[Dict[str, Any]] = []
    for task_id, task_runs in grouped.items():
        task = tasks.get(task_id)
        if not task or (since and (task.get("created_at") or 0) < since):
            continue
        if (task.get("assignee") or "") in private or any((r.get("profile") or "") in private for r in task_runs):
            continue
        joined = [(r, sessions.get(r["session"])) for r in task_runs if r.get("session")]
        joined = [(r, s) for r, s in joined if s]
        if not joined:
            continue
        tokens = sum(s["input"] + s["output"] for _, s in joined)
        text = (task.get("title") or "").strip()
        body = (task.get("body") or "").strip()
        if body:
            text += "\n\n" + body[:body_chars]
        efforts = [s["effort"] for _, s in joined]
        rows.append({
            "id": task_id, "state": {"task": text},
            "had_model_override": bool(task.get("model_override")),
            "profile": task.get("assignee"), "runs": len(task_runs), "runs_with_usage": len(joined),
            "models": sorted({s["model"] for _, s in joined if s["model"]}),
            "effort": efforts[-1], "efforts": efforts,
            "tokens": tokens, "input_tokens": sum(s["input"] for _, s in joined),
            "output_tokens": sum(s["output"] for _, s in joined),
            "cache_read_tokens": sum(s["cache_read"] for _, s in joined),
            "reasoning_tokens": sum(s["reasoning"] for _, s in joined),
            "cost_usd": round(sum(s["cost"] for _, s in joined), 6),
            "outcome": outcome_class([r.get("outcome") for r in task_runs], task.get("status")),
            "first_try_success": (task_runs[0].get("outcome") in SUCCESS),
            "failed_runs": sum(1 for r in task_runs if r.get("outcome") in FAILED),
            "first_effort": efforts[0],
            "created_at": task.get("created_at"),
        })
    rows.sort(key=lambda row: row.get("created_at") or 0)
    return rows[-limit:] if limit else rows


def write_rows(rows: Iterable[Mapping[str, Any]], out: Any) -> int:
    count = 0
    with Path(out).expanduser().open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
            count += 1
    return count


# ── report ──────────────────────────────────────────────────────────────────

def wilson(successes: int, total: int, z: float = 1.96) -> Optional[List[float]]:
    if not total:
        return None
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def _rate(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Finished at all, and finished on the first run (the stricter signal: a task that needed a
    retry cost a second run even if it finished in the end)."""
    judged = [r for r in rows if r.get("outcome") in ("success", "failed", "blocked")]
    wins = sum(1 for r in judged if r["outcome"] == "success")
    first = sum(1 for r in rows if r.get("first_try_success"))
    return {"n": len(rows), "finished_rate": round(wins / len(judged), 3) if judged else None,
            "first_try_rate": round(first / len(rows), 3) if rows else None,
            "first_try_ci95": wilson(first, len(rows)),
            "failed_runs_per_task": round(sum(int(r.get("failed_runs") or 0) for r in rows) / len(rows), 3) if rows else None}


def _median(values: Sequence[float]) -> Optional[float]:
    return float(statistics.median(values)) if values else None


def _within_profile(rows: Sequence[Mapping[str, Any]], key: Any, base: str, min_cell: int) -> Dict[str, Any]:
    cells: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        value = key(row)
        if value:
            cells[str(row.get("profile"))][str(value)].append(float(row.get("tokens") or 0))
    out: Dict[str, Any] = {base: {"multiplier": 1.0, "profiles": None, "weight": None}}
    for level in sorted({v for profile in cells.values() for v in profile} - {base}):
        logs, weights, used = 0.0, 0, 0
        for by_level in cells.values():
            a, b = by_level.get(level) or [], by_level.get(base) or []
            if len(a) >= min_cell and len(b) >= min_cell:
                ma, mb = statistics.median(a), statistics.median(b)
                if ma > 0 and mb > 0:
                    weight = min(len(a), len(b))
                    logs += weight * math.log(ma / mb)
                    weights += weight
                    used += 1
        if weights:
            out[level] = {"multiplier": round(math.exp(logs / weights), 3), "profiles": used, "weight": weights}
    return out


def _first_effort(row: Mapping[str, Any]) -> Optional[str]:
    return row.get("first_effort") or row.get("effort")


def _only_model(row: Mapping[str, Any]) -> Optional[str]:
    models = row.get("models") or []
    return models[0] if len(models) == 1 else None


def effort_multipliers(rows: Sequence[Mapping[str, Any]], *, base: str = "medium", min_cell: int = 8) -> Dict[str, Any]:
    """Tokens per task at each effort relative to ``base``, measured *within* each profile.

    Effort was set per profile, so a fleet-wide median by effort mostly compares profiles. Here
    each profile with at least ``min_cell`` tasks at both efforts contributes the ratio of its two
    medians, and the ratios are pooled as a geometric mean weighted by the smaller cell.
    """
    return _within_profile(rows, _first_effort, base, min_cell)


def model_multipliers(rows: Sequence[Mapping[str, Any]], *, base: str, min_cell: int = 8) -> Dict[str, Any]:
    """The same, for the model a task ran on (tasks that used one model only)."""
    return _within_profile(rows, _only_model, base, min_cell)


def report(rows: Sequence[Mapping[str, Any]], *, host: str = "hermes", min_cell: int = 8,
           top: str = "high") -> Dict[str, Any]:
    """Tokens and first-try success by the lane each task would get, against what actually ran.

    Projected tokens for a task moved from the effort it ran at to effort ``e`` = its actual
    tokens x multiplier(e) / multiplier(ran), with multipliers from ``effort_multipliers``. An
    effort with no measured multiplier keeps the actual tokens (said in ``unmeasured``), so the
    projection never invents a saving. ``top`` is the effort an always-top-model policy uses.
    """
    mapped = lanes.targets(host)
    mult = effort_multipliers(rows, min_cell=min_cell)
    base_model = (mapped.get("medium") or {}).get("model") or ""
    common = _count(_only_model(r) for r in rows if _only_model(r))
    base_model = base_model if base_model in common else next(iter(common), "")
    models = model_multipliers(rows, base=base_model, min_cell=min_cell) if base_model else {}
    unmeasured: set = set()

    def ratio(table: Mapping[str, Any], ran: Optional[str], target: Optional[str]) -> float:
        if not target or not ran or ran == target:
            return 1.0
        if ran not in table or target not in table:
            unmeasured.add(target if target not in table else ran)
            return 1.0
        return table[target]["multiplier"] / table[ran]["multiplier"]

    def moved(row: Mapping[str, Any], target: Optional[Mapping[str, str]]) -> float:
        tokens = float(row.get("tokens") or 0)
        if not target:
            return tokens
        return (tokens * ratio(mult, _first_effort(row), target.get("effort"))
                * ratio(models, _only_model(row), target.get("model")))

    default_target = mapped.get("medium")
    top_target = mapped.get(top) if top in mapped else {**(default_target or {}), "effort": top}

    by_lane: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_lane[str((row.get("decision") or {}).get("action") or "unknown")].append(row)
    order = lanes.LANES + ("keep_current", "unknown")
    out: Dict[str, Any] = {"tasks": len(rows), "host": host, "lane_map": mapped, "effort_multipliers": mult,
                           "model_multipliers": models, "lanes": {}, "sources": _count((r.get("decision") or {}).get("source") for r in rows)}
    actual_total = projected_total = top_total = default_total = 0.0
    for lane in sorted(by_lane, key=lambda name: order.index(name) if name in order else 99):
        members = by_lane[lane]
        actual = sum(float(r.get("tokens") or 0) for r in members)
        projected = sum(moved(r, mapped.get(lane) or default_target) for r in members)
        at_top = sum(moved(r, top_target) for r in members)
        at_default = sum(moved(r, default_target) for r in members)
        default_total += at_default
        actual_total += actual
        projected_total += projected
        top_total += at_top
        by_effort: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for row in members:
            by_effort[str(row.get("first_effort") or row.get("effort") or "unknown")].append(row)
        out["lanes"][lane] = {
            "tasks": len(members), "share": round(len(members) / len(rows), 3) if rows else None,
            "target": mapped.get(lane), "actual_tokens": int(actual),
            "projected_tokens_at_lane_target": int(projected), "projected_tokens_all_default": int(at_default),
            "projected_tokens_always_top": int(at_top),
            "success_all": _rate(members),
            "success_by_effort_it_ran_at": {e: _rate(c) for e, c in sorted(by_effort.items())},
            "models": _count(m for r in members for m in (r.get("models") or [])),
        }
    out["totals"] = {
        "actual_tokens_as_ran": int(actual_total), "projected_all_default": int(default_total),
        "projected_lanes": int(projected_total), "projected_always_top": int(top_total),
        "default_target": default_target, "top_target": top_target,
        "lanes_vs_default": round(projected_total / default_total - 1, 3) if default_total else None,
        "lanes_vs_as_ran": round(projected_total / actual_total - 1, 3) if actual_total else None,
        "lanes_vs_always_top": round(projected_total / top_total - 1, 3) if top_total else None,
    }
    if unmeasured:
        out["unmeasured"] = sorted(unmeasured)
    return out


def _count(values: Iterable[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for value in values:
        counts[str(value)] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def read_jsonl(path: Any) -> List[Dict[str, Any]]:
    rows = []
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows

# ── Claude Code: delegated subagent runs from local transcripts ────────────────

# $ per million tokens: input, output, cache read, cache write (5 min). Anthropic list prices,
# 2026-09; re-check before quoting. Matched on a substring of the model id, longest first.
CLAUDE_PRICES = {
    "fable": (10.0, 50.0, 1.0, 12.5), "opus-5-5": (4.0, 20.0, 0.2, 5.0), "opus-5": (5.0, 25.0, 0.5, 6.25),
    "opus-4": (5.0, 25.0, 0.5, 6.25), "sonnet-5": (2.0, 10.0, 0.2, 2.5), "sonnet-4": (3.0, 15.0, 0.3, 3.75),
    "haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}
CLAUDE_ALIASES = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5", "opus": "claude-opus-5-5"}


def claude_price(model: str) -> Optional[tuple]:
    model = CLAUDE_ALIASES.get(model, model)
    for key in sorted(CLAUDE_PRICES, key=len, reverse=True):
        if key in (model or ""):
            return CLAUDE_PRICES[key]
    return None


def claude_cost(usage: Mapping[str, int], model: str) -> Optional[float]:
    price = claude_price(model)
    if not price:
        return None
    return round((usage.get("input", 0) * price[0] + usage.get("output", 0) * price[1]
                  + usage.get("cache_read", 0) * price[2] + usage.get("cache_write", 0) * price[3]) / 1e6, 6)


def _first_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text") or "") for block in content
                         if isinstance(block, Mapping) and block.get("type") == "text")
    return ""


def build_claude_rows(projects: Any, *, body_chars: int = 3_000) -> List[Dict[str, Any]]:
    """One row per delegated subagent run found under a Claude Code ``projects`` folder.

    The task is the prompt the subagent was given; usage is summed once per API message (a
    message is logged once per content block); nothing is sent anywhere.
    """
    rows: List[Dict[str, Any]] = []
    for path in sorted(Path(projects).expanduser().rglob("*.jsonl")):
        if "subagents" not in path.parts:
            continue
        task, models, effort, seen = "", defaultdict(int), None, set()
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        turns = 0
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, Mapping) else None
            if not isinstance(message, Mapping):
                continue
            if not task and entry.get("type") == "user":
                task = _first_text(message).strip()
            effort = entry.get("effort") or effort
            model = message.get("model")
            used = message.get("usage")
            key = message.get("id") or entry.get("requestId")
            if model and not str(model).startswith("<") and isinstance(used, Mapping) and key not in seen:
                seen.add(key)
                turns += 1
                models[model] += 1
                usage["input"] += int(used.get("input_tokens") or 0)
                usage["output"] += int(used.get("output_tokens") or 0)
                usage["cache_read"] += int(used.get("cache_read_input_tokens") or 0)
                usage["cache_write"] += int(used.get("cache_creation_input_tokens") or 0)
        if not task or not models:
            continue
        model = max(models, key=models.get)
        rows.append({"id": f"{path.stem}-{hashlib.sha256(str(path).encode()).hexdigest()[:8]}", "state": {"task": task[:body_chars]}, "model": model, "effort": effort,
                     "turns": turns, **{f"{k}_tokens": v for k, v in usage.items()},
                     "tokens": usage["input"] + usage["output"] + usage["cache_write"],
                     "cost_usd": claude_cost(usage, model), "workflow": "workflows" in path.parts})
    return rows


def claude_report(rows: Sequence[Mapping[str, Any]], *, top: str = "escalate") -> Dict[str, Any]:
    """Cost by lane at each lane's model against running every task on the top lane's model.

    ``lanes_vs_always_top`` prices ``keep_current`` runs on the top model too, so the two sides
    differ only in the lanes Jev chose; ``lanes_vs_as_ran`` keeps them at what they ran on.

    Token counts are held equal across models, which flatters nothing: a smaller model that
    needs more turns would cost more than shown, so parity must come from a controlled replay.
    """
    mapped = lanes.targets("claude-code")
    top_model = mapped[top]["model"]
    out: Dict[str, Any] = {"runs": len(rows), "lanes": {}, "lane_map": mapped}
    total_ran = total_lanes = total_top = total_keep_top = 0.0
    for lane in lanes.LANES + ("keep_current",):
        members = [r for r in rows if (r.get("decision") or {}).get("action") == lane]
        if not members:
            continue

        def priced(row: Mapping[str, Any], model: str) -> float:
            usage = {k: int(row.get(f"{k}_tokens") or 0) for k in ("input", "output", "cache_read", "cache_write")}
            return claude_cost(usage, model) or 0.0
        ran = sum(float(r.get("cost_usd") or 0) for r in members)
        target = (mapped.get(lane) or {}).get("model")
        at_lane = sum(priced(r, target) if target else float(r.get("cost_usd") or 0) for r in members)
        at_top = sum(priced(r, top_model) for r in members)
        total_ran, total_lanes, total_top = total_ran + ran, total_lanes + at_lane, total_top + at_top
        total_keep_top += at_top if lane == "keep_current" else at_lane
        out["lanes"][lane] = {"runs": len(members), "share": round(len(members) / len(rows), 3),
                              "cost_as_ran": round(ran, 2), "cost_at_lane_model": round(at_lane, 2),
                              "cost_always_top": round(at_top, 2), "models_ran": _count(r.get("model") for r in members),
                              "tokens": sum(int(r.get("tokens") or 0) + int(r.get("cache_read_tokens") or 0) for r in members)}
    out["totals"] = {"cost_as_ran": round(total_ran, 2), "cost_lanes": round(total_lanes, 2),
                     "cost_always_top": round(total_top, 2),
                     "cost_lanes_keep_current_on_top": round(total_keep_top, 2),
                     "lanes_vs_always_top": round(total_keep_top / total_top - 1, 3) if total_top else None,
                     "lanes_vs_as_ran": round(total_lanes / total_ran - 1, 3) if total_ran else None}
    return out
