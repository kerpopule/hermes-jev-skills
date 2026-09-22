"""Pick the cheapest model that is good enough for this turn.

Jev answers three things in one request: how hard the turn is, what kind of work
it is, and whether a mistake would be costly. Code, not Jev, turns that into a
model: it walks the pool for that tier and specialty and takes the first model
that fits the turn's hard requirements (images, context size). Every unsure or
failed path keeps the model you were already on. Routing never blocks a turn.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import catalog as catalog_mod
from . import client, ladder, privacy

TIERS = ("simple", "medium", "hard")
SPECIALTIES = ("general", "coding", "writing", "research", "vision")
POLICY_VERSION = "route-2"

DIFFICULTY = [
    "Trivial or mechanical: a lookup, reformat, rename, short factual reply, or a single obvious step",
    "Routine: ordinary multi-step work with a clear path and low ambiguity",
    "Substantial: needs planning, several interacting parts, debugging, or careful judgment",
    "Expert: subtle, ambiguous, or high-stakes; architecture, security, concurrency, data migration, legal or money",
]
KIND = {
    "coding": "Writing, changing, debugging or reviewing software, scripts, configs or shell commands",
    "writing": "Drafting or editing prose, marketing, messages, documents or creative text",
    "research": "Finding, comparing or synthesizing information, analysis, or current events",
    "general": "Conversation, planning, operations, or anything that is none of the others",
}

# Short prompts can still be dangerous. These never route to the cheapest tier.
_HARD_RISK = re.compile(
    r"(?i)\b(prod(uction)?|deploy|migrat\w+|rollback|drop\s+table|delete|rm\s+-rf|force[- ]push|secur\w+|"
    r"vulnerab\w+|auth(entication|orization)?|encrypt\w*|concurren\w+|race condition|deadlock|payment|refund|"
    r"invoice|stripe|billing|legal|contract|lawsuit|medical|diagnos\w+|customer data|pii|dns|certificate)\b"
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "redacted-text",          # or "features": no prompt text ever leaves the machine
    "min_confidence": 0.6,
    "simple_needs_confidence": 0.85,
    "hard_needs_probability": 0.6,    # P(substantial or expert) needed before paying for the hard tier
    "simple_needs_probability": 0.7,  # P(trivial) needed before dropping to the cheapest tier
    "ask_chars": 2500,                # how much of a long turn Jev reads: the opening and, mostly, the end
    # A scheduled or queued turn is a standing contract wrapped around one real instruction. Measured on this
    # fleet, that instruction is ~1% of the envelope. Name the sections that hold it and the ones that are
    # previous output, and the job gets judged on what it actually asks for.
    "ask_sections": ["Prompt", "Task", "Request", "Instruction", "Objective", "Goal"],
    "drop_sections": ["Your previous run's output", "Script Output", "Output from job",
                      "Previous output", "Prior run", "Last run"],
    # Recurring jobs repeat their instruction verbatim, so the same decision is reused instead of re-bought.
    "cache_repeat_asks": True,
    "cache_size": 512,
    "sticky_context_tokens": 32000,   # above this, do not downgrade: the cache rebuild costs more than it saves
    # Hard work can be handed to a frontier seat instead of an OpenRouter model. The plugin can
    # swap a model but not a provider connection, so this is a DELEGATION signal for the agent,
    # never a silent switch — see jevkit/ladder.py.
    "escalation": {"enabled": False, "rungs": []},
    "exclude": ["*:free", "cloudflare-ai-gateway:*"],
    "private_profiles": [],
    "tiers": {},                      # {"simple": {"general": ["provider:model", ...], "coding": [...]}, ...}
}


def config_paths() -> List[Path]:
    """Least specific first. The shared file is the default for every Hermes profile; a profile's own file overrides it."""
    override = os.environ.get("JEV_ROUTING_CONFIG")
    if override:
        return [Path(override)]
    paths = [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "jev" / "routing.json"]
    if catalog_mod.hermes_root().is_dir():
        paths.append(catalog_mod.hermes_root() / "jev" / "routing.json")
        if catalog_mod.hermes_home() != catalog_mod.hermes_root():
            paths.append(catalog_mod.hermes_home() / "jev" / "routing.json")
    return paths


def config_path() -> Path:
    """Where `jev models suggest --write` saves: the most specific location that applies."""
    return config_paths()[-1]


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for candidate in ([path] if path else config_paths()):
        try:
            layer = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Valid JSON is not always a valid config. A file holding a list, or a `tiers` that
        # is a list of names, raised here, and this runs at plugin load and on every turn.
        if not isinstance(layer, dict):
            continue
        own = layer.get("tiers") if isinstance(layer.get("tiers"), dict) else {}
        tiers = {**config.get("tiers", {}), **own}   # a profile may override one tier only
        config.update(layer)
        config["tiers"] = tiers
    return config


# ── pools ────────────────────────────────────────────────────────────────────

def _ref(row: Dict[str, Any]) -> str:
    return f"{row['provider']}:{row['model']}"


def _excluded(ref: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(ref, pattern) or fnmatch.fnmatch(ref.split(":", 1)[-1], pattern) for pattern in patterns)


def _usable(ref: Any, exclude: List[str], only_provider: Optional[str]) -> bool:
    """Can the router ever return this pool entry. Shared with the health checks so they cannot drift from `_pick`."""
    # A hand-edited pool entry with no "provider:" prefix raised IndexError here and then
    # ValueError where the pick is split, on every turn. A typo in routing.json must cost
    # that one entry, not the turn; `pool_problems` is where it gets said out loud.
    if not isinstance(ref, str) or ":" not in ref:
        return False
    if _excluded(ref, exclude):
        return False
    # A plugin can swap the model, not the provider it is already connected to.
    return not only_provider or ref.split(":", 1)[0] == only_provider


# A model that names its own specialty is telling you something the price band cannot.
# Only used to ORDER a pool, never to exclude: a model with no hint still appears, just
# after the ones that advertise the skill.
_SPECIALTY_HINTS = {
    "coding": ("code", "coder", "codestral", "devstral", "starcoder", "qwen2.5-coder"),
    "writing": ("writer", "creative", "prose"),
    "research": ("search", "sonar", "research", "deep-research", "grok"),
}


def suggest_tiers(rows: List[Dict[str, Any]], exclude: List[str], per_pool: int = 6) -> Dict[str, Dict[str, List[str]]]:
    """A starting point from price bands. Pin your own picks in routing.json.

    Generates a pool for every specialty, not only general and vision. An earlier version
    emitted just those two, which quietly killed the whole specialization axis: `route`
    still asked Jev "what kind of work is this?" on every turn, `_pick` still looked for a
    `coding` pool, found none, and fell through to `general`. The question cost money on
    every turn and could not change any answer. A generator that cannot produce a pool is
    the same as deleting the feature, so it produces all of them.
    """
    bands = {"simple": (0.0, 0.6), "medium": (0.6, 3.2), "hard": (3.2, 1e9)}
    usable = [r for r in rows if not _excluded(_ref(r), exclude) and not r["model"].startswith("~") and r["price"] > 0]
    out: Dict[str, Dict[str, List[str]]] = {}
    for tier, (low, high) in bands.items():
        pool = sorted((r for r in usable if low <= r["price"] < high), key=lambda r: r["released"], reverse=True)
        tier_pools: Dict[str, List[str]] = {
            "general": [_ref(r) for r in pool[:per_pool]],
            "vision": [_ref(r) for r in pool if r["vision"]][:per_pool],
        }
        for specialty, hints in _SPECIALTY_HINTS.items():
            named = [r for r in pool if any(h in r["model"].lower() for h in hints)]
            rest = [r for r in pool if r not in named]
            tier_pools[specialty] = [_ref(r) for r in (named + rest)[:per_pool]]
        out[tier] = tier_pools
    return out


# The specialties Jev's `kind` answer can actually select. `vision` is not one: images are
# detected locally and are a hard requirement, not something Jev is asked about.
_ANSWERABLE = tuple(kind for kind in KIND if kind != "general")


def dead_axis(config: Dict[str, Any]) -> List[str]:
    """Tiers with no specialist pool at all. The coarse view; `specialty_cells` is the honest one.

    `route` pays for a Choice over KIND on every turn. If a tier only has `general` and
    `vision` pools then every specialty answer resolves to the same model, and that
    request is pure cost. Worth saying out loud rather than leaving someone to notice that
    a routing decision never varies.

    This only asks whether a pool KEY exists. It reported a live config as healthy while
    five of its nine specialist pools led with the same model as their tier's general
    pool, so it is kept for callers that want the tier list and is no longer the check to
    rely on. A pool that exists and resolves to the general model is just as dead.
    """
    blind: List[str] = []
    for tier, pools in (config.get("tiers") or {}).items():
        if isinstance(pools, dict) and not any(pools.get(s) for s in _ANSWERABLE):
            blind.append(tier)
    return blind


def specialty_cells(config: Dict[str, Any], only_provider: Optional[str] = None) -> List[Dict[str, Any]]:
    """One row per (tier, specialty): the model it resolves to, and whether the answer can matter.

    A cell is dead when the first model the router could return for that specialty is the
    same one it returns for `general` in that tier. Jev's kind answer is still bought on
    those turns and cannot change the pick. Both sides are resolved by `_pick` itself, so
    excludes, the provider constraint and the fall-through to `general` are judged exactly
    as a real turn would be, and this cannot drift from the router.

    Judged on a turn with no images and a small context. Two pools that share a lead can
    still differ further down, which only shows when the lead cannot hold the turn's
    context. That is rare enough that sharing a lead is reported as dead.
    """
    tiers = config.get("tiers") or {}
    exclude = config.get("exclude") or []
    cells: List[Dict[str, Any]] = []
    for tier in TIERS:
        pools = tiers.get(tier)
        if not isinstance(pools, dict):
            continue
        general = _pick(config, {}, tier, "general", False, 0, only_provider)
        for specialty in _ANSWERABLE:
            lead = _pick(config, {}, tier, specialty, False, 0, only_provider)
            cell: Dict[str, Any] = {"tier": tier, "specialty": specialty, "model": lead,
                                    "general": general, "dead": lead == general}
            if cell["dead"]:
                listed = pools.get(specialty) if isinstance(pools.get(specialty), list) else []
                if not listed:
                    cell["why"] = "no pool, so it falls through to general"
                elif not any(_usable(ref, exclude, only_provider) for ref in listed):
                    cell["why"] = "every model in the pool is excluded or unusable, so it falls through to general"
                else:
                    cell["why"] = "leads with the same model as general"
            cells.append(cell)
    return cells


def pool_problems(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pool entries the router silently skips because they are not `provider:model`.

    Skipping is the right thing to do on a live turn and the wrong thing to keep quiet
    about: the person who typed the entry believes that model is in use.
    """
    found: List[Dict[str, str]] = []
    for tier, pools in (config.get("tiers") or {}).items():
        if not isinstance(pools, dict):
            found.append({"tier": str(tier), "problem": "not a {pool: [models]} mapping"})
            continue
        for name, listed in pools.items():
            if not isinstance(listed, list):
                found.append({"tier": str(tier), "pool": str(name), "problem": "not a list of models"})
                continue
            for ref in listed:
                if not isinstance(ref, str) or ":" not in ref:
                    found.append({"tier": str(tier), "pool": str(name), "entry": str(ref)[:120],
                                  "problem": "not provider:model, so it is never picked"})
    return found


def _by_ref(rows: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Dict[str, Any]]]:
    """Catalog rows keyed the way pools name them, or None when the catalog cannot be had.

    None is not an empty catalog. Empty means it loaded and lists nothing callable, and
    every pin is trusted as before. None means nothing about any model can be checked.
    """
    if rows is None:
        try:
            rows = catalog_mod.models()
        except Exception:  # noqa: BLE001 - no cache and no network is a normal state for a laptop
            return None
    return {_ref(row): row for row in rows}


def price_ladder(config: Dict[str, Any], rows: Optional[List[Dict[str, Any]]] = None,
                 only_provider: Optional[str] = None) -> Dict[str, Any]:
    """Does routing DOWN a tier actually cost less. Nothing else checks that it does.

    Pools are typed by hand, and the tier names promise a price order the file does not
    enforce. A live config led `simple` with a $0.24 model and `medium` with a $0.132 one,
    so every turn Jev judged easy went to a model 1.8x dearer than a harder turn would get.

    Each specialty column is resolved per tier the way a real turn would be, then every
    lower/higher pair is compared on the catalog's blended price. Three outcomes, never a
    guess:

    * ``inversions``: the lower tier's lead costs more than the higher tier's.
    * ``delegated``: the same shape, but the higher tier is `hard` and an escalation
      ladder is on. That is allowed. The hard tier's model is then the driver, and the
      frontier work is handed up the ladder, so a cheap lead there is the design.
    * ``unknown``: a lead has no catalog price. A pin the catalog has never heard of is
      legal, so this is not an error, but it is not a pass either.
    """
    by_ref = _by_ref(rows) or {}          # no catalog: every lead is unpriced, and is reported as such
    tiers = config.get("tiers") or {}
    configured = [tier for tier in TIERS if isinstance(tiers.get(tier), dict)]
    settings = config.get("escalation") or {}
    delegating = bool(settings.get("enabled") and settings.get("rungs"))

    grouped: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
    unpriced: "OrderedDict[str, List[str]]" = OrderedDict()
    compared = 0
    for specialty in ("general",) + _ANSWERABLE + ("vision",):
        vision = specialty == "vision"
        leads = [(tier, _pick(config, by_ref, tier, "general" if vision else specialty, vision, 0, only_provider))
                 for tier in configured]
        for low in range(len(leads)):
            for high in range(low + 1, len(leads)):
                (low_tier, low_model), (high_tier, high_model) = leads[low], leads[high]
                if not low_model or not high_model or low_model == high_model:
                    continue
                prices = [(by_ref.get(model) or {}).get("price") for model in (low_model, high_model)]
                if None in prices:
                    for tier, model, price in ((low_tier, low_model, prices[0]), (high_tier, high_model, prices[1])):
                        if price is None and f"{tier}/{specialty}" not in unpriced.setdefault(model, []):
                            unpriced[model].append(f"{tier}/{specialty}")
                    continue
                compared += 1
                if prices[0] <= prices[1]:
                    continue
                entry = grouped.setdefault((low_tier, low_model, high_tier, high_model), {
                    "lower": {"tier": low_tier, "model": low_model, "price": prices[0]},
                    "higher": {"tier": high_tier, "model": high_model, "price": prices[1]},
                    "ratio": round(prices[0] / prices[1], 2) if prices[1] else None,
                    "specialties": [], "delegated": high_tier == "hard" and delegating})
                entry["specialties"].append(specialty)

    split: Dict[bool, List[Dict[str, Any]]] = {True: [], False: []}
    for entry in grouped.values():
        split[entry.pop("delegated")].append(entry)
    unknown = [{"model": model, "leads": cells} for model, cells in unpriced.items()]
    return {
        "status": "inverted" if split[False] else "unknown" if unknown else "ok",
        "compared": compared, "inversions": split[False], "delegated": split[True], "unknown": unknown,
    }


def _pick(config: Dict[str, Any], rows: Dict[str, Dict[str, Any]], tier: str, specialty: str,
          need_vision: bool, context_tokens: int, only_provider: Optional[str] = None) -> Optional[str]:
    tiers = config.get("tiers") or {}
    exclude = config.get("exclude") or []
    order = [tier] + [t for t in TIERS[TIERS.index(tier):] if t != tier]  # never fall DOWN a tier
    for candidate_tier in order:
        pools = tiers.get(candidate_tier)
        if not isinstance(pools, dict):
            continue
        for name in (("vision",) if need_vision else ()) + (specialty, "general"):
            listed = pools.get(name)
            for ref in listed if isinstance(listed, list) else []:
                if not _usable(ref, exclude, only_provider):
                    continue
                row = rows.get(ref)
                if row is None:          # pinned by the user but unknown to the catalog: trust the pin
                    if not need_vision:
                        return ref
                    continue
                if need_vision and not row["vision"]:
                    continue
                if row["context"] and context_tokens * 1.25 > row["context"]:
                    continue
                return ref
    return None


# ── envelopes ────────────────────────────────────────────────────────────────

_H2 = re.compile(r"(?m)^[ \t]*#{2,3}[ \t]+(\S[^\n]*?)[ \t]*$")


def unwrap(text: str, config: Optional[Dict[str, Any]] = None) -> str:
    """Return the instruction inside a scheduled/queued envelope, or the text unchanged.

    A cron or worker turn is mostly a standing contract that is identical every run: the
    anti-stall protocol, the status format, the previous run's output. Judging that says
    nothing about this run. If a section names the actual ask, use only that section.
    """
    config = config or DEFAULT_CONFIG
    wanted = [w.lower() for w in (config.get("ask_sections") or [])]
    unwanted = [w.lower() for w in (config.get("drop_sections") or [])]
    if not wanted or "#" not in text:
        return text
    heads = list(_H2.finditer(text))
    if not heads:
        return text
    for index, match in enumerate(heads):
        title = match.group(1).strip().lower().rstrip(":")
        if not any(title == w or title.startswith(w + " ") for w in wanted):
            continue
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[match.end():end].strip()
        if len(body) >= 24:                      # a heading with nothing under it is not the ask
            return body
    # No ask section, but previous output can still be dropped so the rest is judged on its merits.
    if unwanted:
        keep, cut = [], 0
        for index, match in enumerate(heads):
            title = match.group(1).strip().lower().rstrip(":")
            if any(title.startswith(w) for w in unwanted):
                end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
                keep.append(text[cut:match.start()])
                cut = end
        if keep:
            keep.append(text[cut:])
            trimmed = "".join(keep).strip()
            if len(trimmed) >= 24:
                return trimmed
    return text


# ── decision ─────────────────────────────────────────────────────────────────

def _features(prompt: str, context_tokens: int) -> Dict[str, Any]:
    words = len(prompt.split())
    return {
        "length": "short" if words < 25 else "medium" if words < 150 else "long",
        "questions": min(prompt.count("?"), 5), "has_code": bool(re.search(r"```|\bdef |\bclass |;\s*$|\{\s*$", prompt, re.M)),
        "numbered_steps": len(re.findall(r"(?m)^\s*(\d+[.)]|[-*])\s", prompt)),
        "risk_words": bool(_HARD_RISK.search(prompt)),
        "context": "small" if context_tokens < 8000 else "medium" if context_tokens < 64000 else "large",
    }


# A repeating job asks the same thing every run; its answer is worth exactly one Jev call.
_DECISIONS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


# Two things the key used to leave out, both reported by a reader of the code (issue #3):
#
# The CONTEXT SIZE. A hit returns before `_pick` runs, so the context-fit check and the
# "large context never switches down" guard were both skipped. A repeated short instruction
# - a cron turn, "continue", a template - is first seen in a small context and again once
# the session has grown, and the second turn was answered from the first: a 300,000-token
# turn routed to a model with a 32,000-token window. Bucketed rather than exact, because an
# exact size would make every turn a miss and the cache pointless.
#
# The CONFIG. Editing tiers in routing.json changed nothing until the process restarted,
# which reads as "my edit did not work".
_CONTEXT_BUCKETS = (4_000, 16_000, 32_000, 64_000, 128_000, 200_000, 400_000, 1_000_000)


def _context_bucket(context_tokens: int) -> int:
    for edge in _CONTEXT_BUCKETS:
        if context_tokens < edge:
            return edge
    return 0                                   # bigger than every bucket: its own class


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    """The parts of the config that can change which model comes back."""
    material = json.dumps({k: config.get(k) for k in ("tiers", "exclude", "escalate", "sticky_context_tokens")},
                          sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def _cache_key(ask: str, profile: Optional[str], only_provider: Optional[str], has_images: bool, pinned: bool,
               context_tokens: int = 0, config: Optional[Mapping[str, Any]] = None) -> str:
    material = "\x00".join([ask, str(profile), str(only_provider), str(has_images), str(pinned), POLICY_VERSION,
                            str(_context_bucket(context_tokens)), _config_fingerprint(config or {})])
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def _remember(key: Optional[str], decision: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Cache only decisions that will still be right next time: never a transient failure."""
    if key and decision.get("tier"):
        _DECISIONS[key] = decision
        _DECISIONS.move_to_end(key)
        while len(_DECISIONS) > int(config.get("cache_size", 512)):
            _DECISIONS.popitem(last=False)
    return decision


def _with_escalation(decision: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the ladder's answer at read time. It is never stored in the decision cache.

    A seat that was free when a recurring job was first judged can be full an hour later.
    The cached decision used to carry the rung it was given the first time, so the notice
    and the audit log kept naming a seat every other lane already knew had refused.
    """
    settings = config.get("escalation") or {}
    if decision.get("tier") != "hard" or not (settings.get("enabled") and settings.get("rungs")):
        return decision
    # A frontier seat is worth its cost only on work that earned the hard tier. Everything
    # below hard stays on OpenRouter, which is the whole point of paying for frontier seats.
    escalation = ladder.choose(settings["rungs"])
    escalation["stakes"] = decision.get("costly_mistake")
    notice = decision.get("notice") or ""
    if escalation.get("rung"):
        notice += f" · escalate to {escalation['rung']}" + (" (forced)" if escalation.get("forced") else "")
    return {**decision, "escalate": escalation, "notice": notice}


def _keep(current: Optional[str], reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"routed": False, "model": current, "reason": reason, "policy": POLICY_VERSION,
           "notice": f"[Jev] kept {current or 'current model'} · {reason}"}
    out.update(extra)
    return out


def questions() -> Dict[str, Any]:
    """The three questions routing asks about one turn, in one object.

    Separate from `decide` so another decision can be asked in the same request (see
    `jevkit/turn.py`): Jev charges one round trip per request, so the questions routing
    needs and the questions skill selection needs cost the same together as apart.
    """
    return {
        "difficulty": client.score("How demanding is it to complete this turn well?", DIFFICULTY),
        "kind": client.choice("What kind of work is this turn mainly?", KIND),
        "costly_mistake": client.noul("A wrong or sloppy answer here would be costly or hard to undo"),
    }


def state_for(ask: str, *, context_tokens: int = 0, private: bool = False, limit: int = 2500) -> Any:
    """What routing sends about one ask: the text, or coarse features when the turn is private."""
    if private:
        return {"turn_features": _features(ask, context_tokens)}
    return {"user_turn": privacy.redact(ask, limit + 50), "context": _features(ask, context_tokens)["context"]}


def decide(
    prompt: str, *, current: Optional[str] = None, context_tokens: int = 0, has_images: bool = False,
    profile: Optional[str] = None, pinned: bool = False, config: Optional[Dict[str, Any]] = None,
    rows: Optional[List[Dict[str, Any]]] = None, transport: Optional[client.Transport] = None,
    timeout: float = 2.5, only_provider: Optional[str] = None, session_id: str = "",
    answers: Optional[Mapping[str, Any]] = None, latency_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Route one fresh user turn. Call it once per turn, never inside a tool loop."""
    config = config or load_config()
    if pinned:
        return _keep(current, "you pinned this model")
    if not (config.get("tiers") or {}):
        return _keep(current, "no tiers configured; run `jev models suggest --write`")
    if not prompt.strip():
        return _keep(current, "empty turn")

    # Judge the ask, not the contract around it. A scheduled or queued turn is a standing brief wrapped
    # around one real instruction; unwrap to that first. Whatever is left, a long turn keeps its opening
    # and — far more often where the actual request lives — its end.
    limit = int(config.get("ask_chars", 2500))
    inner = unwrap(prompt, config)
    unwrapped = inner is not prompt and inner != prompt
    ask = inner if len(inner) <= limit else inner[:limit // 4] + "\n[…]\n" + inner[-(limit - limit // 4):]

    # A recurring job repeats its instruction verbatim, so buy the decision once and reuse it.
    cache_key = None
    if config.get("cache_repeat_asks", True):
        cache_key = _cache_key(ask, profile, only_provider, has_images, bool(pinned), context_tokens, config)
        cached = _DECISIONS.get(cache_key)
        if cached is not None:
            return _with_escalation({**cached, "cached": True}, config)
    # Both of these read the WHOLE turn, not the clipped copy that gets sent. `ask` keeps
    # the opening and the end, so "drop the production database" in the middle of a long
    # paste tripped neither guard: the turn routed to the cheapest tier and its text was
    # sent as ordinary text. What is clipped is what Jev reads, never what we check.
    risky = bool(_HARD_RISK.search(privacy.normalize(inner)))
    private = profile in (config.get("private_profiles") or []) or privacy.is_sensitive(inner)
    mode = "features" if private else config.get("mode", "redacted-text")
    state: Any = state_for(ask, context_tokens=context_tokens, private=(mode == "features"), limit=limit)
    if answers is None:
        try:
            reply = client.ask(state, questions(), timeout=timeout, transport=transport)
        except client.JevError as error:
            return _keep(current, f"Jev unavailable ({error.code})", private=private)
        answers, latency_ms = reply["answers"], reply["latency_ms"]
    elif not all(isinstance(answers.get(name), dict) for name in ("difficulty", "kind", "costly_mistake")):
        # Answers asked by `turn.decide_turn` in somebody else's request. A caller that hands
        # us a partial object is a bug in that caller, not a reason to route on a guess.
        return _keep(current, "routing answers incomplete", private=private)
    difficulty, confidence = answers["difficulty"]["score"], answers["difficulty"]["confidence"]
    stakes = answers["costly_mistake"]["noul"]
    spread = answers["difficulty"].get("probabilities") or {}
    if spread:
        p_simple, p_hard = spread.get(0, 0.0), spread.get(2, 0.0) + spread.get(3, 0.0)
    else:                      # no spread returned: fall back to the averaged score, conservatively
        p_simple, p_hard = float(difficulty < 0.5), float(difficulty >= 2.25)

    # An unsure answer is not evidence of a hard turn. Its averaged score lands mid-rubric by arithmetic,
    # so it must never buy the expensive tier: a harmless unsure turn stays put, a risky one gets medium.
    unsure = confidence < config["min_confidence"]
    if unsure and not (risky or stakes > 0.6):
        return _keep(current, f"low confidence {confidence:.2f}", private=private)

    if unsure:
        tier = "medium"
    elif p_hard >= config["hard_needs_probability"]:
        tier = "hard"
    elif p_simple >= config["simple_needs_probability"] and confidence >= config["simple_needs_confidence"]:
        tier = "simple"
    else:
        tier = "medium"
    if tier == "simple" and (risky or stakes > 0.4 or mode == "features"):
        tier = "medium"        # risk words and costly mistakes set a floor of medium; they do not buy hard
    if not unsure and stakes > 0.85 and p_hard >= 0.35:
        tier = "hard"          # a costly mistake tips a turn that is already leaning hard
    kind = answers["kind"]
    specialty = kind["choice"] if kind["confidence"] >= 0.5 else "general"

    # With no cache and no network the catalog load raises, and it used to take the turn
    # with it. Routing blind is not the answer either: without rows nothing checks that the
    # context fits, so a long conversation could be sent to a model that cannot hold it.
    by_ref = _by_ref(rows)
    if by_ref is None:
        return _keep(current, "model catalog unavailable", private=private)
    picked = _pick(config, by_ref, tier, specialty, has_images, context_tokens, only_provider)
    if not picked:
        return _keep(current, f"no {tier} model fits this turn", private=private)

    if current and picked != current and context_tokens > config["sticky_context_tokens"]:
        current_price = (by_ref.get(current) or {}).get("price")
        picked_price = (by_ref.get(picked) or {}).get("price")
        if current_price is not None and picked_price is not None and picked_price < current_price:
            return _keep(current, "large context; switching down would cost more than it saves", private=private)

    provider, model = picked.split(":", 1)
    return _with_escalation(_remember(cache_key, {
        "routed": picked != current, "model": picked, "provider": provider, "model_id": model, "tier": tier,
        # Carried out so a reader can tell WHICH pool the model came from. Without it a
        # model listed in both `vision` and `general` is unattributable after the fact,
        # and "did the specialty answer earn its keep?" becomes unanswerable.
        "specialty": specialty, "has_images": bool(has_images),
        "confidence": round(confidence, 3), "difficulty": round(difficulty, 2),
        "costly_mistake": round(stakes, 3), "private": private, "mode": mode, "latency_ms": latency_ms,
        "policy": POLICY_VERSION, "reason": f"{tier} {specialty}", "unwrapped": unwrapped,
        "notice": f"[Jev] {tier} · {specialty} → {model} · confidence {confidence:.2f}",
    }, config), config)
