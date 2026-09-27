#!/usr/bin/env python3
"""A controlled lanes replay on real work: commits from a repo's own history, redone by Claude Code.

Each task is a past commit that changed one or two source files together with their tests. The
source files are put back to the parent commit, the tests stay, and the commit message is the
task. Success is deterministic: the commit's own tests pass and no test file was edited.

    python3 evals/lanes/replay_git.py pick   --repo . --n 12 --out tasks.jsonl
    python3 evals/lanes/replay_git.py run    --tasks tasks.jsonl --arm lanes --out lanes.jsonl
    python3 evals/lanes/replay_git.py run    --tasks tasks.jsonl --arm top   --out top.jsonl
    python3 evals/lanes/replay_git.py report --results lanes.jsonl top.jsonl

Arms:
  lanes  `jev lane classify` picks the first lane; after each attempt the deterministic check
         decides (loop-step pre-rules), escalating one lane at a time, at most --max-steps runs.
  top    every task on the top lane (Opus, high effort), one attempt, the usual default.
  floor  every task starts in the small lane (Haiku, low) and escalates on the same evidence as
         `lanes`: tests how far the lowest lane gets, and what escalation costs when it does not.

`claude -p` runs in a throwaway worktree with edits allowed and shell limited to python3 and git
read commands. Each run is capped by --budget-usd. Costs are Claude Code's own reported numbers.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))
from jevkit import lanes  # noqa: E402

SRC_PREFIX = "jevkit/"
TEST_PREFIX = "tests/test_"
ALLOWED_OTHER = ("CHANGELOG.md", "README.md", "docs/", "skills/")
TOOLS = "Read,Edit,Write,Glob,Grep,Bash(python3:*),Bash(git diff:*),Bash(git status:*)"


def sh(args: List[str], cwd: Path, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def numstat(repo: Path, commit: str) -> List[List[str]]:
    out = sh(["git", "show", "--numstat", "--format=", commit], repo).stdout
    return [line.split("\t") for line in out.splitlines() if line.count("\t") == 2]


def test_modules(files: List[str]) -> List[str]:
    return [f[:-3].replace("/", ".") for f in files if f.startswith(TEST_PREFIX) and f.endswith(".py")]


def check(worktree: Path, modules: List[str]) -> Dict[str, Any]:
    done = sh([sys.executable, "-m", "unittest", *modules], worktree, timeout=600)
    return {"passed": done.returncode == 0, "tail": lanes.tail(done.stderr + done.stdout)}


def worktree_at(repo: Path, commit: str, revert: List[str]) -> Path:
    folder = Path(tempfile.mkdtemp(prefix="lane-replay-"))
    sh(["git", "worktree", "add", "--detach", "-q", str(folder), commit], repo)
    for name in revert:
        sh(["git", "checkout", f"{commit}^", "--", name], folder)
    sh(["git", "add", "-A"], folder)
    sh(["git", "-c", "user.name=replay", "-c", "user.email=replay@example.invalid", "commit", "-qm", "task base"],
       folder)
    return folder


def drop(repo: Path, folder: Path) -> None:
    sh(["git", "worktree", "remove", "--force", str(folder)], repo)
    shutil.rmtree(folder, ignore_errors=True)


def cmd_pick(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    commits = sh(["git", "log", "--no-merges", "--format=%H", f"-{args.scan}"], repo).stdout.split()
    picked: List[Dict[str, Any]] = []
    for commit in commits:
        rows = numstat(repo, commit)
        src = [r for r in rows if r[2].startswith(SRC_PREFIX) and r[2].endswith(".py")]
        tests = [r[2] for r in rows if r[2].startswith(TEST_PREFIX)]
        other = [r for r in rows if r not in src and not r[2].startswith("tests/")
                 and not r[2].startswith(ALLOWED_OTHER)]
        lines = sum(int(a) + int(b) for a, b, _ in src if a.isdigit() and b.isdigit())
        if not (1 <= len(src) <= 2 and tests and not other and args.min_lines <= lines <= args.max_lines):
            continue
        modules = test_modules(tests)
        folder = worktree_at(repo, commit, [])
        good = check(folder, modules)["passed"]
        drop(repo, folder)
        if not good:
            continue
        folder = worktree_at(repo, commit, [r[2] for r in src])
        broken = not check(folder, modules)["passed"]
        drop(repo, folder)
        if not broken:
            continue
        message = sh(["git", "log", "-1", "--format=%B", commit], repo).stdout.strip()
        picked.append({"id": commit[:10], "commit": commit, "src": [r[2] for r in src], "tests": tests,
                       "modules": modules, "lines": lines, "message": message})
        print(f"picked {commit[:10]} {lines:>3} lines  {message.splitlines()[0][:70]}", file=sys.stderr)
        if len(picked) >= args.n:
            break
    with open(args.out, "w", encoding="utf-8") as handle:
        for row in picked:
            handle.write(json.dumps(row) + "\n")
    print(json.dumps({"tasks": len(picked), "out": args.out}))
    return 0


def prompt_for(task: Dict[str, Any], feedback: str = "") -> str:
    text = (f"Implement this change in the repository in the current directory:\n\n{task['message']}\n\n"
            f"The tests in {', '.join(task['tests'])} describe the expected behaviour and currently fail. "
            f"Change only the source under jevkit/ (most likely {', '.join(task['src'])}); do not edit any test. "
            f"Check with: python3 -m unittest {' '.join(task['modules'])}")
    if feedback:
        text += f"\n\nA previous attempt left these tests failing:\n{feedback}"
    return text


def run_claude(folder: Path, prompt: str, model: str, effort: str, budget: float) -> Dict[str, Any]:
    started = time.monotonic()
    done = subprocess.run(["claude", "-p", prompt, "--model", model, "--effort", effort, "--output-format", "json",
                           "--permission-mode", "acceptEdits", "--allowedTools", TOOLS,
                           "--max-budget-usd", str(budget)],
                          cwd=folder, capture_output=True, text=True, timeout=1800)
    try:
        data = json.loads(done.stdout)
    except ValueError:
        data = {"is_error": True, "result": (done.stderr or done.stdout)[-400:]}
    usage = data.get("usage") or {}
    return {"model": model, "effort": effort, "seconds": round(time.monotonic() - started, 1),
            "cost_usd": data.get("total_cost_usd"), "turns": data.get("num_turns"), "is_error": data.get("is_error"),
            "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_write_tokens": usage.get("cache_creation_input_tokens"),
            "model_usage": data.get("modelUsage")}


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    tasks = [json.loads(line) for line in Path(args.tasks).read_text().splitlines() if line.strip()]
    done_ids = set()
    if Path(args.out).exists():
        done_ids = {json.loads(line)["id"] for line in Path(args.out).read_text().splitlines() if line.strip()}
    mapped = lanes.targets("claude-code")
    for task in tasks:
        if task["id"] in done_ids:
            continue
        folder = worktree_at(repo, task["commit"], task["src"])
        steps: List[Dict[str, Any]] = []
        record: Dict[str, Any] = {"id": task["id"], "arm": args.arm, "lines": task["lines"]}
        try:
            if args.arm == "top":
                lane = "escalate"
                record["classified"] = None
            elif args.arm == "floor":
                lane = "small"
                record["classified"] = "small"
            else:
                first = lanes.classify(task["message"], record=False)
                lane = first["lane"] if first["lane"] in lanes.LANES else "high"
                record["classified"] = first["lane"]
            feedback = ""
            for attempt in range(1, args.max_steps + 1):
                target = mapped[lane]
                run = run_claude(folder, prompt_for(task, feedback), target["model"], target["effort"], args.budget_usd)
                result = check(folder, task["modules"])
                tests_edited = bool(sh(["git", "diff", "--name-only", "HEAD", "--", "tests/"], folder).stdout.strip())
                facts = {"checks_run": True, "checks_available": True, "checks_failed": 0 if result["passed"] else 1,
                         "out_of_scope_files": 1 if tests_edited else 0, "diff_empty": False, "expects_changes": True,
                         "security_changed": False}
                decision = lanes.step(task["message"], lane=lane, attempts=attempt, facts=facts,
                                      state={"checks": result["tail"]}, record=False) if args.arm != "top" else None
                steps.append({**run, "lane": lane, "passed": result["passed"], "tests_edited": tests_edited,
                              "decision": decision and {k: decision[k] for k in ("action", "lane", "source")}})
                if result["passed"] and not tests_edited:
                    break
                if args.arm == "top":
                    break
                if decision["action"] == "escalate":
                    if decision["lane"] not in lanes.LANES:
                        break
                    lane = decision["lane"]
                feedback = result["tail"]
        finally:
            drop(repo, folder)
        last = steps[-1]
        record.update({"steps": steps, "passed": last["passed"] and not last["tests_edited"],
                       "cost_usd": round(sum(s["cost_usd"] or 0 for s in steps), 4),
                       "final_lane": last["lane"], "attempts": len(steps)})
        with open(args.out, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(f"{task['id']} {args.arm}: {'PASS' if record['passed'] else 'FAIL'} lane={record['final_lane']} "
              f"attempts={record['attempts']} ${record['cost_usd']}", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    arms: Dict[str, List[Dict[str, Any]]] = {}
    for path in args.results:
        for line in Path(path).read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                arms.setdefault(row["arm"], []).append(row)
    out: Dict[str, Any] = {}
    for arm, rows in arms.items():
        tokens = sum((s.get("input_tokens") or 0) + (s.get("output_tokens") or 0) + (s.get("cache_write_tokens") or 0)
                     for r in rows for s in r["steps"])
        out[arm] = {"tasks": len(rows), "passed": sum(r["passed"] for r in rows),
                    "cost_usd": round(sum(r["cost_usd"] for r in rows), 3),
                    "tokens_excl_cache_reads": tokens,
                    "cache_read_tokens": sum((s.get("cache_read_tokens") or 0) for r in rows for s in r["steps"]),
                    "seconds": round(sum(s["seconds"] for r in rows for s in r["steps"]), 1),
                    "first_lane": dict(sorted({str(r.get("classified")): sum(1 for x in rows if x.get("classified") == r.get("classified")) for r in rows}.items())),
                    "final_lane": dict(sorted({r["final_lane"]: sum(1 for x in rows if x["final_lane"] == r["final_lane"]) for r in rows}.items())),
                    "escalated": sum(1 for r in rows if r["attempts"] > 1)}
    both = set.intersection(*[{r["id"] for r in rows} for rows in arms.values()]) if len(arms) > 1 else set()
    if both and "top" in arms:
        top_rows = {r["id"]: r for r in arms["top"]}
        paired_t = sum(top_rows[i]["cost_usd"] for i in both)
        for arm in sorted(set(arms) - {"top"}):
            rows = {r["id"]: r for r in arms[arm]}
            paired = sum(rows[i]["cost_usd"] for i in both)
            out[f"paired_{arm}_vs_top"] = {
                "tasks": len(both), f"{arm}_passed": sum(rows[i]["passed"] for i in both),
                "top_passed": sum(top_rows[i]["passed"] for i in both), f"{arm}_cost_usd": round(paired, 3),
                "top_cost_usd": round(paired_t, 3), "cost_change": round(paired / paired_t - 1, 3) if paired_t else None}
    print(json.dumps(out, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--repo", default=".")
    p.add_argument("--n", type=int, default=12)
    p.add_argument("--scan", type=int, default=400)
    p.add_argument("--min-lines", type=int, default=4)
    p.add_argument("--max-lines", type=int, default=80)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_pick)
    p = sub.add_parser("run")
    p.add_argument("--repo", default=".")
    p.add_argument("--tasks", required=True)
    p.add_argument("--arm", choices=["lanes", "top", "floor"], required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--budget-usd", type=float, default=3.0)
    p.add_argument("--max-steps", type=int, default=3)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--results", nargs="+", required=True)
    p.set_defaults(func=cmd_report)
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
