"""Search: the decisions around a search, not the search and not the writing.

An agent doing research spends its expensive model on three things that are not
writing: which of forty results to actually open, whether what it has already read
answers the question, and which of several candidate queries to run next. Those are
picks, a yes/no and a choice - exactly what Jev answers, in about half a second,
for a fraction of a cent.

Jev never writes a query for you. It picks one you wrote, or says none of them would
add anything. Text generation stays with the model that is good at it; this module
only takes the decisions off its plate.

Nothing retrieved is trusted. Every result's title, URL and snippet go through the
same local, no-network screen the memory filter uses before anything is sent, so a
result carrying hidden instructions or an exfiltration URL is caught and reported
rather than read. Every path says which check it actually got.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import client, privacy, rerank

# The same ceiling the memory filter uses, and for the same reason: past this, results
# are reported as unread rather than silently dropped.
MAX_RESULTS = rerank.MAX_CANDIDATES
DEFAULT_TOP_K = 6
DEFAULT_MAX_ROUNDS = 3
# Passages sent to the second question (sufficiency and the next-query choice) are the
# ones the first question already picked, so this is a context budget, not a second filter.
SUFFICIENCY_CHARS = rerank.PASSAGE_CHARS
TRIM_FLOOR = 240
QUERY_CHARS = 300
TRIED_MAX = 20

# "Enough evidence to answer" is a claim, so it needs a real margin.
SUFFICIENCY_THRESHOLD = 0.5

JEV_AND_LOCAL = rerank.JEV_AND_LOCAL
LOCAL_ONLY = rerank.LOCAL_ONLY
NONE = rerank.NONE

# What the caller should do next. Every value except "answer" means the agent still has
# work to do; none of them ever means "Jev wrote you something".
ANSWER = "answer"
SEARCH_MORE = "search_more"
PROPOSE_QUERIES = "propose_queries"
THIN = "answer_from_what_we_have"
UNKNOWN = "unknown"


def _text(item: Mapping[str, Any]) -> str:
    """Title, URL and snippet as one passage.

    The URL is inside the screened text on purpose: a link shaped to carry conversation
    or private data off the machine is the one thing a search result can do that a
    memory passage cannot, and the local screen only sees what it is given.
    """
    parts = [str(item.get("title") or "").strip(),
             str(item.get("url") or "").strip(),
             str(item.get("snippet") or item.get("text") or "").strip()]
    return "\n".join(part for part in parts if part)


def _results(raw: Sequence[Any]) -> List[Dict[str, str]]:
    """``{"id", "text"}`` candidates for the memory filter's ranker, which already knows
    how to batch, redact, screen and fail open. Ids are made stable and unique here."""
    out: List[Dict[str, str]] = []
    seen: Dict[str, int] = {}
    for position, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"result {position + 1} is not an object")
        ident = str(item.get("id") or f"r{position}")
        if ident in seen:
            seen[ident] += 1
            ident = f"{ident}#{seen[ident]}"
        else:
            seen[ident] = 1
        text = _text(item)
        if not text:
            raise ValueError(f'result {position + 1} has no title, url or snippet to read')
        out.append({"id": ident, "text": text})
    return out


def _tried(queries: Iterable[Any]) -> List[str]:
    return [privacy.redact(str(query).strip(), QUERY_CHARS) for query in list(queries)[:TRIED_MAX] if str(query).strip()]


def _fit(state: Dict[str, Any], passages: Dict[str, str]) -> List[str]:
    """Drop the longest passage until the state fits. Returns the ids that were dropped.

    A record of what has been tried grows every round, and a state that is one character
    too large fails the whole request; trimming is the difference between a smaller answer
    and no answer at all.
    """
    dropped: List[str] = []
    ordered = sorted(passages, key=lambda ident: (-len(passages[ident]), ident))
    while ordered and len(json.dumps({**state, "passages": passages}, separators=(",", ":"), default=str)) > client.MAX_STATE_CHARS:
        longest = ordered.pop(0)
        if len(passages[longest]) <= TRIM_FLOOR:
            break
        passages[longest] = passages[longest][:TRIM_FLOOR] + "…"
        dropped.append(longest)
    return dropped


def _state(stamp: str, question: str, tried: List[str]) -> Dict[str, Any]:
    """What every request in this module shares. Ids, paths and source names never go in."""
    return {"today": stamp, "question": privacy.redact(question, 1500), "queries_tried": tried}


def _next_query_question(options: Sequence[Any]) -> Dict[str, Any]:
    """The pick, over queries the agent wrote plus an explicit way to decline.

    ``none`` is always offered. A closed set that forces a pick would make the tool choose
    a worse query over admitting that none of them add anything, which is the failure this
    whole module exists to avoid; and with two or more options Jev's answer has to be one
    of them, so nothing outside the agent's own list can come back.
    """
    return {"next_query": client.choice(
        "Which single query would find what is still missing? Pick the one worth running next, or none "
        "when none of them would add anything the question needs",
        {**{name: value for name, value in options}, "none": "none of these would add anything"})}


def _take_next_query(result: Dict[str, Any], reply: Any, options: Sequence[Any]) -> None:
    picked = reply["answers"].get("next_query", {}).get("choice")
    if picked and picked != "none":
        result["next_query"] = dict(options).get(picked)
        result["next_query_option"] = picked
    if reply["answers"].get("next_query") is not None:
        result["next_query_probabilities"] = {
            name: round(float(value), 3)
            for name, value in reply["answers"]["next_query"].get("probabilities", {}).items()}


def _add_usage(result: Dict[str, Any], ranked: Mapping[str, Any], reply: Any) -> None:
    """Sum what both requests cost. Reported only when a provider counted something."""
    result["latency_ms"] = max(result.get("latency_ms") or 0, reply.get("latency_ms") or 0)
    usage: Dict[str, Any] = {}
    for source in (ranked.get("usage"), reply.get("usage")):
        for key, value in (source or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
    if usage:
        result["usage"] = usage


def _ask(state: Dict[str, Any], questions: Dict[str, Any], timeout: float, transport: Optional[client.Transport]) -> Any:
    """One request. Jev's failure codes and anything a transport raises come back as a string."""
    try:
        return client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return error.code
    except Exception as error:  # noqa: BLE001 - a transport can raise anything; never crash the caller
        return type(error).__name__


def gate(
    question: str,
    results: Sequence[Any],
    *,
    queries_tried: Sequence[Any] = (),
    candidate_queries: Sequence[Any] = (),
    round_index: int = 1,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    top_k: int = DEFAULT_TOP_K,
    relevance_threshold: float = 0.5,
    injection_threshold: float = 0.5,
    sufficiency_threshold: float = SUFFICIENCY_THRESHOLD,
    reading_failed: bool = False,
    timeout: float = 6.0,
    transport: Optional[client.Transport] = None,
    today: Optional[datetime.date] = None,
) -> Dict[str, Any]:
    """One round of the search loop.

    ``results`` are ``{"id", "title", "url", "snippet"}`` as a search API returned them,
    untrusted. Returns which ids to read (ranked), which were dropped for carrying
    instructions, whether the evidence is enough, and - when it is not - the one query
    from ``candidate_queries`` worth running next, or ``None`` when none would help.

    ``decision`` is what to do: ``answer``, ``search_more``, ``propose_queries`` (Jev had
    nothing to pick from), ``answer_from_what_we_have`` (``max_rounds`` reached: the
    evidence is thin and the result says so) or ``unknown`` (Jev was not consulted).

    ``reading_failed`` says the pages picked last round could not be opened (extract
    timeouts). From round 2 on, a not-enough verdict then ends the loop as
    ``answer_from_what_we_have`` instead of asking for yet another search.

    Fails open. With Jev down this still returns the screened head of the list and
    ``sufficiency: None`` with ``decision: unknown``, never a claim that evidence is enough.
    """
    question = str(question or "").strip()
    if not question:
        raise ValueError("there is no question to search for")
    round_index = max(1, int(round_index))
    max_rounds = max(1, int(max_rounds))
    top_k = max(1, int(top_k))
    all_results = list(results)
    results_seen = len(all_results)
    overflow = results_seen > MAX_RESULTS
    items = all_results[:MAX_RESULTS]
    candidates = _results(items)
    tried = _tried(queries_tried)
    options = [(f"q{position}", privacy.redact(str(query).strip(), QUERY_CHARS))
               for position, query in enumerate(list(candidate_queries)[:5]) if str(query).strip()]

    day = today or datetime.date.today()
    stamp = day.isoformat()[:10]
    notes: List[str] = []

    ranked = rerank.rerank(question, candidates, top_k=top_k, relevance_threshold=relevance_threshold,
                           injection_threshold=injection_threshold, timeout=timeout, transport=transport, today=day)
    result: Dict[str, Any] = {
        "status": "ok" if ranked.get("status") == "ok" else "fail_open",
        "decision": UNKNOWN,
        "evidence_thin": False,
        "screening": ranked.get("screening"),
        "selected_ids": ranked.get("selected_ids", []),
        "dropped_injection_ids": ranked.get("dropped_injection_ids", []),
        "local_screen_ids": ranked.get("local_screen_ids", []),
        "unjudged_ids": ranked.get("unjudged_ids", []),
        "clipped_ids": ranked.get("clipped_ids", []),
        "scores": ranked.get("scores", {}),
        "answerable": ranked.get("answerable"),
        "sufficient": None,
        "sufficiency": None,
        "next_query": None,
        "queries_tried": tried,
        "candidate_queries": [value for _, value in options],
        "round_index": round_index,
        "max_rounds": max_rounds,
        "top_k": top_k,
        "results_seen": results_seen,
        "truncated": bool(ranked.get("truncated")) or overflow,
        "latency_ms": ranked.get("latency_ms"),
    }
    if ranked.get("reason"):
        notes.append(str(ranked["reason"]))
    if overflow:
        notes.append(f"{results_seen - MAX_RESULTS} result(s) past {MAX_RESULTS} were not read this round")

    # A result the local screen flagged is reported and never read; one Jev itself judged
    # unsafe is dropped for the same reason. Anything left is what the agent may open.
    readable = [ident for ident in result["selected_ids"]
                if ident not in result["local_screen_ids"] and ident not in result["dropped_injection_ids"]]
    # Did Jev actually read any of these results? An empty scores map plus a flagged
    # shortlist means it never saw one, which is not the same as "it read them and they
    # were irrelevant", and only the second one is a decision.
    judged = ranked.get("status") == "ok" and bool(ranked.get("scores"))

    # ── the second question: is this enough, and if not, what next ───────────────
    passages: Dict[str, str] = {}
    by_id = {item["id"]: item["text"] for item in candidates}
    for ident in readable:
        text = by_id.get(ident, "")
        if text and not privacy.is_sensitive(text):
            passages[ident] = privacy.redact(text, SUFFICIENCY_CHARS)
    if not passages:
        if judged and result["results_seen"]:
            # Jev read every result and none passed the relevance screen. That is still a
            # decision: these results do not hold the answer. Asking "is this enough" about
            # an empty shortlist would invite a coin-flip on nothing, and answering
            # `unknown` was wrong in the other direction - it says "Jev was not consulted"
            # when Jev had just judged all forty results irrelevant. Only the next-query
            # question is asked.
            result["sufficient"] = False
            notes.append("every result was judged irrelevant to the question; nothing was worth a "
                         "sufficiency check, so only the next-query question was asked")
            if options and round_index < max_rounds:
                reply = _ask({**_state(stamp, question, tried), "passages": {},
                              "results": "none of the results fetched were relevant to the question"},
                             _next_query_question(options), timeout, transport)
                if isinstance(reply, str):
                    notes.append(f"Jev unavailable ({reply}): the next query was not chosen")
                    result["status"] = "partial"
                else:
                    _take_next_query(result, reply, options)
                    _add_usage(result, ranked, reply)
        else:
            notes.append("no readable passage could be sent for a sufficiency check")
    elif privacy.is_sensitive(question):
        notes.append("the question looks sensitive; it was not sent")
    else:
        state = _state(stamp, question, tried)
        trimmed = _fit(state, passages)
        if trimmed:
            notes.append(f"{len(trimmed)} passage(s) were cut to fit one request: {', '.join(trimmed[:4])}")
        questions: Dict[str, Any] = {"enough": client.noul(
            "Taken together, the passages contain enough evidence to answer the question without searching "
            "again; a reader would still have to go and find a named fact, figure, date or source that is "
            "not in them means no")}
        if options and round_index < max_rounds:
            questions.update(_next_query_question(options))
        reply = _ask({**state, "passages": passages}, questions, timeout, transport)
        if isinstance(reply, str):
            notes.append(f"Jev unavailable ({reply}): sufficiency was not decided")
            result["status"] = "fail_open" if ranked.get("status") != "ok" else "partial"
        else:
            enough = float(reply["answers"]["enough"]["noul"])
            result["sufficiency"] = round(enough, 3)
            result["sufficient"] = enough >= sufficiency_threshold
            _take_next_query(result, reply, options)
            _add_usage(result, ranked, reply)

    # One rule for every path that got an answer: what the agent does next.
    if result["sufficient"]:
        result["decision"] = ANSWER
    elif round_index >= max_rounds:
        # Out of rounds. Saying so is the honest end of a loop, and the agent decides
        # whether thin evidence is worth answering from.
        result["decision"] = THIN
        result["evidence_thin"] = True
    elif reading_failed and result["sufficient"] is False and round_index >= 2:
        # The agent could not open the pages it already picked (extract timeouts), so it
        # is judging from snippets. Another search returns more snippets and the same
        # "not enough": the live loop this stops. Round 1 still gets one more search.
        result["decision"] = THIN
        result["evidence_thin"] = True
        notes.append("the selected pages could not be read; another search would only add snippets, "
                     "so answer from what was read and say what is missing")
    elif result["next_query"]:
        result["decision"] = SEARCH_MORE
    elif result["sufficient"] is False:
        result["decision"] = PROPOSE_QUERIES

    if result["status"] == "fail_open":
        notes.append("Jev decided nothing here; selected_ids is the screened head of the original order and "
                     "nothing is claimed about sufficiency")
    if notes:
        result["notes"] = notes
    return result