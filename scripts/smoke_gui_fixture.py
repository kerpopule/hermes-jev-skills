#!/usr/bin/env python3
"""Opt-in macOS smoke: real Jev choice + bundled GUI runner + Cua AXPress.

Compiles and owns a disposable AppKit process, uses Cua to discover its exact
window, executes the installed-style runner twice, and cleans up on every path.
No mock chooser, direct AXPress, or access to another application.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(os.environ.get("JEV_GUI_RUNNER", str(ROOT / "skills/jev-computer-use/scripts/jev_gui_agent.py")))
SOURCE = ROOT / "tests/fixtures/JevGuiFixture.swift"
sys.path.insert(0, str(RUNNER.parent))
import jev_gui_agent as gui  # noqa: E402


def window_for(pid: int) -> int:
    driver = gui.Driver(gui._find_driver())
    try:
        for _ in range(30):
            windows = [w for w in gui.list_windows(driver) if w.get("pid") == pid
                       and w.get("title") == "Jev GUI Fixture - idle" and w.get("is_on_screen")]
            if windows:
                return int(windows[0]["window_id"])
            time.sleep(0.1)
        raise RuntimeError("fixture window did not appear")
    finally:
        driver.stop()


def one_run(binary: Path, tmp: Path, index: int) -> dict:
    log = tmp / f"events-{index}.log"
    fixture = subprocess.Popen([str(binary), str(log)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        window_id = window_for(fixture.pid)
        command = [sys.executable, str(RUNNER), "--pid", str(fixture.pid),
                   "--window-id", str(window_id), "--goal", "Activate fixture",
                   "--expect", "activated by Jev", "--max-steps", "2", "--json"]
        result = subprocess.run(command, text=True, capture_output=True, timeout=45,
                                env=dict(os.environ, JEV_MEMO="off"))
        lines = result.stdout.splitlines()
        payload = json.loads(lines[-1]) if lines and lines[-1].startswith("{") else {}
        events = log.read_text().splitlines() if log.exists() else []
        count = events.count("activated")
        if result.returncode != 0 or not payload.get("verified") or count != 1:
            raise RuntimeError(f"run {index}: exit={result.returncode}; "
                               f"verified={payload.get('verified')}; activations={count}; "
                               f"stdout={result.stdout[-1200:]}; stderr={result.stderr[-400:]}")
        return {"run": index, "steps": payload["steps"], "verified": True,
                "activations": count, "window_title": payload["window_title"]}
    finally:
        fixture.terminate()
        try:
            fixture.wait(timeout=3)
        except subprocess.TimeoutExpired:
            fixture.kill()
            fixture.wait(timeout=3)


def main() -> int:
    if sys.platform != "darwin":
        print("macOS/AppKit required", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="jev-gui-smoke-") as directory:
        tmp = Path(directory)
        binary = tmp / "JevGuiFixture"
        subprocess.run(["swiftc", str(SOURCE), "-o", str(binary)], check=True, timeout=60)
        results = [one_run(binary, tmp, index) for index in (1, 2)]
        print(json.dumps({"schema": "jev.gui_fixture_smoke_v1", "runs": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
