#!/usr/bin/env python3
"""Read and apply Hermes model-routing config across the default profile and every
profile, with comment-preserving edits, backups, and verified read-back.

Design rules:
  * Never rewrite the whole YAML file. Edit only the exact scalar lines, so
    comments, ordering, and unrelated settings survive untouched.
  * Read values back with a real YAML parse after writing; a write that does not
    verify is reported as failed.
  * Reject anything that could inject YAML (newlines / odd characters).
  * Applying never restarts a gateway and never touches a remote. It returns a
    receipt that says a reload is required for the change to take effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

import pools

# ---------------------------------------------------------------- use cases

#: Auxiliary task slots Hermes routes independently, with human labels.
AUX_SLOTS: list[tuple[str, str, str]] = [
    ("compression", "Compression", "context"),
    ("flush_memories", "Memory flush", "context"),
    ("session_search", "Session search", "context"),
    ("curator", "Curator", "context"),
    ("skills_hub", "Skills hub", "context"),
    ("title_generation", "Title generation", "housekeeping"),
    ("tts_audio_tags", "Audio tags", "housekeeping"),
    ("profile_describer", "Profile describer", "housekeeping"),
    ("web_extract", "Web extract", "knowledge"),
    ("vision", "Vision", "knowledge"),
    ("triage_specifier", "Triage / specifier", "judgment"),
    ("approval", "Approval judgment", "judgment"),
    ("mcp", "MCP calling", "judgment"),
    ("monitor", "Monitor", "judgment"),
    ("kanban_decomposer", "Kanban decomposer", "judgment"),
    ("background_review", "Background review", "judgment"),
]
SLOT_KEYS = [s[0] for s in AUX_SLOTS]
SLOT_LABELS = {s[0]: s[1] for s in AUX_SLOTS}
SLOT_GROUPS = {s[0]: s[2] for s in AUX_SLOTS}

MAIN_LABEL = "Main model (user-facing responses)"

#: Jev mode is implemented in a candidate commit, not in the running release.
JEV_MODE = {
    "key": "jev/state.json",
    "installed": True,
    "intent": "Jev reads each fresh turn and answers two things: how hard it is (simple / medium / hard) and "
              "what kind of work it is (coding, writing, research or general). Those two pick a pool in "
              "<hermes home>/jev/routing.json, and the cheapest model in it that fits the turn wins. Any model "
              "on the connected provider can be in a pool.",
    "desired_default": "shadow",
    "note": "Routing is done by the hermes-jev plugin. The switch above takes effect on the next message. "
            "Press Live to watch decisions as they happen. Inside Hermes the same switch is /jev routing on|shadow|off.",
}

_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9._\-]*$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9._\-/:@+]*$")


def _check_safe(value: str, kind: str) -> str:
    value = (value or "").strip()
    if "\n" in value or "\r" in value:
        raise ValueError(f"{kind} must not contain newlines")
    pat = _SAFE_PROVIDER if kind == "provider" else _SAFE_MODEL
    if not pat.match(value):
        raise ValueError(f"{kind} contains unsupported characters: {value!r}")
    return value


@dataclass
class Target:
    """One configurable config file."""

    name: str
    path: str
    kind: str  # 'default' | 'profile'

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)


def discover_targets(hermes_home: str) -> list[Target]:
    """Default profile first, then every profile dir holding a config.yaml."""
    targets: list[Target] = []
    root_cfg = os.path.join(hermes_home, "config.yaml")
    if os.path.isfile(root_cfg):
        targets.append(Target("default", root_cfg, "default"))
    pdir = os.path.join(hermes_home, "profiles")
    if os.path.isdir(pdir):
        for name in sorted(os.listdir(pdir)):
            cfg = os.path.join(pdir, name, "config.yaml")
            if os.path.isfile(cfg):
                targets.append(Target(name, cfg, "profile"))
    return targets


# ------------------------------------------------------------- text editing


def _bounds(lines: list[str], section: str, indent: int = 0) -> Optional[tuple[int, int]]:
    """Line bounds of a block starting at `section` with given indent."""
    start = None
    prefix = " " * indent
    for i, line in enumerate(lines):
        if re.match(r"^%s%s\s*:" % (re.escape(prefix), re.escape(section)), line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        if ind <= indent:
            end = j
            break
    return (start, end)


def _find_scalar(lines: list[str], bnd: tuple[int, int], key: str, indent: int) -> Optional[int]:
    for i in range(bnd[0] + 1, bnd[1]):
        if re.match(r"^%s%s\s*:" % (" " * indent, re.escape(key)), lines[i]):
            return i
    return None


def _fmt(value: str) -> str:
    if value == "":
        return "''"
    return value


def _set_scalar(text: str, section: str, key: str, value: str, *, sub: Optional[str] = None,
                section_indent: int = 0) -> str:
    """Set `section[.sub].key` to value, preserving every other character."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"

    top = _bounds(lines, section, section_indent)
    if top is None:
        # Append a whole new block.
        body = "%s%s:\n" % (" " * section_indent, section)
        if sub:
            body += "%s  %s:\n" % (" " * section_indent, sub)
            body += "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value))
        else:
            body += "%s  %s: %s\n" % (" " * section_indent, key, _fmt(value))
        return text + ("" if text.endswith("\n") or not text else "\n") + body

    if sub is None:
        idx = _find_scalar(lines, top, key, section_indent + 2)
        if idx is None:
            insert_at = top[1]
            line = "%s  %s: %s\n" % (" " * section_indent, key, _fmt(value))
            lines.insert(insert_at, line)
        else:
            lines[idx] = "%s  %s: %s\n" % (" " * section_indent, key, _fmt(value))
        return "".join(lines)

    # two-level: section -> sub -> key
    sub_bnd = None
    prefix = " " * (section_indent + 2)
    for i in range(top[0] + 1, top[1]):
        if re.match(r"^%s%s\s*:" % (re.escape(prefix), re.escape(sub)), lines[i]):
            sub_bnd = (i, top[1])
            break
        ind = len(lines[i]) - len(lines[i].lstrip(" ")) if lines[i].strip() else None
        if ind is not None and ind <= section_indent + 2:
            sub_bnd = None
    if sub_bnd is None:
        insert_at = top[1]
        lines.insert(insert_at, "%s  %s:\n" % (" " * section_indent, sub))
        lines.insert(insert_at + 1, "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value)))
        return "".join(lines)

    # bound the sub block properly
    end = sub_bnd[1]
    for j in range(sub_bnd[0] + 1, sub_bnd[1]):
        if not lines[j].strip():
            continue
        ind = len(lines[j]) - len(lines[j].lstrip(" "))
        if ind <= section_indent + 2:
            end = j
            break
    sub_bnd = (sub_bnd[0], end)

    idx = _find_scalar(lines, sub_bnd, key, section_indent + 4)
    if idx is None:
        lines.insert(sub_bnd[1], "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value)))
    else:
        lines[idx] = "%s    %s: %s\n" % (" " * section_indent, key, _fmt(value))
    return "".join(lines)


# ------------------------------------------------------------------- reading


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read_config(path: str) -> dict[str, Any]:
    """Current main + auxiliary routing for one config file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    model = data.get("model") or {}
    aux = data.get("auxiliary") or {}
    slots: dict[str, dict[str, str]] = {}
    for key in SLOT_KEYS:
        entry = aux.get(key) or {}
        if isinstance(entry, dict) and (entry.get("provider") or entry.get("model")):
            slots[key] = {
                "provider": str(entry.get("provider") or ""),
                "model": str(entry.get("model") or ""),
            }
    return {
        "main": {
            "provider": str(model.get("provider") or ""),
            "model": str(model.get("default") or ""),
            "base_url": str(model.get("base_url") or ""),
        },
        "slots": slots,
        "fallback_providers": data.get("fallback_providers") or [],
        "jev": _jev_state(data),
    }


def _jev_state(data: dict[str, Any]) -> dict[str, Any]:
    routing = data.get("model_routing") or {}
    jev = (routing.get("jev") or {}) if isinstance(routing, dict) else {}
    mode = str(jev.get("mode") or "") if isinstance(jev, dict) else ""
    return {"mode": mode or "not-configured", "key": JEV_MODE["key"]}


def snapshot(hermes_home: str) -> dict[str, Any]:
    targets = discover_targets(hermes_home)
    profiles = []
    for t in targets:
        try:
            cfg = read_config(t.path)
            err = None
        except Exception as exc:  # a broken profile must not blank the dashboard
            cfg = {"main": {"provider": "", "model": ""}, "slots": {}, "jev": {"mode": "unknown"}}
            err = f"{type(exc).__name__}: {exc}"
        profiles.append({
            "name": t.name,
            "kind": t.kind,
            "path": t.path,
            "main": cfg["main"],
            "slots": cfg["slots"],
            "jev": cfg.get("jev", {}),
            "error": err,
        })
    return {
        "hermes_home": hermes_home,
        "generated_at": time.time(),
        "profiles": profiles,
        "use_cases": [
            {"key": "__main__", "label": MAIN_LABEL, "group": "main"},
            *[{"key": k, "label": SLOT_LABELS[k], "group": SLOT_GROUPS[k]} for k in SLOT_KEYS],
        ],
        "jev_mode": JEV_MODE,
    }


# models.dev provider id -> the provider name Hermes uses in config.yaml, where they differ.
_HERMES_PROVIDER_NAME = {"xai": "xai", "google": "gemini", "openai": "openai-codex", "github-copilot": "copilot",
                         "kimi-for-coding": "kimi-coding", "moonshotai": "moonshot"}
_CATALOG_CACHE: dict[str, Any] = {}


def _accessible_models(hermes_home: str) -> list[tuple[str, str]]:
    """(hermes provider, model id) for every provider this install holds a key or login for.

    Only the NAMES of keys are read. The models.dev cache is JSON; parsing it as YAML took ~8 s.
    """
    cache = os.path.join(hermes_home, "models_dev_cache.json")
    try:
        stamp = os.path.getmtime(cache)
    except OSError:
        return []
    if _CATALOG_CACHE.get("stamp") == stamp:
        return _CATALOG_CACHE["rows"]
    try:
        with open(cache, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return []
    have: set[str] = set()
    try:
        with open(os.path.join(hermes_home, ".env"), "r", encoding="utf-8") as fh:
            for line in fh:
                name, sep, value = line.partition("=")
                if sep and value.strip():
                    have.add(name.strip())
    except OSError:
        pass
    logins: set[str] = set()
    try:
        with open(os.path.join(hermes_home, "auth.json"), "r", encoding="utf-8") as fh:
            auth = json.load(fh)
        for section in ("credential_pool", "providers"):
            if isinstance(auth.get(section), dict):
                logins.update(auth[section])
    except (OSError, ValueError):
        pass
    reverse = {v: k for k, v in _HERMES_PROVIDER_NAME.items()}
    login_ids = {reverse.get(name, name) for name in logins} | {"xai" if "xai-oauth" in logins else ""}
    rows: list[tuple[str, str]] = []
    for provider_id, entry in (blob.items() if isinstance(blob, dict) else []):
        if not isinstance(entry, dict) or provider_id == "cloudflare-ai-gateway":
            continue
        if provider_id not in login_ids and not any(n in have for n in (entry.get("env") or [])):
            continue
        name = _HERMES_PROVIDER_NAME.get(provider_id, provider_id)
        for mid, spec in (entry.get("models") or {}).items():
            if isinstance(spec, dict) and spec.get("status") not in ("deprecated", "retired"):
                rows.append((name, mid))
    _CATALOG_CACHE.update(stamp=stamp, rows=rows)
    return rows


def model_catalog(hermes_home: str, limit: int = 4000) -> list[dict[str, str]]:
    """Model ids worth offering: current values, then any local model cache."""
    seen: dict[str, str] = {}

    def add(mid: str, provider: str) -> None:
        mid = (mid or "").strip()
        if not mid or len(seen) >= limit:
            return
        seen.setdefault(mid, provider or "")

    for t in discover_targets(hermes_home):
        try:
            cfg = read_config(t.path)
        except Exception:
            continue
        add(cfg["main"]["model"], cfg["main"]["provider"])
        for slot in cfg["slots"].values():
            add(slot.get("model", ""), slot.get("provider", ""))

    for provider, mid in _accessible_models(hermes_home):
        add(mid, provider)

    return [{"id": k, "provider": v} for k, v in sorted(seen.items())]


# ------------------------------------------------------------------- writing


def plan(config_path: str, changes: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    """Compute before/after rows for the UI preview without writing anything."""
    before = read_config(config_path)
    rows: list[dict[str, str]] = []
    main = changes.get("__main__")
    if main:
        for field_name, key, old in (
            ("provider", "provider", before["main"]["provider"]),
            ("model", "default", before["main"]["model"]),
        ):
            new = _check_safe(main.get(field_name, old), field_name)
            if new != old:
                rows.append({"scope": "__main__", "field": key, "before": old, "after": new})
    for slot_key, spec in changes.items():
        if slot_key == "__main__":
            continue
        if slot_key not in SLOT_KEYS:
            raise ValueError(f"unknown use case: {slot_key}")
        cur = before["slots"].get(slot_key, {"provider": "", "model": ""})
        for field_name in ("provider", "model"):
            if field_name not in spec:
                continue
            new = _check_safe(spec[field_name], field_name)
            old = cur.get(field_name, "")
            if new != old:
                rows.append({"scope": slot_key, "field": field_name, "before": old, "after": new})
    return rows


def apply_changes(hermes_home: str, config_path: str, changes: dict[str, dict[str, str]],
                  backup_root: Optional[str] = None) -> dict[str, Any]:
    """Apply routing changes to one config file, with backup + verified read-back."""
    rp = os.path.realpath(config_path)
    allowed = {os.path.realpath(t.path) for t in discover_targets(hermes_home)}
    if rp not in allowed:
        raise ValueError("config path is not a live Hermes config")

    rows = plan(rp, changes)
    if not rows:
        return {"ok": True, "changed": 0, "rows": [], "backup": None,
                "verified": True, "reload_required": False, "mismatches": [],
                "message": "nothing to change"}

    before_hash = _sha256(rp)
    backup_root = backup_root or os.path.join(hermes_home, "backups", "model-routing-dashboard")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bdir = os.path.join(backup_root, stamp)
    os.makedirs(bdir, exist_ok=True)
    backup = os.path.join(bdir, os.path.basename(rp) + "." + hashlib.sha256(rp.encode()).hexdigest()[:8])
    shutil.copy2(rp, backup)

    with open(rp, "r", encoding="utf-8") as fh:
        text = fh.read()

    main = changes.get("__main__")
    if main:
        if "provider" in main:
            text = _set_scalar(text, "model", "provider", _check_safe(main["provider"], "provider"))
        if "model" in main:
            text = _set_scalar(text, "model", "default", _check_safe(main["model"], "model"))
    for slot_key, spec in changes.items():
        if slot_key == "__main__":
            continue
        if slot_key not in SLOT_KEYS:
            raise ValueError(f"unknown use case: {slot_key}")
        if "provider" in spec:
            text = _set_scalar(text, "auxiliary", "provider", _check_safe(spec["provider"], "provider"),
                               sub=slot_key)
        if "model" in spec:
            text = _set_scalar(text, "auxiliary", "model", _check_safe(spec["model"], "model"),
                               sub=slot_key)

    tmp = rp + ".dashboard-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, rp)

    after = read_config(rp)  # raises if we produced invalid YAML
    mismatches = []
    for row in rows:
        if row["scope"] == "__main__":
            got = after["main"]["provider" if row["field"] == "provider" else "model"]
        else:
            got = after["slots"].get(row["scope"], {}).get(row["field"], "")
        if got != row["after"]:
            mismatches.append({**row, "read_back": got})

    return {
        "ok": not mismatches,
        "changed": len(rows),
        "rows": rows,
        "backup": backup,
        "before_sha256": before_hash,
        "after_sha256": _sha256(rp),
        "verified": not mismatches,
        "mismatches": mismatches,
        "reload_required": bool(rows),
        "message": ("applied and verified; gateway restart required for running sessions"
                    if not mismatches else "write did not verify — restore from backup"),
    }


# ------------------------------------------------------------------ jev live


def _jev_homes(hermes_home: str) -> list[tuple[str, str]]:
    homes = [("default", hermes_home)]
    pdir = os.path.join(hermes_home, "profiles")
    if os.path.isdir(pdir):
        homes += [(n, os.path.join(pdir, n)) for n in sorted(os.listdir(pdir))
                  if os.path.isdir(os.path.join(pdir, n))]
    return homes


def _tail_lines(path: str, max_bytes: int = 96_000) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    return lines[1:] if size > max_bytes else lines  # first line may be cut in half


def jev_live(hermes_home: str, since: float = 0.0, limit: int = 200) -> dict[str, Any]:
    """Recent Jev decisions across every profile, newest first.

    The hermes-jev plugin writes decisions only (tier, model, confidence, latency)
    to <profile home>/logs/jev-decisions.jsonl; prompt text is never in that file.
    """
    events: list[dict[str, Any]] = []
    switches: dict[str, dict[str, str]] = {}
    for name, home in _jev_homes(hermes_home):
        try:
            with open(os.path.join(home, "jev", "state.json"), "r", encoding="utf-8") as fh:
                state = json.load(fh)
            if isinstance(state, dict) and state:
                switches[name] = {k: str(v) for k, v in state.items()}
        except (OSError, ValueError):
            pass
        for line in _tail_lines(os.path.join(home, "logs", "jev-decisions.jsonl")):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and float(row.get("ts") or 0) > since:
                row.setdefault("profile", name)
                events.append(row)
    events.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    events = events[:limit]
    routes = [e for e in events if e.get("kind") == "route"]
    # Count only the first request per turn. Tool-loop requests repeat the same
    # decision; shadow choices and suggestions are not applied model changes.
    effective = [e for e in events if e.get("kind") == "route_effective" and e.get("first_request") is True]
    by_model: dict[str, int] = {}
    for e in effective:
        model = e.get("effective_request_model")
        if isinstance(model, str) and model:
            by_model[model] = by_model.get(model, 0) + 1

    # Which pool a model came from is the only thing that says whether the specialty
    # answer earned its money, and the log records the decision, not the pool. Grids are
    # built only for the profiles on screen because this is polled every couple of seconds.
    homes = dict(_jev_homes(hermes_home))
    grids: dict[str, dict] = {}
    for e in routes:
        name = str(e.get("profile") or "")
        if e.get("model") and e.get("tier") and name in homes:
            if name not in grids:
                grids[name] = pools.grid_for(hermes_home, homes[name])["tiers"]
            e["pool"] = pools.locate(grids[name], str(e["tier"]), str(e.get("specialty") or ""),
                                      str(e["model"]), bool(e.get("has_images")))

    return {"now": time.time(), "events": events, "switches": switches,
            "plugin_installed": os.path.isdir(os.path.join(hermes_home, "plugins", "hermes-jev")),
            "by_model": sorted(by_model.items(), key=lambda kv: -kv[1])[:12],
            "effective_turns": len(effective), "applied_turns": sum(e.get("applied") is True for e in effective)}


def jev_pools(hermes_home: str) -> dict[str, Any]:
    """Every profile's routing pools as a tier x specialty grid, with the dead axes named."""
    return pools.tier_grid(hermes_home, _jev_homes(hermes_home))


JEV_SWITCHES = {"routing": ("off", "shadow", "on"), "skills": ("off", "on"), "notice": ("off", "on")}


def jev_switch_state(hermes_home: str) -> dict[str, Any]:
    """The shared default plus each profile's own override, and what each profile ends up with."""
    def read(home: str) -> dict[str, str]:
        try:
            with open(os.path.join(home, "jev", "state.json"), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {k: str(v) for k, v in data.items() if k in JEV_SWITCHES} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    shared = read(hermes_home)
    profiles = {}
    for name, home in _jev_homes(hermes_home):
        own = {} if home == hermes_home else read(home)
        profiles[name] = {"own": own, "effective": {k: own.get(k, shared.get(k, "off")) for k in JEV_SWITCHES}}
    return {"shared": {k: shared.get(k, "off") for k in JEV_SWITCHES}, "profiles": profiles,
            "plugin_installed": os.path.isdir(os.path.join(hermes_home, "plugins", "hermes-jev"))}


def set_jev_switch(hermes_home: str, scope: str, name: str, value: str) -> dict[str, Any]:
    """scope "__all__" writes the shared default AND clears that switch in every profile, so it really is all.

    The hermes-jev plugin reads these files on every turn, so this takes effect at once: no restart.
    """
    if name not in JEV_SWITCHES or value not in JEV_SWITCHES[name]:
        raise ValueError(f"{name} must be one of {JEV_SWITCHES.get(name)}")
    homes = dict(_jev_homes(hermes_home))
    if scope != "__all__" and scope not in homes:
        raise ValueError(f"unknown profile: {scope!r}")

    def write(home: str, mutate) -> None:
        path = os.path.join(home, "jev", "state.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        before = dict(data)
        mutate(data)
        if data == before:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".dashboard-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)

    if scope == "__all__":
        write(hermes_home, lambda d: d.__setitem__(name, value))
        for pname, home in homes.items():
            if home != hermes_home:
                write(home, lambda d: d.pop(name, None))
    elif homes[scope] == hermes_home:
        write(hermes_home, lambda d: d.__setitem__(name, value))
    else:
        write(homes[scope], lambda d: d.__setitem__(name, value))
    return {"ok": True, "scope": scope, "switch": name, "value": value, **jev_switch_state(hermes_home)}
