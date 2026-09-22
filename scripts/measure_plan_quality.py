#!/usr/bin/env python3
"""Does `jev plan` actually plan, or does it fall back on everything it is asked?

The planner asks one model to turn a spoken command into steps from a closed vocabulary, and
`parse_response` rejects the WHOLE plan if a single step is outside it — deliberately, because
the steps after a bad one were written assuming it happened. The cost of that rule is the thing
nobody has counted: the fallback is a single `goal` step, which is exactly the planning the
feature exists to do. A model that emits `press_key: "volume down"` once takes the plan with it.

First live probe, 2026-09-22: `open the Sound settings pane and turn the volume down one notch`
came back `status: fallback, reason: schema_mismatch` in 1297 ms, on a reply whose other two
steps were clean. So this counts it: N ordinary commands, run live, one call each, with the raw
reply kept, and every fallback diagnosed down to the step that killed it.

A command that plans is a plan. A command that falls back is a second of waiting plus a loop
that has to work the command out itself, which the feature was built to avoid.

Bar, stated before the numbers are seen: every command in this set should produce a plan, and
no wording change may drop a step the command asked for. The root cause of a fallback is printed
with it — the reply is kept, so the step that killed the plan is read, not guessed.

    python3 scripts/measure_plan_quality.py                  # ~12 calls, ~$0.02
    python3 scripts/measure_plan_quality.py --runs 3 --json /tmp/plan-quality.json

Nothing here touches the OS: `plan.plan` only ever returns steps, and the runner that would
execute them lives elsewhere and is not called.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from jevkit import plan  # noqa: E402

# Ordinary computer-use commands of the kind the fleet hands the planner: each one names an app
# or a control and an intent, none of them asks for anything that sends, pays or deletes.
COMMANDS = (
    "open the Sound settings pane and turn the volume down one notch",
    "open System Settings and switch the display to dark mode",
    "in Safari, open a new tab and go to the Apple support page",
    "find the word invoice in this document",
    "rename the selected file in Finder to budget-2026",
    "open the email from Dana about the quote",
    "open the Calculator and multiply 47 by 19",
    "turn the Bluetooth off, then back on",
    "scroll down three screens on this page and take a screenshot",
    "show me the trash in Finder",
    "open Calendar and go to tomorrow",
)
# A command that must never be planned is a different question, answered by `enforce_never_send`
# and its own tests. These are ordinary work, so every one of them SHOULD produce a plan.


class Recorder:
    """The real transport, keeping the raw reply so a fallback can be diagnosed."""

    def __init__(self):
        self.raw = b""
        self.calls = 0

    def __call__(self, url, body, headers, timeout):
        self.calls += 1
        self.raw = plan._http_transport(url, body, headers, timeout)
        return self.raw


def offending_step(raw: bytes) -> dict:
    """Why this plan was rejected, down to the step: the reply is kept, so this is read, not guessed."""
    try:
        payload = json.loads(raw)
        content = payload["choices"][0]["message"].get("content") or ""
        start, end = content.find("{"), content.rfind("}")
        steps = json.loads(content[start:end + 1]).get("steps")
    except Exception as error:  # noqa: BLE001 - a broken reply is the finding, not a crash
        return {"error": f"{type(error).__name__}: {error}"}
    if not isinstance(steps, list):
        return {"error": "no steps list in the reply"}
    for position, step in enumerate(steps, 1):
        if plan.clean_step(step) is None:
            return {"position": position, "of": len(steps), "step": step,
                    "kind": step.get("kind") if isinstance(step, dict) else None}
    return {"error": "every step is clean, so the rejection was elsewhere"}


def one(command: str) -> dict:
    recorder = Recorder()
    started = time.monotonic()
    out = plan.plan(command, transport=recorder, timeout=20.0)
    wall = round((time.monotonic() - started) * 1000)
    row = {"command": command, "status": out["status"], "reason": out["reason"],
           "steps": len(out["steps"]), "kinds": [s["kind"] for s in out["steps"]],
           "step_detail": out["steps"],
           "dropped": out.get("dropped") or [],
           "latency_ms": out["latency_ms"], "wall_ms": wall, "model": out["model"],
           "calls": recorder.calls}
    if out["status"] != "planned":
        row["killed_by"] = offending_step(recorder.raw)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--json", default="")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    # The plan cache would answer the second run for free, which would measure the cache.
    os.environ["JEV_MEMO"] = "off"

    runs = []
    for index in range(args.runs):
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(one, COMMANDS))
        runs.append(rows)
        planned = [r for r in rows if r["status"] == "planned"]
        print(f"── run {index + 1}: {len(planned)}/{len(rows)} planned "
              f"({100 * len(planned) // len(rows)}%)")

    rows = [r for run_rows in runs for r in run_rows]
    planned = [r for r in rows if r["status"] == "planned"]
    kills: dict = {}
    for row in rows:
        if row["status"] != "planned":
            key = (row["reason"], (row.get("killed_by") or {}).get("kind"))
            kills[key] = kills.get(key, 0) + 1
    print(f"\n{len(planned)}/{len(rows)} commands produced a plan")
    if planned:
        print(f"  steps per plan: {[r['steps'] for r in planned]}")
        print(f"  latency: median {int(statistics.median(r['latency_ms'] for r in planned))} ms")
    print("\nfallbacks, by reason and by the step kind that killed them:")
    for (reason, kind), count in sorted(kills.items(), key=lambda item: -item[1]):
        print(f"  {count:>3}  {reason:18} killed by kind={kind}")

    print("\nwhat killed each plan:")
    for row in rows:
        if row["status"] != "planned":
            print(f"  {row['reason']:14} {json.dumps(row.get('killed_by'))[:110]:112} {row['command'][:38]}")

    if args.json:
        Path(args.json).write_text(json.dumps(runs, indent=1), encoding="utf-8")
        print(f"\nraw rows: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
