#!/usr/bin/env python3
"""Does a SECOND question buy fewer wrong actions? Measure it, do not assume it.

Stagehand's Act primitive gates a Jev decision on two questions, not one: *which
candidate is best*, and *does any candidate match the goal at all*, accepting at
0.7 and falling back to a language model otherwise. This repo gates on one number,
`choose.MIN_CONFIDENCE` (0.65). Those are different failures wearing the same
probability:

  * "the best candidate is weak"   - Jev saw the right action and was unsure
  * "no candidate serves the goal" - the right action is not on the table at all

A single `confidence` cannot tell them apart, and the floor has to cover both. The
floor's own history says this is where the cost sits: it was set to 0.65 by hand
after 0.80 threw away a correct answer at 0.74, and the one known wrong answer was
wrong 9 runs in 10 but never above 0.58 - a 0.07 margin inside +-0.08 of noise.

So: ask both questions in ONE request (the second is nearly free - same state,
same call), keep the raw answer, and replay every gate offline.

    floor only      act iff conf >= floor
    floor + match   act iff conf >= floor AND noul >= match

What matters is WRONG ACTIONS at an equal or lower stall count. A gate that trades
one wrong click for ten stalls is not an improvement; a stall costs a step.

    python3 scripts/calibrate_choose_match.py

One live call per case, both questions in the same request, so the second one costs
nothing beyond the tokens it adds to a call this repo already makes.

The cases, and the meaning of each kind, are the ones `calibrate_choose.py` uses -
imported rather than copied, so the two calibrations can never drift apart.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from jevkit import choose, client  # noqa: E402

# The two-question gate, phrased as Stagehand asks it. The safety valves are named
# explicitly because they are always on the table and always "match" in the weak
# sense; a question that accepts them answers yes on every screen ever observed.
MATCH_QUESTION = ("Is one of the candidate actions, other than `reobserve` and `abstain`, "
                  "the correct next action for the goal - advancing it without a wrong, "
                  "irreversible or destructive effect?")


def _cases():
    spec = importlib.util.spec_from_file_location("calibrate_choose", Path(__file__).with_name("calibrate_choose.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CASES


def run(case):
    """One live call, both questions, raw answers kept for offline replay."""
    floor, choose.MIN_CONFIDENCE = choose.MIN_CONFIDENCE, 0.0     # see the raw answer
    try:
        valid = choose.validate(case["request"])
    finally:
        choose.MIN_CONFIDENCE = floor
    state = {"goal": valid["goal"], "on_screen": valid["regions"], "already_tried": valid["history"]}
    questions = {
        "next_action": client.choice("Which single action should be taken next to move toward the goal?",
                                     valid["table"]),
        "any_match": client.noul(MATCH_QUESTION),
    }
    row = {k: case[k] for k in ("kind", "goal", "right")}
    row.update({"raw": None, "confidence": 0.0, "match": None, "error": None})
    try:
        reply = client.ask(state, questions, timeout=10.0)
    except client.JevError as error:
        row["error"] = f"Jev unavailable ({error.code})"
        return row
    picked = reply["answers"]["next_action"]
    row.update({"raw": picked["choice"], "confidence": picked["confidence"],
                "match": reply["answers"]["any_match"]["noul"],
                "latency_ms": reply.get("latency_ms")})
    return row


SAFE = {"reobserve", "abstain"}


def score(rows, floor, match):
    """What would have happened at this gate. `match=None` is the gate we ship today."""
    out = {"right": 0, "stalled": 0, "wrong_action": 0, "declined_ok": 0}
    for r in rows:
        acted = r["confidence"] >= floor and r["raw"] not in SAFE
        if match is not None:
            acted = acted and (r["match"] or 0.0) >= match
        if r["right"] is None:
            out["wrong_action" if acted else "declined_ok"] += 1
        elif not acted:
            out["stalled"] += 1
        elif r["raw"] == r["right"]:
            out["right"] += 1
        else:
            out["wrong_action"] += 1
    return out


def split(rows):
    """The rows with both answers, and the rows whose call failed.

    A successful row carries its own reason for acting, so "has a reason" is not the
    test for failure: the first version of this script filtered on truthiness and
    reported all 31 good rows as failures.
    """
    return ([r for r in rows if not r["error"]], [r for r in rows if r["error"]])


def main() -> int:
    rows = [r for r in ThreadPoolExecutor(max_workers=4).map(run, _cases())]
    answered, failed = split(rows)
    if failed:
        print(f"{len(failed)} call(s) failed ({failed[0]['error']}); results below exclude nothing, treat with care")
    if not answered:
        print("no usable rows - is the key present and Jev reachable?")
        return 1

    kinds = {}
    for r in answered:
        kinds.setdefault(r["kind"], []).append(r)
    print(f"{len(answered)} cases with both answers\n")
    print("Does the match question separate the kinds at all?")
    print(f"{'kind':>10} {'n':>3} {'match min':>10} {'median':>7} {'max':>6} {'mean conf':>10}")
    for kind, group in sorted(kinds.items()):
        vals = sorted(r["match"] or 0.0 for r in group)
        confs = sum(r["confidence"] for r in group) / len(group)
        print(f"{kind:>10} {len(group):>3} {vals[0]:>10.2f} {vals[len(vals) // 2]:>7.2f} "
              f"{vals[-1]:>6.2f} {confs:>10.2f}")

    print("\nGates. `-` is the single-question floor this repo ships.")
    print(f"{'floor':>6} {'match':>6} {'right':>6} {'stalled':>8} {'declined ok':>12} {'WRONG ACTIONS':>14}")
    for floor in (0.60, 0.65, 0.70):
        for match in (None, 0.50, 0.60, 0.70, 0.80, 0.90):
            s = score(answered, floor, match)
            mark = "  <- current" if match is None and abs(floor - choose.MIN_CONFIDENCE) < 1e-9 else ""
            print(f"{floor:>6.2f} {('-' if match is None else f'{match:.2f}'):>6} {s['right']:>6} "
                  f"{s['stalled']:>8} {s['declined_ok']:>12} {s['wrong_action']:>14}{mark}")

    print("\nThe match question alone, ignoring confidence (is it the better single gate?):")
    for match in (0.50, 0.60, 0.70, 0.80, 0.90):
        s = score(answered, 0.0, match)
        print(f"{'  noul':>6} {match:>6.2f} {s['right']:>6} {s['stalled']:>8} "
              f"{s['declined_ok']:>12} {s['wrong_action']:>14}")

    print("\nEvery case, by how much the two answers disagree:")
    for r in sorted(answered, key=lambda r: (r["match"] or 0.0) - r["confidence"]):
        verdict = "no answer exists" if r["right"] is None else (
            "right" if r["raw"] == r["right"] else "WRONG")
        print(f"  conf {r['confidence']:.2f}  match {r['match']:.2f}  {r['kind']:>9}  "
              f"{verdict:<15} {r['goal'][:46]}")

    Path("/tmp/calibrate_choose_match.json").write_text(json.dumps(answered, indent=1))
    print("\nraw rows: /tmp/calibrate_choose_match.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
