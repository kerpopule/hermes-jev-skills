#!/usr/bin/env python3
"""Does skill selection's stage 2 earn its ~500 ms, or would stage 1 alone do?

Stage 1 ranks the whole catalog with one Choice per batch. Stage 2 is a second request:
`needs_skill` plus one Noul per finalist, and the plugin only acts on what stage 2 leaves
standing. Measured 2026-09-22, that second request costs about as much as the first, so the
question is what it buys.

This runs each labelled case live ONCE, keeps every raw stage-1 probability and stage-2
answer, and replays both policies offline:

    stage 1 + stage 2   what ships: the top verified skill, withheld if needs_skill is low
    stage 1 only        the highest-probability candidate above the shortlist floor

The number that matters is not accuracy but the two ways a suggestion fails: a WRONG
suggestion (the agent is sent to a procedure that does not fit) and a SPURIOUS one (a skill
offered for a turn that wants none). A missed suggestion is the quieter failure.

Cases are authored here on purpose: real fleet turns do not belong in a public repo, and an
authored case carries the expectation with it. The caveat travels with the numbers — the
expectations are one agent's judgement, so this measures agreement with a written intent,
not ground truth.

    python3 scripts/calibrate_skill_stage2.py            # ~14 merged + 14 verified calls
    python3 scripts/calibrate_skill_stage2.py --runs 3 --json /tmp/skill-stage2.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jevkit import privacy, skillpick, turn  # noqa: E402

# Authored cases. `expect` is the public skill the turn should reach, or None for a turn
# that wants no skill at all. Turns with an expectation are ordinary work that happens to
# need a procedure; the None cases are deliberately unremarkable, not trivial — a trivial
# turn is skipped locally and never reaches Jev, which is a different test.
CASES = [
    {"turn": "read this mailbox export and sort every message into a lane", "expect": "jev-mailbox"},
    {"turn": "cut this old transcript down to what still matters so it fits the window", "expect": "jev-compaction"},
    {"turn": "the filtered memory results came back mostly noise, thin them out for me", "expect": "jev-memory"},
    {"turn": "run this search and tell me which three results are actually worth opening", "expect": "jev-search"},
    {"turn": "click through the checkout flow in the browser and confirm each step", "expect": "jev-browser-use"},
    {"turn": "open System Settings on this Mac and switch the display resolution", "expect": "jev-computer-use"},
    {"turn": "should this turn go to the cheap model or the big one?", "expect": "jev-model-routing"},
    {"turn": "Jev says no_key, walk me through getting the key set up again", "expect": "jev-setup"},
    {"turn": "this is a genuinely hard architecture problem, which frontier seat should take it?", "expect": "jev-frontier-work"},
    {"turn": "which of my installed skills is the right procedure for this request?", "expect": "jev-skill-select"},
    {"turn": "what day is it today", "expect": None},
    {"turn": "rename the column in the changelog table", "expect": None},
    {"turn": "summarise the last three release notes", "expect": None},
    {"turn": "add a rate limit to that endpoint", "expect": None},
]


def catalog(roots: list) -> list:
    """The skills to rank. By default this public repo's own, so a clone reproduces the run."""
    return skillpick.discover(roots)


def stage_one_choice(case: dict, skills: list, stage_one: dict) -> tuple:
    """(skill name, probability) for the strongest stage-1 candidate, `none` excluded."""
    best, probability = None, 0.0
    for start, batch in stage_one.items():
        for key, value in batch.items():
            if key == "none":
                continue
            if value > probability:
                best, probability = skills[int(key[1:])]["name"], value
    return best, round(probability, 3)


def run_once(skills: list) -> list:
    rows = []
    for case in CASES:
        started = time.perf_counter()
        merged = turn.decide_turn(case["turn"], skills, profile="eval")
        if merged.get("status") != "ok":
            rows.append({**case, "error": merged.get("status") or merged.get("reason")})
            continue
        stage_one_name, stage_one_p = stage_one_choice(case, skills, merged["stage_one"])
        shipped = skillpick.pick(case["turn"], skills, top_k=1, stage_one=merged["stage_one"],
                                 stage_one_latency=merged.get("latency_ms"))
        picked = (shipped.get("skills") or [{}])[0].get("name")
        rows.append({
            "turn": case["turn"], "expect": case["expect"],
            "stage_one": stage_one_name, "stage_one_p": stage_one_p,
            "shipped": picked, "needs_skill": shipped.get("needs_skill"),
            "shipped_match": (shipped.get("skills") or [{}])[0].get("match"),
            "merged_ms": merged.get("latency_ms"), "total_ms": round((time.perf_counter() - started) * 1000),
            "floor": skillpick.SHORTLIST_FLOOR, "need_threshold": 0.5,
        })
    return rows


def verdict(row: dict, arm: str) -> str:
    """right / wrong / spurious / missed — the four ways a suggestion can land."""
    suggested = row.get(arm)
    expect = row.get("expect")
    if expect is None:
        return "right" if suggested is None else "spurious"
    if suggested is None:
        return "missed"
    return "right" if suggested == expect else "wrong"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--json", default="")
    parser.add_argument("--root", action="append", default=[],
                        help="a skills folder to rank (repeatable); default: this repo's skills/")
    args = parser.parse_args()

    roots = [Path(r) for r in args.root] or [Path(__file__).resolve().parents[1] / "skills"]
    skills = catalog(roots)
    names = [s["name"] for s in skills]
    print(f"catalog: {len(skills)} skills ({', '.join(names)})\n")

    all_runs = []
    for run in range(args.runs):
        rows = run_once(skills)
        all_runs.append(rows)
        print(f"── run {run + 1} " + "─" * 60)
        print(f"{'expect':22} {'stage1':22} {'s1_only':22} {'shipped':22} {'need':>5} {'ms':>6}")
        for row in rows:
            if row.get("error"):
                print(f"{'ERROR ' + row['error']:22} {row['turn'][:40]}")
                continue
            print(f"{str(row['expect']):22} {str(row['stage_one']):22} {str(row['stage_one']):22} "
                  f"{str(row['shipped']):22} {str(row['needs_skill']):>5} {row['total_ms']:>6}")

    print("\n── summary " + "─" * 60)
    for arm, label in (("stage_one", "stage 1 only"), ("shipped", "stage 1 + stage 2")):
        counts = {"right": 0, "wrong": 0, "spurious": 0, "missed": 0}
        for rows in all_runs:
            for row in rows:
                if not row.get("error"):
                    counts[verdict(row, arm)] += 1
        total = sum(counts.values())
        print(f"{label:18} n={total:<3} right {counts['right']:<3} WRONG {counts['wrong']:<3} "
              f"spurious {counts['spurious']:<3} missed {counts['missed']:<3}")

    latency = [r["total_ms"] for rows in all_runs for r in rows if not r.get("error")]
    if latency:
        latency.sort()
        print(f"\nper case, merged + verification: median {latency[len(latency) // 2]} ms")
    print("\ncases that disagreed between the two policies:")
    for rows in all_runs:
        for row in rows:
            if not row.get("error") and verdict(row, "stage_one") != verdict(row, "shipped"):
                print(f"  {row['turn'][:52]:54} stage1={row['stage_one']} ({row['stage_one_p']}) "
                      f"shipped={row['shipped']} (needs {row['needs_skill']}) expect={row['expect']}")
    if args.json:
        Path(args.json).write_text(json.dumps(all_runs, indent=1), encoding="utf-8")
        print(f"\nraw answers: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
