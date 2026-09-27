"""Lanes on a live Hermes Kanban board: shadow first, and a switch that stays off until earned.

    jev lane shadow          # one tick: classify cards created since the last tick
    jev lane shadow-report   # join the shadow log to how those cards actually ran

Run the tick from cron or launchd every few minutes. What it does depends on the ``lanes``
switch (``jev switches``, or ``/jev lanes <mode>`` in Hermes; ``<root>/jev/LANES_OFF`` wins):

* ``off`` (the default): nothing.
* ``shadow``: classify each new card with the policy (``lane-kanban`` by default), log what
  it would choose next to what the card will run on, and change nothing. The log line holds
  ids, the lane, the target and the readings' confidence, never the card's text.
* ``on``: the same, and set the card's per-task model and reasoning effort before dispatch,
  only for a card that has no override yet, is not running, and whose lane is not
  ``keep_current``. Promote to ``on`` only after ``shadow-report`` shows fewer tokens at the
  same first-try success on your own cards, and only with the owner's yes.

Every database is opened read-only; the only write is through ``apply`` in ``on`` mode.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from . import decide as engine, lane_replay, lanes, switches

FEATURE = "lanes"
DEFAULT_POLICY = "lane-kanban"
FIRST_TICK_LOOKBACK = 6 * 3600
MAX_PER_TICK = 50


def root() -> Path:
    return switches.root()


def log_path() -> Path:
    return root() / "logs" / "jev-lanes.jsonl"


def cursor_path() -> Path:
    return root() / "jev" / "lanes-shadow.cursor.json"


def _read_cursor() -> Dict[str, Any]:
    try:
        return json.loads(cursor_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cursor(value: Mapping[str, Any]) -> None:
    path = cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)


def new_cards(kanban_db: Path, since: float, limit: int = MAX_PER_TICK) -> List[Dict[str, Any]]:
    con = sqlite3.connect(f"file:{kanban_db}?immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, title, body, status, assignee, created_at, model_override, reasoning_effort FROM tasks "
            "WHERE created_at > ? AND status NOT IN ('archived', 'done') ORDER BY created_at LIMIT ?",
            (since, limit)).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def hermes_apply(card_id: str, target: Mapping[str, str]) -> bool:
    """Set a card's model and effort through Hermes's own Kanban API (its events and observers
    fire as for a person's edit). Returns False, never raises, when Hermes cannot be reached."""
    agent = root() / "hermes-agent"
    python = agent / "venv" / "bin" / "python"
    if not python.is_file():
        return False
    code = ("import sys; from hermes_cli import kanban_db as kb, kanban_db_connect as kbc\n"
            "with kbc.connect_closing() as c:\n"
            " a = kb.set_model_override(c, sys.argv[1], sys.argv[2], provider=sys.argv[3] or None)\n"
            " b = kb.set_reasoning_effort(c, sys.argv[1], sys.argv[4])\n"
            "sys.exit(0 if a and b else 1)\n")
    try:
        done = subprocess.run([str(python), "-c", code, card_id, target["model"], target.get("provider", ""),
                               target["effort"]], cwd=agent, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def tick(*, kanban_db: Optional[Path] = None, policy: str = DEFAULT_POLICY, now: Optional[float] = None,
         transport: Any = None, apply: Callable[[str, Mapping[str, str]], bool] = hermes_apply,
         limit: int = MAX_PER_TICK) -> Dict[str, Any]:
    mode = switches.mode(FEATURE)
    if mode == "off":
        return {"mode": "off", "classified": 0}
    now = time.time() if now is None else now
    db = Path(kanban_db) if kanban_db else root() / "kanban.db"
    cursor = _read_cursor()
    since = float(cursor.get("created_at") or now - FIRST_TICK_LOOKBACK)
    private = set(lane_replay._private_profiles(root()))
    mapped = lanes.targets("hermes")
    cards = new_cards(db, since, limit)
    counts: Dict[str, int] = {}
    applied = 0
    log_path().parent.mkdir(parents=True, exist_ok=True)
    with log_path().open("a", encoding="utf-8") as handle:
        for card in cards:
            since = max(since, float(card["created_at"] or 0))
            if (card.get("assignee") or "") in private:
                continue
            text = (card.get("title") or "").strip()
            if card.get("body"):
                text += "\n\n" + str(card["body"])[:2_500]
            facts = {"person_named_model": bool(card.get("model_override"))}
            decision = engine.decide({"task": text}, policy, mode="shadow" if mode == "shadow" else "live",
                                           facts=facts, transport=transport, feature=FEATURE)
            lane = decision["action"]
            target = mapped.get(lane)
            did_apply = False
            if (mode == "on" and target and not card.get("model_override") and not card.get("reasoning_effort")
                    and card.get("status") in ("todo", "ready", "triage", "scheduled")):
                did_apply = bool(apply(card["id"], target))
                applied += int(did_apply)
            counts[lane] = counts.get(lane, 0) + 1
            lane_reading = (decision.get("answers") or {}).get("lane") or {}
            handle.write(json.dumps({
                "ts": round(now, 3), "task_id": card["id"], "profile": card.get("assignee"), "mode": mode,
                "lane": lane, "target": target, "source": decision.get("source"), "policy": decision.get("policy"),
                "confidence": lane_reading.get("confidence"), "fallback_used": decision.get("fallback_used"),
                "had_override": bool(card.get("model_override") or card.get("reasoning_effort")),
                "applied": did_apply,
            }, separators=(",", ":")) + "\n")
    _write_cursor({"created_at": since, "ts": now})
    return {"mode": mode, "classified": sum(counts.values()), "lanes": counts, "applied": applied,
            "log": str(log_path())}


def shadow_report(*, kanban_db: Optional[Path] = None, hermes_root: Optional[Path] = None,
                  log: Optional[Path] = None, min_cell: int = 8) -> Dict[str, Any]:
    """The shadow log joined to how each card actually ran, through the replay report."""
    base = Path(hermes_root) if hermes_root else root()
    db = Path(kanban_db) if kanban_db else base / "kanban.db"
    decisions: Dict[str, Dict[str, Any]] = {}
    for line in Path(log or log_path()).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        decisions[row["task_id"]] = {"action": row["lane"], "source": row.get("source")}
    rows = [dict(row, decision=decisions[row["id"]]) for row in lane_replay.build_rows(db, base)
            if row["id"] in decisions]
    out = lane_replay.report(rows, host="hermes", min_cell=min_cell)
    out["logged"] = len(decisions)
    out["joined"] = len(rows)
    return out
