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
import math
import re
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from urllib.parse import urlsplit, urlunsplit
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

# Complete prevalidated plans avoid the cross-product of source/query/time picks.
# Selection never invokes an engine, enables a paid source or changes permissions.
TIME_RANGES = ('any', 'day', 'week', 'month', 'year')
_PLAN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$')


def plan_sources(request: Mapping[str, Any], *, timeout=3.0, transport=None) -> Dict[str, Any]:
    """Pick one of at most twelve complete caller-authored search plans, or none.

    Source IDs remain local; the model sees descriptions. It cannot invent a
    query, time range, engine, tool call or authorization. Call only when this
    extra decision is worthwhile; the ordinary search path is unchanged.
    """
    if (not isinstance(request, Mapping) or set(request) - {'question', 'sources', 'plans', 'private'}
            or type(request.get('private', False)) is not bool):
        raise ValueError('Expected question, sources, plans and optional boolean private')
    def text(value, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError('Plan text is missing or exceeds its bound')
        return value
    def identifier(value):
        if not isinstance(value, str) or not _PLAN_ID.fullmatch(value):
            raise ValueError('Plan and source IDs must be short opaque identifiers')
        return value
    question = text(request.get('question'), 1500)
    sources = request.get('sources')
    plans = request.get('plans')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise ValueError('Provide between one and eight available sources')
    if not isinstance(plans, list) or not 1 <= len(plans) <= 12:
        raise ValueError('Provide between one and twelve complete plans')
    source_map = {}
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != {'id', 'description'}:
            raise ValueError('Source fields must be exactly id and description')
        ident = identifier(source['id'])
        if ident in source_map:
            raise ValueError('Duplicate source ID')
        source_map[ident] = text(source['description'], 200)
    table = {}
    local = {}
    ids = set()
    for i, plan in enumerate(plans):
        if not isinstance(plan, Mapping) or set(plan) != {'id', 'query', 'sources', 'time_range'}:
            raise ValueError('Plan fields must be exactly id, query, sources and time_range')
        ident = identifier(plan['id'])
        if ident in ids:
            raise ValueError('Duplicate plan ID')
        ids.add(ident)
        query = text(plan['query'], QUERY_CHARS)
        selected = plan['sources']
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 3
                or any(not isinstance(s, str) or s not in source_map for s in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError('Each plan names one to three unique available sources')
        if not isinstance(plan['time_range'], str) or plan['time_range'] not in TIME_RANGES:
            raise ValueError('Unknown time range')
        key = 'p' + str(i)
        table[key] = json.dumps({'query': query, 'sources': [source_map[s] for s in selected],
                                 'time_range': plan['time_range']})
        local[key] = {**plan, 'sources': list(selected)}
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError('timeout must be positive, finite and at most 30 seconds')
    table['none'] = 'No offered plan is appropriate; return to the caller without searching'
    result = {'schema': 'jev.search_plan_v1', 'status': 'unknown', 'plan': None,
              'executed': False, 'jev_called': False, 'confidence': None}
    if request.get('private', False) or privacy.is_sensitive(question + json.dumps(table)):
        return {**result, 'reason': 'private_or_sensitive_not_sent'}
    if len(local) == 1:
        return {**result, 'status': 'ok', 'plan': local['p0'], 'reason': 'single_caller_plan'}
    safe_table = {key: privacy.redact(value, 1200) for key, value in table.items()}
    try:
        result['jev_called'] = True
        reply = client.ask({'question': privacy.redact(question, 1500)}, {'plan': client.choice(
            'Which offered search plan best fits the question, including freshness and authoritative sources? '
            'Choose none when none is suitable. This is advice, not permission to execute.', safe_table)},
            timeout=timeout, retries=0, transport=transport)
        answer = reply['answers']['plan']
        picked, confidence = answer['choice'], answer['confidence']
        if (picked not in table or type(confidence) not in (int, float)
                or not math.isfinite(confidence) or not .65 <= confidence <= 1):
            return {**result, 'reason': 'unusable_or_low_confidence'}
        return {**result, 'status': 'ok', 'plan': local.get(picked),
                'confidence': confidence, 'reason': 'none_suitable' if picked == 'none' else 'chosen'}
    except (client.JevError, KeyError, TypeError, ValueError):
        return {**result, 'reason': 'jev_unavailable_or_unusable'}


def _canonical_url(raw):
    if not isinstance(raw, str) or not 1 <= len(raw) <= 2048 or any(c.isspace() for c in raw):
        raise ValueError('Expected a bounded HTTP(S) URL without whitespace')
    parts = urlsplit(raw)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Only HTTP(S) URLs without embedded credentials can be grouped')
    # Validate the port as well, even though the URL is never fetched here.
    port = parts.port
    host = parts.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    if port is not None and port != (443 if parts.scheme == 'https' else 80):
        host += ':' + str(port)
    # Keep query bytes/order and meaningful fragments (including SPA routes).
    # Decoding/re-encoding can collapse signed URLs or distinct server semantics.
    query = '&'.join(part for part in parts.query.split('&')
                     if not part.split('=', 1)[0].lower().startswith('utm_')
                     and part.split('=', 1)[0].lower() not in ('fbclid', 'gclid'))
    return urlunsplit((parts.scheme, host, parts.path or '/', query, parts.fragment))


def merge_sources(batches) -> Dict[str, Any]:
    """Group URLs locally without throwing away conflicting/untrusted snippets.

    All variants are returned for the existing screen. A merged group is NOT a
    vetted result; no source failure is represented as a successful empty query.
    Tracking parameters alone are removed; semantic query bytes/order and fragments stay.
    """
    if not isinstance(batches, list) or not 1 <= len(batches) <= 8:
        raise ValueError('Provide between one and eight source batches')
    groups, flattened, seen = {}, [], set()
    failed, empty = [], []
    for i, batch in enumerate(batches):
        if not isinstance(batch, Mapping) or set(batch) != {'source', 'status', 'results'}:
            raise ValueError('A batch has exactly source, status and results')
        source = batch['source']
        if not isinstance(source, str) or not _PLAN_ID.fullmatch(source) or source in seen:
            raise ValueError('Source IDs must be unique short opaque identifiers')
        seen.add(source)
        rows = batch['results']
        status = batch['status']
        if not isinstance(status, str) or status not in ('ok', 'error', 'unavailable'):
            raise ValueError('Source status must be ok, error or unavailable')
        if not isinstance(rows, list) or len(rows) > 100 or (status != 'ok' and rows):
            raise ValueError('A batch has at most 100 results; failed batches cannot carry results')
        if status != 'ok':
            failed.append(source)
        elif not rows:
            empty.append(source)
        for j, row in enumerate(rows):
            if not isinstance(row, Mapping) or set(row) - {'id', 'url', 'title', 'snippet'} or 'url' not in row:
                raise ValueError('A result has url and optional id, title and snippet')
            for key, limit in (('title', 500), ('snippet', 3000), ('id', 128)):
                if key in row and (not isinstance(row[key], str) or len(row[key]) > limit):
                    raise ValueError('Result text is malformed or exceeds its bound')
            canonical = _canonical_url(row['url'])
            ident = f'b{i}r{j}'
            flattened.append({'id': ident, 'url': row['url'], 'title': row.get('title', ''),
                              'snippet': row.get('snippet', '')})
            if canonical not in groups:
                groups[canonical] = {'url': row['url'], 'canonical_url': canonical,
                                     'sources': [], 'variant_ids': []}
            group = groups[canonical]
            if source not in group['sources']:
                group['sources'].append(source)
            group['variant_ids'].append(ident)
    return {'schema': 'jev.search_merge_v1', 'groups': list(groups.values()),
            'screening_results': flattened, 'failed_sources': failed, 'empty_sources': empty,
            'source_status': {batch['source']: batch['status'] for batch in batches},
            'results_seen': len(flattened), 'unique_urls': len(groups),
            'screening': 'none', 'requires_variant_screening': True}


def gate_sources(question, batches, **kwargs):
    """Screen all snippet variants before emitting a deduplicated reading list.

    A duplicate cannot hide a flagged or unread companion. The result retains
    source failures and all normal gate accounting; it never treats model
    relevance as a fact or as permission to execute anything.
    """
    merged = merge_sources(batches)
    result = gate(question, merged['screening_results'], **kwargs)
    rejected = set(result.get('dropped_injection_ids', [])) | set(result.get('local_screen_ids', []))
    rejected |= set(result.get('unjudged_ids', [])) | set(result.get('clipped_ids', []))
    # The existing gate truncates before assigning overflow IDs to its report.
    # Keep those variants explicit so an unread duplicate cannot be laundered.
    unread = [row['id'] for row in merged['screening_results'][MAX_RESULTS:]]
    rejected.update(unread)
    selected = result.get('selected_ids', [])
    by_id = {row['id']: row for row in merged['screening_results']}
    selected_groups = []
    withheld = []
    for group in merged['groups']:
        variants = group['variant_ids']
        readable = [ident for ident in selected if ident in variants]
        if set(variants) & rejected or result.get('screening') != JEV_AND_LOCAL:
            withheld.extend(variants)
        elif readable:
            # Fetch the selected variant's exact original URL, never the grouping key.
            selected_groups.append({**group, 'url': by_id[readable[0]]['url'], 'selected_ids': readable})
    if set(selected) & set(withheld):
        result['selected_ids'] = [ident for ident in selected if ident not in withheld]
        result.update(sufficient=None, sufficiency=None, decision=UNKNOWN, status='partial')
    selected_groups.sort(key=lambda group: min(selected.index(ident) for ident in group['selected_ids']))
    return {**result, 'selected_groups': selected_groups,
            'withheld_variant_ids': withheld, 'unread_variant_ids': unread,
            'failed_sources': merged['failed_sources'],
            'empty_sources': merged['empty_sources'], 'source_status': merged['source_status'],
            'unique_urls': merged['unique_urls']}


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