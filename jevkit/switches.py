"""Two switches per policy feature: a mode, and a kill-switch file that always wins.

The mode lives in ``jev/state.json`` (the shared file under the Hermes root, overridden by a
profile's own), set with ``/jev <feature> <mode>``. The kill switch is a marker file,
``<hermes root>/jev/<FEATURE>_OFF``: a setting that can quietly fall back to "off" eventually
will (docs/turning-a-jev-feature-on.md §3), and the reverse is the danger here — a feature
that should be off must not stay on because a config read failed. ``ls`` proves a marker.

Every feature defaults to ``off``. Nothing in this module turns anything on.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

MODES: Dict[str, Tuple[str, ...]] = {
    "gate": ("off", "shadow", "tiebreak", "ask", "block"),
    "cron_wake": ("off", "shadow", "on"),
    "retry": ("off", "shadow", "advise"),
    "blockcheck": ("off", "shadow", "advise"),
    "owner": ("off", "shadow", "advise"),
    "kanban_event": ("off", "shadow", "advise"),
    "decide_tools": ("off", "on"),
}
# Block is the one mode that can stop an agent. It needs a second key that only a person makes.
BLOCK_MARKER = "GATE_BLOCK_ALLOWED"


def home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def root() -> Path:
    here = home()
    return here.parent.parent if here.parent.name == "profiles" else here


def jev_dir(shared: bool = True) -> Path:
    return (root() if shared else home()) / "jev"


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def state() -> Dict[str, Any]:
    return {**_read(jev_dir(True) / "state.json"), **_read(jev_dir(False) / "state.json")}


def kill_switch(feature: str) -> Path:
    return jev_dir(True) / f"{feature.upper()}_OFF"


def mode(feature: str) -> str:
    """The effective mode: the kill switch first, then the setting, then ``off``.

    An unknown or misspelt setting reads as ``off``, and ``block`` without its marker reads
    as ``ask`` — the strongest thing a person can switch on by typing alone.
    """
    allowed = MODES.get(feature, ("off", "shadow", "on"))
    if kill_switch(feature).exists():
        return "off"
    value = str(state().get(feature) or "off").lower()
    if value not in allowed:
        return "off"
    if feature == "gate" and value == "block" and not (jev_dir(True) / BLOCK_MARKER).exists():
        return "ask"
    return value


def set_mode(feature: str, value: str, shared: bool = False) -> Path:
    allowed = MODES.get(feature)
    if allowed is None:
        raise ValueError(f"unknown feature {feature!r}; one of {', '.join(sorted(MODES))}")
    if value not in allowed:
        raise ValueError(f"{feature} takes {'|'.join(allowed)}")
    path = jev_dir(shared) / "state.json"
    current = _read(path)
    current[feature] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return path


def describe() -> Dict[str, Any]:
    return {feature: {"mode": mode(feature), "kill_switch": kill_switch(feature).exists()}
            for feature in MODES}
