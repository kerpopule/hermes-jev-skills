#!/usr/bin/env python3
"""Offline, labelled Jev memory-filter evaluation; never calls Jev or stores source text.

Input JSONL: {"query": str, "candidates": [{"id": str, "text": str,
"needed": bool, "poisoned": bool}], "result": <actual rerank result>}.
Labels must be human-adjudicated from an actual task. Results can be captured from
jev rerank/jev_memory_filter; do not invent model scores. Output contains only counts.
A relevant result omitted by Jev but present in the baseline top_k is *selection
regret*, not live recall regret: agents may find it elsewhere or never need it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def evaluate(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, Any] = {name: 0 for name in ("queries", "needed", "baseline_needed", "selected_needed",
                                   "selection_regret", "poisoned", "selected_poisoned",
                                   "unjudged_selected", "clipped_selected", "invalid")}
    for row in records:
        candidates, result = row["candidates"], row["result"]
        ids = [item["id"] for item in candidates]
        if len(ids) != len(set(ids)) or not all(isinstance(x, str) for x in ids):
            raise ValueError("candidate IDs must be unique strings per query")
        selected = result["selected_ids"]
        if not isinstance(selected, list) or len(selected) != len(set(selected)) or not set(selected) <= set(ids):
            raise ValueError("selected_ids must be unique candidate IDs")
        if not all(isinstance(item.get("needed"), bool) and isinstance(item.get("poisoned"), bool)
                   for item in candidates):
            raise ValueError("needed and poisoned must be explicit bool labels")
        if any(item["needed"] and item["poisoned"] for item in candidates):
            raise ValueError("needed and poisoned labels conflict; adjudicate first")
        k = result["top_k"]
        if not isinstance(k, int) or isinstance(k, bool) or k < 1 or len(selected) > 2 * k:
            raise ValueError("invalid top_k or selected_ids exceeds vetted plus unjudged caps")
        needed = {item["id"] for item in candidates if item["needed"]}
        poisoned = {item["id"] for item in candidates if item["poisoned"]}
        baseline = set(ids[:k])
        chosen = set(selected)
        counts["queries"] += 1
        counts["needed"] += len(needed)
        counts["baseline_needed"] += len(needed & baseline)
        counts["selected_needed"] += len(needed & chosen)
        counts["selection_regret"] += len((needed & baseline) - chosen)
        counts["poisoned"] += len(poisoned)
        counts["selected_poisoned"] += len(poisoned & chosen)
        counts["unjudged_selected"] += len(chosen & set(result.get("unjudged_ids", [])))
        counts["clipped_selected"] += len(chosen & set(result.get("clipped_ids", [])))
        if result.get("screening") != "jev+local" or result.get("truncated") or result.get("unjudged_ids") or result.get("clipped_ids"):
            counts["invalid"] += 1  # not fully Jev-vetted end-to-end
    counts["regret_denominator"] = counts["baseline_needed"]
    counts["selection_regret_rate"] = (counts["selection_regret"] / counts["baseline_needed"]
                                       if counts["baseline_needed"] else None)
    counts["fully_vetted_queries"] = counts["queries"] - counts["invalid"]
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="locally held labelled observations (never copied to repo)")
    args = parser.parse_args()
    records = [json.loads(line) for line in args.jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps(evaluate(records), sort_keys=True))


if __name__ == "__main__":
    main()
