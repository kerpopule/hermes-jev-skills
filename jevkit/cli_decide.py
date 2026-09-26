"""The `jev` subcommands for policy decisions: decide, score, route-to, gate, batch, shadow,
ledger, policies and switches.

Same contract as every other `jev` command: JSON out on stdout; bad input is
``{"error": "invalid_request", "detail": ...}`` with exit 2 and nothing sent; a Jev failure is
a normal answer carrying the policy's fallback action, exit 0, so an agent never gets stuck.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import (batch as batches, decide as engine, evaluate, gate, ledger, limits, policy as policies,
               route_to as router, shadow, switches, triage)


def _out(value: Any) -> int:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")
    return 0


def _bad(detail: str) -> int:
    _out({"error": "invalid_request", "detail": detail})
    return 2


def _read_json(source: Optional[str], what: str) -> Any:
    """A file path, ``-`` or nothing (stdin). Every way it can be unreadable is a ValueError."""
    try:
        if source in (None, "-"):
            text = sys.stdin.read(2_000_000)
        else:
            text = Path(source).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"{what} could not be read: {error}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{what} is not valid JSON: {error}") from None
    except RecursionError:
        raise ValueError(f"{what} is nested too deeply to read") from None


def _timeout(value: float) -> float:
    if not 0 < value <= 3600:
        raise ValueError("--timeout must be a number of seconds, above 0 and at most 3600")
    return value


def _brief(decision: Dict[str, Any], explain: bool, rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(decision)
    if explain and rules is not None and decision.get("answers"):
        out["explain"] = policies.explain(rules, decision["answers"])
    return out


# ── decide ───────────────────────────────────────────────────────────────────

def cmd_decide(args: argparse.Namespace) -> int:
    try:
        timeout = _timeout(args.timeout)
        state = _read_json(args.state, "--state")
        if args.question:
            if args.policy:
                raise ValueError("give --policy or --question, not both")
            asked: Dict[str, Any] = {}
            for item in args.question:
                name, sep, text = item.partition("=")
                if not sep or not name.strip() or not text.strip():
                    raise ValueError('--question takes name="the yes/no question"')
                if name in asked:
                    raise ValueError(f"two questions are both named {name!r}")
                asked[name.strip()] = text.strip()
            return _out(engine.adhoc(state, asked, timeout=timeout))
        if not args.policy:
            raise ValueError("give --policy NAME|FILE, or up to eight --question name=\"...\"")
        choices = _read_json(args.options, "--options") if args.options else None
        if choices is not None and not isinstance(choices, dict):
            raise ValueError('--options is {"question": {"option": "what it means", ...}}')
        rules = policies.load(args.policy, choices=choices)
        decision = engine.decide(state, rules, mode=args.mode, timeout=timeout)
    except (ValueError, policies.PolicyError) as error:
        return _bad(str(error))
    return _out(_brief(decision, args.explain, rules))


def cmd_score(args: argparse.Namespace) -> int:
    try:
        timeout = _timeout(args.timeout)
        state = _read_json(args.state, "--state")
        rubric = _read_json(args.rubric, "--rubric") if args.rubric else None
        result = evaluate.score(state, rubric=rubric, policy=args.policy, timeout=timeout, mode=args.mode)
    except (ValueError, policies.PolicyError) as error:
        return _bad(str(error))
    rules = policies.load(evaluate.with_rubric(policies.load(args.policy), rubric)) if args.explain else None
    return _out(_brief(result, args.explain, rules))


def cmd_route_to(args: argparse.Namespace) -> int:
    try:
        timeout = _timeout(args.timeout)
        destinations = _read_json(args.destinations, "--destinations")
        if not isinstance(destinations, dict):
            raise ValueError('--destinations is {"id": "what goes there", ...}')
        state = _read_json(args.state, "--state")
        result = router.route_to(state, destinations, fallback=args.fallback, floor=args.floor,
                                 min_margin=args.margin, timeout=timeout, mode=args.mode)
    except (ValueError, policies.PolicyError) as error:
        return _bad(str(error))
    return _out(result)


# ── gate ─────────────────────────────────────────────────────────────────────

def _gate_rows(path: str) -> List[Dict[str, Any]]:
    rows = []
    for raw in batches.read_rows(Path(path)):
        if "state" not in raw:
            raw = dict(raw)
            raw["state"] = gate.build_state(str(raw.get("tool") or "terminal"), command=raw.get("command"),
                                            args=raw.get("args"), flagged_as=raw.get("flagged_as"),
                                            pattern_keys=raw.get("pattern_keys") or (),
                                            workdir_kind_value=raw.get("workdir_kind") or "unknown",
                                            surface=raw.get("surface"))
            raw.pop("command", None)
            raw.pop("args", None)
        rows.append(raw)
    return rows


def cmd_gate(args: argparse.Namespace) -> int:
    try:
        if args.action == "replay":
            if not args.file or not args.out:
                raise ValueError("replay takes FILE.jsonl and --out FILE.jsonl")
            rows = _gate_rows(args.file)
            summary = batches.run(args.policy, rows, Path(args.out), yes=args.yes, workers=args.workers,
                                  feature="gate", limit=args.limit)
            if summary.get("status") == "needs_yes":
                _out(summary)
                return 3
            if args.report:
                done = [json.loads(line) for line in Path(args.out).read_text(encoding="utf-8").splitlines() if line]
                summary["redteam"] = gate.score_fixtures(
                    [r for r in done if r.get("expect")], {str(r["id"]): r["decision"].get("action") for r in done})
            return _out(summary)
        timeout = _timeout(args.timeout)
        if args.args:
            payload = _read_json(args.args, "--args")
            command = payload.get("command") if isinstance(payload, dict) else None
            tool_args = None if command is not None else payload
        elif args.command is not None:
            command, tool_args = args.command, None
        else:
            raise ValueError("give --command TEXT or --args FILE (the tool's arguments as JSON)")
        decision = gate.check(args.tool, command=command, args=tool_args, flagged_as=args.flagged_as,
                              pattern_keys=args.pattern_key or (), workdir=args.workdir, policy=args.policy,
                              timeout=timeout, mode="shadow" if args.dry_run else "live")
    except (ValueError, policies.PolicyError) as error:
        return _bad(str(error))
    decision["would_return"] = {mode: gate.hook_result(decision, mode) for mode in ("shadow", "ask", "block")}
    decision["tiebreak"] = gate.tiebreak_verdict(decision)
    return _out(decision)


# ── batch, shadow report, ledger, policies, switches ─────────────────────────

def cmd_batch(args: argparse.Namespace) -> int:
    try:
        rows = batches.read_rows(Path(args.input))
        choices = _read_json(args.options, "--options") if args.options else None
        summary = batches.run(args.policy, rows, Path(args.out), yes=args.yes, rescore=args.rescore,
                              workers=args.workers, timeout=_timeout(args.timeout), choices=choices,
                              limit=args.limit)
    except (ValueError, policies.PolicyError, OSError) as error:
        return _bad(str(error))
    _out(summary)
    return 3 if summary.get("status") == "needs_yes" else 0


def cmd_shadow(args: argparse.Namespace) -> int:
    try:
        rows = [json.loads(line) for line in Path(args.rows).read_text(encoding="utf-8").splitlines() if line.strip()]
        promotion = None
        if args.policy:
            promotion = policies.load(args.policy).get("promotion")
    except (OSError, ValueError, policies.PolicyError) as error:
        return _bad(str(error))
    return _out(shadow.report(args.feature, rows, promotion))


def cmd_ledger(args: argparse.Namespace) -> int:
    import time

    since = time.time() - args.days * 86400
    rows = [r for r in ledger.read(since=since) if not args.feature or r.get("feature") == args.feature]
    out = ledger.summarize(rows, by_day=not args.total)
    out["limits"] = limits.status()
    out["file"] = str(ledger.path())
    return _out(out)


def cmd_policies(args: argparse.Namespace) -> int:
    try:
        if args.action == "show":
            if not args.name:
                raise ValueError("show takes a policy name or file")
            loaded = policies.load(args.name)
            return _out({"label": policies.label(loaded), "origin": loaded["_origin"],
                         **{k: v for k, v in loaded.items() if not k.startswith("_")}})
        if args.action == "lint":
            if not args.name:
                raise ValueError("lint takes a policy file")
            policies.load(args.name)
            return _out({"ok": True, "policy": args.name})
    except (ValueError, policies.PolicyError) as error:
        return _bad(str(error))
    return _out({"policies": policies.describe(), "override_dir": str(policies.override_dir())})


def cmd_switches(args: argparse.Namespace) -> int:
    if args.feature:
        if not args.mode:
            return _out({args.feature: switches.describe().get(args.feature)})
        try:
            path = switches.set_mode(args.feature, args.mode, shared=args.all)
        except ValueError as error:
            return _bad(str(error))
        return _out({"set": {args.feature: args.mode}, "file": str(path),
                     "effective": switches.mode(args.feature),
                     "note": "the kill switch file always wins; plugin hooks load at gateway start"})
    return _out({"features": switches.describe(), "kill_switch_dir": str(switches.jev_dir(True))})


def cmd_triage_preset(args: argparse.Namespace, entries: List[Any]) -> int:
    rows = []
    for index, entry in enumerate(entries):
        decision = triage.classify_state(entry, args.preset, timeout=args.timeout)
        decision["index"] = index
        rows.append(decision)
    counts: Dict[str, int] = {}
    for row in rows:
        key = str(row.get("action") or row.get("route"))
        counts[key] = counts.get(key, 0) + 1
    return _out({"preset": args.preset, "counts": counts, "items": rows})


def add_parsers(sub: Any) -> None:
    p = sub.add_parser("decide", help="run a named policy (or up to 8 yes/no questions) against one state")
    p.add_argument("--policy", help="a shipped policy name, a local override name, or a .json file")
    p.add_argument("--question", action="append", help='ad-hoc yes/no question: name="..." (repeatable, up to 8)')
    p.add_argument("--state", help="JSON file with the state; - or omitted reads stdin")
    p.add_argument("--options", help='JSON {"question": {"option": "meaning"}} for a policy that takes options at run time')
    p.add_argument("--mode", choices=list(engine.MODES), default="live",
                   help="shadow keeps the rule's answer even when the Jev version drifted")
    p.add_argument("--explain", action="store_true", help="show every rule, its readings and whether it matched")
    p.add_argument("--timeout", type=float, default=4.0)
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("score", help="grade an output with rubric scores and thresholds instead of an LLM judge")
    p.add_argument("--state", help='JSON {"task": ..., "output": ...}; - or omitted reads stdin')
    p.add_argument("--rubric", help='JSON {"quality": {"instructions": ..., "levels": [lowest, ..., highest]}, ...}')
    p.add_argument("--policy", default=evaluate.DEFAULT_POLICY)
    p.add_argument("--mode", choices=list(engine.MODES), default="live")
    p.add_argument("--explain", action="store_true")
    p.add_argument("--timeout", type=float, default=4.0)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("route-to", help="send work to one of N agents, queues or lanes, or to your fallback")
    p.add_argument("--destinations", required=True, help='JSON {"id": "what goes there"} (or {"id": {"description", "group"}})')
    p.add_argument("--state", help="JSON file with the work; - or omitted reads stdin")
    p.add_argument("--fallback", help="what to return when no destination is clear")
    p.add_argument("--floor", type=float, default=router.DEFAULT_FLOOR, help="minimum confidence")
    p.add_argument("--margin", type=float, default=router.DEFAULT_MARGIN, help="minimum lead over the runner-up")
    p.add_argument("--mode", choices=list(engine.MODES), default="live")
    p.add_argument("--timeout", type=float, default=4.0)
    p.set_defaults(func=cmd_route_to)

    p = sub.add_parser("gate", help="risk-check a tool call before it runs: approve / ask a person / deny")
    p.add_argument("action", nargs="?", choices=["check", "replay"], default="check")
    p.add_argument("file", nargs="?", help="replay: JSONL of {id, tool, command, ...labels}")
    p.add_argument("--tool", default="terminal")
    p.add_argument("--command", help="the command to check")
    p.add_argument("--args", help="JSON file with the tool's arguments")
    p.add_argument("--flagged-as", help="what the host's pattern list called it, if anything")
    p.add_argument("--pattern-key", action="append", help="a host pattern key (repeatable)")
    p.add_argument("--workdir", help="the folder it would run in; its kind is worked out locally")
    p.add_argument("--policy", default=gate.DEFAULT_POLICY)
    p.add_argument("--dry-run", action="store_true", help="shadow mode: log it, change nothing")
    p.add_argument("--timeout", type=float, default=1.5)
    p.add_argument("--out", help="replay: where decisions go (resumable)")
    p.add_argument("--yes", action="store_true", help="replay: run the paid calls after seeing the estimate")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--report", action="store_true", help="replay: score rows that carry an `expect` label")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("batch", help="one policy over a JSONL of states (backtests); resumable, asks before paying")
    p.add_argument("--policy", required=True)
    p.add_argument("--in", dest="input", required=True, help='JSONL: {"id", "state", ...labels}')
    p.add_argument("--out", required=True)
    p.add_argument("--options", help="JSON run-time options for a policy that takes them")
    p.add_argument("--rescore", action="store_true", help="apply the policy to recorded answers; nothing is sent")
    p.add_argument("--yes", action="store_true", help="run the paid calls after seeing the estimate")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("shadow", help="report: did a shadow run earn promotion? PASS/FAIL per criterion")
    p.add_argument("action", choices=["report"])
    p.add_argument("--feature", required=True, choices=sorted(shadow.METRICS))
    p.add_argument("--rows", required=True, help="JSONL from `jev batch` (or joined shadow logs) with truth labels")
    p.add_argument("--policy", help="read the promotion criteria from this policy")
    p.set_defaults(func=cmd_shadow)

    p = sub.add_parser("ledger", help="what policy decisions cost: calls, latency, errors, dollars, per feature and day")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--feature")
    p.add_argument("--total", action="store_true", help="one row per feature instead of per day")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("policies", help="list, show or lint decision policies")
    p.add_argument("action", nargs="?", choices=["list", "show", "lint"], default="list")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_policies)

    p = sub.add_parser("switches", help="each policy feature's mode and kill switch; set one with FEATURE MODE")
    p.add_argument("feature", nargs="?", choices=sorted(switches.MODES))
    p.add_argument("mode", nargs="?")
    p.add_argument("--all", action="store_true", help="set the shared default for every profile")
    p.set_defaults(func=cmd_switches)
