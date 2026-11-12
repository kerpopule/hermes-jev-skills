#!/usr/bin/env python3
"""Bounded synthetic nightmare corpus. Zero model calls; no live data or secrets.

Run: python3 scripts/nightmare_contract.py --record evidence/nightmare/trace.json
Then: python3 scripts/nightmare_contract.py --replay evidence/nightmare/trace.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jevkit import client
from jevkit.decision_trace import fingerprint, record, replay


def corpus():
    questions = {
        "noul": client.noul("A synthetic fixture is safe"),
        "box": {"type": "box", "instructions": "Locate the synthetic rectangle"},
        "choice": client.choice("Select a synthetic action", {"stop": "Refuse", "read": "Read fixture"}),
        "score": client.score("Synthetic evidence strength", ["None", "Some", "Complete"]),
    }
    good = {"noul": {"type": "noul", "noul": .5},
            "box": {"type": "box", "box": {"left": .1, "top": .1, "right": .9, "bottom": .9}, "confidence": 1},
            "choice": {"type": "choice", "choice": "stop", "confidence": 1, "probabilities": {"stop": .5, "read": .5}},
            "score": {"type": "score", "score": 1, "probabilities": {"0": .25, "1": .5, "2": .25}}}
    cases = []
    def add(kind, wire, valid=False):
        cases.append({"id": "fixture_%04d" % len(cases), "questions": {"q": questions[kind]},
                      "response": wire, "expected": "validated" if valid else "refused"})
    def answer(kind, item, valid=False):
        add(kind, json.dumps({"answers": {"q": item}}), valid)
    for kind in questions:
        answer(kind, good[kind], True)
        for value in (None, [], {}, True, "accept", 0):
            answer(kind, value)
        for wire in ('null', '[]', 'true', '{}', '{"answers":null}', '{"answers":[]}',
                     '{"answers":{}}', '{', 'not-json', '\udcff',
                     '{"answers":{"q":null,"q":{}}}',
                     '[' * 1500 + '0' + ']' * 1500):
            # Invalid UTF-8 surrogate cannot be represented as a normal byte fixture.
            if wire == '\udcff':
                continue
            add(kind, wire)
        wrong = dict(good[kind]); wrong["type"] = "unknown"
        answer(kind, wrong)
        add(kind, json.dumps({"answers": {"q": good[kind], "extra": good[kind]}}))
        wire = json.dumps({"answers": {"q": good[kind]}})
        add(kind, '{"answers":{},' + wire[1:])
    for edge in ("left", "top", "right", "bottom"):
        for value in (-1, 2, True, None, "0.5", 10**400):
            item = {**good["box"], "box": {**good["box"]["box"], edge: value}}
            answer("box", item)
    for i in range(33):
        answer("noul", {"type": "noul", "noul": i / 32}, True)
        p = i / 32
        answer("choice", {"type": "choice", "choice": "stop" if p >= .5 else "read",
                          "confidence": max(p, 1-p), "probabilities": {"stop": p, "read": 1-p}}, True)
        answer("score", {"type": "score", "score": 2*p, "probabilities": {"0": 1-p, "2": p}}, True)
    answer("score", {"type": "score", "score": 1, "probabilities": None}, True)
    for i in range(1, 161):
        answer("noul", {"type": "noul", "noul": -i / 100})
        answer("noul", {"type": "noul", "noul": 1 + i / 100})
        answer("choice", {"type": "choice", "choice": "stop", "confidence": 1, "probabilities": {"stop": .5, "read": .5, "invented_%d" % i: 0}})
        answer("score", {"type": "score", "score": 1, "probabilities": {str(i + 2): 1}})
    for bad in (None, True, "1", [], {}, float("inf"), float("nan"), 10**400):
        answer("noul", {"type": "noul", "noul": bad})
    for spread in ([], True, "1", 1, {"²": 1}, {"0": .4, "00": .6}, {"0": 10**400}):
        answer("score", {"type": "score", "score": 1, "probabilities": spread})
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", type=Path)
    group.add_argument("--replay", type=Path)
    args = parser.parse_args()
    cases = corpus()
    trace = record(cases)
    mismatches = [{"id": c["id"], "expected": c["expected"], "actual": row["outcome"]["status"]}
                  for c, row in zip(cases, trace["rows"])
                  if c["expected"] != row["outcome"]["status"]]
    result = {"cases": len(cases), "validated_controls": sum(c["expected"] == "validated" for c in cases),
              "mismatches": mismatches, "model_calls": 0, "execution_verified": False,
              "source_sha256": fingerprint({p: (ROOT / p).read_text() for p in
                    ("jevkit/client.py", "jevkit/decision_trace.py", "scripts/nightmare_contract.py")})}
    if args.record:
        if args.record.exists():
            parser.error("record target exists; choose a new explicit evidence file")
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(trace, indent=2) + "\n")
    else:
        result["replay"] = replay(cases, json.loads(args.replay.read_text()))
    print(json.dumps(result, indent=2))
    return int(bool(mismatches or result.get("replay", {}).get("changes")))


if __name__ == "__main__":
    raise SystemExit(main())
