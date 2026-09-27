"""`jev lane`: pick the smallest sufficient lane, then decide each loop step with checks first.

    jev lane classify --task "rename foo to bar in utils.py"            # first lane + model/effort
    jev lane step --task "..." --lane small --attempt 1 --run "pytest -q" --scope "src/*"
    jev lane evidence --run "pytest -q" --run "ruff check ."             # the facts alone, no Jev
    jev lane next --lane medium                                           # one step up the ladder
    jev lane targets --host hermes                                        # the lane map in force
    jev lane shadow                                                       # Hermes: one shadow tick (cron)
    jev lane shadow-report                                                # Hermes: shadow log vs outcomes
    jev lane replay-build / replay-report                                 # backtest on your own history

Same contract as every other `jev` command: JSON on stdout; bad input exits 2 with nothing
sent; a Jev failure is a normal answer carrying the fallback (keep the current model; verify).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import decide as engine, lane_replay, lane_shadow, lanes


def _out(value: Any) -> int:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")
    return 0


def _bad(detail: str) -> int:
    _out({"error": "invalid_request", "detail": detail})
    return 2


def _facts(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    text = Path(raw).expanduser().read_text(encoding="utf-8") if not raw.lstrip().startswith("{") else raw
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--facts must be a JSON object")
    return data


def _task(args: Any) -> str:
    task = args.task
    if task in (None, "-"):
        task = sys.stdin.read(200_000)
    task = (task or "").strip()
    if not task:
        raise ValueError("give the work with --task (or - to read it from stdin)")
    return task


def _brief(decision: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("action", "source", "matched_rule", "matched_pre_rule", "unsure", "fallback_used", "error",
            "latency_ms", "cost_usd", "policy", "mode")
    out = {key: decision.get(key) for key in keep if key in decision}
    answers = decision.get("answers") or {}
    out["readings"] = {name: ({"choice": r.get("choice"), "confidence": r.get("confidence")} if r.get("kind") == "choice"
                              else round(float(r.get("p", 0.0)), 3)) for name, r in answers.items()}
    return out


def cmd_lane(args: Any) -> int:
    try:
        if args.action == "targets":
            return _out({"host": args.host, "lanes": lanes.targets(args.host)})
        if args.action == "next":
            if not args.lane:
                return _bad("give --lane")
            up = lanes.next_lane(args.lane)
            return _out({"from": args.lane, "lane": up or "person",
                         "target": lanes.targets(args.host).get(up) if up else None})
        if args.action == "shadow":
            return _out(lane_shadow.tick(kanban_db=Path(args.kanban_db).expanduser() if args.kanban_db else None,
                                         policy=args.policy or lane_shadow.DEFAULT_POLICY))
        if args.action == "shadow-report":
            return _out(lane_shadow.shadow_report(
                kanban_db=Path(args.kanban_db).expanduser() if args.kanban_db else None,
                hermes_root=Path(args.hermes_root).expanduser() if args.hermes_root else None,
                min_cell=args.min_cell))
        if args.action == "replay-build" and args.claude_projects:
            if not args.out:
                return _bad("give --out")
            count = lane_replay.write_rows(lane_replay.build_claude_rows(args.claude_projects), args.out)
            return _out({"rows": count, "out": args.out})
        if args.action == "replay-build":
            if not (args.kanban_db and args.hermes_root and args.out):
                return _bad("give --kanban-db, --hermes-root and --out (or --claude-projects and --out)")
            import time as _time
            since = _time.time() - args.days * 86400 if args.days else None
            rows = lane_replay.build_rows(args.kanban_db, args.hermes_root, since=since, limit=args.limit)
            count = lane_replay.write_rows(rows, args.out)
            return _out({"rows": count, "out": args.out, "next": f"jev batch --policy lane --in {args.out} "
                                                                  f"--out decisions.jsonl"})
        if args.action == "replay-report":
            if not args.rows:
                return _bad("give --rows (the output of jev batch --policy lane)")
            rows = lane_replay.read_jsonl(args.rows)
            if args.host == "claude-code":
                return _out(lane_replay.claude_report(rows))
            return _out(lane_replay.report(rows, host=args.host, min_cell=args.min_cell, top=args.top_effort))
        if args.action == "evidence":
            found = lanes.evidence(args.repo, runs=args.run or (), scope=args.scope or (), base=args.base,
                                   expects_changes=not args.no_changes_expected, timeout=args.check_timeout)
            return _out(found)
        task = _task(args)
        facts = _facts(args.facts)
        if args.action == "classify":
            result = lanes.classify(task, context=args.context or "", facts=facts, host=args.host,
                                    mode=args.mode, timeout=args.timeout)
            result["decision"] = _brief(result["decision"])
            return _out(result)
        # step
        if not args.lane:
            return _bad("give --lane (the lane this cycle ran in)")
        state: Dict[str, Any] = {}
        found: Dict[str, Any] = {}
        if args.run or args.scope or args.repo_evidence:
            found = lanes.evidence(args.repo, runs=args.run or (), scope=args.scope or (), base=args.base,
                                   expects_changes=not args.no_changes_expected, timeout=args.check_timeout)
            facts = {**found["facts"], **facts}
            state = found["state"]
        result = lanes.step(task, lane=args.lane, attempts=args.attempt, facts=facts, state=state,
                            notes=args.notes or "", host=args.host, same_failure_repeated=args.same_failure,
                            mode=args.mode, timeout=args.timeout)
        result["decision"] = _brief(result["decision"])
        if found:
            result["evidence"] = {"facts": found["facts"], "checks": found["checks"],
                                  "out_of_scope": found["out_of_scope"], "sensitive": found["sensitive"]}
        return _out(result)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        return _bad(str(error))


def add_parsers(sub: Any) -> None:
    p = sub.add_parser("lane", help="smallest sufficient model lane, and continue/retry/verify/escalate/complete "
                                    "after each cycle with deterministic checks first")
    p.add_argument("action", choices=["classify", "step", "evidence", "next", "targets", "replay-build",
                                         "replay-report", "shadow", "shadow-report"])
    p.add_argument("--task", help="the work, in the person's words (- reads stdin)")
    p.add_argument("--context", help="classify: a line of context (repo, files involved)")
    p.add_argument("--facts", help="JSON object or file of values your code computed; never sent")
    p.add_argument("--host", choices=list(lanes.HOSTS), default="claude-code")
    p.add_argument("--lane", choices=list(lanes.LANES), help="step/next: the lane this cycle ran in")
    p.add_argument("--attempt", type=int, default=1, help="step: attempts so far in this lane")
    p.add_argument("--same-failure", action="store_true", help="step: the same failure as the last attempt")
    p.add_argument("--run", action="append", help="a deterministic check to run (test, build, typecheck, lint)")
    p.add_argument("--scope", action="append", help="a path or glob the work may change (repeatable)")
    p.add_argument("--repo", default=".", help="the git checkout the work is in")
    p.add_argument("--repo-evidence", action="store_true", help="step: read git diff even with no --run/--scope")
    p.add_argument("--base", default="HEAD", help="diff against this ref")
    p.add_argument("--no-changes-expected", action="store_true", help="the work is read-only (a review, a report)")
    p.add_argument("--notes", help="step: one short paragraph of what this cycle did")
    p.add_argument("--mode", choices=list(engine.MODES), default="live")
    p.add_argument("--timeout", type=float, default=4.0)
    p.add_argument("--policy", help="shadow: the lane policy (default lane-kanban)")
    p.add_argument("--kanban-db", help="replay-build: the fleet's kanban.db (opened read-only)")
    p.add_argument("--claude-projects", help="replay-build: a Claude Code projects folder (subagent transcripts)")
    p.add_argument("--hermes-root", help="replay-build: the Hermes root holding profiles/*/state.db")
    p.add_argument("--out", help="replay-build: where the rows go")
    p.add_argument("--days", type=float, default=0, help="replay-build: only tasks created in the last N days")
    p.add_argument("--limit", type=int, default=0, help="replay-build: only the newest N tasks")
    p.add_argument("--rows", help="replay-report: decisions from jev batch --policy lane")
    p.add_argument("--min-cell", type=int, default=8, help="replay-report: smallest effort cell used")
    p.add_argument("--top-effort", default="high", help="replay-report: the lane (or effort) always-top would use")
    p.add_argument("--check-timeout", type=float, default=lanes.CHECK_TIMEOUT)
    p.set_defaults(func=cmd_lane)
