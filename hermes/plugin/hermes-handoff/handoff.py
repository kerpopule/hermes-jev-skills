"""Handoff: end a session deliberately, and start the next one already knowing what matters.

An agent that never starts fresh drags every past turn into every future one. An agent
that starts fresh with nothing repeats work and re-asks settled questions. A handoff is
the third option: close the session, carry forward a short capsule of what was decided
and what is still open, and begin again light.

Jev reads the transcript and marks which turns must survive word for word. A text model
writes the five-section capsule from that reduced digest. Both jobs are small, and
neither model does the other's.

Everything here degrades rather than fails. No Jev key: every turn is treated as
background and the writer still gets a transcript. No writer model, or a writer that
returns something that is not a capsule: the raw digest is kept instead, which is worse
to read but loses nothing.

Confidentiality is the one exception. Where a capsule may not carry customer detail and
the pieces that guarantee that are missing, nothing is written and the status says why.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

TRIGGERS = frozenset({"handoff", "hand off", "hand-off"})
# The writer used to see the last 24,000 characters and answer in 400 words. Measured
# (evals/compaction, 104 questions): the whole dialogue with a 1,200-word budget recalled
# 58.7% closed-book against 48.1% for that, and 75.0% against 68.3% with one search. The
# longest of those sessions was 118,000 characters of dialogue, about a cent to read.
TRANSCRIPT_CHARS = 300_000
# A confidential lane keeps the old, narrower read. Its capsule is a 400-word breadcrumb
# whatever the writer saw, and at 400 words the measurement says reading everything is worth
# nothing (46.2% against 48.1% for the tail). The gain was at 1,200 words, which these lanes
# do not get. So widening it would send far more of a customer's conversation to the
# auxiliary model to buy nothing, and "no measured benefit" is not a good enough reason.
CONFIDENTIAL_TRANSCRIPT_CHARS = 24_000
CAPSULE_MAX_CHARS = 12_000
# Jev judges 40 turns per request, one request after another. A 600-turn lane was 15
# sequential calls of up to 8 s before the writer even started. The compaction hook has
# always capped this; the plugin never did.
MAX_JUDGED_TURNS = 240
# An 800-message session exports in about half a second. Thirty seconds is a wedged CLI,
# not a slow one, and nothing should wait two minutes to find that out.
EXPORT_TIMEOUT = 30


def home() -> Path:
    # A gateway that serves several profiles from one process scopes each turn to a
    # profile with a context-local override and leaves HERMES_HOME pointing at the root.
    # Reading only the variable there looks for the session in the wrong database, finds
    # nothing, and reports a long conversation as too short to hand off.
    try:
        from hermes_constants import get_hermes_home_override  # type: ignore
        override = get_hermes_home_override()
        if override:
            return Path(override)
    except Exception:  # noqa: BLE001 - not running inside Hermes, or an older one
        pass
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def handoff_dir() -> Path:
    path = home() / "handoffs"
    path.mkdir(parents=True, exist_ok=True)
    return path


LANE_MAX = 120

# A lane becomes a filename, every lane contains ':', and ':' is illegal in a Windows filename.
# The other characters Windows rejects (: * ? " < > |) are already mapped to '_' by the substitution
# in _clean(); ':' is kept there deliberately, so it is handled separately below.


def _clean(key: str) -> str:
    """A filesystem-safe lane key that cannot collide.

    Conversation ids are long — Teams' run to 131 characters — so a plain truncation
    would make two conversations share a lane whenever they agree on their first N
    characters. That is not a cosmetic bug: the capsule from one customer's conversation
    would be handed to another. Past the limit, keep a readable prefix and let a hash of
    the FULL key carry the identity.
    """
    # '%' is allowed through so the percent-encoding below SURVIVES a second pass: capsule_path()
    # re-cleans whatever lane it is handed, so without this a cleaned "telegram%3A1" came back as
    # "telegram_3A1" and the writer and the reader disagreed (measured: 5 failing tests).
    # Residual, documented: a lane that literally contained "%3A" would alias an encoded colon —
    # platform names are words and chat/thread ids are numeric or GUID-shaped, so it cannot arise.
    safe = re.sub(r"[^A-Za-z0-9._:%-]", "_", key)
    # The lane is also a FILENAME: ``handoff-<lane>.md`` and ``pending-<lane>.json``. ':' is illegal
    # in a Windows filename and NTFS does not raise — it stores everything after the colon as an
    # ALTERNATE DATA STREAM. Python then reads and writes that path happily, while
    # ls/Explorer/git/backup tools see a single extensionless file named ``handoff-telegram`` shared
    # by EVERY Telegram lane; deleting that one innocuous file destroys every lane's capsule at
    # once. Two or more colons — every thread/topic lane — is not even a stream: it is
    # ``[Errno 22] Invalid argument``, so ``build()`` returns ``write_failed`` and no capsule exists.
    #
    # PERCENT-ENCODED rather than substituted with '_': ':' is the STRUCTURAL separator, so mapping
    # it to '_' would make lane "telegram:a_b" (chat "a_b") and "telegram:a:b" (chat "a", thread
    # "b") the SAME key — the collision this function promises never to allow, and the one that
    # hands one conversation's capsule to another. '%' cannot appear in ``safe`` here (the
    # substitution above maps it to '_'), so "%3A" is unambiguous.
    #
    # Applied on EVERY platform rather than behind ``os.name == "nt"``: CI is Linux and macOS, where
    # ':' is a legal filename character, so a platform-gated fix could never be tested there. Writer
    # and reader both go through _clean, so they keep agreeing on the key. Migration cost on POSIX: an
    # existing capsule file is orphaned once (capsules are NOT pruned or TTL'd — only the pending
    # marker is), so build() falls back to the pre-encoding filename when reading "previous".
    safe = safe.replace(":", "%3A")
    if len(safe) <= LANE_MAX:
        return safe
    fingerprint = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:20]
    return f"{safe[:LANE_MAX - len(fingerprint) - 1]}-{fingerprint}"


def lane_from_session(session_id: str, *, state_db: Optional[Path] = None) -> Optional[str]:
    """Look the conversation up by session id.

    The hook that writes a capsule and the hook that injects it do not receive the same
    fields — ``pre_llm_call`` gets ``platform`` and ``sender_id`` but not ``chat_id``. A
    capsule keyed on one and read with the other never matches, and the feature fails
    silently. The session store has the conversation identity for both, so ask it.
    """
    if not session_id:
        return None
    db = state_db or (home() / "state.db")
    if not db.is_file():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        columns = {r[1] for r in conn.execute("pragma table_info(sessions)")}
        wanted = [c for c in ("source", "chat_id", "thread_id") if c in columns]
        if not wanted:
            conn.close()
            return None
        row = conn.execute(f"select {', '.join(wanted)} from sessions where id=?", (session_id,)).fetchone()
        conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    key = ":".join(str(v or "") for v in row).strip(":")
    return _clean(key) if key else None


def open_session_for(context: Mapping[str, Any], *, state_db: Optional[Path] = None) -> Optional[str]:
    """The newest open session for a conversation, read from the session store's database.

    The gateway's own key-to-session mapping is the authority and the plugin asks that
    first. This is what is left when a host renames those methods: a conversation can
    have several rows that were never marked ended, and the one still in use is the one
    started last.
    """
    chat = str(context.get("chat_id") or "")
    if not chat:
        return None
    db = state_db or (home() / "state.db")
    if not db.is_file():
        return None
    try:
        import sqlite3
        # One second, not the usual ten: this runs on the gateway's dispatch path, and a
        # locked database must cost the person a missing capsule, not a frozen chat.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            columns = {r[1] for r in conn.execute("pragma table_info(sessions)")}
            if not {"id", "chat_id", "ended_at", "started_at"} <= columns:
                return None
            where, values = ["chat_id=?", "ended_at is null"], [chat]
            platform = str(context.get("platform") or context.get("source") or "")
            if platform and "source" in columns:
                where.append("source=?")
                values.append(platform)
            if "thread_id" in columns:
                where.append("coalesce(thread_id, '')=?")
                values.append(str(context.get("thread_id") or ""))
            row = conn.execute(f"select id from sessions where {' and '.join(where)} "
                               "order by started_at desc limit 1", values).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    return str(row[0]) if row and row[0] else None


def lane_key(context: Mapping[str, Any]) -> str:
    """Stable per-conversation key. One capsule per conversation, not per session id."""
    # `platform` is what the hooks call it; `source` is what the session store calls it.
    # One conversation must produce one key whichever side is asking.
    platform = str(context.get("platform") or context.get("source") or "")
    parts = [platform, str(context.get("chat_id") or ""), str(context.get("thread_id") or "")]
    direct = ":".join(parts).strip(":")
    # A platform with no conversation id is not an identity — every chat would collapse
    # onto one key. Resolve it from the session store instead.
    if not context.get("chat_id"):
        resolved = lane_from_session(str(context.get("session_id") or ""))
        if resolved:
            return resolved
        sender = str(context.get("sender_id") or "")
        if sender:
            direct = ":".join(p for p in (parts[0], sender) if p)
    key = direct or str(context.get("session_id") or "default")
    return _clean(key)


CONFIDENTIAL_MARKER = "CONFIDENTIAL"


def confidential_here() -> bool:
    """Whether capsules for this home must carry no customer detail.

    There are three ways to switch this on, and that is deliberate. A host may not
    expose a plugin-config API at all — ask one that does not and you get the default
    back, silently, which here means writing customer detail to disk against a rule that
    forbids it. So the marker file is the authority: it is one ``ls`` to verify, it
    cannot be swallowed by an exception handler, and it travels with the profile it
    protects.
    """
    if str(os.environ.get("HANDOFF_CONFIDENTIAL", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        # Not handoff_dir(): that creates the directory, and the nightly script asks this
        # question during --dry-run, which promises to touch nothing.
        return (home() / "handoffs" / CONFIDENTIAL_MARKER).exists()
    except OSError:
        return False


def capsule_path(lane: str) -> Path:
    # _clean() at the PATH boundary, not only in lane_key(): build() and take_pending() are both
    # callable with a lane from anywhere (tests, the nightly script, a third-party generator), and a
    # raw "platform:chat:thread" lane produced "[Errno 22] Invalid argument" on Windows — one colon
    # was a silent NTFS alternate data stream, two were an outright failure. Cleaning here is
    # idempotent, so an already-clean key is unchanged and both sides always agree on the filename.
    return handoff_dir() / f"handoff-{_clean(lane)}.md"


def pending_path(lane: str) -> Path:
    return handoff_dir() / f"pending-{_clean(lane)}.json"


def is_trigger(text: Any) -> bool:
    """Only an exact, bare word. A sentence that mentions a handoff is a normal message."""
    if not isinstance(text, str):
        return False
    cleaned = text.strip().strip(".!").lower()
    return cleaned in TRIGGERS


# ── reading the conversation ─────────────────────────────────────────────────

def _hermes_bin() -> List[str]:
    """The packaged CLI is the stable contract; internals are not.

    HERMES_HOME may point at a PROFILE (that is how per-profile state is addressed), but
    the interpreter lives under the installation root. Look in both, and let
    ``HERMES_CLI`` override when an install puts it somewhere else entirely.
    """
    override = os.environ.get("HERMES_CLI")
    if override and Path(override).exists():
        return [override]
    roots = [home()]
    parent = home().parent
    if parent.name == "profiles":                      # <root>/profiles/<name> -> <root>
        roots.append(parent.parent)
    for root in roots:
        direct = root / "hermes-agent" / "venv" / "bin" / "hermes"
        if direct.exists():
            return [str(direct)]
        for candidate in sorted(root.glob("hermes-agent/venv*/bin/hermes")):
            return [str(candidate)]
    return ["hermes"]


def export_messages(session_id: str, *, runner: Optional[Any] = None) -> List[Dict[str, str]]:
    """The session's user/assistant turns, via the CLI rather than the session store.

    The CLI is a contract that survives upgrades; the store's schema is not.
    """
    run = runner or subprocess.run
    try:
        # The child only sees the environment, so the home this process resolved (which
        # may come from a per-turn override, see home()) has to be handed to it by name.
        done = run(_hermes_bin() + ["sessions", "export", "--session-id", str(session_id),
                                    "--format", "jsonl", "-"],
                   capture_output=True, text=True, timeout=EXPORT_TIMEOUT,
                   env={**os.environ, "HERMES_HOME": str(home())})
    except Exception:  # noqa: BLE001 - a handoff must never take the session down with it
        return []
    if getattr(done, "returncode", 1) != 0:
        return []
    messages: List[Dict[str, str]] = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for message in row.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            if isinstance(content, str) and content.strip():
                messages.append({"role": message["role"], "content": content.strip()})
    return messages


def extract_text(response: Any) -> str:
    """Pull the text out of whatever the host's LLM client returned.

    Hosts differ: a plain string, an OpenAI-shaped dict, or a ``ChatCompletion``
    object with attributes and no ``.get``. Assuming one shape is how the writer
    silently failed and every capsule quietly became a raw transcript.
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    choices = None
    if isinstance(response, Mapping):
        choices = response.get("choices")
    else:
        choices = getattr(response, "choices", None)
    if not choices:
        # Some clients return the content directly on the object.
        for attribute in ("content", "text", "output_text"):
            value = getattr(response, attribute, None)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else getattr(first, "message", None)
    if message is None:
        value = first.get("text") if isinstance(first, Mapping) else getattr(first, "text", None)
        return value if isinstance(value, str) else ""
    content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    if isinstance(content, list):      # content-parts form
        content = " ".join(str((p.get("text") if isinstance(p, Mapping) else getattr(p, "text", "")) or "")
                           for p in content)
    return content if isinstance(content, str) else ""


# ── writing the capsule ──────────────────────────────────────────────────────

# Only reached when a caller asks for confidentiality and supplies no prompt builder.
# jevkit's handoff_prompt(confidential=True) is the real rule and is what every shipped
# caller uses; this exists so that path can never mean "the writer was told nothing".
_CONFIDENTIAL_FALLBACK_PROMPT = (
    "Write a handoff breadcrumb for the work in the transcript below, under these headings: "
    "## Working on, ## State, ## Next. Record only the task, what is still missing, who must "
    "approve it, and the next safe action. Never carry a person's name, an email address, a "
    "phone number, a street address, a document or file name, a link, an account or invoice "
    "number, or any payment or health detail; write \"the customer\" or \"the staff member\" "
    "instead. If a section would have nothing left, write \"nothing recorded\".\n\n"
    "TRANSCRIPT:\n\n"
)


def build(
    session_id: str, lane: str, *, write: Any, select: Any = None, digest: Any = None,
    prompt_for: Any = None, valid: Any = None, runner: Optional[Any] = None,
    confidential: bool = False, scrub: Any = None,
) -> Dict[str, Any]:
    """Produce and store the capsule. `write(prompt) -> str` is the text model.

    The jevkit callables are injected so this module stays importable, and testable,
    on a machine with no Jev key and no network.
    """
    if confidential and scrub is None:
        # No scrubber means no jevkit, which means no confidential prompt and no validator
        # either: the writer's free text about a customer would go to disk unchecked.
        # Decided before the export so a refusal costs nothing and reads nothing.
        return {"status": "confidential_unsupported", "reason": "no_scrub"}

    messages = export_messages(session_id, runner=runner)
    if len(messages) < 4:
        return {"status": "too_short", "messages": len(messages)}

    # `jev` travels with the result because status "ok" only means a capsule was written.
    # A Jev outage still writes one, from an unfiltered transcript, and whoever reads the
    # report has to be able to tell that night from a good one.
    budget = CONFIDENTIAL_TRANSCRIPT_CHARS if confidential else TRANSCRIPT_CHARS
    jev_calls, counts, jev, jev_errors = 0, {}, "not_used", []
    marked = False                  # does `body` carry [KEEP VERBATIM] / [background] tags?
    if select and digest:
        try:
            recent = messages[-MAX_JUDGED_TURNS:]
            selection = select(recent, keep_last=8)
            counts = selection.get("counts") or {}
            jev_calls = selection.get("jev_calls") or 0
            jev = str(selection.get("status") or "ok")
            jev_errors = list(selection.get("errors") or [])
            body = digest(recent, selection, budget)
            marked = True
        except Exception as error:  # noqa: BLE001
            jev, jev_errors = "error", [type(error).__name__]
            body = _plain(messages, budget)
    else:
        body = _plain(messages, budget)

    previous = ""
    existing = capsule_path(lane)
    if not existing.exists() and _clean(lane) != lane and os.name != "nt":
        # The lane key gained percent-encoding, so a capsule written by an older build sits at the
        # unencoded name. Capsules are never pruned and are read back as "previous" context, so
        # without this fallback one upgrade silently breaks the chain instead of costing "at most
        # one handoff".
        legacy = handoff_dir() / f"handoff-{lane}.md"
        if legacy.exists():
            existing = legacy
    if existing.exists():
        try:
            previous = existing.read_text(encoding="utf-8")
        except OSError:
            previous = ""
        # The old Recovery block names the OLD session. Left in, the writer copies it into
        # the new capsule, and the note ends with two blocks pointing at different sessions.
        previous = previous.split("\n## Recovery", 1)[0]

    capsule = ""
    try:
        if prompt_for:
            try:
                try:
                    prompt = prompt_for(body, previous, confidential=confidential, marked=marked)
                except TypeError:
                    # A jevkit from before `marked`. Its prompt talks about markers that a
                    # plain transcript does not have, which is untidy but not unsafe.
                    prompt = prompt_for(body, previous, confidential=confidential)
            except TypeError:
                # An older jevkit has no confidentiality mode. This used to carry on when a
                # scrubber was supplied and still report "ok", on the theory that the regex
                # layer was enough. It is not: a regex removes a phone number and leaves
                # "Jane Doe wants the quote revised" exactly as written. Only the
                # prompt can keep a name out, so without it there is no capsule.
                if confidential:
                    return {"status": "confidential_unsupported", "reason": "prompt_has_no_confidential_mode"}
                prompt = prompt_for(body, previous)
        elif confidential:
            # No prompt builder at all. Handing the writer a bare transcript would leave
            # the scrubber as the only control, which is the same downgrade by another door.
            prompt = _CONFIDENTIAL_FALLBACK_PROMPT + body
        else:
            prompt = body
        capsule = (write(prompt) or "").strip()
    except Exception:  # noqa: BLE001
        capsule = ""
    if valid and not valid(capsule):
        # The writer refused, timed out, or answered something else. The digest is a worse
        # read than a capsule but it loses nothing, which matters more.
        if confidential:
            # The fallback is the raw transcript. Under a confidentiality contract that
            # is the one thing we must never write, so the capsule says nothing instead.
            # A thin handoff is a bad morning; a transcript on disk is a broken promise.
            capsule = ("## Working on\nA previous session ended without a usable handoff, and its "
                       "transcript may not be carried forward under this profile's continuity rules. "
                       "Ask the person what they were working on.\n\n## Next\nRe-establish the task "
                       "from the person, not from stored history.")
        else:
            capsule = ("## Working on\nThe writer model did not return a usable handoff, so this is the "
                       "filtered transcript instead. Lines marked KEEP VERBATIM are the ones that matter.\n\n"
                       "## State\n" + body[:CAPSULE_MAX_CHARS])

    if scrub:
        # Mechanical second pass. Runs even when the writer behaved, because "it looked
        # fine last time" is not a privacy control.
        try:
            capsule = scrub(capsule)
        except Exception:  # noqa: BLE001
            if confidential:
                return {"status": "scrub_failed"}

    capsule = _fit(capsule, CAPSULE_MAX_CHARS)
    if not confidential:
        # A confidential lane carries a breadcrumb on purpose, and a list of file names and
        # ids is exactly what its contract forbids. Everyone else gets the way back.
        capsule += "\n\n" + recovery_block(session_id, messages)
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    text = f"# Handoff — {stamp}\n\n{capsule}\n"
    try:
        capsule_path(lane).write_text(text, encoding="utf-8")
        # The marker names the CLEANED lane, because that is what the file on disk is keyed on;
        # writing the raw lane here would leave a marker pointing at a filename that does not exist
        # (the two-identity split this patch exists to remove). The raw form is kept for debugging.
        pending_path(lane).write_text(json.dumps({"at": time.time(), "lane": _clean(lane),
                                                  "raw_lane": lane,
                                                  "session_id": str(session_id)}), encoding="utf-8")
    except OSError as error:
        return {"status": "write_failed", "error": str(error)[:200]}
    return {"status": "ok", "lane": _clean(lane), "path": str(capsule_path(lane)),
            "messages": len(messages),
            "jev": jev, "jev_errors": jev_errors, "jev_calls": jev_calls, "counts": counts,
            "chars": len(text)}


def jev_prepass_enabled() -> bool:
    """Whether Jev marks the turns before the writer sees them. Off unless asked for.

    It used to be on whenever a key was present, on the belief that a handoff written from
    Jev's keep / summarize / drop digest "stops losing the one line that mattered". Measured
    on seven real sessions and 104 exam questions (evals/compaction), a capsule written
    from that digest answered 37.5% closed-book against 48.1% for one written from the
    plain tail of the same size: 4 wins, 15 losses question by question. With one search of
    the old session allowed they tied. Jev's marks did beat the same marks assigned by
    recency (11 wins to 4), so the judgement is real; it was the digest built around it
    that cost more than the judgement earned.

    So the default is the path that measured best, is free, adds no latency and sends
    nothing anywhere. ``HANDOFF_JEV=1`` turns the pre-pass back on.
    """
    return os.environ.get("HANDOFF_JEV", "").strip().lower() in ("1", "true", "yes", "on")


def _fit(capsule: str, limit: int) -> str:
    """Trim an over-long capsule from the middle, never from the end.

    The headings run Working on, State, Decisions, Pointers, Next. A plain ``[:limit]``
    cut therefore removed Pointers and Next first: the exact paths and the first thing to
    do, which are the two sections the next session cannot reconstruct.
    """
    if len(capsule) <= limit:
        return capsule
    at = capsule.find("## Pointers")
    tail = capsule[at:] if at > 0 else ""
    if not tail or len(tail) > limit * 0.6:
        return capsule[:limit]
    note = "\n[… trimmed to fit …]\n\n"
    return capsule[:max(0, limit - len(tail) - len(note))].rstrip() + note + tail


def recovery_block(session_id: str, messages: List[Dict[str, str]]) -> str:
    """How the next session gets back to what this note left out. No model writes this.

    A capsule cannot hold a working session, and measuring it says so: on seven real
    sessions the best capsule answered 59% of a recall exam alone and 75% with one search
    of the old session, and a session with NO capsule and one search answered 57%
    (evals/compaction; Nous Research found the same shape, 43% to 79%, in hermes-agent PR
    116246). This capsule used to carry no session id and never said a search exists, so
    the one mechanism shown to work was out of reach of whoever read it.

    The call shapes are spelled out because the obvious one is wrong: passing ``query``
    together with ``session_id`` reads the session from the top and ignores the query.

    A list of the identifiers the session used was tried here too. It changed nothing with
    a capsule and cost 13 points without one, so it is not here.
    """
    return "\n".join([
        "## Recovery",
        f"This note summarises session `{session_id}` ({len(messages)} messages). The full session is still "
        "searchable, and looking a value up is cheaper than guessing it or asking the person again.",
        '- Find it: `session_search(query="2 to 5 keywords")`. Each result carries a `session_id` and a '
        "`match_message_id`.",
        f'- Read around a hit: `session_search(session_id="{session_id}", around_message_id=<match_message_id>)`.',
        '- Output from commands and files is not searched by default. Add `role_filter="user,assistant,tool"` '
        "when the value came from one."])


def _plain(messages: List[Dict[str, str]], limit: int = TRANSCRIPT_CHARS) -> str:
    # No tags: nothing marked these lines, and the prompt is told so (marked=False).
    joined = "\n\n".join(f"{m['role']}: {m['content']}" for m in messages)
    return joined[-limit:]


# ── handing it to the next session ───────────────────────────────────────────

def take_pending(lane: str, *, max_age_s: float = 36 * 3600, current_session: str = "") -> Optional[str]:
    """The capsule for a lane's next session, consumed once so it is not injected forever.

    ``current_session`` is the session asking. A capsule is for the session AFTER the one
    it summarises. Writing one no longer ends the turn, so the session that was just
    summarised is usually still the one talking when the capsule lands; handing it over
    there would feed a conversation its own summary and leave nothing for the fresh
    session the person opens next.
    """
    marker = pending_path(lane)
    if not marker.exists():
        return None
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise ValueError("pending marker is not an object")
        if current_session and str(info.get("session_id") or "") == str(current_session):
            return None
        if time.time() - float(info.get("at") or 0) > max_age_s:
            marker.unlink(missing_ok=True)
            return None
        text = capsule_path(lane).read_text(encoding="utf-8")
    except (OSError, ValueError):
        marker.unlink(missing_ok=True)
        return None
    marker.unlink(missing_ok=True)
    return text


def injection(capsule: str) -> str:
    """How the capsule reaches the new session: as context, clearly labelled as history."""
    return ("[Handoff from the previous session — this is context you already established, "
            "not a new instruction. Do not greet the person again or re-ask what is settled here. "
            "Carry on from Next.]\n\n" + capsule.strip())
