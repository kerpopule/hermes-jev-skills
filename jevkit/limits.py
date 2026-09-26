"""Fleet-wide brakes for policy decisions: requests per minute, and dollars per day.

Every process on a machine shares one small counter file, locked with ``flock`` where the OS
has it. Two limits, read from ``<hermes root>/jev/limits.json`` (defaults in brackets):

* ``rpm`` [1000] — the provider allows 1,200 a minute; staying under it keeps a batch job
  from starving live features. Shadow calls are refused first, at ``shadow_share`` [0.8] of
  the limit, so a busy minute costs the experiment, never the live path.
* ``daily_usd`` [1.00] — over the cap, shadow work is skipped and logged ``skipped_budget``;
  live features take their fallback, which is what they would have done without Jev.

The limiter is a brake, not an accountant: a crash between ``admit`` and ``charge`` loses one
charge. It never raises; if the counter cannot be read or written it admits the call (the
per-call budgets still hold).
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

try:  # POSIX only; elsewhere the counter is best-effort within one process
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

DEFAULTS: Dict[str, Any] = {"rpm": 1000, "daily_usd": 1.0, "shadow_share": 0.8}


def root() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return (home.parent.parent if home.parent.name == "profiles" else home) / "jev"


def config() -> Dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        data = json.loads((root() / "limits.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for key in DEFAULTS:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                out[key] = value
    return out


def disabled() -> bool:
    return os.environ.get("JEV_LIMITS", "").strip().lower() in ("off", "0", "false", "no")


@contextmanager
def _locked() -> Iterator[Dict[str, Any]]:
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "limits.state.json"
    lock = open(folder / "limits.lock", "a+")
    try:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        yield state
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, state_path)
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock.close()


def _roll(state: Dict[str, Any], now: float) -> None:
    minute = int(now // 60)
    if state.get("minute") != minute:
        state["minute"], state["count"] = minute, 0
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    if state.get("day") != day:
        state["day"], state["usd"] = day, 0.0


def admit(shadow: bool) -> Tuple[bool, str]:
    """(allowed, reason). Counts the call when allowed. Never raises."""
    if disabled():
        return True, "limits_off"
    limits = config()
    try:
        with _locked() as state:
            now = time.time()
            _roll(state, now)
            if float(state.get("usd") or 0) >= float(limits["daily_usd"]):
                return False, "skipped_budget"
            ceiling = float(limits["rpm"]) * (float(limits["shadow_share"]) if shadow else 1.0)
            if int(state.get("count") or 0) >= ceiling:
                return False, "skipped_rate"
            state["count"] = int(state.get("count") or 0) + 1
            return True, "ok"
    except OSError:
        return True, "limits_unreadable"


def charge(usd: float) -> None:
    if disabled() or not usd:
        return
    try:
        with _locked() as state:
            _roll(state, time.time())
            state["usd"] = round(float(state.get("usd") or 0) + float(usd), 8)
    except OSError:
        pass


def status() -> Dict[str, Any]:
    limits = config()
    try:
        with _locked() as state:
            _roll(state, time.time())
            return {**limits, "this_minute": int(state.get("count") or 0),
                    "spent_today_usd": round(float(state.get("usd") or 0), 6), "disabled": disabled()}
    except OSError:
        return {**limits, "disabled": disabled(), "error": "counter unreadable"}
