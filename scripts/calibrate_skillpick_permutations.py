#!/usr/bin/env python3
"""Does option order move skillpick's stage-1 ranking, and does averaging help there?

Spike context: `scripts/calibrate_choose_permutations.py` found order sensitivity almost
nowhere for 5-option `choose` tables and pijev-style averaging actively harmful on the one
torn case. skillpick's stage 1 ranks 121 options per batch — long lists are where order
effects are claimed to bite — and its stage-1 arm carries measured failures to move (the
379-skill stage-2 run: 2 wrong + 8 spurious stage-1 verdicts per 28 case-decisions).

Per case (the 14 authored ones from `scripts/calibrate_skill_stage2.py`), live ONCE:
  * a production-shaped request (canonical batch questions, as the merged W1 call ships)
  * a shuffle request (canonical + 2 fresh shuffles per batch, one question each)
Raw kept; all arms replay offline: canonical, averaged (mean of normalized vectors),
each shuffle alone.

The bar, stated before the numbers were seen:

    Averaging is adopted for stage 1 only if it produces ZERO wrong and STRICTLY fewer
    spurious stage-1 verdicts than canonical, with no more misses. Else: a finding.

Stage 2 is deliberately not replayed here: it answers `noul` questions, which carry no
option order to permute. Verdicts are stage-1 top-1, counted exactly as
`calibrate_skill_stage2.verdict` counts them.

    python3 scripts/calibrate_skillpick_permutations.py
    python3 scripts/calibrate_skillpick_permutations.py --replay /tmp/perm-avg-skillpick-raw.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jevkit import privacy, skillpick  # noqa: E402
import calibrate_skill_stage2 as corpus  # noqa: E402

N_SHUFFLES = 2
SEED = 20260922
RAW = "/tmp/perm-avg-skillpick-raw.json"


def catalogs() -> list:
    """The fleet catalog when this machine has one; the repo's own skills otherwise."""
    skills = skillpick.discover(skillpick.discover_roots(Path.home() / ".hermes"))
    return skills or skillpick.discover([Path(__file__).resolve().parents[1] / "skills"])


def shuffled_questions(catalog: list, seed: int) -> tuple:
    """Canonical + N_SHUFFLES fresh orders per batch: ({name: question}, {name: option order})."""
    questions, orders = {}, {}
    for start in range(0, len(catalog), skillpick.BATCH):
        options = skillpick._options(catalog, start)
        questions[skillpick._pick_name(start)] = skillpick.client.choice(
            skillpick._PICK_INSTRUCTION, dict(options))
        orders[skillpick._pick_name(start)] = list(options)
        for s in range(N_SHUFFLES):
            items = list(options.items())
            random.Random(seed + start + s * 1000).shuffle(items)
            name = f"shuf:{start}:{s}"
            questions[name] = skillpick.client.choice(skillpick._PICK_INSTRUCTION, dict(items))
            orders[name] = [key for key, _ in items]
    return questions, orders


def collect(skills: list) -> list:
    rows = []
    total = len(corpus.CASES)

    def one(index_case):
        index, case = index_case
        state = {"turn": privacy.redact(case["turn"], 2000)}
        try:
            prod = skillpick.client.ask(state, skillpick.stage_one_questions(skills), timeout=30.0)
            questions, orders = shuffled_questions(skills, SEED + index * 100)
            shuf = skillpick.client.ask(state, questions, timeout=60.0)
        except skillpick.client.JevError as error:
            return {**case, "index": index, "error": f"{error.code}: {error}"}
        return {**case, "index": index, "orders": orders,
                "prod": {n: a["probabilities"] for n, a in prod["answers"].items()},
                "shuf": {n: a["probabilities"] for n, a in shuf["answers"].items()},
                "latency_ms": {"prod": prod["latency_ms"], "shuf": shuf["latency_ms"]}}

    with ThreadPoolExecutor(max_workers=3) as pool:
        for row in pool.map(one, enumerate(corpus.CASES)):
            label = ("ERROR " + row["error"]) if "error" in row else row["turn"][:60]
            print(f"[{len(rows) + 1}/{total}] {label}")
            rows.append(row)
    rows.sort(key=lambda r: r["index"])
    Path(RAW).write_text(json.dumps(rows, indent=1))
    print(f"\nraw answers kept: {RAW}")
    return rows


def normalize(probs: dict) -> dict:
    """One order's vector scaled to sum one. The pijev aggregate averages these."""
    total = sum(probs.values())
    return {k: v / total for k, v in probs.items()} if total else dict(probs)


def batch_start(name: str) -> int:
    return int(name.split(":")[1])


def averaged_stage_one(row, names) -> dict:
    """{batch start: mean vector over `names`} as `skillpick.pick(stage_one=...)` takes it."""
    per_batch = {}
    for name in names:
        per_batch.setdefault(batch_start(name), []).append(name)
    out = {}
    for start, group in per_batch.items():
        vecs = [normalize(row["shuf"][n]) for n in group]
        keys = set().union(*vecs)
        out[start] = {k: sum(v.get(k, 0.0) for v in vecs) / len(vecs) for k in keys}
    return out


def canonical_stage_one(row) -> dict:
    return {batch_start(n): probs for n, probs in row["prod"].items()}


def shuffle_stage_one(row, s: str) -> dict:
    return {batch_start(n): row["shuf"][n] for n in row["shuf"]
            if n.startswith("shuf:") and n.split(":")[2] == s}


def rank_top(stage_one: dict, skills: list, k: int = 1) -> list:
    ranked = []
    for batch in stage_one.values():
        for option, probability in batch.items():
            if option != "none":
                ranked.append((probability, int(option[1:])))
    ranked.sort(reverse=True)
    return [(skills[index]["name"], probability) for probability, index in ranked[:k]
            if probability >= skillpick.SHORTLIST_FLOOR]


def analyze(rows, skills: list) -> None:
    shuf_names = [n for n in (rows[0].get("orders") or {}) if n.startswith("shuf:")]

    def verdict_pairs(stage_one_fn):
        pairs = []
        for row in rows:
            if "error" in row:
                continue
            top = rank_top(stage_one_fn(row), skills, 1)
            pairs.append((top[0][0] if top else None, row["expect"]))
        return pairs

    def counts_of(pairs):
        counts = {"right": 0, "wrong": 0, "spurious": 0, "missed": 0}
        for picked, expect in pairs:
            counts[corpus.verdict({"expect": expect, "v": picked}, "v")] += 1
        return counts

    print(f"\n{'arm':<22} right wrong spurious missed   (stage-1 top-1 verdicts)")
    all_names = lambda row: [m for m in row["shuf"] if m.startswith(("pick:", "shuf:"))]  # noqa: E731
    arms = [("A canonical", canonical_stage_one),
            ("B averaged", lambda row: averaged_stage_one(row, all_names(row)))]
    for s in sorted({n.split(":")[2] for n in shuf_names}):
        arms.append((f"rnd shuffle {s}", (lambda row, s=s: shuffle_stage_one(row, s))))
    summary = {}
    for name, fn in arms:
        counts = counts_of(verdict_pairs(fn))
        summary[name] = counts
        print(f"{name:<22} {counts['right']:>4} {counts['wrong']:>5} {counts['spurious']:>8} {counts['missed']:>7}")

    print("\nper case — canonical top -> averaged top; expected skill's spread within its own batch:")
    for row in rows:
        if "error" in row:
            continue
        a = rank_top(canonical_stage_one(row), skills, 1)
        b = rank_top(averaged_stage_one(row, all_names(row)), skills, 1)
        a_name, b_name = (a[0][0] if a else None), (b[0][0] if b else None)
        spread = ""
        if row["expect"]:
            index = next((i for i, s in enumerate(skills) if s["name"] == row["expect"]), None)
            if index is not None:
                want, start = f"S{index}", (index // skillpick.BATCH) * skillpick.BATCH
                group = [n for n in row["shuf"] if batch_start(n) == start]
                ps = [row["shuf"][n].get(want, 0.0) for n in group]
                tops = set()
                for n in group:
                    d = {k: v for k, v in row["shuf"][n].items() if k != "none"}
                    tops.add(max(sorted(d), key=lambda k: d[k]))
                spread = f"  p({row['expect']}) min {min(ps):.3f} max {max(ps):.3f}, in-batch tops {len(tops)}"
        mark = "  FLIP" if a_name != b_name else ""
        print(f"  {str(a_name):28} -> {str(b_name):28}{mark}{spread}  {row['turn'][:30]}")

    prod_ms = sorted(r["latency_ms"]["prod"] for r in rows if "latency_ms" in r)
    shuf_ms = sorted(r["latency_ms"]["shuf"] for r in rows if "latency_ms" in r)
    if prod_ms:
        print(f"\nlatency: production request median {prod_ms[len(prod_ms) // 2]} ms, "
              f"shuffle request (3x questions) median {shuf_ms[len(shuf_ms) // 2]} ms")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", metavar="RAW", help="re-analyze kept raw answers; no calls")
    args = parser.parse_args()
    skills = catalogs()
    print(f"catalog: {len(skills)} skills")
    rows = json.loads(Path(args.replay).read_text()) if args.replay else collect(skills)
    analyze(rows, skills)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
