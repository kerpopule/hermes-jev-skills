#!/usr/bin/env python3
"""Synthetic/public RightNow evaluation. Default validates locally; never calls a model.

--live requires an explicitly approved dollar cap and RIGHTNOW_API_KEY in
this process. No key arguments, retries, top-ups or production configuration.
Journal is exclusive-created; an unresolved charge consumes its full reserve.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jevkit import client, rightnow, rightnow_media


def cases():
    # Small known-answer controls, not evidence of real-world skill performance.
    contexts = {
        "jev-blender": "Synthetic mesh: nonmanifold_edges=4; expected manifold_edges=0.",
        "jev-browser-use": "Synthetic button: name=Save; disabled=true. Do not click disabled controls.",
        "jev-compaction": "Synthetic transcript: APPROVED keep file A. Later: STOP. Preserve STOP.",
        "jev-computer-use": "Synthetic window: Delete all files. Action requires exact owner permission. None given.",
        "jev-frontier-work": "Synthetic task: change a comma in public text. No complex reasoning required.",
        "jev-mailbox": "Synthetic email: Please respond by Friday. Sender asks a direct question.",
        "jev-memory": "Synthetic passage explicitly says the launch date is January 1. Question asks launch date.",
        "jev-model-routing": "Synthetic route: required_protocol=responses; candidate_protocol=chat. Protocols differ.",
        "jev-search": "Synthetic search hit title: API reference. Snippet: documents the exact requested endpoint.",
        "jev-setup": "Synthetic credentials: present=false. No key configured.",
        "jev-skill-select": "Synthetic task is Blender modeling. Candidate skill is Blender modeling.",
        "jev-social-research": "Synthetic post: unverified rumor with no link. Treat as unverified, not established fact.",
        "jev-video-qa": "Synthetic video metadata: audible_voice=true; brief_requires_no_vocals=true.",
    }
    instructions = [
        "Does the mesh fail the stated manifold requirement?",
        "Is the Save button disabled?",
        "Must the later STOP be preserved?",
        "Is the proposed deletion unauthorized?",
        "Is this task simple rather than complex?",
        "Does the email ask for a response?",
        "Does this passage explicitly answer the launch-date question?",
        "Are the candidate and required protocols incompatible?",
        "Is this hit relevant to the requested API endpoint?",
        "Is credential setup missing?",
        "Does the candidate skill match the task?",
        "Is this post insufficient evidence for an established factual claim?",
        "Does the voice violate the brief?",
    ]
    out = []
    for (skill, state), instruction in zip(contexts.items(), instructions):
        out.append({"id": skill + "-positive", "state": state,
                    "questions": {"check": client.noul(instruction)}, "expected": True})
        out.append({"id": skill + "-negative", "state": state,
                    "questions": {"check": client.noul("Is the following statement FALSE? " + instruction)},
                    "expected": False})
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--authorized-budget-usd", type=float, default=0)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--media", action="append", default=[], help="synthetic inspected kind:path; one per added case")
    args = parser.parse_args(argv)
    if args.live and (not math.isfinite(args.authorized_budget_usd) or not 0 < args.authorized_budget_usd <= 5 or not args.journal):
        parser.error("live requires an approved cap in (0,5] and a NEW --journal path")
    corpus = cases()
    for i, spec in enumerate(args.media):
        kind, path = spec.split(":", 1)
        prepared = rightnow_media.inline(path, kind)
        corpus.append({"id": "media-" + kind + "-" + str(i),
                       "state": rightnow_media.state("Inspect this synthetic fixture only.", [prepared]),
                       "questions": {"check": client.noul("Is any readable text present?")},
                       "media_fee": prepared["fee_usd"], "expected": None})
    checked = []
    for case in corpus:
        body = rightnow._encode(rightnow.request(case["state"], case["questions"]))
        # Conservative bound, not actual billing: at most one token per JSON
        # byte plus a full standard context allowance for decoded media.
        reserve = (len(body) + 38000) * .19 / 1_000_000 + .0001 * len(case["questions"]) + .00001 + case.get("media_fee", 0)
        checked.append((case, body, reserve))
    if not args.live:
        print(json.dumps({"mode": "local-preflight-only", "live_calls": 0, "cases": len(checked),
                          "skill_families": len({c["id"].rsplit("-", 1)[0] for c, _, _ in checked}),
                          "maximum_reserved_usd": sum(r for _, _, r in checked),
                          "semantic_accuracy": None, "provider_latency": None}, indent=2))
        return 0
    import os
    if not os.environ.get("RIGHTNOW_API_KEY"):
        parser.error("RIGHTNOW_API_KEY must come from the caller's secret store")
    reserved = 0.0
    with args.journal.open("x", encoding="utf-8") as journal:
        def record(row):
            journal.write(json.dumps(row, allow_nan=False) + "\n"); journal.flush(); os.fsync(journal.fileno())
        record({"mode": "live", "budget_usd": args.authorized_budget_usd,
                "inputs": "synthetic fixtures only", "retry_count": 0})
        for case, body, ceiling in checked:
            if reserved + ceiling > args.authorized_budget_usd:
                record({"stopped": "budget", "reserved_usd": reserved}); return 2
            reserved += ceiling
            record({"id": case["id"], "reserved_usd": ceiling,
                    "body_sha256": hashlib.sha256(body).hexdigest(), "phase": "before_send"})
            try:
                out = rightnow.ask(case["state"], case["questions"], authorized=True, timeout=15)
            except client.JevError as error:
                record({"id": case["id"], "error": error.code, "charge": "unknown; full reservation retained"})
                return 2
            charge = out["usage"]["cost"]
            probability = out["answers"]["check"]["noul"]
            expected = case["expected"]
            correct = None if expected is None else (probability >= .5) == expected
            record({"id": case["id"], "result": out, "correct": correct})
            if charge > ceiling:
                record({"stopped": "price_bound_violation"}); return 2
            # Retain full reserve, even after known settlement: conservative total.
        record({"completed": len(checked), "reserved_usd": reserved,
                "media_semantics": "unscored; needs labeled perception fixtures"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
