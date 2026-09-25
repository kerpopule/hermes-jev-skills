"""Score paired actual-result runs, not predictions or synthetic Jev answers.

Input JSONL: one row per task and arm. Required fields: task_id (public pseudonym),
arm ('baseline' or 'jev'), completed (human-verified bool), latency_ms (elapsed
wall time), usage_tokens (provider-reported nonnegative integer), spurious_skills
(nonnegative integer), needed_context_missed (nonnegative integer). The operator
must provide outcomes and provider-reported usage; this script never infers them.
"""
import argparse
import json
import statistics
from pathlib import Path

FIELDS = ("completed", "latency_ms", "usage_tokens", "spurious_skills", "needed_context_missed")


def compare(path):
    pairs = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        task, arm = row["task_id"], row["arm"]
        if not isinstance(task, str) or not task or arm not in ("baseline", "jev"):
            raise ValueError(f"line {number}: invalid task_id/arm")
        if (task, arm) in pairs:
            raise ValueError(f"line {number}: duplicate task/arm")
        if type(row["completed"]) is not bool:
            raise ValueError(f"line {number}: completed must be boolean")
        for field in FIELDS[1:]:
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
                raise ValueError(f"line {number}: invalid {field}")
        pairs[task, arm] = row
    tasks = sorted({task for task, _ in pairs})
    if not tasks or any((task, arm) not in pairs for task in tasks for arm in ("baseline", "jev")):
        raise ValueError("nonempty complete pairs required; missing arm")
    output = {"tasks": len(tasks), "source": str(path), "arms": {}}
    for arm in ("baseline", "jev"):
        rows = [pairs[task, arm] for task in tasks]
        output["arms"][arm] = {"completed": sum(r["completed"] for r in rows),
            "median_latency_ms": statistics.median(r["latency_ms"] for r in rows),
            "usage_tokens": sum(r["usage_tokens"] for r in rows),
            "spurious_skills": sum(r["spurious_skills"] for r in rows),
            "needed_context_missed": sum(r["needed_context_missed"] for r in rows)}
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", help="Paired, human-labelled actual-result JSONL")
    print(json.dumps(compare(parser.parse_args().results), indent=2))
