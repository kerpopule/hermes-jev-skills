"""`jev`: one command for every Jev skill. JSON in on stdin, JSON out on stdout.

Nothing here ever prints an API key. Every subcommand that talks to Jev exits 0
with a usable fail-open answer when Jev is down, so an agent never gets stuck.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, catalog, choose, cli_decide, client, compact, key_setup, keystore, ladder, mailbox, memo, plan, rerank, replay, route, search, skillpick, spend, supervise, triage


def _stdin_json() -> Any:
    raw = sys.stdin.read(2_000_000)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise SystemExit(f"stdin is not valid JSON: {error}") from None


def _out(value: Any) -> int:
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")
    return 0


def _skill_roots(extra: List[str]) -> List[Path]:
    home = Path.home()
    hermes = catalog.hermes_home()
    roots = [Path(p).expanduser() for p in extra] or [
        hermes / "skills", home / ".claude" / "skills", home / ".codex" / "skills", home / ".agents" / "skills",
        Path.cwd() / ".claude" / "skills", Path.cwd() / "skills"]
    return roots


def cmd_setup_key(args: argparse.Namespace) -> int:
    hermes_home = Path(args.hermes_home).expanduser() if args.hermes_home else None
    common = {"verify": not args.no_verify, "hermes": not args.no_hermes, "hermes_home": hermes_home,
              "provider": args.provider}
    if args.tty:
        result = key_setup.run_tty(**common)
    else:
        result = key_setup.run_browser(host=args.host, port=args.port, timeout=args.timeout,
                                       open_browser=not args.no_open, **common)
    _out(result)
    return 0 if result.get("status") == "stored" else 1


def _catalog_is_cached() -> bool:
    return any(path.is_file() for path in (catalog.hermes_home() / "models_dev_cache.json",
                                           catalog.hermes_root() / "models_dev_cache.json", catalog._cache_path()))


def _catalog_rows(offline: bool) -> Optional[List[Dict[str, Any]]]:
    """Catalog prices for the routing checks, or None when they cannot be had."""
    # --offline is a promise not to touch the network, and the catalog fetches whenever it
    # has no copy on disk. Read it only if a copy is already there.
    if offline and not _catalog_is_cached():
        return None
    try:
        return catalog.models()
    except Exception:  # noqa: BLE001 - doctor reports a problem, it must not become one
        return None


def _names(items: List[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _routing_health(config: Dict[str, Any], rows: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """What the pools cost when they are wrong, as data plus one plain sentence each.

    Everything here is a warning. None of it stops Jev working, so none of it changes the
    exit code: a config can be expensive and still be a working install.
    """
    out: Dict[str, Any] = {}
    warnings: List[str] = []
    notes: List[str] = []

    blind = route.dead_axis(config)
    if blind:
        out["dead_specialty_axis"] = blind
        warnings.append(
            f"tier(s) {', '.join(blind)} have no specialist pools, so the 'what kind of work "
            f"is this?' question is asked and paid for on every turn and cannot change the "
            f"answer. Add coding/writing/research pools, or accept the cost knowingly.")

    cells = route.specialty_cells(config)
    dead = [cell for cell in cells if cell["dead"]]
    if dead:
        out["dead_specialty_cells"] = [{k: cell.get(k) for k in ("tier", "specialty", "model", "why")} for cell in dead]
        # A tier already named above would only be listed a second time here.
        extra = [f"{cell['tier']}/{cell['specialty']}" for cell in dead if cell["tier"] not in blind]
        if extra:
            warnings.append(
                f"{'beyond that, ' if blind else ''}{len(extra)} of {len(cells)} tier/specialty pools cannot "
                f"change the model: {_names(extra)}. "
                f"Each one resolves to the same model its tier's general pool leads with, or is missing and "
                f"falls through to it, so on those turns Jev's 'what kind of work is this?' answer is paid "
                f"for and makes no difference. Put a different model first in those pools, or accept the "
                f"cost knowingly.")

    malformed = route.pool_problems(config)
    if malformed:
        out["malformed_pool_entries"] = malformed
        warnings.append(
            f"{len(malformed)} part(s) of the pools cannot be read as lists of provider:model and are skipped "
            f"on every turn, so a model you expect to be in use is not. They are listed under "
            f"malformed_pool_entries. Copy ids from `jev models list`.")

    prices = route.price_ladder(config, rows if rows is not None else [])
    # With no catalog a config whose tiers all lead with one model has nothing to compare
    # and would read "ok". Nothing was checked, so it must not say so.
    out["price_order"] = "unknown" if rows is None else prices["status"]
    for found in prices["inversions"]:
        low, high = found["lower"], found["higher"]
        times = f"{found['ratio']:.1f}x what" if found["ratio"] else "more than"
        warnings.append(
            f"routing down costs more: {low['tier']} leads with {low['model']} at ${low['price']:g} per million "
            f"tokens, but {high['tier']} leads with {high['model']} at ${high['price']:g} "
            f"({_names(found['specialties'])} turns). A turn Jev sends down to {low['tier']} costs {times} "
            f"it would on {high['tier']}. Put a cheaper model first in {low['tier']}.")
    if prices["inversions"]:
        out["price_inversions"] = prices["inversions"]
    for found in prices["delegated"]:
        low, high = found["lower"], found["higher"]
        notes.append(
            f"hard leads with {high['model']} at ${high['price']:g} per million tokens, cheaper than "
            f"{low['tier']}'s {low['model']} at ${low['price']:g} ({_names(found['specialties'])} turns). "
            f"Not a fault: the escalation ladder is on, so the hard tier's model drives and the frontier "
            f"work is delegated through the ladder.")
    if rows is None:
        notes.append("the model catalog could not be read, so no pool was price-checked. "
                     "Tier price order is unknown, not confirmed.")
    elif prices["unknown"]:
        out["price_unknown"] = prices["unknown"]
        notes.append(
            f"no catalog price for {_names([u['model'] for u in prices['unknown']])}, so the price order "
            f"of the tiers they lead is unknown, not confirmed.")
    if warnings:
        out["warnings"] = warnings
    if notes:
        out["notes"] = notes
    return out


def _override_endpoint() -> Optional[Dict[str, Any]]:
    """What TYPESAFE_BASE_URL points at, if it overrides the official endpoint.

    A gateway that answers 401 used to look exactly like "no key" here, or like nothing at
    all, because every feature reads a failed call as "Jev had no opinion". The override is
    named, whether a bearer goes with it, and whether it is usable at all.
    """
    base = os.environ.get("TYPESAFE_BASE_URL", "").strip()
    if not base or base.rstrip("/") == "https://api.typesafe.ai":
        return None
    try:
        endpoint = client._custom_typesafe_endpoint(base)
    except client.JevError:
        return {"url": None, "valid": False,
                "error": "invalid_endpoint: use https:// (or http:// on 127.0.0.1 or ::1), no user, query or fragment"}
    return {"url": endpoint, "valid": True,
            "bearer": client.PROXY_KEY_ENV if client._proxy_key() else None}


def cmd_doctor(args: argparse.Namespace) -> int:
    report: Dict[str, Any] = {"version": __version__, "key": keystore.describe()}
    override = _override_endpoint()
    if override is not None:
        report["endpoint_override"] = override
    # An override is asked even without a stored key: the override never receives that key.
    ask_it = (report["key"]["present"] or (override or {}).get("valid")) and not args.offline
    if ask_it:
        try:
            reply = client.ask("The build finished and all tests passed.",
                               {"ok": client.noul("The text reports a successful outcome")}, timeout=10)
            report["jev"] = {"reachable": True, "latency_ms": reply["latency_ms"]}
        except client.JevError as error:
            report["jev"] = {"reachable": False, "error": error.code}
    config = route.load_config()
    report["routing"] = {"config": str(route.config_path()),
                         # This is the PRIVACY mode (what is sent), not the on/shadow/off
                         # switch. They were both called "mode" and it read as the answer
                         # to "is routing on?", which it has never been.
                         "privacy_mode": config["mode"],
                         "tiers_configured": sorted(config.get("tiers") or {})}
    if config.get("tiers"):
        try:
            report["routing"].update(_routing_health(config, _catalog_rows(args.offline)))
        except Exception as error:  # noqa: BLE001 - a routing.json odd enough to break a check is itself the finding
            report["routing"]["warnings"] = [f"the routing checks could not run ({type(error).__name__}); "
                                             f"the pools were NOT checked. Look at {route.config_path()}."]
    report["hermes_home"] = str(catalog.hermes_home()) if catalog.hermes_home().is_dir() else None
    _out(report)
    # Only a missing key fails doctor. The routing findings are warnings about cost: an
    # install script that gates on this exit code must not fail because a pool is pricey.
    # With an override in force the stored key is never used, so the override answering is
    # what counts, and an invalid override fails even when a key is present.
    if override is not None:
        return 0 if override.get("valid") and (args.offline or report.get("jev", {}).get("reachable")) else 1
    return 0 if report["key"]["present"] else 1


def cmd_models(args: argparse.Namespace) -> int:
    data = catalog.load_models_dev(refresh=args.refresh)
    if args.action == "providers":
        return _out({"available": catalog.available_providers(data)})
    rows = catalog.models(data)
    config = route.load_config()
    if args.action == "list":
        if args.provider:
            rows = [r for r in rows if r["provider"] == args.provider]
        if args.search:
            rows = [r for r in rows if args.search.lower() in (r["model"] + r["name"]).lower()]
        return _out({"count": len(rows), "models": rows})
    tiers = route.suggest_tiers(rows, config["exclude"])
    if args.write:
        config["tiers"] = tiers
        path = route.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return _out({"written": str(path), "tiers": tiers})
    return _out({"suggested_tiers": tiers, "note": "add --write to save, then edit the pools to taste"})


def cmd_route(args: argparse.Namespace) -> int:
    request = _stdin_json() if args.prompt is None else {"prompt": args.prompt}
    return _out(route.decide(
        request["prompt"], current=request.get("current") or args.current,
        context_tokens=int(request.get("context_tokens") or args.context_tokens),
        has_images=bool(request.get("has_images") or args.has_images), profile=request.get("profile") or args.profile,
        pinned=bool(request.get("pinned")), timeout=args.timeout))


def cmd_rerank(args: argparse.Namespace) -> int:
    request = _stdin_json()
    return _out(rerank.rerank(request["query"], request["candidates"], top_k=int(request.get("top_k", 8))))


def cmd_compact(args: argparse.Namespace) -> int:
    request = _stdin_json()
    messages = request["messages"] if isinstance(request, dict) else request
    selection = compact.select(messages, keep_last=args.keep_last)
    if args.digest:
        selection["digest"] = compact.digest(messages, selection)
    return _out(selection)


def cmd_pick_skill(args: argparse.Namespace) -> int:
    turn = args.turn if args.turn is not None else _stdin_json()["turn"]
    return _out(skillpick.pick(turn, skillpick.discover(_skill_roots(args.root)), top_k=args.top_k))


def cmd_choose(args: argparse.Namespace) -> int:
    try:
        return _out(choose.choose(_stdin_json(), mock=args.mock))
    except ValueError as error:
        raise SystemExit(f"invalid request: {error}") from None


def cmd_search(args: argparse.Namespace) -> int:
    """One round of the search loop: which results to read, whether that is enough, what next.

    Bad input is refused in JSON with exit 2, like `jev ask` and `jev mail`. A Jev failure
    is not an error here: the module fails open and the reply says so.
    """
    try:
        # Not _stdin_json(): it turns only a JSONDecodeError into a SystemExit on stderr,
        # and the promise every other command makes is a JSON refusal an agent can read.
        try:
            raw = sys.stdin.read(2_000_000) if sys.stdin is not None else ""
        except UnicodeDecodeError:
            raise ValueError("stdin is not valid JSON: it is not UTF-8 text") from None
        try:
            request = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"stdin is not valid JSON: {error}") from None
        except RecursionError:
            raise ValueError("stdin is nested too deeply to read") from None
        if not isinstance(request, dict):
            raise ValueError('the request must be a JSON object: {"question": ..., "results": [...]}')
        for field in ("question", "results"):
            if request.get(field) is None:
                raise ValueError(f'the request has no "{field}"')
        if not isinstance(request["question"], str):
            raise ValueError('"question" must be a string')
        if not isinstance(request["results"], list):
            raise ValueError('"results" must be a list of {"id", "title", "url", "snippet"} objects')
        return _out(search.gate(
            request["question"], request["results"],
            queries_tried=request.get("queries_tried") or [],
            candidate_queries=request.get("candidate_queries") or [],
            round_index=int(request.get("round_index") or args.round),
            max_rounds=int(request.get("max_rounds") or args.max_rounds),
            reading_failed=bool(request.get("reading_failed")),
            top_k=int(request.get("top_k") or args.top_k),
            timeout=args.timeout))
    except ValueError as error:
        _out({"error": "invalid_request", "detail": str(error)})
        return 2


def cmd_memo(args: argparse.Namespace) -> int:
    """Counts and sizes only. A cached plan is somebody's command, so it is never printed."""
    if args.action == "clear":
        return _out({"cleared": memo.clear(), "mode": memo.mode()})
    return _out(memo.stats())


def cmd_plan(args: argparse.Namespace) -> int:
    """Plan a spoken-style command once, up front. Falls back to a single goal step, never raises."""
    command = " ".join(args.command).strip() if args.command else str(_stdin_json().get("command", "")).strip()
    return _out(plan.plan(command, front_app=args.front_app or "", running_apps=args.running or [],
                          timeout=args.timeout))


def _rungs(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    settings = (config or route.load_config()).get("escalation") or {}
    return list(settings.get("rungs") or [])


def _timeout_seconds(value: float, flag: str = "--timeout") -> float:
    """A timeout the socket will take, or ValueError naming the flag.

    urllib hands the timeout to the socket, which answers nan with ValueError and inf
    with OverflowError, and zero or less aborts the call before it is sent — none of
    which classify's fail-open path knew about. cmd_ask has carried the upper half of
    this guard since the day the flag was added.
    """
    if not 0 < value <= 3600:
        raise ValueError(f"{flag} must be a number of seconds, above 0 and at most 3600")
    return value


_STDIN_LIMIT = 2_000_000
_STDIN_TOO_BIG = (f"stdin is larger than {_STDIN_LIMIT} bytes; pass the batch with --file, "
                  f"which reads the whole file")


def _triage_stdin_text() -> str:
    """stdin as text, or a ValueError — without trusting the decoder stdin arrived with.

    sys.stdin.errors is 'surrogateescape' whenever Python runs in UTF-8 mode, which is
    the default both on macOS here and in the CI container, so reading through sys.stdin
    NEVER raises on undecodable bytes: \\xff\\xfe silently became the lone surrogates
    "\\udcff\\udcfe", json.loads read them happily, and that subject was sent to Jev and
    came back http_400. A refusal that only fires when stdin happens to be a strict
    decoder is not a refusal, so the bytes are decoded here instead.

    The size guard is the other half of the same promise. Reading a fixed slice and then
    parsing it reported a valid 3MB batch as "stdin is not valid JSON: Unterminated
    string" — the JSON was fine, the reader cut it — and --file, which has no cap, took
    the same bytes and classified all 12000 messages. Whichever limit stdin has, saying
    which one it hit is the difference between a caller fixing it and a caller believing
    its export is corrupt.
    """
    stream = sys.stdin
    if stream is None:  # no stdin at all is bad input, not an AttributeError traceback
        raise ValueError("there is no stdin to read; pass the batch with --file")
    buffer = getattr(stream, "buffer", None)
    if buffer is None:  # a StringIO stands in for stdin under test
        text = stream.read(_STDIN_LIMIT + 1)
        if len(text) > _STDIN_LIMIT:
            raise ValueError(_STDIN_TOO_BIG)
        return text
    data = buffer.read(_STDIN_LIMIT + 1)
    if len(data) > _STDIN_LIMIT:
        raise ValueError(_STDIN_TOO_BIG)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("stdin is not valid JSON: it is not UTF-8 text") from None


def _triage_input(args: argparse.Namespace) -> Any:
    """The JSON, with every way it can be unreadable turned into a ValueError.

    `jev ask` promises {"error": "invalid_request", ...} and exit 2 for bad input; this
    command answered a missing file, a directory, non-UTF-8 bytes, invalid JSON, a bare
    JSON scalar and a JSON null with a raw Python traceback on stderr and nothing at all
    on stdout. The file half is `jev mail`'s reader, unchanged — the same file, opened
    the same way, has to be refused with the same words.
    """
    if args.file:
        return _mail_input(args)
    # Not _stdin_json(): it turns only a JSONDecodeError into a message, and exits 1
    # rather than printing the refusal an agent reading stdout can act on.
    try:
        return json.loads(_triage_stdin_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"stdin is not valid JSON: {error}") from None
    except RecursionError:
        raise ValueError("stdin is nested too deeply to read") from None


# The fields triage actually reads off a message. A dict naming any of them describes a
# message, whatever else it carries.
_MESSAGE_FIELDS = ("subject", "content", "body")


def _triage_messages(raw: Any) -> List[Any]:
    """One reader for --file and for stdin, which disagreed exactly as mail's did.

    Piped in, `{"messages": [...]}` was wrapped as ONE message carrying neither subject
    nor body: it classified locally as "empty message", the summary reported an inbox of
    one, exit 0, and nothing said the real messages had never been read. The same
    envelope in --file gave three. A triage tool that reports fewer messages than it was
    handed and succeeds is the one failure a caller cannot notice.

    A lone message object is still accepted on either path, because piping one message in
    is what stdin has always taken — including one that itself carries a "messages" or
    "items" field, which a thread object and several exporters do. Read as an envelope,
    {"subject": ..., "content": ..., "items": []} became a batch of nothing and exited
    "no messages to classify": the one message handed in was lost, and the exit said the
    inbox was empty. An object carrying a "messages" or "items" key that is not a list
    and no message field of its own is a malformed envelope, not a message, and is
    refused.
    """
    if isinstance(raw, dict):
        if not any(key in raw for key in ("messages", "items")):
            return [raw]
        if any(key in raw for key in _MESSAGE_FIELDS):
            return [raw]
    return _mail_messages(raw)


def cmd_triage(args: argparse.Namespace) -> int:
    """Classify incoming messages: act now, today, queue, or ignore."""
    try:
        timeout = _timeout_seconds(args.timeout)
        entries = _triage_messages(_triage_input(args))
    except ValueError as error:
        # Same JSON-and-exit-2 contract `jev ask` and `jev mail` give. The caller is an
        # agent reading stdout, and a traceback on stderr never told it what to fix.
        _out({"error": "invalid_request", "detail": str(error)})
        return 2
    if args.preset and args.preset != "support-mail":
        # A preset is a named decision policy (cron-wake, blockcheck, kanban-event, urgency):
        # each entry is a state object, and the answer is the policy's action.
        states = [m for m in entries if isinstance(m, dict)]
        if not states:
            _out({"error": "invalid_request", "detail": "no JSON objects to classify"})
            return 2
        return cli_decide.cmd_triage_preset(args, states)
    messages = [m for m in entries if isinstance(m, dict)]
    dropped = len(entries) - len(messages)
    if not messages:
        if dropped:
            # "no messages to classify" is true of an empty batch and false of a batch of
            # 5000 junk rows, and it went to stderr with an empty stdout, so the count
            # that says what actually went wrong was thrown away with the rows.
            _out({"error": "invalid_request",
                  "detail": f"none of the {dropped} entries is a JSON object, so there is "
                            f"nothing to classify"})
            return 2
        raise SystemExit("no messages to classify")
    rows = triage.classify_many(messages, workers=args.workers, timeout=timeout,
                                known_domains=args.customer_domain or [])
    summary = triage.summarize(rows)
    if dropped:
        # An exporter row that is not an object used to kill the whole batch with an
        # AttributeError from inside the thread pool, after other messages had already
        # been sent and paid for. Counted here, the run finishes and says what it skipped.
        summary["dropped_not_an_object"] = dropped
    if args.summary:
        return _out(summary)
    return _out({"summary": summary, "messages": rows})


_MAIL_SHAPES = ('the input must be a JSON list of messages, an object with "messages": [...], '
                'or an object with "items": [...]')


def _mail_input(args: argparse.Namespace) -> Any:
    """Read the JSON, turning every way it can be unreadable into a ValueError.

    `jev ask` promises {"error": "invalid_request", ...} and exit 2 for bad input; this
    command answered a missing file, a directory, non-UTF-8 bytes, invalid JSON and a
    bare JSON scalar with a raw Python traceback on stderr and nothing on stdout.
    """
    if not args.file:
        return _stdin_json()
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("--file is not UTF-8 text") from None
    except OSError as error:
        raise ValueError(f"--file could not be read: {error}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"--file is not valid JSON: {error}") from None
    except RecursionError:
        raise ValueError("--file is nested too deeply to read") from None


def _mail_messages(raw: Any) -> List[Any]:
    """One reader for --file and for stdin.

    They disagreed. `{"items": [...]}` was honoured from a file and, piped in, became a
    single empty message: the summary reported a mailbox of one, exit 0, and nothing said
    three messages had been dropped. A mailbox tool that reports fewer messages than it
    was handed and succeeds is the one failure a caller cannot notice.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("messages", "items"):
            if isinstance(raw.get(key), list):
                return raw[key]
    raise ValueError(_MAIL_SHAPES)


def cmd_mail(args: argparse.Namespace) -> int:
    """Sort a mailbox into lanes: which mail is even addressed to me as a person."""
    try:
        # urllib hands the timeout to the socket, which answers nan with ValueError and
        # inf with OverflowError — neither of which classify's fail-open path knew about.
        # cmd_ask has carried this guard since the day the flag was added.
        if not args.timeout <= 3600:
            raise ValueError("--timeout must be a number of seconds, at most 3600")
        entries = _mail_messages(_mail_input(args))
    except ValueError as error:
        _out({"error": "invalid_request", "detail": str(error)})
        return 2
    messages = [m for m in entries if isinstance(m, dict)]
    dropped = len(entries) - len(messages)
    if not messages:
        raise SystemExit("no messages to sort")
    rows = mailbox.classify_many(messages, workers=args.workers, timeout=args.timeout)
    summary = mailbox.summarize(rows)
    if dropped:
        # An exporter row that is not an object used to vanish completely: not in the
        # rows, not in not_sent_to_jev, and the summary reported the smaller number.
        summary["dropped_not_an_object"] = dropped
    if args.summary:
        return _out(summary)
    return _out({"summary": summary, "messages": rows})


def cmd_spend(args: argparse.Namespace) -> int:
    """What the window cost, and what it would have cost on every alternative."""
    rows: List[spend.Usage] = []
    for path in args.usage or []:
        rows += spend.from_json(path)
    for db in args.hermes_db or []:
        rows += spend.from_hermes_sessions(db, since=time.time() - args.days * 86400,
                                           label=Path(db).parent.name)
    if not rows:
        raise SystemExit("no usage: pass --usage <export.json> and/or --hermes-db <state.db>")
    seats = [spend.Seat(**s) for s in json.loads(Path(args.seats).read_text(encoding="utf-8"))] if args.seats else []
    data = spend.report(rows, candidates=args.compare or [], seats=seats, days=args.days)
    if args.json:
        return _out(data)
    sys.stdout.write(spend.render(data) + "\n")
    return 0


def cmd_ladder(args: argparse.Namespace) -> int:
    """Which frontier seat takes hard work, and which ones are currently full."""
    rungs = _rungs()
    if args.action == "status":
        return _out(ladder.status(rungs))
    if args.action == "choose":
        if not rungs:
            raise SystemExit("no escalation.rungs configured in routing.json")
        return _out(ladder.choose(rungs, skip_probe=args.no_probe))
    if args.action == "refuse":
        if not args.rung:
            raise SystemExit("refuse needs --rung")
        return _out(ladder.refuse(args.rung, args.reason or "refused", cooldown=args.cooldown))
    ladder.clear(args.rung)
    return _out({"cleared": args.rung or "all"})


def cmd_supervise(args: argparse.Namespace) -> int:
    """Assess one delegated run: is it progressing, stuck, waiting on you, or done."""
    request = _stdin_json() if not args.tail_file else {
        "goal": args.goal or "", "tail": Path(args.tail_file).read_text(encoding="utf-8", errors="replace")}
    goal = request.get("goal") or args.goal or ""
    if not goal.strip():
        raise SystemExit("supervise needs a goal (--goal, or a `goal` field on stdin)")
    snap = supervise.assess(
        goal, str(request.get("tail") or ""), elapsed_s=float(request.get("elapsed_s") or 0),
        quiet_s=float(request.get("quiet_s") or 0), new_output=bool(request.get("new_output", True)),
        looping=bool(request.get("looping")), exited=request.get("exited"), timeout=args.timeout)
    return _out(snap.as_dict())


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay real turns through the policy and price the result against the baseline."""
    turns = replay.turns_from_jsonl(args.turns, current=args.current, limit=args.limit)
    if not turns:
        raise SystemExit(f"no usable turns in {args.turns} (expect JSONL with a `prompt` field)")
    out = replay.replay(turns, only_provider=args.only_provider, workers=args.workers)
    if args.verbose:
        return _out(out)
    return _out(out["summary"])


def cmd_dashboard(args: argparse.Namespace) -> int:
    """The model-routing page: per-profile models, an all-profiles target, the Jev switch and live decisions."""
    import os
    import subprocess

    server = Path(__file__).resolve().parents[1] / "router-dashboard" / "server.py"
    if not server.is_file():
        raise SystemExit("the dashboard ships with the repo checkout; run `jev` from there")
    # The dashboard needs PyYAML. Hermes' own interpreter always has it.
    venv = catalog.hermes_root() / "hermes-agent" / "venv" / "bin" / "python"
    python = str(venv) if venv.is_file() else sys.executable
    command = [python, str(server), "--host", args.host, "--port", str(args.port), "--hermes-home", str(catalog.hermes_root())]
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        import secrets
        token = os.environ.get("DASHBOARD_TOKEN") or secrets.token_urlsafe(24)
        os.environ["DASHBOARD_TOKEN"] = token
        print(f"open once with the token: http://{args.host}:{args.port}/?token={token}", file=sys.stderr)
    return subprocess.call(command)


def cmd_ask(args: argparse.Namespace) -> int:
    """The raw call. Bad input is answered in JSON with exit 2 and is never sent."""

    def shown(value: Any) -> str:
        # A refusal quotes the caller's own id or type back. Quoted whole, a 1 MB type came
        # back as a 1 MB error with the actual complaint at the far end of it.
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return text if len(text) <= 80 else text[:77] + "..."

    def wire(full_name: str, raw: Any) -> Dict[str, Any]:
        """One question spelled the way Jev's wire format spells it, or ValueError saying what is wrong."""
        name = shown(full_name)
        if not isinstance(raw, dict):
            raise ValueError(f'question "{name}" must be an object like {{"type": ..., "instructions": ...}}')
        # The help text used to advertise {id, kind, text}. The wire format says type and
        # instructions, so the advertised shape went out verbatim and client._check_answer
        # died on question["type"] with a KeyError. Both spellings are accepted; only the
        # wire one is sent. The shape itself is checked once, in client.check_question.
        question = {key: value for key, value in raw.items() if key not in ("id", "kind", "text")}
        for alias, key in (("kind", "type"), ("text", "instructions")):
            if alias in raw:
                if key in raw and raw[key] != raw[alias]:
                    raise ValueError(f'question "{name}" gives both "{key}" and "{alias}", and they disagree')
                question[key] = raw[alias]
        return client.check_question(name, question)

    def named(questions: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(questions, (dict, list)):
            raise ValueError('"questions" must be a list of questions, or an object of {name: question}')
        if not questions:
            raise ValueError('"questions" is empty; ask at least one')
        pairs = list(questions.items()) if isinstance(questions, dict) else []
        for position, raw in enumerate(questions if isinstance(questions, list) else (), 1):
            ident = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(ident, bool) or not isinstance(ident, (str, int, type(None))):
                raise ValueError(f"question {position} has an id that is not a string or a number")
            # `q.get("id") or ...` read an id of 0 as missing and renamed the question.
            pairs.append((f"q{position}" if ident in (None, "") else str(ident), raw))
        out: Dict[str, Dict[str, Any]] = {}
        for name, raw in pairs:
            # Building the mapping straight from the list let a repeated id overwrite the
            # earlier question, which was then never asked and never reported.
            if name in out:
                raise ValueError(f'two questions are both named "{shown(name)}"; ids must be unique, and a question '
                                 f'with no id is named q1, q2, ... by its position')
            out[name] = wire(name, raw)
        return out

    def no_repeats(pairs: List[Any]) -> Dict[str, Any]:
        # json.loads keeps the last of two equal keys without a word. In the {name: question}
        # form that is the duplicate-id bug again: the earlier question is never asked and
        # never reported. The same goes for two options of one choice.
        out: Dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f'the key "{shown(key)}" appears twice in one JSON object; the earlier one '
                                 f'would be dropped without a word')
            out[key] = value
        return out

    try:
        # Not _stdin_json(): it cannot take the hook, and it only turns a JSONDecodeError into
        # a message. stdin that is not UTF-8, is nested past the recursion limit, or (3.11+)
        # holds an integer of 5000 digits came out as a traceback. Here all of it is bad
        # input like any other, which is also what `jev ask --help` promises.
        try:
            request = json.loads(sys.stdin.read(2_000_000), object_pairs_hook=no_repeats)
        except json.JSONDecodeError as error:
            raise ValueError(f"stdin is not valid JSON: {error}") from None
        except UnicodeDecodeError:
            raise ValueError("stdin is not valid JSON: it is not UTF-8 text") from None
        except RecursionError:
            raise ValueError("stdin is nested too deeply to read") from None
        # urllib hands the timeout to the socket, which answers nan with ValueError and inf or
        # 1e300 with OverflowError. Zero and below still come back as Jev's own "timeout".
        if not args.timeout <= 3600:
            raise ValueError("--timeout must be a number of seconds, at most 3600")
        if not isinstance(request, dict):
            raise ValueError('the request must be a JSON object: {"state": ..., "questions": ...}')
        for field in ("state", "questions"):
            if request.get(field) is None:
                raise ValueError(f'the request has no "{field}"')
        questions = named(request["questions"])
    except ValueError as error:
        # Same JSON-and-exit-2 contract a Jev failure gets below. The caller is an agent
        # reading stdout, and a traceback on stderr never told it which field to fix.
        _out({"error": "invalid_request", "detail": str(error)})
        return 2
    try:
        return _out(client.ask(request["state"], questions, timeout=args.timeout))
    except client.JevError as error:
        _out({"error": error.code})
        return 2
    except RecursionError:
        # A state nested a few levels short of what json.loads refuses is read fine here and
        # then overflows json.dumps inside client.ask, which sits deeper in the stack.
        _out({"error": "invalid_request", "detail": "the request, or Jev's reply, is nested too deeply to handle"})
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev", description="Hermes Jev Skills")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup-key", help="open a private page for the person to paste their Jev key")
    p.add_argument("--provider", choices=list(keystore.PROVIDERS), default="typesafe",
                   help="typesafe (default) is a key from console.typesafe.ai; openrouter reaches the same Jev "
                        "through OpenRouter, which is one key instead of two if you already use it; venice "
                        "reaches it through Venice, where the decision model is currently free; zen reaches "
                        "it through OpenCode Zen, which has a free tier")
    p.add_argument("--tty", action="store_true", help="hidden terminal prompt instead of a browser page")
    p.add_argument("--host", default="127.0.0.1", help="bind address; keep loopback unless you are on a private network")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--no-open", action="store_true", help="do not try to open a browser; just print the link")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--no-hermes", action="store_true", help="do not write the key into Hermes lane .env files")
    p.add_argument("--hermes-home")
    p.set_defaults(func=cmd_setup_key)

    p = sub.add_parser("doctor", help="is the key present, does Jev answer, and do the routing pools waste money")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("models", help="every model this machine can call")
    p.add_argument("action", choices=["list", "providers", "suggest"])
    p.add_argument("--provider")
    p.add_argument("--search")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("route", help="pick the model for one fresh user turn")
    p.add_argument("--prompt")
    p.add_argument("--current")
    p.add_argument("--profile")
    p.add_argument("--context-tokens", type=int, default=0)
    p.add_argument("--has-images", action="store_true")
    p.add_argument("--timeout", type=float, default=2.5)
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("rerank", help="filter retrieved memory passages")
    p.set_defaults(func=cmd_rerank)

    p = sub.add_parser("compact-select", help="mark each transcript turn keep / summarize / drop")
    p.add_argument("--keep-last", type=int, default=6)
    p.add_argument("--digest", action="store_true", help="also return the reduced transcript for the summarizer")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("pick-skill", help="which skill, if any, this turn needs")
    p.add_argument("--turn")
    p.add_argument("--root", action="append", default=[])
    p.add_argument("--top-k", type=int, default=3)
    p.set_defaults(func=cmd_pick_skill)

    p = sub.add_parser("choose", help="pick the next GUI or browser action from a candidate table")
    p.add_argument("--mock", action="store_true")
    p.set_defaults(func=cmd_choose)

    p = sub.add_parser("search", help="one round of a search: which results to read, whether that is enough, which query next")
    p.add_argument("--round", type=int, default=1, help="which round this is, 1-based")
    p.add_argument("--max-rounds", type=int, default=3, help="rounds you are willing to run in total")
    p.add_argument("--top-k", type=int, default=6, help="how many results to hand back to read")
    p.add_argument("--timeout", type=float, default=6.0)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("plan", help="break one computer-use command into steps, once, before the Jev loop starts")
    p.add_argument("command", nargs="*", help='the command; omit to read {"command": ...} from stdin')
    p.add_argument("--front-app", default="", help="the app in front right now, if known")
    p.add_argument("--running", action="append", help="a running app (repeatable)")
    p.add_argument("--timeout", type=float, default=plan.DEFAULT_TIMEOUT)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("memo", help="the plan cache: how many entries it holds, or empty it")
    p.add_argument("action", choices=["stats", "clear"])
    p.set_defaults(func=cmd_memo)

    p = sub.add_parser("triage", help="classify incoming messages: now / today / queue / ignore")
    p.add_argument("--file", help='JSON list of messages (subject, content, sender), '
                                  'or {"messages": [...]} or {"items": [...]}; else the same on stdin')
    p.add_argument("--customer-domain", action="append", help="a domain whose mail is a real customer (repeatable)")
    p.add_argument("--workers", type=int, default=8)
    # The same default classify_many already used when nothing was passed: naming the flag
    # changes what a caller can say, not what an existing caller gets.
    p.add_argument("--timeout", type=float, default=6.0, help="seconds per message, at most 3600")
    p.add_argument("--summary", action="store_true", help="counts only, no per-message rows")
    p.add_argument("--preset", choices=list(triage.PRESETS), default="support-mail",
                   help="support-mail (default: now/today/queue/ignore) or a decision preset: urgency, "
                        "cron-wake, blockcheck, kanban-event (each entry is then a state object)")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("mail", help="sort a mailbox into lanes: needs reply / updates / promotional / sales / spam")
    p.add_argument("--file", help='JSON list of messages (subject, content/snippet, sender, headers), '
                                  'or {"messages": [...]} or {"items": [...]}; else the same on stdin')
    p.add_argument("--workers", type=int, default=8, help="messages sorted side by side")
    p.add_argument("--timeout", type=float, default=6.0, help="seconds per message, at most 3600")
    p.add_argument("--summary", action="store_true", help="counts only, no per-message rows")
    p.set_defaults(func=cmd_mail)

    p = sub.add_parser("spend", help="weekly cost report: what ran, what it cost, what would have been cheaper")
    p.add_argument("--usage", action="append", help="JSON export of metered usage rows (repeatable)")
    p.add_argument("--hermes-db", action="append", help="Hermes state.db, to value subscription work (repeatable)")
    p.add_argument("--compare", action="append", help="model id to price the same tokens against (repeatable)")
    p.add_argument("--seats", help="JSON list of flat-fee seats: name, monthly_usd, market_equivalent")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_spend)

    p = sub.add_parser("ladder", help="the frontier escalation ladder: who takes hard work, and who is full")
    p.add_argument("action", choices=["status", "choose", "refuse", "clear"])
    p.add_argument("--rung", help="rung name, for refuse/clear")
    p.add_argument("--reason", help="why it refused, e.g. the quota message")
    p.add_argument("--cooldown", type=float, default=ladder.DEFAULT_COOLDOWN, help="seconds to skip this rung")
    p.add_argument("--no-probe", action="store_true", help="trust the cooldowns; do not run availability probes")
    p.set_defaults(func=cmd_ladder)

    p = sub.add_parser("supervise", help="assess a delegated run: progressing, stuck, waiting on you, or done")
    p.add_argument("--goal", help="what the delegated run was asked to do")
    p.add_argument("--tail-file", help="file holding the run's recent output; otherwise read JSON on stdin")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("replay", help="replay logged turns through the policy and price it against the baseline")
    p.add_argument("turns", help="JSONL file, one object per turn with at least a `prompt` field")
    p.add_argument("--current", help="the model these turns ran on, e.g. openrouter:deepseek/deepseek-v4.1-flash")
    p.add_argument("--only-provider", help="restrict picks to one provider, as the Hermes plugin does")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--verbose", action="store_true", help="include every per-turn decision, not just the summary")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("dashboard", help="open the model-routing page (models per profile, Jev switch, live decisions)")
    p.add_argument("--host", default="127.0.0.1", help="keep loopback unless you are on a private network; other hosts require a token")
    p.add_argument("--port", type=int, default=8791)
    p.set_defaults(func=cmd_dashboard)

    # The example is the contract: a test reads it back out of this text and runs it, because
    # the shape shown here before, [{"id","kind","text"}], was one Jev could not answer.
    p = sub.add_parser(
        "ask", formatter_class=argparse.RawDescriptionHelpFormatter,
        help='raw Jev call: {"state": ..., "questions": [{"id", "type": choice|score|noul, "instructions", '
             '"criteria"}]}; choice and score need criteria. `jev ask --help` has a full example',
        description="""Ask Jev typed questions about one state. JSON on stdin, JSON on stdout.

  {"state": "The deploy failed twice on the same migration step.",
   "questions": [
     {"id": "blocked", "type": "noul",
      "instructions": "The work cannot continue until a person steps in"},
     {"id": "next", "type": "choice", "instructions": "What should happen next?",
      "criteria": {"retry": "Run the same step again", "escalate": "Hand it to a person"}},
     {"id": "severity", "type": "score", "instructions": "How serious is this?",
      "criteria": ["Cosmetic", "Degraded", "Down"]}]}

questions is a list as above, or an object of name -> question. A question with no id is
named q1, q2, ... by its position. "kind" and "text" are accepted for "type" and
"instructions".

  noul    the probability that the instructions are true. Optional criteria: an object
          with "true" and "false" saying what a yes and a no mean.
  choice  one option out of criteria: an object of at least two "option": "what it means".
  score   a position on criteria: a list of at least two levels, lowest first.

Bad input prints {"error": "invalid_request", "detail": ...} and exits 2, and nothing is sent.
A Jev failure prints {"error": code} and exits 2.""")
    p.add_argument("--timeout", type=float, default=5)
    p.set_defaults(func=cmd_ask)

    cli_decide.add_parsers(sub)
    return parser


def main(argv: Any = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
