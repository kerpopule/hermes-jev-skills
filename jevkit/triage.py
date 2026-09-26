"""Classify an incoming message the moment it arrives: act now, today, queue, or ignore.

Support inboxes fail in two directions. Treat everything as urgent and the genuinely
urgent thing waits behind a newsletter. Treat nothing as urgent and the outage sits
unread until morning. Either way the cost lands on the customer.

One Jev request answers everything needed to place a message — how urgent, what kind,
whether a person must decide, whether the sender is blocked — in about 400ms for a
fraction of a cent. That is cheap enough to run on *every* message as it lands, which
is the point: triage that only runs when someone remembers to look is not triage.

Code makes the routing decision, not the model. Jev supplies calibrated readings; the
thresholds here are ours and auditable. A failure routes to `today` rather than
`ignore` — an unclassified message must never be silently dropped.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import client, privacy

BODY_CHARS = 2_500
SUBJECT_CHARS = 300

# Ordered, because urgency is a rubric and the spread between levels is the useful signal.
URGENCY = [
    "No action is needed: an automated notice, a newsletter, a receipt, or a thank-you",
    "Whenever convenient: a general question or a nice-to-have with no date attached",
    "This week: real work is requested and there is a deadline or an expectation of progress",
    "Today: someone is waiting on this to do their job, or a commitment is at risk",
    "Right now: something is broken, money or data is at risk, or a customer is blocked and cannot work",
]

KIND = {
    "problem": "Something is broken, failing, or not behaving as it should",
    "request": "Asking for work to be done: an order, a quote, a change, a document",
    "question": "Asking for information or advice, with nothing to produce",
    "billing": "Invoices, payments, pricing, contracts, or account administration",
    "scheduling": "Arranging, moving, confirming or cancelling a time",
    "notice": "An automated notification, delivery receipt, calendar invite, or out-of-office",
    "vendor": "Marketing, sales outreach, or a supplier communication that was not solicited",
}

# Routing is the caller's decision, expressed in code so it can be read and argued with.
ROUTES = ("now", "today", "queue", "ignore")


def _field(value: Any, limit: int) -> str:
    return privacy.redact(str(value or "").strip(), limit)


def _looks_automated(sender: str, subject: str) -> bool:
    """Cheap, deterministic, and free — no model needed to recognise a mailer daemon."""
    probe = f"{sender} {subject}".lower()
    return bool(re.search(
        r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|notifications?@|"
        r"bounce|auto(matic)?[-_ ]?(reply|response)|out of office|undeliverable|"
        r"delivery status notification|unsubscribe)", probe))


def classify(
    subject: str, body: str, *, sender: str = "", to: str = "", known_customer: Optional[bool] = None,
    timeout: float = 5.0, transport: Optional[client.Transport] = None,
) -> Dict[str, Any]:
    """Read one message and say where it belongs. Never raises."""
    result: Dict[str, Any] = {
        "route": "today", "urgency": None, "kind": None, "confidence": 0.0,
        "reason": "", "automated": _looks_automated(sender, subject), "sent_to_jev": False,
    }

    subject_text, body_text = _field(subject, SUBJECT_CHARS), _field(body, BODY_CHARS)
    if not (subject_text or body_text):
        result.update(route="ignore", reason="empty message")
        return result

    if privacy.is_sensitive(f"{subject}\n{body}"):
        # A message carrying a credential is not sent anywhere. It is also exactly the kind
        # of thing a person should see, so it goes to the top rather than the bottom.
        result.update(route="now", reason="looks like it contains a secret; not sent to Jev, flagged for a person")
        return result

    state = {
        "subject": subject_text,
        "body": body_text,
        "from_domain": (sender.split("@")[-1].strip(">").lower() if "@" in sender else ""),
        "to": _field(to, 120),
        "looks_automated": result["automated"],
    }
    questions = {
        "urgency": client.score("How soon does this message need a response?", URGENCY),
        "kind": client.choice("What kind of message is this?", KIND),
        "needs_human": client.noul("Answering this needs a person's judgement or authority, not just information lookup"),
        "blocked": client.noul("The sender cannot do their work until this is dealt with"),
        "deadline": client.noul("The message states or implies a specific deadline"),
        "actionable": client.noul("There is enough detail here to act on without asking the sender for more"),
        "frustrated": client.noul("The sender sounds frustrated, worried, or is escalating"),
    }
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        # Unread is worse than misfiled: default to a human seeing it today.
        result.update(reason=f"Jev unavailable ({error.code}); defaulted to today")
        return result

    answers = reply["answers"]
    urgency = answers["urgency"]
    spread = urgency.get("probabilities") or {}
    level = urgency["score"]
    confidence = urgency["confidence"]
    kind = answers["kind"]["choice"]
    blocked = answers["blocked"]["noul"]
    needs_human = answers["needs_human"]["noul"]

    # Probability mass at the top of the rubric, not the averaged score — an unsure
    # answer averages to the middle and would otherwise read as "today" every time.
    p_now = spread.get(4, 0.0)
    p_soon = spread.get(3, 0.0) + p_now
    p_none = spread.get(0, 0.0)

    # A known customer reporting a broken thing they cannot work around is the case this
    # exists for. Requiring near-certainty on "blocked" misses exactly those messages —
    # people describe being stuck plainly, not urgently ("cannot do anything with these").
    # ...but "stuck" alone is not enough. A customer musing about agent issues reads as
    # mildly blocked too, and escalating that trains everyone to ignore the now pile.
    # Require the message to also sit at least at "this week" on the rubric.
    stuck_customer = bool(known_customer) and kind == "problem" and blocked >= 0.5 and level >= 2.0
    if p_now >= 0.5 or blocked >= 0.8 or stuck_customer:
        route = "now"
    elif p_soon >= 0.5 or answers["deadline"]["noul"] >= 0.7:
        route = "today"
    elif p_none >= 0.7 and confidence >= 0.6 and kind in ("notice", "vendor"):
        route = "ignore"
    else:
        route = "queue"

    # A real person reporting a real problem is never filed away unseen, whatever the score.
    if kind == "problem" and known_customer and route == "ignore":
        route = "queue"
    if confidence < 0.5 and route == "ignore":
        route = "queue"

    result.update({
        "route": route, "urgency": round(level, 2), "urgency_confidence": round(confidence, 3),
        "kind": kind, "kind_confidence": round(answers["kind"]["confidence"], 3),
        "needs_human": round(needs_human, 3), "blocked": round(blocked, 3),
        "deadline": round(answers["deadline"]["noul"], 3),
        "actionable": round(answers["actionable"]["noul"], 3),
        "frustrated": round(answers["frustrated"]["noul"], 3),
        "confidence": round(confidence, 3), "latency_ms": reply["latency_ms"], "sent_to_jev": True,
        "reason": (f"{kind}, urgency {level:.1f}/4"
                   + (", customer is stuck" if stuck_customer else ", sender blocked" if blocked >= 0.8 else "")),
    })
    return result


def classify_many(
    messages: Sequence[Mapping[str, Any]], *, workers: int = 8, timeout: float = 6.0,
    transport: Optional[client.Transport] = None, known_domains: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Classify a batch. Each message is one independent Jev call, run side by side."""
    from concurrent.futures import ThreadPoolExecutor

    domains = {d.lower().lstrip("@") for d in known_domains}

    def one(message: Mapping[str, Any]) -> Dict[str, Any]:
        sender = str(message.get("sender") or message.get("from") or "")
        known = any(d in sender.lower() for d in domains) if domains else None
        out = classify(str(message.get("subject") or ""), str(message.get("content") or message.get("body") or ""),
                       sender=sender, to=str(message.get("to") or ""), known_customer=known,
                       timeout=timeout, transport=transport)
        out["id"] = message.get("id")
        out["subject"] = str(message.get("subject") or "")[:120]
        out["sender"] = sender[:120]
        out["received"] = message.get("received")
        out["known_customer"] = known
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, messages))


def summarize(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the batch says about the inbox, and where the classifier was unsure."""
    counts: Dict[str, int] = {r: 0 for r in ROUTES}
    kinds: Dict[str, int] = {}
    for row in rows:
        counts[row.get("route", "today")] = counts.get(row.get("route", "today"), 0) + 1
        if row.get("kind"):
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    confident = [r for r in rows if (r.get("confidence") or 0) >= 0.6]
    unsure = [r for r in rows if r.get("sent_to_jev") and (r.get("confidence") or 0) < 0.5]
    latencies = sorted(r["latency_ms"] for r in rows if r.get("latency_ms"))
    return {
        "messages": len(rows),
        "routes": counts,
        "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "confident_share": round(len(confident) / max(len(rows), 1), 3),
        "needs_review": [{"subject": r.get("subject"), "route": r.get("route"),
                          "confidence": r.get("confidence")} for r in unsure][:10],
        "not_sent_to_jev": sum(1 for r in rows if not r.get("sent_to_jev")),
        "latency_ms": {"p50": latencies[len(latencies) // 2] if latencies else None,
                       "p90": latencies[int(len(latencies) * 0.9)] if latencies else None},
        "cost_estimate_usd": round(len(rows) * 1500 * 0.042 / 1e6, 5),
    }


# ── presets: the same idea for things that are not support mail ─────────────
#
# Support mail keeps `classify` above, unchanged. Everything else is a named policy run
# through `decide`: the policy file holds the questions and thresholds, so a preset is data a
# person can read. Each preset carries the article's escalation rule (urgency > 0.90 on the
# 0..1 scale escalates; @0xMorlex, "Jev Engineering", Step 9's urgency triage) and its own
# lanes below that, and each fails to the lane that loses nothing: wake, needs the owner,
# the normal queue.

PRESETS = {
    "support-mail": None,          # `classify`, unchanged
    "urgency": "triage-urgency",
    "cron-wake": "cron-wake",
    "blockcheck": "blockcheck",
    "kanban-event": "kanban-event",
}


def classify_state(state: Any, preset: str, *, timeout: float = 4.0, mode: str = "live",
                   transport: Optional[client.Transport] = None, record: bool = True) -> Dict[str, Any]:
    """Classify one state with a named preset. Never raises for Jev's sake.

    ``support-mail`` takes ``{"subject", "body", "sender", ...}`` and returns what
    ``classify`` always returned; every other preset returns a policy decision.
    """
    from . import decide as engine

    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; one of {', '.join(PRESETS)}")
    policy_name = PRESETS[preset]
    if policy_name is None:
        message = state if isinstance(state, Mapping) else {"body": str(state)}
        return classify(str(message.get("subject") or ""), str(message.get("body") or message.get("content") or ""),
                        sender=str(message.get("sender") or message.get("from") or ""),
                        to=str(message.get("to") or ""), known_customer=message.get("known_customer"),
                        timeout=timeout, transport=transport)
    return engine.decide(state, policy_name, mode=mode, feature=preset.replace("-", "_"), timeout=timeout,
                         transport=transport, record=record)
