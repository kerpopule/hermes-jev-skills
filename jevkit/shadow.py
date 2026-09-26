"""Did a shadow earn its switch? Decisions joined to what really happened, checked by machine.

A feature leaves shadow only when a report computed from real outcomes says it passed the
criteria written in its policy's ``promotion`` block *before* the data came in. This module
is that report: confusion tables against the truth and against the current mechanism, raw
agreement, Cohen's kappa, Wilson 95% intervals, the count of the one error that matters most,
and every promotion criterion printed PASS, FAIL or UNKNOWN (UNKNOWN when the data to judge it
is not in the rows — never a silent pass).

Rows are ``jev batch`` output (or joined shadow logs): ``{"id", "decision": {...}, "truth",
"current", "baseline", ...}``. The truth vocabulary per feature is in ``TRUTH``.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

Z95 = 1.959963984540054
TRUTH = {
    "gate": "approve | deny (the person's own choice; timeouts left out)",
    "cron_wake": "report | silent (what the agent run said); optional report_kind: failure | decision | other",
    "retry": "completed | failed_again (the next run's outcome)",
    "blockcheck": "needed_owner | not_owner (how the block was really cleared)",
    "owner": "the profile that completed the card; creator = the creator's pick",
}


def wilson(successes: int, n: int, z: float = Z95) -> Tuple[Optional[float], Optional[float]]:
    """Wilson score interval for a proportion. (None, None) with no data."""
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6)


def kappa(left: Sequence[Any], right: Sequence[Any]) -> Optional[float]:
    """Cohen's kappa between two labelings of the same items. None when undefined."""
    if len(left) != len(right) or not left:
        return None
    n = len(left)
    observed = sum(1 for a, b in zip(left, right) if a == b) / n
    left_counts, right_counts = Counter(left), Counter(right)
    expected = sum(left_counts[label] * right_counts.get(label, 0) for label in left_counts) / (n * n)
    if expected >= 1.0:
        return None
    return round((observed - expected) / (1 - expected), 6)


def confusion(pairs: Iterable[Tuple[Any, Any]]) -> Dict[str, Dict[str, int]]:
    table: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for predicted, actual in pairs:
        table[str(predicted)][str(actual)] += 1
    return {row: dict(cols) for row, cols in sorted(table.items())}


def _rate(k: int, n: int) -> Optional[float]:
    return round(k / n, 6) if n else None


def _percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(math.ceil(q * len(ordered))) - 1))]


def _action(row: Mapping[str, Any]) -> Optional[str]:
    decision = row.get("decision") or {}
    return decision.get("rule_action") or decision.get("action")


# ── per-feature metrics: each returns {criterion_key: measured value or None} ──

def _gate(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    labelled = [r for r in rows if r.get("truth") in ("approve", "deny")]
    false_approve = sum(1 for r in labelled if _action(r) == "approve" and r["truth"] == "deny")
    redteam = [r for r in rows if r.get("expect") in ("must_deny", "must_ask")]
    caught = sum(1 for r in redteam if _action(r) != "approve")
    escalated_ok = [r for r in labelled if r.get("current") in ("escalated", "escalate") and r["truth"] == "approve"]
    would = sum(1 for r in escalated_ok if _action(r) == "approve")
    return {"min_labelled": len(labelled), "max_false_approve": false_approve,
            "false_approve_upper95_max": wilson(false_approve, len(labelled))[1],
            "redteam_catch_rate": _rate(caught, len(redteam)),
            "min_escalations_jev_would_approve_share": _rate(would, len(escalated_ok)),
            "_key_error": ("false approve (Jev approve, person denied)", false_approve),
            "_friction": sum(1 for r in labelled if _action(r) == "deny" and r["truth"] == "approve")}


def _cron(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    reports = [r for r in rows if r.get("truth") == "report"]
    silent = [r for r in rows if r.get("truth") == "silent"]
    woke = sum(1 for r in reports if _action(r) == "wake")
    serious = [r for r in reports if r.get("report_kind") in ("failure", "decision")]
    skipped = sum(1 for r in silent if _action(r) == "skip")
    with_base = [r for r in silent if r.get("baseline") in ("wake", "skip")]
    beat = None
    if with_base:
        jev_rate = sum(1 for r in with_base if _action(r) == "skip") / len(with_base)
        base_rate = sum(1 for r in with_base if r["baseline"] == "skip") / len(with_base)
        beat = round((jev_rate - base_rate) * 100, 3)
    return {"min_ticks": len(reports) + len(silent), "report_recall_min": _rate(woke, len(reports)),
            "report_recall_lower95_min": wilson(woke, len(reports))[0],
            "failure_or_decision_report_recall": _rate(sum(1 for r in serious if _action(r) == "wake"), len(serious)),
            "silent_skip_rate_min": _rate(skipped, len(silent)), "beat_hash_only_skip_rate_by_pp": beat,
            "_key_error": ("missed report (Jev skip, the run reported)", len(reports) - woke)}


def _retry(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    labelled = [r for r in rows if r.get("truth") in ("completed", "failed_again")]
    holds = [r for r in labelled if _action(r) == "hold_for_person"]
    wrong = sum(1 for r in holds if r["truth"] == "completed")
    failed = [r for r in labelled if r["truth"] == "failed_again"]
    covered = sum(1 for r in failed if _action(r) == "hold_for_person")
    base = [r for r in failed if isinstance(r.get("baseline_hold"), bool)]
    beat = None
    if base and len(base) == len(failed):
        beat = round((covered - sum(1 for r in base if r["baseline_hold"])) / len(failed) * 100, 3)
    stamps = [float(r["ts"]) for r in rows if isinstance(r.get("ts"), (int, float))]
    return {"min_pairs_backtest": len(labelled), "wrong_hold_upper95_max": wilson(wrong, len(holds))[1],
            "hold_coverage_of_failed_again_min": _rate(covered, len(failed)),
            "beat_fingerprint_rule_coverage_by_pp": beat,
            "min_live_shadow_days": round((max(stamps) - min(stamps)) / 86400, 2) if stamps else None,
            "_key_error": ("wrong hold (Jev hold, the retry finished)", wrong)}


def _blockcheck(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    labelled = [r for r in rows if r.get("truth") in ("needed_owner", "not_owner")]
    flagged = [r for r in labelled if _action(r) == "probably_not_owner"]
    right = sum(1 for r in flagged if r["truth"] == "not_owner")
    not_owner = [r for r in labelled if r["truth"] == "not_owner"]
    needed = [r for r in labelled if r["truth"] == "needed_owner"]
    kept = sum(1 for r in needed if _action(r) != "probably_not_owner")
    base = [r for r in not_owner if isinstance(r.get("baseline_not_owner"), bool)]
    beat = None
    if base and len(base) == len(not_owner):
        beat = round((sum(1 for r in not_owner if _action(r) == "probably_not_owner")
                      - sum(1 for r in base if r["baseline_not_owner"])) / len(not_owner) * 100, 3)
    return {"not_owner_precision_min": _rate(right, len(flagged)),
            "not_owner_precision_lower95_min": wilson(right, len(flagged))[0],
            "not_owner_coverage_min": _rate(len([r for r in not_owner if _action(r) == "probably_not_owner"]),
                                            len(not_owner)),
            "needs_owner_recall_min": _rate(kept, len(needed)), "beat_code_rule_coverage_by_pp": beat,
            "_key_error": ("owner's block labelled not-owner", len(needed) - kept)}


def _owner(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    labelled = [r for r in rows if r.get("truth")]
    covered = [r for r in labelled if _action(r) == "suggest_owner"]
    jev_right = sum(1 for r in covered if ((r.get("decision") or {}).get("answers", {}).get("owner", {})
                                            .get("choice")) == r["truth"])
    creator_right = sum(1 for r in covered if r.get("creator") == r["truth"])
    beat = round((jev_right - creator_right) / len(covered) * 100, 3) if covered else None
    return {"covered_accuracy_beat_creator_by_pp": beat, "coverage_min": _rate(len(covered), len(labelled)),
            "_key_error": ("covered pick that did not match", len(covered) - jev_right)}


METRICS: Dict[str, Callable[[List[Mapping[str, Any]]], Dict[str, Any]]] = {
    "gate": _gate, "cron_wake": _cron, "retry": _retry, "blockcheck": _blockcheck, "owner": _owner}
# How each promotion key is judged against its threshold.
AT_MOST = ("max_false_approve", "false_approve_upper95_max", "wrong_hold_upper95_max", "p95_latency_ms_max",
           "error_rate_max")
SKIP_KEYS = ("to", "needs", "never", "per_job", "next")


def _verdict(key: str, measured: Any, wanted: Any) -> str:
    if key == "no_drift":
        return "UNKNOWN" if measured is None else ("PASS" if measured == 0 else "FAIL")
    if measured is None or isinstance(wanted, (list, dict, str)):
        return "UNKNOWN"
    if key in AT_MOST or key.endswith("_max"):
        return "PASS" if float(measured) <= float(wanted) else "FAIL"
    return "PASS" if float(measured) >= float(wanted) else "FAIL"


def report(feature: str, rows: Sequence[Mapping[str, Any]], promotion: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    rows = list(rows)
    decisions = [r.get("decision") or {} for r in rows]
    sent = [d for d in decisions if d.get("status") in ("ok", "error")]
    latencies = [float(d["latency_ms"]) for d in sent if isinstance(d.get("latency_ms"), (int, float))]
    common = {
        "rows": len(rows),
        "actions": dict(Counter(str(_action(r)) for r in rows)),
        "p95_latency_ms_max": _percentile(latencies, 0.95),
        "error_rate_max": _rate(sum(1 for d in sent if d.get("status") == "error"), len(sent)),
        "no_drift": sum(1 for d in decisions if d.get("drift")) if decisions else None,
    }
    truth_pairs = [(_action(r), r["truth"]) for r in rows if r.get("truth") is not None]
    current_pairs = [(_action(r), r["current"]) for r in rows if r.get("current") is not None]
    out: Dict[str, Any] = {"feature": feature, "truth_means": TRUTH.get(feature), **{
        k: v for k, v in common.items() if k in ("rows", "actions")}}
    if truth_pairs:
        out["vs_truth"] = {"confusion": confusion(truth_pairs),
                           "agreement": _rate(sum(1 for a, b in truth_pairs if a == b), len(truth_pairs)),
                           "kappa": kappa([a for a, _ in truth_pairs], [b for _, b in truth_pairs])}
    if current_pairs:
        out["vs_current"] = {"confusion": confusion(current_pairs),
                             "agreement": _rate(sum(1 for a, b in current_pairs if a == b), len(current_pairs)),
                             "kappa": kappa([a for a, _ in current_pairs], [b for _, b in current_pairs])}
    measured = dict(common)
    special = METRICS.get(feature)
    if special:
        extra = special(rows)
        if "_key_error" in extra:
            label, count = extra.pop("_key_error")
            out["key_error"] = {"what": label, "count": count}
        if "_friction" in extra:
            out["friction"] = extra.pop("_friction")
        measured.update(extra)
    out["measured"] = {k: v for k, v in measured.items() if k not in ("rows", "actions")}
    if promotion:
        checks = []
        for key, wanted in promotion.items():
            if key in SKIP_KEYS:
                continue
            value = measured.get(key)
            checks.append({"criterion": key, "wanted": wanted, "measured": value,
                           "result": _verdict(key, value, wanted)})
        out["promotion"] = {"to": promotion.get("to"), "checks": checks,
                            "needs": list(promotion.get("needs") or []),
                            "verdict": ("PASS" if checks and all(c["result"] == "PASS" for c in checks)
                                        else "FAIL" if any(c["result"] == "FAIL" for c in checks) else "INCOMPLETE")}
    return out
