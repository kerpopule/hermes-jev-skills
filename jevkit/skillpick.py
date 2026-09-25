"""Skill selection: load the one skill a turn needs, or none.

An agent with a hundred skills either reads a hundred descriptions every turn or
guesses. One Jev request ranks the whole catalog against the turn and also asks
whether any skill is needed at all, so most turns load nothing and the rest load
the right one.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from . import client, privacy

logger = logging.getLogger(__name__)

BATCH = 120
WORKERS = 8
# The cap is what the pool ranks in one round trip, so a catalog that grows towards it pays
# in calls, not in waiting. It was a bare 400 when 377 skills were installed, and the same
# fleet has 460 once the external root Hermes reads is counted: 60 skills fell off the end
# in directory order and nothing said so.
MAX_SKILLS = BATCH * WORKERS
FINALISTS = 5
SHORTLIST_FLOOR = 0.02
DESCRIPTION_CHARS = 200

_WARNED: Set[Any] = set()


def _warn_once(key: Any, message: str, *args: Any) -> None:
    # The plugin calls this module on every turn of a gateway that stays up for weeks. A
    # warning per turn buries the log it is supposed to be read in.
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning(message, *args)


# A block scalar header: `>` or `|`, then an indentation indicator and a
# chomping indicator in either order, then an optional comment. `>2-`, `|+2`
# and `> # folded` are all valid headers that a fixed list of strings misses.
_BLOCK_HEADER = re.compile(r"^[>|][0-9+-]*\s*(?:#.*)?$")


def _front_matter(text: str) -> Dict[str, str]:
    """Read the front matter, block scalars included.

    A long description is commonly written as `description: >` with the text
    indented underneath, which is valid YAML and the only readable way to write
    the several sentences a good description needs. Reading the key's own line
    and stopping gave that skill the description ">", so it reached Jev with
    nothing to be ranked on and could only be picked for what its name already
    said.

    This is not a YAML parser and does not try to be. It reads the shapes a
    front matter actually uses: a block runs until a non-blank line is less
    indented than the block's own first line, and its lines are joined with
    spaces. `|` is deliberately normalised to one line like `>` rather than
    keeping its breaks, because the two are the same description to the reader
    downstream: it is about to be truncated to a few hundred characters and put
    in a list for a decision model. For the same reason the indentation
    indicator is accepted but its explicit width is not honoured, and chomping
    is not applied -- the result is stripped either way.
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    fields: Dict[str, str] = {}
    if not match:
        return fields
    lines = match.group(1).splitlines()
    index = 0
    while index < len(lines):
        key, sep, value = lines[index].partition(":")
        if not sep or key.startswith((" ", "\t")):
            index += 1
            continue
        header = _BLOCK_HEADER.match(value.strip())
        if not header:
            fields[key.strip()] = value.strip().strip("'\"")
            index += 1
            continue
        index += 1
        body: List[str] = []
        indent = None
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                body.append("")
                index += 1
                continue
            width = len(line) - len(line.lstrip())
            if indent is None:
                indent = width
            elif width < indent:
                break
            body.append(line[indent:])
            index += 1
        fields[key.strip()] = " ".join(part for part in body if part).strip()
    return fields

def discover(roots: Iterable[Path], disabled: Iterable[str] = ()) -> List[Dict[str, str]]:
    """Find SKILL.md files. Works for Hermes, Claude Code and Codex skill folders alike.

    Returns every skill it finds. It used to cut the list at MAX_SKILLS itself, where
    nothing could report the loss; `pick` is the only place that caps a catalog now,
    because it is the only place that can say so in its reply.
    """
    seen: Dict[str, Dict[str, str]] = {}
    skip = set(disabled)
    for root in roots:
        if not root.is_dir():
            continue
        for skill_file in sorted(root.rglob("SKILL.md")):
            if any(part.startswith(".") or part in ("quarantine", "node_modules") for part in skill_file.parts[len(root.parts):]):
                continue
            try:
                fields = _front_matter(skill_file.read_text(encoding="utf-8", errors="replace")[:4000])
            except OSError:
                continue
            name = fields.get("name") or skill_file.parent.name
            if name not in seen and name not in skip and name not in META_SKILLS and fields.get("description"):
                seen[name] = {"name": name, "description": fields["description"], "path": str(skill_file)}
    return list(seen.values())


# ── where Hermes keeps skills ────────────────────────────────────────────────

_YAML_NULLS = ("", "~", "null", "Null", "NULL")


def _yaml_scalar(raw: str) -> Optional[str]:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else None
    raw = re.sub(r"(^|\s+)#.*$", "", raw).strip()
    # A bare `~` is YAML for "nothing". Read as text it expands to the home folder, and
    # discover() would then walk the person's whole disk on every turn.
    return None if raw in _YAML_NULLS else raw


def _external_dirs_in_yaml(text: str) -> Optional[List[str]]:
    """Read `skills.external_dirs`, and only that, out of a Hermes config.yaml.

    Not a YAML parser, and it must not grow into one: this package is stdlib-only and
    needs one key from one known file. It reads the three shapes Hermes accepts for the
    key (a block list, an inline list, one string). None means "there is a value here I
    cannot read", which is a different thing from an empty list.
    """
    block: List[str] = []
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():                # any top-level line opens or closes a block
            if inside:
                break
            opened = re.match(r"skills\s*:(.*)$", line)
            if opened:
                if _yaml_scalar(opened.group(1)) not in (None, "{}"):
                    return None                  # the whole section written on one line
                inside = True
        elif inside:
            block.append(line)

    items: List[Optional[str]] = []
    child_indent = key_indent = -1
    for line in block:
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if child_indent < 0:
            child_indent = indent
        if key_indent >= 0:                      # inside the block list under the key
            if indent >= key_indent and (stripped == "-" or stripped.startswith("- ")):
                items.append(_yaml_scalar(stripped[1:]))
                continue
            break
        found = re.match(r"external_dirs\s*:(.*)$", stripped)
        # Only a direct child of `skills:`. A deeper key of the same name belongs to
        # something else.
        if not found or indent != child_indent:
            continue
        value = found.group(1).strip()
        if value.startswith("["):
            value = re.sub(r"\]\s+#.*$", "]", value)
            if not value.endswith("]"):
                return None                      # an inline list that runs over several lines
            items = [_yaml_scalar(part) for part in re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', value[1:-1])]
            break
        if value[:1] in ("&", "*", "!", "|", ">", "{"):
            return None                          # anchors, tags, block scalars, inline maps
        if _yaml_scalar(value) is not None:
            items = [_yaml_scalar(value)]
            break
        key_indent = indent
    return [item for item in items if item]


def discover_roots(home: Path, config: Optional[Mapping[str, Any]] = None) -> List[Path]:
    """Every folder Hermes itself reads skills from for this home, in Hermes's order.

    The plugin scanned `<home>/skills` alone. Hermes also reads `skills.external_dirs`,
    which is where a fleet keeps the skills every profile shares, so none of those could
    ever be suggested. The rules are Hermes's own (`get_external_skills_dirs`): local
    folder first so it wins a name clash, then each entry in config order, `~` and
    `$VAR` expanded, a relative entry resolved against the home and not the working
    directory, duplicates and missing folders dropped.

    `config` is the parsed profile config. Leave it out and `<home>/config.yaml` is read
    for that one key. Project-local skill folders are deliberately not included: Hermes
    puts those through a trust check and an injection scan this module cannot repeat.
    """
    home = Path(home)
    local = home / "skills"
    entries: Any = None
    if config is None:
        try:
            entries = _external_dirs_in_yaml((home / "config.yaml").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            entries = []
        if entries is None:
            _warn_once(("unreadable", str(home)), "skills.external_dirs in %s is written in a way jevkit cannot "
                       "read, so those folders are not searched; pass the parsed config to discover_roots()",
                       home / "config.yaml")
    else:
        section = config.get("skills") if isinstance(config, Mapping) else None
        entries = section.get("external_dirs") if isinstance(section, Mapping) else None
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        entries = []

    roots = [local]
    try:
        seen = {local.resolve()}
    except (OSError, RuntimeError):
        seen = {local}
    for entry in entries:
        text = "" if entry is None else str(entry).strip()
        if not text:
            continue
        path = Path(os.path.expanduser(os.path.expandvars(text)))
        try:
            path = (path if path.is_absolute() else home / path).resolve()
        except (OSError, RuntimeError):          # a symlink loop; Hermes skips it too
            continue
        if path not in seen and path.is_dir():
            seen.add(path)
            roots.append(path)
    return roots


# ── the local gate ───────────────────────────────────────────────────────────

# Turns that cannot be asking for a specialised procedure. "ok", "thanks, that worked" —
# the answer is always "no skill", and asking costs the whole round trip on the very turns
# a person notices latency most.
#
# A word belongs here only if it is a whole acknowledgement by itself and cannot start an
# instruction or be the thing one acts on. This list used to hold "do", "go", "keep",
# "hold", "it", "that", "them", "all", "now" and "again" as well, and twenty follow-ups
# made of nothing else ("do all of them now", "stop it", "keep it up", "you do it") were
# answered here as if the person had said "ok".
#
# "stop" and "wait" are verbs and stay: said with no object they interrupt, and no catalog
# has a procedure for that. With an object ("stop it", "stop the gateway") the object is
# not in this list, so the turn is asked.
_ACK_WORDS = frozenset("""
yes yeah yep yup no nope nah ok okay k sure certainly definitely absolutely
thanks ta cheers cool nice great perfect lovely brilliant awesome excellent
gotcha understood right correct exactly agreed fine good indeed alright
please nvm hi hey hello yo hiya morning afternoon evening night
bye later cya lol haha hah hmm hm huh oh ah sorry np
works worked working ready done thats its
and but so well then too also
stop wait
""".split())

# Acknowledgements whose words mean something else apart. They count only as the whole
# phrase, in order: "got it" is here and "it" is not, so "do it" is asked.
#
# "go ahead" and "please do" are consent, and consent is an instruction of a kind, so
# keeping them is a decision and not an oversight. They are the usual reply to an agent's
# "shall I?", which is the turn where a second of waiting is felt most, and they name no
# task: everything they refer to is in the turn before, which the picker is never shown.
# Asked with this gate bypassed, a 381-skill catalog returned no skill for either
# (needs_skill 0.37 and 0.38). Only these two exact phrases skip. "go on", "carry on",
# "keep going", "continue" and "do it" are asked, because each of them readily continues
# into a real instruction ("go on to the next file").
_ACK_PHRASES = frozenset(tuple(phrase.split()) for phrase in (
    "go ahead", "please do",
    "got it", "thank you", "thanks again", "never mind", "no worries", "no problem", "no prob",
    "of course", "all done", "all set", "all good", "all right", "my bad", "hold on",
    "that worked", "that works", "it worked", "it works", "whats up",
    "how are you", "how are you doing", "how are you today", "how are you doing today",
    "hows it going", "how is it going", "how are things", "hows things",
    "good morning", "good night", "nice work", "well done",
))
_CONTINUATION_PHRASES = frozenset(tuple(phrase.split()) for phrase in (
    "next", "and next", "cool next", "ok next", "next one", "go next", "whats next",
))
META_SKILLS = frozenset(("using-superpowers", "skill-selector", "skill-selection"))
_LONGEST_PHRASE = max(len(phrase) for phrase in _ACK_PHRASES | _CONTINUATION_PHRASES)
_MAX_TRIVIAL_WORDS = 6
_QUESTION_MARKS = "?\uff1f\u061f\u00bf"           # ASCII, full-width, Arabic, inverted
_APOSTROPHES = {ord(mark): None for mark in "'\u2019\u02bc"}


def looks_trivial(turn: str) -> bool:
    """True when no catalog could help, decided locally and for free.

    The turn has to be made of acknowledgements and nothing else. Length is not the
    test: "open settings" is two words and is a real request, while "thanks, that worked"
    is three and is not. Whatever this vocabulary cannot read goes to Jev — the safe
    direction is asking, because a wrong ask costs half a cent and a wrong skip is a
    feature that quietly does nothing.
    """
    text = (turn or "").strip()
    if not text:
        return True
    # "all working?" and "that done?" are made of acknowledgement words and are still
    # questions the person wants answered.
    if any(mark in text for mark in _QUESTION_MARKS):
        without_marks = text.translate(str.maketrans("", "", _QUESTION_MARKS))
        continuation = tuple(word for word in re.split(r"[\W_]+", without_marks.lower().translate(_APOSTROPHES)) if word)
        if continuation not in _CONTINUATION_PHRASES:
            return False
    # Nothing to read: punctuation, symbols or emoji only. The test is "no letter or digit
    # in any script". It used to be "no ASCII letter", which is also true of every request
    # written in Chinese, Japanese, Korean, Russian, Arabic, Hebrew, Greek, Hindi or Thai,
    # so all of those were skipped at any length.
    if not any(char.isalnum() for char in text):
        return True
    # Split on what is not a letter or digit in any script, so a word this vocabulary
    # cannot read survives as a word and fails the test below. Splitting on non-ASCII threw
    # those words away, and "ok 请审查这个拉取请求" was left looking like "ok".
    words = [word for word in re.split(r"[\W_]+", text.lower().translate(_APOSTROPHES)) if word]
    # No words at this point means the split lost something readable, never that the turn
    # is trivial: "no words, so trivial" is the line that skipped every non-Latin request.
    # Over the limit, the turn is long enough to carry a real request.
    if not words or len(words) > _MAX_TRIVIAL_WORDS:
        return False
    if tuple(words) in _CONTINUATION_PHRASES:
        return True
    position = 0
    while position < len(words):
        for size in range(min(_LONGEST_PHRASE, len(words) - position), 1, -1):
            if tuple(words[position:position + size]) in _ACK_PHRASES:
                position += size
                break
        else:
            if words[position] not in _ACK_WORDS:
                return False
            position += 1
    return True


# ── ranking ──────────────────────────────────────────────────────────────────

def pick(
    turn: str, skills: List[Dict[str, str]], *, top_k: int = 3, need_threshold: float = 0.5,
    match_threshold: float = 0.5, timeout: float = 5.0, transport: Optional[client.Transport] = None,
    stage_one: Optional[Mapping[int, Mapping[str, float]]] = None, stage_one_latency: Optional[int] = None,
) -> Dict[str, Any]:
    """Which skill, if any, this turn needs.

    `stage_one` (with `stage_one_latency`) is a stage-1 answer already bought — by the same
    request that answered routing's questions, through `jevkit/turn.py`. Everything after
    stage 1, including the stage-2 verification and its thresholds, is unchanged.
    """
    skills = [skill for skill in skills if skill.get("name") not in META_SKILLS]
    if looks_trivial(turn):
        return {"status": "ok", "needs_skill": 0.0, "skills": [], "latency_ms": 0, "skipped": "trivial"}
    if not skills or privacy.is_sensitive(turn):
        return {"status": "fail_open", "reason": "no skills" if not skills else "turn looks sensitive; not sent", "skills": []}
    catalog, dropped = skills[:MAX_SKILLS], skills[MAX_SKILLS:]
    reply = _rank(privacy.redact(turn, 2000), catalog, top_k=top_k, need_threshold=need_threshold,
                  match_threshold=match_threshold, timeout=timeout, transport=transport,
                  stage_one=stage_one, stage_one_latency=stage_one_latency)
    if dropped:
        # "No skill fits" is not a clean answer when some skills were never looked at, so
        # the count travels with every reply and the names go to the log once.
        reply["skills_dropped"] = len(dropped)
        _warn_once(("dropped", len(dropped)),
                   "%d of %d skills were not ranked: the catalog is over the %d-skill cap and the last ones found "
                   "are left out (%s%s). Disable skills you do not use.", len(dropped), len(skills), MAX_SKILLS,
                   ", ".join(skill["name"] for skill in dropped[:5]), ", ..." if len(dropped) > 5 else "")
    return reply


_PICK_INSTRUCTION = "Which skill is the specialised procedure this turn calls for?"


def _pick_name(start: int) -> str:
    """The code-owned id of one batch's stage-1 question.

    Ids never reach the model — the meaning lives in `instructions` — and numbering them by
    the batch's first catalog index is what lets several batches ship in one request.
    """
    return f"pick:{start}"


def _options(catalog: List[Dict[str, str]], start: int) -> Dict[str, str]:
    group = catalog[start:start + BATCH]
    options = {f"S{start + i}": f"{s['name']}: {s['description'][:DESCRIPTION_CHARS]}"
               for i, s in enumerate(group)}
    options["none"] = "No listed skill is a specialised procedure for this turn"
    return options


def stage_one_questions(catalog: List[Dict[str, str]]) -> Dict[str, Any]:
    """Stage 1's question for each batch, for a caller that ships them in its own request.

    `stage_one_answers` turns the reply back into the shape `pick(stage_one=...)` takes, so
    the ranking, the floors and stage 2 are the same code whichever request carried stage 1.
    """
    return {_pick_name(start): client.choice(_PICK_INSTRUCTION, _options(catalog, start))
            for start in range(0, len(catalog), BATCH)}


def stage_one_answers(reply: Mapping[str, Any], catalog: List[Dict[str, str]],
                      latency_ms: Optional[int] = None) -> Dict[int, Dict[str, float]]:
    """Project a reply to `stage_one_questions` into what `pick(stage_one=...)` wants."""
    answers = reply["answers"]
    return {start: answers[_pick_name(start)]["probabilities"] for start in range(0, len(catalog), BATCH)}


def _rank(
    turn_text: str, catalog: List[Dict[str, str]], *, top_k: int, need_threshold: float,
    match_threshold: float, timeout: float, transport: Optional[client.Transport],
    stage_one: Optional[Mapping[int, Mapping[str, float]]] = None, stage_one_latency: Optional[int] = None,
) -> Dict[str, Any]:
    # Stage 1: each batch is one Choice over its skills plus "none". The probabilities rank the whole
    # catalog in one round trip, because the batches run side by side. `stage_one`, when given, is
    # that same answer bought by another caller's request (`jevkit/turn.py`), which is why this
    # function takes probabilities rather than a transport in that case.
    starts = list(range(0, len(catalog), BATCH))
    if stage_one is None:
        def shortlist(start: int) -> Dict[str, Any]:
            question = client.choice(_PICK_INSTRUCTION, _options(catalog, start))
            return client.ask({"turn": turn_text}, {_pick_name(start): question}, timeout=timeout, transport=transport)

        try:
            with ThreadPoolExecutor(max_workers=min(WORKERS, len(starts))) as pool:
                replies = list(pool.map(shortlist, starts))
        except client.JevError as error:
            # One lost batch fails the whole pick. Ranking the batches that did answer would
            # report "no skill" with confidence whenever the right skill sat in the lost one.
            return {"status": "fail_open", "reason": f"Jev unavailable ({error.code})", "skills": []}
        latency = max(reply["latency_ms"] for reply in replies)
        per_batch = [reply["answers"][_pick_name(start)]["probabilities"] for start, reply in zip(starts, replies)]
    else:
        missing = [start for start in starts if start not in stage_one]
        if missing:
            # A merged request that lost a batch is the same failure as a lost round trip.
            return {"status": "fail_open", "reason": f"stage 1 incomplete (batches {missing})", "skills": []}
        latency = int(stage_one_latency or 0)
        per_batch = [stage_one[start] for start in starts]
    ranked: List[tuple] = []
    for batch in per_batch:
        for option, probability in batch.items():
            if option != "none":
                ranked.append((probability, int(option[1:])))
    ranked.sort(reverse=True)
    finalists = [index for probability, index in ranked[:FINALISTS] if probability >= SHORTLIST_FLOOR]
    if not finalists:
        return {"status": "ok", "needs_skill": 0.0, "skills": [], "latency_ms": latency}

    # Stage 2: read the finalists properly, each judged on its own, and allow "none of them".
    state = {"turn": turn_text, "skills": {f"S{i}": f"{catalog[i]['name']}: {catalog[i]['description'][:600]}" for i in finalists}}
    questions: Dict[str, Any] = {
        "needs_skill": client.noul("Doing this turn well requires the specialised instructions of one of these skills")}
    for i in finalists:
        questions[f"s{i}"] = client.noul(f"Skill S{i} is the right specialised procedure for this turn")
    try:
        reply = client.ask(state, questions, timeout=timeout, transport=transport)
    except client.JevError as error:
        return {"status": "fail_open", "reason": f"Jev unavailable ({error.code})", "skills": []}
    answers = reply["answers"]
    need = answers["needs_skill"]["noul"]
    verified = sorted(((answers[f"s{i}"]["noul"], i) for i in finalists), reverse=True)
    chosen = [] if need < need_threshold else [
        {"name": catalog[i]["name"], "path": catalog[i]["path"], "match": round(p, 3)}
        for p, i in verified[:top_k] if p >= match_threshold]
    return {"status": "ok", "needs_skill": round(need, 3), "skills": chosen, "latency_ms": latency + reply["latency_ms"]}
