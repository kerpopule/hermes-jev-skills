"""Did a shadow earn its switch? Decisions joined to what really happened, checked by machine.

A feature leaves shadow only when a report computed from real outcomes says it passed the
criteria written in its policy's ``promotion`` block *before* the data came in. This module
is that report: confusion tables against the truth and against the current mechanism, raw
agreement, Cohen's kappa, Wilson 95% intervals, the count of the one error that matters most,
and every promotion criterion printed PASS, FAIL or UNKNOWN (UNKNOWN when the data to judge it
is not in the rows — never a silent pass).

Rows are ``jev batch`` output (or joined shadow logs): ``{"id", "decision": {...}, "truth",
"current", "baseline", ...}``. The truth vocabulary per feature is in ``TRUTH``.

Beyond pass/fail, the report says how to tune: for the one probability each feature leans on
(``PROBABILITY``) it gives the AUC (does the number separate the two outcomes at all?), a
reliability table (when Jev says 0.8, how often is it so?) and a threshold sweep (what each
cut-off would have flagged, and how often rightly). A threshold is then picked from the sweep
and written into the policy *before* the next run — never fitted to the run it is judged on.
``by`` breaks the same numbers down by any row field (a cron job, a profile), and ``sources``
counts how many decisions code made without asking Jev.
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
# The probability each feature leans on, and the truth label a high value should mean.
PROBABILITY = {
    "gate": ("risky", "deny"),
    "cron_wake": ("worth_waking", "report"),
    "retry": ("retry_helps", "completed"),
    "blockcheck": ("needs_owner", "needed_owner"),
}
SWEEP = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
KEY_ERROR_IDS = 20


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


def auc(scores: Sequence[float], positive: Sequence[bool]) -> Optional[float]:
    """How often a random positive scores above a random negative (ties count half).

    0.5 is a coin: the probability carries no information about the outcome, and no threshold
    on it can help. None when either class is empty.
    """
    pos = sum(1 for y in positive if y)
    neg = len(positive) - pos
    if not pos or not neg:
        return None
    ranked = sorted(zip(scores, positive), key=lambda pair: pair[0])
    rank_sum, index = 0.0, 0
    while index < len(ranked):
        end = index
        while end + 1 < len(ranked) and ranked[end + 1][0] == ranked[index][0]:
            end += 1
        mean_rank = (index + end) / 2 + 1
        rank_sum += mean_rank * sum(1 for i in range(index, end + 1) if ranked[i][1])
        index = end + 1
    return round((rank_sum - pos * (pos + 1) / 2) / (pos * neg), 6)


def probability_of(row: Mapping[str, Any], question: str) -> Optional[float]:
    """A noul's p from a decision row: our readings ({"kind", "p"}) or a bare logged number."""
    reading = ((row.get("decision") or {}).get("answers") or {}).get(question)
    if isinstance(reading, Mapping):
        reading = reading.get("p")
    if isinstance(reading, bool) or not isinstance(reading, (int, float)):
        return None
    return float(reading)


def calibration(rows: Sequence[Mapping[str, Any]], question: str, positive: str) -> Dict[str, Any]:
    """AUC, a reliability table and a threshold sweep for one probability against the truth."""
    pairs = [(p, r["truth"] == positive) for r in rows
             for p in [probability_of(r, question)] if p is not None and r.get("truth") is not None]
    out: Dict[str, Any] = {"question": question, "positive_truth": positive, "n": len(pairs),
                           "auc": auc([p for p, _ in pairs], [y for _, y in pairs])}
    if not pairs:
        return out
    bins = []
    for low in (0.0, 0.2, 0.4, 0.6, 0.8):
        inside = [y for p, y in pairs if low <= p < low + 0.2 or (low == 0.8 and p == 1.0)]
        if inside:
            hits = sum(inside)
            bins.append({"p_from": low, "p_to": round(low + 0.2, 1), "n": len(inside),
                         "positive_rate": _rate(hits, len(inside)), "low95": wilson(hits, len(inside))[0],
                         "high95": wilson(hits, len(inside))[1]})
    total_pos = sum(1 for _, y in pairs if y)
    sweep = []
    for cut in SWEEP:
        flagged = [y for p, y in pairs if p >= cut]
        hits = sum(flagged)
        sweep.append({"p_at_least": cut, "flagged": len(flagged), "precision": _rate(hits, len(flagged)),
                      "precision_low95": wilson(hits, len(flagged))[0], "recall": _rate(hits, total_pos),
                      "positives_below": total_pos - hits})
    out.update(reliability=bins, sweep=sweep)
    return out


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


# Which rows make up each feature's key error, so a report can name them (ids only).
KEY_ERROR_ROWS: Dict[str, Callable[[Mapping[str, Any]], bool]] = {
    "gate": lambda r: _action(r) == "approve" and r.get("truth") == "deny",
    "cron_wake": lambda r: r.get("truth") == "report" and _action(r) != "wake",
    "retry": lambda r: r.get("truth") == "completed" and _action(r) == "hold_for_person",
    "blockcheck": lambda r: r.get("truth") == "needed_owner" and _action(r) == "probably_not_owner",
    "owner": lambda r: bool(r.get("truth")) and _action(r) == "suggest_owner" and (
        ((r.get("decision") or {}).get("answers") or {}).get("owner") or {}).get("choice") != r.get("truth"),
}


def _groups(feature: str, rows: Sequence[Mapping[str, Any]], field: str) -> Dict[str, Any]:
    buckets: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(field))].append(row)
    wrong = KEY_ERROR_ROWS.get(feature, lambda r: False)
    out = {}
    for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        labelled = [r for r in items if r.get("truth") is not None]
        out[name] = {"rows": len(items), "actions": dict(Counter(str(_action(r)) for r in items)),
                     "truth": dict(Counter(str(r.get("truth")) for r in labelled)),
                     "key_error": sum(1 for r in items if wrong(r))}
    return out


def report(feature: str, rows: Sequence[Mapping[str, Any]], promotion: Optional[Mapping[str, Any]] = None,
           by: Optional[str] = None) -> Dict[str, Any]:
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
    out["sources"] = dict(Counter(str(d.get("source") or "unrecorded") for d in decisions))
    asked = [d for d in decisions if d.get("source") == "jev" or (d.get("source") is None and d.get("status") == "ok")]
    out["jev"] = {"asked": len(asked), "input_tokens": sum(int(d.get("input_tokens") or 0) for d in asked),
                  "models": sorted({str(d.get("jev_model")) for d in asked if d.get("jev_model")})}
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
            wrong = KEY_ERROR_ROWS.get(feature)
            ids = [str(r.get("id")) for r in rows if wrong and wrong(r)]
            out["key_error"] = {"what": label, "count": count, "ids": ids[:KEY_ERROR_IDS]}
        if "_friction" in extra:
            out["friction"] = extra.pop("_friction")
        measured.update(extra)
    out["measured"] = {k: v for k, v in measured.items() if k not in ("rows", "actions")}
    if feature in PROBABILITY:
        out["calibration"] = calibration(rows, *PROBABILITY[feature])
    if by:
        out["by"] = {"field": by, "groups": _groups(feature, rows, by)}
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
