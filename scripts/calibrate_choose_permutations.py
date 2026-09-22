#!/usr/bin/env python3
"""Does permutation averaging beat one option order for `choose`? Measure it, do not trust it.

`TypeLLM/pijev` wraps the TypeSafe SDK on the claim that a Choice answer wobbles with the
order the options are listed in, and fixes it by asking the same question in several orders
in ONE request and averaging the probability vectors (aligned by label, each normalized to
sum to one). If that holds, `choose` would be steadier for free — the table is already built,
the state is already sent.

The A/B, on the 31 labelled cases from `scripts/calibrate_choose.py`, live once per case:

  * solo      one request, one question — production shape today
  * batch     one request: the canonical question plus up to 8 sampled orders (the treatment)
  * every arm replayed offline from the kept raw answers: the canonical answer alone (also
    inside the batch, which prices pijev's own known confound), the averaged answer at
    2/3/5/9 orders with its confidence swap (the winner's MEAN probability, not Jev's own),
    decision-only averaging (averaged winner, native confidence), and each single order
    scored alone as the noise being averaged.

The bar, stated before the numbers were seen:

    Averaging is adopted only if, at the 0.65 floor, it matches or beats solo on WRONG
    ACTIONS (solo's record is zero per run) AND strictly beats it on stalls or on Brier
    loss for the right label. Anything else stays a finding, not a change.

`--trap-repeat` re-asks just the known trap ("Cancel this dialog without losing my work",
the one wrong answer in this corpus's history) with fresh order samples, because one draw
of nine orders is one draw.

    python3 scripts/calibrate_choose_permutations.py                    # ~$0.01 of Jev calls
    python3 scripts/calibrate_choose_permutations.py --trap-repeat
    python3 scripts/calibrate_choose_permutations.py --replay /tmp/perm-avg-choose-raw.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jevkit import client  # noqa: E402
import calibrate_choose as corpus  # noqa: E402

INSTRUCTIONS = "Which single action should be taken next to move toward the goal?"
N_EXTRA = 8          # sampled orders per case, plus the canonical one
SEED = 20260922
TRAP_SEED = 777
RAW = "/tmp/perm-avg-choose-raw.json"
TRAP_RAW = "/tmp/perm-avg-trap-repeat.json"


def question(items):
    return {"type": "choice", "instructions": INSTRUCTIONS, "criteria": dict(items)}


def state_of(case: dict) -> dict:
    request = case["request"]
    return {"goal": request["goal"], "on_screen": request["regions"], "already_tried": request["history"]}


def orders_for(table, seed: int, extra: int = N_EXTRA) -> list:
    """The canonical table order first, then `extra` distinct sampled ones."""
    pool = [p for p in permutations(table) if p != tuple(table)]
    random.Random(seed).shuffle(pool)
    return [tuple(table)] + pool[:min(extra, len(pool))]


def mean_probs(answers: dict, names: list) -> dict:
    """The pijev aggregate: each answer's vector normalized to sum one, then averaged by label."""
    vecs = []
    for name in names:
        probs = answers[name]["probabilities"]
        total = sum(probs.values())
        vecs.append({k: v / total for k, v in probs.items()} if total else dict(probs))
    if not vecs:
        return {}
    keys = set().union(*vecs)
    return {k: sum(v.get(k, 0.0) for v in vecs) / len(vecs) for k in keys}


def winner(probs: dict) -> str:
    """Highest probability; ties go to the lexicographically first label, deterministically."""
    return max(sorted(probs), key=lambda k: probs[k])


def average_answer(answers: dict, names: list) -> tuple:
    """(label, confidence) under pijev semantics — note confidence is the winner's MEAN
    probability, not the `confidence` field Jev returned. That swap is what this eval prices."""
    probs = mean_probs(answers, names)
    top = winner(probs)
    return top, probs[top]


def arm_rows(rows, pick):
    out = []
    for row in rows:
        if "error" in row:
            continue
        raw, confidence = pick(row)
        out.append({"kind": row["kind"], "goal": row["goal"], "right": row["right"],
                    "raw": raw, "confidence": confidence, "reason": ""})
    return out


def brier(rows, pick) -> float:
    """Mean Brier loss against the right label, right-labelled cases only."""
    total, count = 0.0, 0
    for row in rows:
        if "error" in row or row["right"] is None:
            continue
        probs = pick(row)
        total += sum((v - (1.0 if k == row["right"] else 0.0)) ** 2 for k, v in probs.items())
        count += 1
    return total / count if count else 0.0


def collect() -> list:
    rows = []

    def one(index_case):
        index, case = index_case
        orders = orders_for([(c["id"], c["description"]) for c in case["request"]["candidates"]], SEED + index)
        state = state_of(case)
        try:
            solo = client.ask(state, {"next_action": question(orders[0])}, timeout=10.0)
            questions = {"next_action": question(orders[0])}
            questions.update({f"__p{i}": question(o) for i, o in enumerate(orders[1:], 1)})
            batch = client.ask(state, questions, timeout=20.0)
        except client.JevError as error:
            return {**case, "index": index, "error": f"{error.code}: {error}"}
        return {**case, "index": index,
                "orders": [[i for i, _ in o] for o in orders],
                "solo": solo["answers"], "batch": batch["answers"],
                "usage": {"solo": solo["usage"], "batch": batch["usage"]},
                "latency_ms": {"solo": solo["latency_ms"], "batch": batch["latency_ms"]}}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for row in pool.map(one, enumerate(corpus.CASES)):
            if "error" in row:
                print(f"[{row['index'] + 1}/{len(corpus.CASES)}] ERROR {row['error']}")
            else:
                pick = row["solo"]["next_action"]
                print(f"[{row['index'] + 1}/{len(corpus.CASES)}] {row['kind']:11} "
                      f"solo={pick['choice']:22} @{pick['confidence']:.2f}")
            rows.append(row)
    rows.sort(key=lambda r: r["index"])
    Path(RAW).write_text(json.dumps(rows, indent=1))
    print(f"\nraw answers kept: {RAW}")
    return rows


def trap_repeat() -> list:
    """The one known wrong answer, re-asked with fresh order samples."""
    case = next(c for c in corpus.CASES if c["goal"].startswith("Cancel this dialog"))
    table = [(c["id"], c["description"]) for c in case["request"]["candidates"]]
    state = state_of(case)
    rows = []
    for run in range(3):
        orders = orders_for(table, TRAP_SEED + run)
        try:
            solo = client.ask(state, {"next_action": question(orders[0])}, timeout=10.0)["answers"]["next_action"]
            questions = {"next_action": question(orders[0])}
            questions.update({f"__p{i}": question(o) for i, o in enumerate(orders[1:], 1)})
            answers = client.ask(state, questions, timeout=20.0)["answers"]
        except client.JevError as error:
            print(f"run {run + 1}: ERROR {error.code}: {error}")
            continue
        split = {}
        for name in answers:
            split[answers[name]["choice"]] = split.get(answers[name]["choice"], 0) + 1
        top, confidence = average_answer(answers, list(answers))
        rows.append({"run": run + 1, "solo": solo, "answers": answers,
                     "orders": [[i for i, _ in o] for o in orders]})
        print(f"run {run + 1}: solo={solo['choice']}@{solo['confidence']:.2f}  split={split}  "
              f"averaged={top}@{confidence:.3f}")
    Path(TRAP_RAW).write_text(json.dumps(rows, indent=1))
    print(f"raw answers kept: {TRAP_RAW}")
    return rows


def analyze(rows) -> None:
    batch_names = lambda r: list(r["batch"])  # noqa: E731

    arms = {
        "A solo": arm_rows(rows, lambda r: (r["solo"]["next_action"]["choice"], r["solo"]["next_action"]["confidence"])),
        "A' batched-canon": arm_rows(rows, lambda r: (r["batch"]["next_action"]["choice"], r["batch"]["next_action"]["confidence"])),
    }
    briers = {
        "A solo": brier(rows, lambda r: r["solo"]["next_action"]["probabilities"]),
        "A' batched-canon": brier(rows, lambda r: r["batch"]["next_action"]["probabilities"]),
    }
    for m in (2, 3, 5, 9):
        names = lambda r, m=m: batch_names(r)[:m]  # noqa: E731
        arms[f"B{m} averaged"] = arm_rows(rows, lambda r, ns=names: average_answer(r["batch"], ns(r)))
        briers[f"B{m} averaged"] = brier(rows, lambda r, ns=names: mean_probs(r["batch"], ns(r)))
        arms[f"C{m} decision-only"] = arm_rows(
            rows, lambda r, ns=names: (winner(mean_probs(r["batch"], ns(r))), r["solo"]["next_action"]["confidence"]))
        briers[f"C{m} decision-only"] = briers[f"B{m} averaged"]

    floors = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
    print(f"\n{'arm':<18} {'Brier':>6} " + " ".join(f"{f:>7}" for f in floors))
    print(f"{'':<18} {'':>6} " + " right/stalled/declined/wrong at each floor")
    for name, arm in arms.items():
        cells = []
        for floor in floors:
            s = corpus.score(arm, floor)
            star = "*" if abs(floor - 0.65) < 1e-9 else " "
            cells.append(f"{s['right']}/{s['stalled']}/{s['declined_ok']}/{s['wrong_action']}{star}")
        print(f"{name:<18} {briers[name]:>6.3f} " + " ".join(f"{c:>7}" for c in cells))

    print("\nrandom single order (each batch order scored alone — the noise being averaged):")
    for name in ["next_action"] + [f"__p{i}" for i in range(1, 9)]:
        arm = arm_rows([r for r in rows if "error" not in r and name in r["batch"]],
                       lambda r, n=name: (r["batch"][n]["choice"], r["batch"][n]["confidence"]))
        if not arm:
            continue
        s65 = corpus.score(arm, 0.65)
        print(f"        {name:10} right {s65['right']:2} stalled {s65['stalled']:2} "
              f"declined {s65['declined_ok']:2} WRONG {s65['wrong_action']:2}")

    print("\norder sensitivity per case (spread of p(right) across the orders sent):")
    for row in rows:
        if "error" in row or row["right"] is None:
            continue
        ps = [row["batch"][n]["probabilities"].get(row["right"], 0.0) for n in batch_names(row)]
        tops = {row["batch"][n]["choice"] for n in batch_names(row)}
        if max(ps) - min(ps) > 0.05 or len(tops) > 1:
            print(f"  p({row['right']:>18}) min {min(ps):.2f} max {max(ps):.2f}  "
                  f"tops: {len(tops)} distinct  {row['goal'][:44]}")

    print(f"\ndecision flips solo -> B5 (what averaging would change about today's answers):")
    flipped = 0
    for row in rows:
        if "error" in row:
            continue
        a = row["solo"]["next_action"]["choice"]
        b = winner(mean_probs(row["batch"], batch_names(row)[:5]))
        if a != b:
            flipped += 1
            a_hold, b_hold = a in corpus.SAFE, b in corpus.SAFE
            print(f"  {a:22} -> {b:22} solo={'holds' if a_hold else 'acts'} "
                  f"vs avg={'holds' if b_hold else 'acts'}  {row['goal'][:40]}")
    if not flipped:
        print("  none")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", metavar="RAW", help="re-analyze kept raw answers; no calls")
    parser.add_argument("--trap-repeat", action="store_true",
                        help="re-ask just the known trap with fresh order samples")
    args = parser.parse_args()
    if args.trap_repeat:
        trap_repeat()
        return 0
    rows = json.loads(Path(args.replay).read_text()) if args.replay else collect()
    analyze(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
